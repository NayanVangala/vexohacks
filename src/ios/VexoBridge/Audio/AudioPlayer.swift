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
