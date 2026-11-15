//
//  BridgeController.swift
//  VexoBridge
//
//  Pipeline owner. CLAUDE-ios.md §7.3.
//
//  Android twin: BridgeService. THERE IS NO iOS FOREGROUND SERVICE — this is a
//  plain @MainActor class owned by the app, started and stopped from the UI, and
//  the app must stay frontmost with the screen awake for the whole demo (§11.1).
//

import Foundation
import QuartzCore
import UIKit

import MWDATCore
import MWDATCamera

@MainActor
final class BridgeController {

    // MARK: - Constants (Appendix A)
    //
    // Identical to Android so both bridges produce the same server-side cadence.
    // The server cannot tell the two bridges apart, and that is the point.

    static let frameIntervalSeconds: CFTimeInterval = 0.5   // every frame at the glasses' 2 fps; the server drops frames rather than lag
    static let uploadDrainSeconds: UInt64 = 3

    // MARK: - Collaborators

    let backend = BackendClient()
    private let wearables = WearablesManager()

    /// Feed, tasks, things, payments. Drives the phone screens AND the glasses.
    let memory: MemoryStore
    /// Phone GPS -> /location, for the ordering agent's "nearest" place.
    private(set) lazy var location = LocationReporter(backend: backend)
    private let player = AudioPlayer()

    #if DEBUG
    let mock = MockDeviceController()

    /// OFF: the real glasses camera works on SDK 0.7.0 (verified 2026-07-30), so
    /// the mock is no longer the fallback it was under 0.8.0. Flip to `true` to
    /// run the pipeline off the phone's back camera with no glasses present.
    var useMockDevice = false

    /// Turn the glasses mic off to isolate the §11.3 question: camera streaming
    /// runs over Bluetooth Classic, and HFP/SCO reserves a synchronous channel on
    /// that same radio. With this off, `start()` never activates an AVAudioSession
    /// and never claims HFP, so a stream that then reaches `.streaming` proves the
    /// two contend — and one that still dies with "Device is not connected"
    /// exonerates audio entirely.
    ///
    /// Defaults to true: this is a diagnostic switch, not a behaviour change.
    var useGlassesMic = true
    #endif

    // These two are touched from the audio tap's real-time thread and from the
    // main actor. Each is internally locked and `@unchecked Sendable`, so the
    // isolation opt-out is the documented §11.7 pattern, not an oversight.
    private nonisolated(unsafe) var passiveChunker: AudioChunker!
    private nonisolated(unsafe) var recorder: GlassesAudioRecorder!

    /// Owns the encoder and the throttle gate, and runs entirely on the SDK's
    /// frame-delivery thread. Kept out of @MainActor on purpose.
    private nonisolated(unsafe) var framePipeline: FramePipeline!

    private var uploads: [Task<Void, Never>] = []

    // MARK: - Freeze diagnostics
    //
    // A run streamed cleanly for ~7.5 min, stalled 93.7 s, then stopped dead with
    // both video and audio frozen on the same second — and nothing in the log
    // explained it, because only POST *failures* were recorded and there was no
    // periodic heartbeat. These counters make the four candidate causes
    // distinguishable at a glance:
    //
    //   frames_rx stalls, pcm climbs      -> SDK stopped delivering video
    //   both stall                        -> app suspended / backgrounded
    //   both climb, posted stalls         -> uploads failing (network)
    //   rec=false                         -> audio engine died
    private var heartbeatTask: Task<Void, Never>?
    private var micWatchdog: Task<Void, Never>?

    /// Throttle for the silent-drop log: dropping is normal in a quiet room,
    /// so it logs at most once per 30 s, never per chunk.
    private var lastSilentDropLogAt = Date.distantPast

    private func noteSilentChunkDropped() {
        guard Date().timeIntervalSince(lastSilentDropLogAt) > 30 else { return }
        lastSilentDropLogAt = Date()
        BridgeState.log("dropping silent audio chunks (\(passiveChunker.droppedSilentWindows) so far) — mic is silent/room tone only, nothing uploaded to /capture")
    }

