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
    func chime() {
        queue.insert(Self.chimeWAV, at: 0)
        if player == nil { playNext() }
    }

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
