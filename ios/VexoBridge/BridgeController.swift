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

    static let frameIntervalSeconds: CFTimeInterval = 2.5
    static let uploadDrainSeconds: UInt64 = 3

    // MARK: - Collaborators

    let backend = BackendClient()
    private let wearables = WearablesManager()

    /// Feed, tasks, things, payments. Drives the phone screens AND the glasses.
    let memory: MemoryStore
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

    /// All three are lock-protected rather than actor-isolated: the POST closures
    /// are `@Sendable` and run off the main actor, and the mic tap fires on the
    /// audio thread, which must not hop or await (§11.7).
    private nonisolated let framesPostedCounter = FrameCounter()
    private nonisolated let audioPostedCounter = FrameCounter()
    private nonisolated let pcmCounter = FrameCounter()

    // MARK: - Init — all wiring happens once, here.

    init() {
        memory = MemoryStore(backend: backend)

        // Passive chunker: every full 2.5 s window goes straight to /capture.
        passiveChunker = AudioChunker { [weak self] wav in
            Task { @MainActor in
                self?.enqueueUpload { [weak self] in
                    guard let self else { return }
                    do {
                        try await self.backend.postAudio(wav)
                        _ = self.audioPostedCounter.increment()
                    } catch { BridgeState.logError("POST /capture (audio)", error) }
                }
            }
        }

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
            BridgeState.log("if this persists: Settings ▸ Privacy & Security ▸ Local Network ▸ Vexo Bridge")
        }
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
        passiveChunker.flush()

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
            + " | pcm=\(pcmCounter.value) audio_posted=\(audioPostedCounter.value)"
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

    init(interval: CFTimeInterval, send: @escaping @Sendable (Data) -> Void) {
        self.interval = interval
        self.send = send
    }

    func handle(_ frame: VideoFrame) {
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
