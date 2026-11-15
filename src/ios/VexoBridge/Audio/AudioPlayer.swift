//
//  AudioPlayer.swift
//  VexoBridge
//
//  Activation chime. CLAUDE-ios.md §7.10.
//
//  Also Scene's spoken lines (ElevenLabs answers, "Want me to…?" questions): the
//  phone plays them and iOS routes them to the glasses' speakers. That's clean
//  because recording now uses the iPhone mic, so the glasses' Bluetooth isn't
//  also carrying a recording (playing and recording over HFP together gives
//  telephone-grade audio and can drop the recording).
//

import AVFoundation
import Foundation

@MainActor
final class AudioPlayer: NSObject {

    /// A leaked player holds the route and the next answer plays over the top of
    /// it — released on completion, always (§7.10).
    private var player: AVAudioPlayer?

    /// True while a line is actually playing (not merely queued).
    var isPlaying: Bool { player != nil }

    /// When the fast local chime last pinged. The backend chime for the same
    /// wake word lands ~3 s later (after the 2.5 s chunk + Gemini); without
    /// this the wearer hears two pings for one "hey scene".
    private var lastLocalChimeAt = Date.distantPast
    /// Dedup window for the backend chime after a local one: covers the chunk
    /// + transcription lag, and matches the 4 s wake cooldown, so a genuine
    /// second "hey scene" right after the first shares the one ack.
    private static let localChimeDedupS: TimeInterval = 4.0

    // MARK: - Chime

    /// Fires the instant activation starts. ~3.5 s separates the end of the
    /// user's question from the first spoken word; an immediate acknowledgement
    /// is most of the perceived-latency fix, and it costs nothing.
    func playThinkingChime() {
        if let url = Bundle.main.url(forResource: "thinking_chime", withExtension: "wav") {
            play(contentsOf: url)
            return
        }
        // No bundled asset: fall back to a system sound rather than silence.
        // TODO(assets): add a short thinking_chime.wav to the bundle.
        AudioServicesPlaySystemSound(1113)   // "begin recording"
    }

    private func play(contentsOf url: URL) {
        stop()
        do {
            let player = try AVAudioPlayer(contentsOf: url)
            player.delegate = self
            self.player = player
            player.prepareToPlay()
            player.play()
        } catch {
            BridgeState.logError("chime playback", error)
            self.player = nil
        }
    }

    // MARK: - Wake chime

    /// "I heard you": two soft notes, played the moment "hey scene" is recognized. Jumps the queue.
    /// Preempts whatever is playing: stop the current player only (the queue of
    /// unplayed lines stays) so the ack is heard now, not after a whole answer
    /// finishes. Stopping an AVAudioPlayer does not fire
    /// audioPlayerDidFinishPlaying, so this cannot double-start the next item.
    /// chimeOut() deliberately does NOT preempt: it appends behind the answer.
    /// Skipped when the fast local chime already pinged for this wake word
    /// (see playLocalChime): one "hey scene", one ping, never two.
    func chime() {
        guard Date().timeIntervalSince(lastLocalChimeAt) >= Self.localChimeDedupS else { return }
        player?.stop()
        player = nil
        queue.insert(Self.chimeWAV, at: 0)
        playNext()
    }

    /// Fast wake chime: the same E5 then A5 notes, played the moment speech
    /// STARTS (AudioChunker's onset callback, ~100-250 ms in), not after the
    /// backend transcribes "hey scene" seconds later. Unlike chime(), this
    /// NEVER preempts: if Jessica is speaking, the onset is her echo or a
    /// barge-in, and the backend's barge-in path owns that -- a local ping
    /// over her voice would be noise on top of noise. Returns false when it
    /// did not play, so the caller can skip telling the backend about it.
    /// The caller also notifies the backend (POST /chime/local) so its
    /// chime-echo filter drops the tone the mic picks up.
    @discardableResult
    func playLocalChime() -> Bool {
        guard player == nil else { return false }
        guard Date().timeIntervalSince(lastLocalChimeAt) >= 1 else { return false }
        lastLocalChimeAt = Date()
        queue.insert(Self.chimeWAV, at: 0)
        playNext()
        return true
    }

    /// "I'm done": two soft notes, DESCENDING, played the moment a real answer finishes.
    /// Unlike chime(), this APPENDS to the queue: the backend queues the chime-out line
    /// behind the spoken answer, so it sounds after the answer ends, never before it and
    /// never over it. Only fired for a real answer (or a shown answer card); dropped or
    /// hallucinated chunks never become a speech line, so they never chime out.
    func chimeOut() {
        queue.append(Self.chimeOutWAV)
        if player == nil { playNext() }
    }

