//
//  AudioPlayer.swift
//  VexoBridge
//
//  Activation chime. CLAUDE-ios.md §7.10.
//
//  Answer playback was removed with the rest of the Findr query path. Read
//  §11.5 before adding any: TTS out over the HFP channel while recording over
//  it gives telephone-grade audio and may duck or drop the recording.
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

    func stop() {
        player?.stop()
        player = nil
    }
}

extension AudioPlayer: AVAudioPlayerDelegate {
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor [weak self] in
            self?.player = nil
        }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        if let error { BridgeState.logError("audio decode", error) }
        Task { @MainActor [weak self] in
            self?.player = nil
        }
    }
}
