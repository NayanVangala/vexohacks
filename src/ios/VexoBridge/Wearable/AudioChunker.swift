//
//  AudioChunker.swift
//  VexoBridge
//
//  PCM -> WAV windowing. CLAUDE-ios.md §7.7.
//
//  Near-verbatim port of the Android class: pure logic, no platform dependency,
//  and the one component that unit-tests off-device on both platforms.
//
//  Thread safety: fed from the audio tap thread, flushed from the main actor.
//  Android used @Synchronized; here it is an NSLock around the buffer. A lock
//  is simpler than an actor and, critically, adds no await points to the audio
//  path — you must not suspend inside a real-time tap (§11.7).
//

import Foundation

final class AudioChunker: @unchecked Sendable {

    // MARK: - Format constants
    //
    // These MUST equal GlassesAudioRecorder's converter output. A mismatch does
    // not error — it produces speed-distorted WAVs and silently wrecks Phase 3
    // transcription accuracy. Change both together or neither (§7.6, Appendix A).

    let sampleRate: Int
    let channels: Int
    let bitsPerSample: Int
    let windowMillis: Int

    /// 16000 * 1 * 2 * 2500/1000 = 80_000 bytes at the defaults.
    let bytesPerWindow: Int

    /// Windows whose LOUDEST 100 ms is quieter than this are silence, not speech,
    /// and are dropped before they can reach /capture. The transcriber hallucinates
    /// fluent phrases out of pure silence and room tone ("where are my keys",
    /// "Thank you so much for watching"), so feeding it empty audio manufactures
    /// questions nobody asked. RMS 32 LSB at 16-bit is about -60 dBFS: a live mic's
    /// room tone sits well below it, and even quiet speech reads far above it (one
    /// soft word at RMS 300 measures ~300 in its loudest 100 ms window). Deliberately
    /// conservative — dropping real speech is worse than uploading one more silent chunk.
    static let silenceRMS: Double = 32

    /// Speech-onset level: a 100 ms slice at or above this RMS is the wearer
    /// (or someone) starting to talk. Deliberately well above the silence gate
    /// (32: room tone) and below even quiet speech (one soft word reads ~300
    /// in its loudest 100 ms window), so room tone never fires it and real
    /// speech fires it on its first loud slice.
    static let onsetRMS: Double = 150
    /// Onset only counts after this long with no loud slice: one continuous
    /// sentence fires once, at its start, not once per word.
    static let onsetSilenceGapS: TimeInterval = 2.0
    /// Minimum time between onsets, matching the server's 4 s post-answer
    /// wake cooldown (voice.COOLDOWN_S): the ack chime is not a metronome.
    static let onsetCooldownS: TimeInterval = 4.0

    private let onChunk: @Sendable (Data) -> Void

    /// Fired once per dropped silent window. The chunker stays pure logic and
    /// never logs itself; the caller throttles this into one log line.
    private let onSilentWindow: (@Sendable () -> Void)?

    /// Fired the moment speech starts (first loud 100 ms slice after silence),
    /// NOT at the end of the 2.5 s window. This is the fast wake chime: the
    /// old path waited for a full window + upload + Gemini transcription +
    /// the phone's 0.5 s speech poll, so the chime landed 3+ s after "hey".
    /// The caller plays the local chime from this; it must not block, await,
    /// or touch the main actor directly (it runs on the audio tap thread).
    /// It is VAD only, not a wake-word detector: it cannot tell "hey scene"
    /// from the boss talking, so the caller gates it (not while Jessica is
    /// speaking, not in Conversation Mode) and the backend chime still
    /// confirms a real wake word a few seconds later.
    private let onSpeechOnset: (@Sendable () -> Void)?

    private let lock = NSLock()
    private var buffer = Data()
    private var _droppedSilentWindows = 0
    private var _lastLoudAt = Date.distantPast
    private var _lastOnsetAt = Date.distantPast
    /// Last 100 ms slice loud enough to be speech (RMS >= onsetRMS). Tracked
    /// whether or not anyone asked for the onset chime: the end-of-speech
    /// early flush in feed() reads this, not scanForOnset's onset gating.
    private var _lastSpeechAt = Date.distantPast
    /// The current window already holds speech: a partial flush must never
    /// emit a fragment of room tone as a chunk.
    private var _windowHasSpeech = false
    /// Earliest a partial window may flush: shorter than this is a fragment,
    /// not a question (Gemini also does worse on <0.5 s of audio).
    static let earlyFlushMinS: Double = 0.8
    /// Silence after the last loud slice before a partial window flushes.
    static let earlyFlushSilenceS: Double = 0.8

    /// Silent windows dropped so far. Read it from the heartbeat/diagnostics.
    var droppedSilentWindows: Int {
        lock.lock(); defer { lock.unlock() }
        return _droppedSilentWindows
    }