    /// G6 then C6, descending (the wake chime climbs E5 then A5). Same quiet, round
    /// rendering as chimeWAV, so the two are easy to tell apart by direction alone.
    private static let chimeOutWAV: Data = {
        let sr = 44_100.0, total = 0.38
        let notes: [(freq: Double, start: Double, dur: Double)] = [(1568.0, 0, 0.16), (1046.5, 0.09, 0.28)]
        var samples = [Double](repeating: 0, count: Int(total * sr))
        for note in notes {
            let s0 = Int(note.start * sr)
            for i in 0..<Int(note.dur * sr) where s0 + i < samples.count {
                let t = Double(i) / sr
                let envelope = min(1, t * 55) * exp(-t * 15)           // ~18 ms swell in, quick fade: no click, no ring
                samples[s0 + i] += sin(2 * .pi * note.freq * t) * envelope * 0.13
            }
        }
        var pcm = Data(capacity: samples.count * 2)
        for s in samples {
            var v = Int16(max(-1, min(1, s)) * 32_767).littleEndian
            withUnsafeBytes(of: &v) { pcm.append(contentsOf: $0) }
        }
        func le32(_ x: UInt32) -> Data { withUnsafeBytes(of: x.littleEndian) { Data($0) } }
        func le16(_ x: UInt16) -> Data { withUnsafeBytes(of: x.littleEndian) { Data($0) } }
        var wav = Data("RIFF".utf8) + le32(UInt32(36 + pcm.count)) + Data("WAVEfmt ".utf8)
        wav += le32(16) + le16(1) + le16(1) + le32(UInt32(sr)) + le32(UInt32(sr) * 2) + le16(2) + le16(16)
        wav += Data("data".utf8) + le32(UInt32(pcm.count)) + pcm
        return wav
    }()

    /// E5 then A5, quiet and round (low pitch, slow attack, short tail), rendered once as 16-bit mono WAV.
    private static let chimeWAV: Data = {
        let sr = 44_100.0, total = 0.38
        let notes: [(freq: Double, start: Double, dur: Double)] = [(659.3, 0, 0.16), (880.0, 0.09, 0.28)]
        var samples = [Double](repeating: 0, count: Int(total * sr))
        for note in notes {
            let s0 = Int(note.start * sr)
            for i in 0..<Int(note.dur * sr) where s0 + i < samples.count {
                let t = Double(i) / sr
                let envelope = min(1, t * 55) * exp(-t * 15)           // ~18 ms swell in, quick fade: no click, no ring
                samples[s0 + i] += sin(2 * .pi * note.freq * t) * envelope * 0.13
            }
        }
        var pcm = Data(capacity: samples.count * 2)
        for s in samples {
            var v = Int16(max(-1, min(1, s)) * 32_767).littleEndian
            withUnsafeBytes(of: &v) { pcm.append(contentsOf: $0) }
        }
        func le32(_ x: UInt32) -> Data { withUnsafeBytes(of: x.littleEndian) { Data($0) } }
        func le16(_ x: UInt16) -> Data { withUnsafeBytes(of: x.littleEndian) { Data($0) } }
        var wav = Data("RIFF".utf8) + le32(UInt32(36 + pcm.count)) + Data("WAVEfmt ".utf8)
        wav += le32(16) + le16(1) + le16(1) + le32(UInt32(sr)) + le32(UInt32(sr) * 2) + le16(2) + le16(16)
        wav += Data("data".utf8) + le32(UInt32(pcm.count)) + pcm
        return wav
    }()

    // MARK: - Spoken lines

    /// Lines waiting their turn: a new answer never cuts off the one being spoken.
    private var queue: [Data] = []

    func speak(_ audio: Data) {
        queue.append(audio)
        if player == nil { playNext() }
    }

    private func playNext() {
        guard !queue.isEmpty else { return }
        let data = queue.removeFirst()
        do {
            let player = try AVAudioPlayer(data: data)
            player.delegate = self
            self.player = player
            player.prepareToPlay()
            player.play()
            BridgeState.log("SPEAK on \(AVAudioSession.sharedInstance().currentRoute.outputs.first?.portName ?? "?")")
        } catch {
            BridgeState.logError("speech playback", error)
            self.player = nil
            playNext()
        }
    }

    func stop() {
        player?.stop()
        player = nil
        queue.removeAll()
    }
}

extension AudioPlayer: AVAudioPlayerDelegate {
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor [weak self] in
            self?.player = nil
            self?.playNext()
        }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        if let error { BridgeState.logError("audio decode", error) }
        Task { @MainActor [weak self] in
            self?.player = nil
        }
    }
}
