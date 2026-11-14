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

    private let onChunk: @Sendable (Data) -> Void

    private let lock = NSLock()
    private var buffer = Data()

    init(
        sampleRate: Int = 16_000,
        channels: Int = 1,
        bitsPerSample: Int = 16,
        windowMillis: Int = 2_500,
        onChunk: @escaping @Sendable (Data) -> Void
    ) {
        self.sampleRate = sampleRate
        self.channels = channels
        self.bitsPerSample = bitsPerSample
        self.windowMillis = windowMillis
        self.bytesPerWindow = sampleRate * channels * (bitsPerSample / 8) * windowMillis / 1000
        self.onChunk = onChunk
    }

    // MARK: - Entry points (three, matching Android)

    /// Append PCM. Emits one WAV per full window; a single large feed can emit
    /// several. Called from the audio tap thread — no allocation beyond the
    /// buffer growth, no awaits.
    func feed(_ pcm: Data) {
        var ready: [Data] = []

        lock.lock()
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
            ready.append(window)
        }
        lock.unlock()

        // Callbacks outside the lock: the callback POSTs, and holding a lock
        // across a network hand-off is how the audio thread stalls.
        for window in ready {
            onChunk(Self.wav(pcm: window,
                             sampleRate: sampleRate,
                             channels: channels,
                             bitsPerSample: bitsPerSample))
        }
    }

    /// Emit whatever is buffered, even a partial window, and clear.
    /// Called before teardown — flushing first is what preserves the tail audio (§7.3).
    func flush() {
        lock.lock()
        let remainder = buffer
        buffer.removeAll(keepingCapacity: true)
        lock.unlock()

        guard !remainder.isEmpty else { return }
        onChunk(Self.wav(pcm: remainder,
                         sampleRate: sampleRate,
                         channels: channels,
                         bitsPerSample: bitsPerSample))
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