    init(
        sampleRate: Int = 16_000,
        channels: Int = 1,
        bitsPerSample: Int = 16,
        windowMillis: Int = 2_500,
        onChunk: @escaping @Sendable (Data) -> Void,
        onSilentWindow: (@Sendable () -> Void)? = nil,
        onSpeechOnset: (@Sendable () -> Void)? = nil
    ) {
        self.sampleRate = sampleRate
        self.channels = channels
        self.bitsPerSample = bitsPerSample
        self.windowMillis = windowMillis
        self.bytesPerWindow = sampleRate * channels * (bitsPerSample / 8) * windowMillis / 1000
        self.onChunk = onChunk
        self.onSilentWindow = onSilentWindow
        self.onSpeechOnset = onSpeechOnset
    }

    // MARK: - Entry points (three, matching Android)

    /// Append PCM. Emits one WAV per full window; a single large feed can emit
    /// several. A partial window also flushes early, ~0.8 s after speech ends
    /// (see feed): a question no longer waits out the rest of its 2.5 s window.
    /// Called from the audio tap thread — no allocation beyond the
    /// buffer growth, no awaits.
    func feed(_ pcm: Data) {
        var ready: [Data] = []
        var onset = false
        var partial: Data? = nil

        lock.lock()
        // Speech onset is judged on the audio as it ARRIVES, in 100 ms
        // slices, so the chime fires ~100-250 ms after speech starts (one
        // tap buffer) instead of at the end of the 2.5 s window.
        onset = scanForOnset(pcm)
        buffer.append(pcm)
        while buffer.count >= bytesPerWindow {
            // Materialize the window into its OWN storage before mutating the
            // buffer. `buffer.prefix(n)` returns a slice that aliases `buffer`
            // and carries absolute indices, so copying it only after
            // `removeFirst` reads through stale bounds.
            let start = buffer.startIndex
            let end = buffer.index(start, offsetBy: bytesPerWindow)
            let window = Data(buffer[start..<end])
            buffer.removeSubrange(start..<end)
            _windowHasSpeech = false   // new window starts clean; later slices set it again
            ready.append(window)
        }
        // End-of-speech early flush: the wearer stopped talking ~0.8 s ago
        // and at least 0.8 s of speech is buffered, so send it now instead of
        // waiting for the rest of the 2.5 s window. A question ending 2 s into
        // a window used to wait ~0.5 s here; one ending 0.1 s in waited ~2.4 s.
        // Mid-sentence pauses over 0.8 s split the question across two chunks;
        // the server accumulates chunks for 6 s (voice.WINDOW_S), so a split
        // question still becomes one answer. Speech that keeps going never
        // flushes early: _lastSpeechAt keeps moving, so only the tail flushes.
        if _windowHasSpeech,
           buffer.count >= Int(Double(sampleRate * channels * (bitsPerSample / 8)) * Self.earlyFlushMinS),
           Date().timeIntervalSince(_lastSpeechAt) >= Self.earlyFlushSilenceS {
            partial = buffer
            buffer.removeAll(keepingCapacity: true)
            _windowHasSpeech = false
        }
        lock.unlock()

        // Callbacks outside the lock: the callback POSTs, and holding a lock
        // across a network hand-off is how the audio thread stalls.
        // Onset first: the chime is the point, it must not wait behind uploads.
        if onset { onSpeechOnset?() }
        for window in ready {
            emitOrDrop(window)
        }
        if let partial {
            emitOrDrop(partial)
        }
    }