    /// Fast wake chime: the phone pings the moment speech starts, instead of
    /// waiting for the 2.5 s chunk + upload + Gemini transcription + 0.5 s
    /// speech poll that made the old chime land 3+ s after "hey". The sound
    /// is the same E5 then A5 notes (AudioPlayer.playLocalChime).
    ///
    /// Gated, because onset is VAD, not a wake-word detector -- it cannot
    /// tell "hey scene" from anyone else talking:
    ///  - Conversation Mode is on: the boss talking would chime constantly.
    ///  - Jessica is speaking (player busy): the onset is her echo or a
    ///    barge-in, and the backend's barge-in path owns that; the local chime
    ///    never preempts her (playLocalChime returns false then).
    /// A false ping on any other loud speech is the honest trade-off: the
    /// chime means "I heard speech", and the backend chime a few seconds
    /// later still confirms a real "hey scene" (deduped to one ping by
    /// AudioPlayer when it is the same wake word).
    private func playLocalWakeChime() {
        guard BridgeState.shared.isRunning else { return }
        guard memory.conversation?.isActive != true else { return }
        guard player.playLocalChime() else { return }
        BridgeState.log("local wake chime (speech onset, before transcription)")
        // The tone reaches the mic: tell the backend so its chime-echo
        // filter drops it instead of transcribing it. Fire-and-forget -- a
        // failed beacon must never delay or block the chime that just played.
        Task { [weak self] in
            guard let self else { return }
            let _: Ignored? = try? await self.backend.post("/chime/local")
        }
    }