    /// Called with the lock held. True once per speech onset: a 100 ms slice
    /// at or above onsetRMS, after at least onsetSilenceGapS with no loud
    /// slice, and at least onsetCooldownS since the last onset. Any loud slice
    /// refreshes _lastLoudAt, so a continuous sentence keeps the gap closed
    /// and fires only at its start. Slices are aligned to each feed() call;
    /// a loud slice straddling a feed boundary is caught by the next feed's
    /// first slice instead, at most one tap buffer later.
    private func scanForOnset(_ pcm: Data) -> Bool {
        guard bitsPerSample == 16, channels == 1,
              pcm.count >= 2, pcm.count % 2 == 0 else { return false }
        let now = Date()
        var fired = false
        let sliceSamples = sampleRate / 10  // 100 ms at 16 kHz = 1600 samples
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            var i = 0
            while i + sliceSamples <= samples.count {
                var sumSquares: Double = 0
                for j in i..<(i + sliceSamples) {
                    let v = Double(samples[j])
                    sumSquares += v * v
                }
                let rms = (sumSquares / Double(sliceSamples)).squareRoot()
                if rms >= Self.onsetRMS {
                    // Tracked whether or not anyone wants the onset chime:
                    // the end-of-speech early flush in feed() reads these.
                    _lastSpeechAt = now
                    _windowHasSpeech = true
                    if onSpeechOnset != nil, !fired
                        && now.timeIntervalSince(_lastLoudAt) >= Self.onsetSilenceGapS
                        && now.timeIntervalSince(_lastOnsetAt) >= Self.onsetCooldownS {
                        fired = true
                        _lastOnsetAt = now
                    }
                    _lastLoudAt = now
                }
                i += sliceSamples
            }
        }
        return fired
    }

    /// One window's fate: speech uploads, silence is dropped and counted.
    /// Called outside the lock, on the feed/flush caller's thread.
    private func emitOrDrop(_ window: Data) {
        guard !isSilent(window) else {
            lock.lock(); _droppedSilentWindows += 1; lock.unlock()
            onSilentWindow?()
            return
        }
        onChunk(Self.wav(pcm: window,
                         sampleRate: sampleRate,
                         channels: channels,
                         bitsPerSample: bitsPerSample))
    }

    /// True when the window is pure silence or room tone only: the LOUDEST 100 ms
    /// window's RMS is below `silenceRMS`. Whole-window RMS used to dilute one
    /// short quiet word in 2.5 s of room tone below the threshold and drop real
    /// speech; the loudest window is what matters (same rule as the server's
    /// voice.peak_window_rms, so device and server agree on what is silence).
    /// All-zero windows (a dead glasses mic) measure 0 and are dropped here, so
    /// they never reach Gemini to be hallucinated into words. Only the shipped
    /// 16-bit mono format is judged; any other format is treated as speech
    /// rather than guessed at.
    private func isSilent(_ pcm: Data) -> Bool {
        guard bitsPerSample == 16, channels == 1, pcm.count >= 2, pcm.count % 2 == 0 else { return false }
        let windowSamples = sampleRate / 10  // 100 ms at 16 kHz = 1600 samples
        var best: Double = 0
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            var i = 0
            while i < samples.count {
                let end = min(i + windowSamples, samples.count)
                var sumSquares: Double = 0
                for j in i..<end {
                    let v = Double(samples[j])
                    sumSquares += v * v
                }
                best = max(best, (sumSquares / Double(end - i)).squareRoot())
                i = end
            }
        }
        return best < Self.silenceRMS
    }

    /// Emit whatever is buffered, even a partial window, and clear.
    /// Called before teardown — flushing first is what preserves the tail audio (§7.3).
    func flush() {
        lock.lock()
        let remainder = buffer
        buffer.removeAll(keepingCapacity: true)
        lock.unlock()

        guard !remainder.isEmpty else { return }
        // A silent tail is dropped like a silent window: stopping the run
        // must not upload one last empty chunk for Whisper to invent words for.
        emitOrDrop(remainder)
    }

    /// Drop buffered audio without emitting. Exists so a query recording starts
    /// clean and cannot be prefixed by a fragment of an abandoned one (§7.7).
    func discard() {
        lock.lock()
        buffer.removeAll(keepingCapacity: true)
        lock.unlock()
    }

    /// Bytes currently buffered. Test/debug affordance only.
    var bufferedByteCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return buffer.count
    }

    // MARK: - WAV header
    //
    // Standard 44-byte RIFF/WAVE prefix, all multi-byte fields little-endian.
    // Built with explicit little-endian writes, NOT `withUnsafeBytes` of a
    // struct — struct padding corrupts the header in a way that plays fine in
    // QuickTime and fails in faster-whisper (§7.7).

    static func wav(pcm: Data, sampleRate: Int, channels: Int, bitsPerSample: Int) -> Data {
        let byteRate = sampleRate * channels * (bitsPerSample / 8)
        let blockAlign = channels * (bitsPerSample / 8)
        let dataSize = pcm.count

        var header = Data(capacity: 44 + dataSize)

        // RIFF chunk descriptor
        header.append(ascii: "RIFF")
        header.append(le32: UInt32(36 + dataSize))   // ChunkSize
        header.append(ascii: "WAVE")

        // fmt subchunk
        header.append(ascii: "fmt ")
        header.append(le32: 16)                      // Subchunk1Size, PCM
        header.append(le16: 1)                       // AudioFormat, 1 = PCM
        header.append(le16: UInt16(channels))
        header.append(le32: UInt32(sampleRate))
        header.append(le32: UInt32(byteRate))
        header.append(le16: UInt16(blockAlign))
        header.append(le16: UInt16(bitsPerSample))

        // data subchunk
        header.append(ascii: "data")
        header.append(le32: UInt32(dataSize))

        header.append(pcm)
        return header
    }
}

// MARK: - Little-endian writers

extension Data {
    mutating func append(ascii string: String) {
        append(contentsOf: Array(string.utf8))
    }

    mutating func append(le16 value: UInt16) {
        append(contentsOf: [
            UInt8(value & 0xFF),
            UInt8((value >> 8) & 0xFF),
        ])
    }

    mutating func append(le32 value: UInt32) {
        append(contentsOf: [
            UInt8(value & 0xFF),
            UInt8((value >> 8) & 0xFF),
            UInt8((value >> 16) & 0xFF),
            UInt8((value >> 24) & 0xFF),
        ])
    }
}