    /// Restarts the recorder when its input goes dead (pure zeros). Checks every 2 s; a restart
    /// is skipped if one happened in the last 10 s, so a genuinely broken mic can't thrash.
    /// Two restarts that both come back silent mean the selected input itself is dead, not
    /// flaky: say so loudly in Diagnostics instead of restarting quietly forever. Audio is
    /// glasses-only (Nayan, 2026-11-14): a silent glasses mic stays silent and visible, it
    /// is never quietly replaced by phone audio. The only way onto the iPhone mic is the
    /// manual Diagnostics toggle, which defaults OFF. In Display Audio Mode the mode's own
    /// 8 s check owns the messaging, so the watchdog only restarts there. Sound returning
    /// clears the warning and the count.
    private func startMicWatchdog() {
        micWatchdog?.cancel()
        micWatchdog = Task { [weak self] in
            var lastRestart = Date.distantPast
            var deadRestarts = 0
            var warnStage = 0   // 0 none, 1 silent-mic warning shown, 2 still-silent warning shown
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard let self, !Task.isCancelled, BridgeState.shared.isRunning, self.recorder.isRunning else { continue }
                if self.recorder.secondsSinceSound > 5 {
                    guard Date().timeIntervalSince(lastRestart) > 10 else { continue }
                    lastRestart = Date()
                    deadRestarts += 1
                    // Which mic is actually selected, so the warning names the right one.
                    let micLabel = GlassesAudioRecorder.usePhoneMic ? "iPhone mic" : "glasses mic"
                    if deadRestarts >= 2, !self.displayAudioMode, warnStage == 0 {
                        BridgeState.log("!! \(micLabel) SILENT after restarts — no audio is being captured. A silent mic stays silent; it is never replaced by phone audio. Turn 'Use iPhone microphone' ON in Settings to override manually, or tap Stop then Start to retry.")
                        BridgeState.setAudioWarning("Microphone is silent (\(micLabel)) — no audio is being captured. Tap Stop then Start to retry, or turn 'Use iPhone microphone' ON in Settings.")
                        warnStage = 1
                        deadRestarts = 0
                    } else if deadRestarts >= 2, !self.displayAudioMode, warnStage < 2 {
                        warnStage = 2
                        BridgeState.log("!! MIC STILL SILENT on \(micLabel) — no audio is being captured. Tap Stop then Start to retry.")
                        BridgeState.setAudioWarning("Microphone is silent — no audio is being captured. Tap Stop then Start to retry.")
                    } else {
                        BridgeState.log("mic delivering only silence for 5 s — restarting the recorder")
                    }
                    self.recorder.stop()
                    await self.recorder.start()
                } else {
                    deadRestarts = 0
                    if warnStage > 0 {
                        warnStage = 0
                        BridgeState.setAudioWarning(nil)
                        BridgeState.log("mic has sound again — silence warning cleared")
                    }
                }
            }
        }
    }

    // MARK: - Display Audio Mode
    //
    // The glasses web app and the DAT camera cannot run at the same time, but
    // the glasses mic is a separate path from the camera, so Scene can still
    // listen while the display is open — just without new frames. Audio is
    // glasses-only: nothing is recorded from the phone in this mode. The lens
    // beacons the backend (POST /display/heartbeat); the phone learns the mode
    // by polling /session in MemoryStore and calls enter/exit here via
    // onDisplayAudioModeChanged.
    //
    // Enter: pause the frame pipeline (no JPEG POSTs, no Gemini vision),
    // suspend wearables auto-recovery (the device ended the session because
    // the display opened — rebuilding it would fight the display), and restart
    // the audio engine once (opening the web app disrupts the audio session
    // and the tap can go dead with all-zero buffers). The glasses mic stays
    // the source; nothing forces the iPhone mic.
    // Exit: resume frames and let recovery rebuild the session if the stream
    // is gone. No manual Start/Stop needed either way.

    private(set) var displayAudioMode = false
    private var micSilentCheckTask: Task<Void, Never>?

    func enterDisplayAudioMode() {
        guard BridgeState.shared.isRunning, !displayAudioMode else { return }
        displayAudioMode = true
        framePipeline.paused = true
        wearables.suspendRecoveryForDisplay()
        BridgeState.log("display audio mode: frames paused, glasses mic listening (camera off)")
        recorder.restartForDisplayAudioMode()
        // Honesty check, not a pretence: if the mic is still silent 8 s after
        // the restart, say so on the phone instead of silently hearing nothing.
        micSilentCheckTask?.cancel()
        micSilentCheckTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(8))
            guard let self, !Task.isCancelled, self.displayAudioMode, self.recorder.isRunning else { return }
            if self.recorder.secondsSinceSound > 5 {
                BridgeState.log("!! display audio mode: mic still silent after restart — tap Stop then Start to retry")
                self.memory.notice = "Mic silent, tap Start to retry"
            }
        }
    }

    func exitDisplayAudioMode() {
        guard displayAudioMode else { return }
        displayAudioMode = false
        micSilentCheckTask?.cancel(); micSilentCheckTask = nil
        framePipeline.paused = false
        recorder.clearDisplayAudioMode()
        wearables.resumeRecoveryAfterDisplay()
        BridgeState.log("display closed: frames resume")
        // The audio engine was restarted for the display; re-assert the
        // normal route (user's mic toggle, glasses by default) so a later
        // session starts on the right input.
        Task { [weak self] in
            guard let self else { return }
            self.recorder.stop()
            await self.recorder.start()
        }
    }

    /// All three are lock-protected rather than actor-isolated: the POST closures
    /// are `@Sendable` and run off the main actor, and the mic tap fires on the
    /// audio thread, which must not hop or await (§11.7).
    private nonisolated let framesPostedCounter = FrameCounter()
    private nonisolated let audioPostedCounter = FrameCounter()
    private nonisolated let pcmCounter = FrameCounter()

    // MARK: - Init — all wiring happens once, here.

    init() {
        memory = MemoryStore(backend: backend)

        // Passive chunker: every 2.5 s window WITH SPEECH goes to /capture,
        // or sooner: a partial window flushes ~0.8 s after speech ends, so a
        // question does not wait out the rest of its window.
        // Silent windows never leave the phone: the chunker drops them (RMS <
        // 32, about -60 dBFS) so Whisper can't hallucinate questions out of
        // silence. The drop callback only throttles a log line — dropping is
        // normal in a quiet room, not an error.
        passiveChunker = AudioChunker(
            onChunk: { [weak self] wav in
                Task { @MainActor in
                    self?.enqueueUpload { [weak self] in
                        guard let self else { return }
                        do {
                            try await self.backend.postAudio(wav)
                            _ = self.audioPostedCounter.increment()
                        } catch { BridgeState.logError("POST /capture (audio)", error) }
                    }
                }
            },
            onSilentWindow: { [weak self] in
                Task { @MainActor in self?.noteSilentChunkDropped() }
            },
            // Fast wake chime: fires on speech onset (~100-250 ms in), on the
            // tap thread -- hop to the main actor before touching the player.
            onSpeechOnset: { [weak self] in
                Task { @MainActor in self?.playLocalWakeChime() }
            }
        )

        // One mic, one consumer: every PCM buffer feeds the 2.5 s chunker.
        recorder = GlassesAudioRecorder { [weak self] pcm in
            guard let self else { return }
            _ = self.pcmCounter.increment()
            self.passiveChunker.feed(pcm)
        }

        // Video path: throttle -> encode -> POST. Everything up to the POST stays
        // on the SDK's background thread (§7.5, §11.7).
        framePipeline = FramePipeline(interval: Self.frameIntervalSeconds) { [weak self] jpeg in
            Task { @MainActor in
                self?.enqueueUpload { [weak self] in
                    guard let self else { return }
                    do {
                        try await self.backend.postPhoto(jpeg)
                        _ = self.framesPostedCounter.increment()
                        BridgeState.markFrame()
                    } catch {
                        BridgeState.logError("POST /capture (photo)", error)
                    }
                }
            }
        }

        wearables.onFrame = { [weak self] frame in
            self?.framePipeline.handle(frame)
        }

        // Every new card goes to the glasses; their buttons act through the store.
        wearables.glassesDisplay.store = memory
        memory.onNewCard = { [weak self] card in
            self?.wearables.glassesDisplay.show(card)
        }
        // Scene's voice plays from the phone, which iOS routes to the glasses' speakers.
        memory.onSpeech = { [weak self] audio in
            self?.player.speak(audio)
        }
        memory.onChime = { [weak self] in
            self?.player.chime()
        }
        // The answer is done: the descending chime-out, queued behind the answer.
        memory.onChimeOut = { [weak self] in
            self?.player.chimeOut()
        }
        // Barge-in: the wearer talked over Jessica -- stop her on the glasses
        // now. The backend already cleared its queue and killed the laptop
        // player; the phone drops its queued lines in the speech poll.
        memory.onInterrupt = { [weak self] in
            self?.player.stop()
        }
        // The lens beacons the backend when the display opens; /session carries
        // the mode back to the phone, which pauses frames and keeps the mic.
        memory.onDisplayAudioModeChanged = { [weak self] on in
            Task { @MainActor in
                if on { self?.enterDisplayAudioMode() } else { self?.exitDisplayAudioMode() }
            }
        }

        // NOTHING in this initializer may touch the MWDAT SDK. SwiftUI evaluates
        // an App's stored properties before its init() body, so this runs before
        // Wearables.configure() — and `Wearables.shared` traps if read first.
        // Registration observation therefore starts in onLaunch() (§6.1 step 1).
    }

    // MARK: - Launch-time work (§6.1)

    /// Runs from ContentView's `.task`, i.e. after `Wearables.configure()` has
    /// executed in the App's init body. This is the first point at which any SDK
    /// call is legal.
    func onLaunch() async {
        wearables.observeRegistration()

        let granted = await GlassesAudioRecorder.requestMicrophonePermission()
        BridgeState.log("microphone permission: \(granted ? "granted" : "DENIED")")
        await pingBackend()
        memory.startPolling()
        location.start()

        #if DEBUG
        // Unattended MockDeviceKit runs (docs/VERIFICATION.md):
        //   -autostartMock YES   enable the mock device and press Start.
        // Debug-only; Release has no mock code at all.
        if UserDefaults.standard.bool(forKey: "autostartMock") {
            BridgeState.log("autostartMock: enabling mock device and starting")
            useMockDevice = true
            await start()
        }
        #endif
    }

    /// Fire one throwaway request at /stats to raise the Local Network prompt
    /// while the user is still looking at the screen. There is no API to query
    /// that permission's status, so a failure here is a hint, not a verdict.
    /// Note the prompt fires again on a new network interface — re-run this after
    /// switching to a hotspot (§11.2).
    func pingBackend() async {
        do {
            _ = try await backend.getStats()
            BridgeState.log("backend reachable at \(backend.baseURL)")
        } catch {
            BridgeState.log("backend unreachable at \(backend.baseURL) — \(error.localizedDescription)")
            // Laptop moved networks or IPs: find it over Bonjour instead of making
            // someone retype an address on stage.
            if let found = await Discovery.findReceiver(), found != backend.baseURL {
                backend.setBaseURL(found)
                BridgeState.log("found receiver via Bonjour at \(found)")
                if (try? await backend.getStats()) != nil { return }
            }
            BridgeState.log("if this persists: Settings ▸ Privacy & Security ▸ Local Network ▸ Scene")
        }
    }

    // MARK: - Read-outs for the Glasses tab (polled, not observed)

    var framesSent: Int { framesPostedCounter.value }
    var framesFromGlasses: Int { wearables.framesReceived }
    var reconnects: Int { max(0, wearables.generation - 1) }
    var displayState: String { wearables.glassesDisplay.stateText }

    /// Look for the laptop over Bonjour and switch to it. True if found and reachable.
    func findLaptop() async -> Bool {
        guard let found = await Discovery.findReceiver() else { return false }
        backend.setBaseURL(found)
        return (try? await backend.getStats()) != nil
    }

    func connectWearables() async {
        await wearables.startRegistration()
    }

    func ensureCameraPermission() async {
        await wearables.ensureCameraPermission()
    }

    func handleOpenURL(_ url: URL) async {
        guard WearablesRuntime.isConfigured else {
            BridgeState.shared.append("!! dropped callback URL: SDK not configured")
            return
        }
        do {
            _ = try await Wearables.shared.handleUrl(url)
            BridgeState.log("handled callback URL")
        } catch {
            BridgeState.logError("handleUrl", error)
        }
    }

    // MARK: - Start / Stop

    /// Idempotent.
    func start() async {
        guard !BridgeState.shared.isRunning else { return }
        BridgeState.shared.isRunning = true

        // Without this the screen locks mid-demo and streaming stops (§7.2).
        UIApplication.shared.isIdleTimerDisabled = true

        #if DEBUG
        if useMockDevice {
            await mock.enable()
        }
        #endif

        await wearables.start()

        #if DEBUG
        if useGlassesMic {
            await recorder.start()
        } else {
            // Say it loudly. A silent audio path looks identical to a broken one
            // in the log, and this is the exact confusion §11.4 warns about.
            BridgeState.log("*** glasses mic DISABLED (debug) — no HFP, no audio will be captured")
        }
        #else
        await recorder.start()
        #endif
        startMicWatchdog()

        #if DEBUG
        startHeartbeat()
        #endif
        BridgeState.log("bridge started")
    }

    /// Order matters, same as Android: recorder -> flush -> player -> wearables
    /// -> mock -> idle timer -> drain. Flushing before teardown is what preserves
    /// the tail audio (§7.3).
    func stop() async {
        guard BridgeState.shared.isRunning else { return }

        #if DEBUG
        logHeartbeat()          // final tally before the counters go away
        stopHeartbeat()
        #endif

        recorder.stop()
        BridgeState.setAudioWarning(nil)
        passiveChunker.flush()

        micWatchdog?.cancel(); micWatchdog = nil
        micSilentCheckTask?.cancel(); micSilentCheckTask = nil
        displayAudioMode = false
        framePipeline.paused = false
        recorder.clearDisplayAudioMode()
        player.stop()
        // async under SDK 0.7.0 — see WearablesManager.stop().
        await wearables.stop()

        #if DEBUG
        await mock.disable()
        #endif

        UIApplication.shared.isIdleTimerDisabled = false
        BridgeState.shared.isRunning = false

        await drainUploads()
        BridgeState.log("bridge stopped")
    }

    /// Let in-flight uploads finish for 3 s, then cancel what is left.
    private func drainUploads() async {
        try? await Task.sleep(for: .seconds(Self.uploadDrainSeconds))
        for task in uploads { task.cancel() }
        uploads.removeAll()
    }

    // MARK: - Activation (§7.3)
    //
    // Each step has INDEPENDENT error handling. A failed /activate must not
    // suppress the photo.

    func activate() async {
        // 1 — chime immediately, so the user hears the press landed.
        player.playThinkingChime()

        // 2 — /activate
        do { try await backend.postActivate() }
        catch { BridgeState.logError("POST /activate", error) }

        // 3 — full-resolution photo, tagged so the receiver can tell it apart.
        if let jpeg = await wearables.capturePhotoJPEG() {
            do { try await backend.postPhoto(jpeg, source: "activation") }
            catch { BridgeState.logError("POST /capture (activation photo)", error) }
        } else {
            BridgeState.shared.append("activation photo unavailable")
        }
    }

    // MARK: - Heartbeat

    #if DEBUG
    static let heartbeatInterval: Duration = .seconds(15)

    private func startHeartbeat() {
        heartbeatTask?.cancel()
        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: Self.heartbeatInterval)
                guard !Task.isCancelled, let self else { break }
                self.logHeartbeat()
            }
        }
    }

    private func stopHeartbeat() {
        heartbeatTask?.cancel()
        heartbeatTask = nil
    }

    /// One line, every interval, unconditionally — including while everything is
    /// frozen. That is the point: a heartbeat that keeps printing while the
    /// counters stop moving localises the freeze, and a heartbeat that itself
    /// stops printing proves the whole app was suspended.
    private func logHeartbeat() {
        BridgeState.log(
            "HB gen=\(wearables.generation) rx=\(wearables.framesReceived) posted=\(framesPostedCounter.value)"
            + " | pcm=\(pcmCounter.value) audio_posted=\(audioPostedCounter.value) silent_dropped=\(passiveChunker.droppedSilentWindows)"
            + " | stream=\(wearables.streamStateText) disp=\(wearables.glassesDisplay.stateText) rec=\(recorder.isRunning)"
            + " phase=\(BridgeState.shared.scenePhaseText)"
        )
    }
    #endif

    // MARK: - Scene phase (§11.1)

    func noteScenePhaseChange(_ description: String) {
        #if DEBUG
        BridgeState.shared.scenePhaseText = description
        #endif
        BridgeState.shared.append("scenePhase: \(description)")
    }

    // MARK: - Upload bookkeeping
    //
    // No retry queue anywhere. This array exists only so stop() can cancel what
    // is still in flight after the 3 s drain.

    private func enqueueUpload(_ body: @escaping @Sendable () async -> Void) {
        uploads.removeAll { $0.isCancelled }
        if uploads.count > 64 { uploads.removeFirst(uploads.count - 64) }
        uploads.append(Task { await body() })
    }
}

// MARK: - FramePipeline
//
// Deliberately NOT @MainActor. Throttle and JPEG encode both run on the SDK's
// frame-delivery thread; only the POST hops to the main actor, inside `send`.

final class FramePipeline: @unchecked Sendable {

    private let encoder = ImageEncoder()
    private let interval: CFTimeInterval
    private let send: @Sendable (Data) -> Void

    private let lock = NSLock()
    private var lastSentAt: CFTimeInterval = 0

    /// Display Audio Mode: the glasses web app is open, so the DAT camera cannot
    /// run. Frames are dropped here (and the server skips vision on any that
    /// sneak through) while the glasses mic keeps posting audio.
    var paused = false

    init(interval: CFTimeInterval, send: @escaping @Sendable (Data) -> Void) {
        self.interval = interval
        self.send = send
    }

    func handle(_ frame: VideoFrame) {
        guard !paused else { return }
        guard passesGate() else { return }
        guard let jpeg = encoder.jpeg(from: frame) else { return }
        send(jpeg)
    }

    /// CACurrentMediaTime() is monotonic. Date() is not and can jump, stalling
    /// or flooding the pipeline (§7.3).
    private func passesGate() -> Bool {
        let now = CACurrentMediaTime()
        lock.lock()
        defer { lock.unlock() }
        guard now - lastSentAt >= interval else { return false }
        lastSentAt = now
        return true
    }
}
