//
//  GlassesAudioRecorder.swift
//  VexoBridge
//
//  The iOS answer to Android's Bluetooth SCO. CLAUDE-ios.md §7.6.
//
//  Same idea, different plumbing: HFP is the bidirectional voice channel SCO
//  carries. You do not start it explicitly the way Android does — you ask the
//  audio session for a category that permits it, then select the HFP input port.
//
//  Every failure path writes to BridgeState and returns. Nothing here throws to
//  the caller: a missing glasses mic must never take the service down.
//

import AVFoundation
import Foundation

final class GlassesAudioRecorder: @unchecked Sendable {

    // MARK: - Constants (Appendix A)
    //
    // MUST equal AudioChunker's. A mismatch does not error — it produces
    // speed-distorted WAVs and silently wrecks Phase 3 transcription accuracy.

    static let targetSampleRate: Double = 16_000
    static let targetChannels: AVAudioChannelCount = 1
    static let tapBufferSize: AVAudioFrameCount = 4_096

    /// How long to let a requested input route land before reading the format.
    /// Generous on purpose: an HFP switch is slower than a wired one, and the
    /// cost of overshooting is a moment of latency, while the cost of
    /// undershooting is a whole session of silent WAVs.
    static let routeSettleTimeout: CFTimeInterval = 1.5

    // MARK: - Callback

    /// 16 kHz mono Int16 LE PCM. Called on the audio tap's real-time thread —
    /// do not await inside it, do not touch the main actor from it directly.
    private let onPCM: @Sendable (Data) -> Void

    // MARK: - State

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var targetFormat: AVAudioFormat?
    private let stateLock = NSLock()
    private var running = false
    private var observers: [NSObjectProtocol] = []

    /// Re-entrancy/debounce guards for route-change restarts.
    private var restarting = false
    private var lastRestartAt: CFTimeInterval = 0

    /// Last port we logged, so a repeated selection stays quiet.
    private var lastLoggedPortUID: String?

    init(onPCM: @escaping @Sendable (Data) -> Void) {
        self.onPCM = onPCM
    }

    var isRunning: Bool {
        stateLock.lock(); defer { stateLock.unlock() }
        return running
    }

    // MARK: - Permission (§6.1 step 2)

    static func requestMicrophonePermission() async -> Bool {
        // iOS 17+ spelling. AVAudioSession.requestRecordPermission is deprecated.
        await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    // MARK: - Start

    func start() async {
        stateLock.lock()
        if running { stateLock.unlock(); return }
        running = true
        stateLock.unlock()

        do {
            try await configureSession()
            let chosen = selectPreferredInput()
            if let chosen { await awaitRouteSettled(expecting: chosen) }
            try installTapAndStart()
            installNotificationObservers()
            BridgeState.log("audio recorder started")
        } catch {
            BridgeState.log("!! audio recorder start FAILED: \(Self.describe(error))")
            BridgeState.log("!! no audio will be captured — video path is unaffected")
            stateLock.lock(); running = false; stateLock.unlock()
        }
    }

    /// AVAudioSession errors surface as OSStatus four-char codes that are far
    /// more diagnostic than `localizedDescription`. `'!act'` (561017449) is
    /// "session activation failed" and usually means the app was not active, or
    /// the category/mode/option combination was rejected.
    private static func describe(_ error: Error) -> String {
        let ns = error as NSError
        let code = ns.code
        var fourCC = ""
        if code > 0 {
            let value = UInt32(truncatingIfNeeded: code)
            for shift in stride(from: 24, through: 0, by: -8) {
                let byte = UInt8(truncatingIfNeeded: value >> UInt32(shift))
                fourCC.append(byte >= 32 && byte < 127 ? Character(UnicodeScalar(byte)) : "?")
            }
        }
        return "\(ns.localizedDescription) [code \(code)\(fourCC.isEmpty ? "" : " '\(fourCC)'")]"
    }

    /// Step 1 — category/mode/options.
    ///
    /// `.playAndRecord` because Phase 3 may play the answer on this device. If
    /// you take §11.5 option 1 (laptop speakers, recommended) `.record` is
    /// enough and removes the duplex problem entirely.
    private func configureSession() async throws {
        let session = AVAudioSession.sharedInstance()

        // Deprecation accepted deliberately (§7.6): `.allowBluetooth` is renamed
        // `.allowBluetoothHFP` on the newest SDKs. Use the new spelling where the
        // toolchain offers it.
        var bluetooth: AVAudioSession.CategoryOptions = []
        #if compiler(>=6.2)
        if #available(iOS 26.0, *) {
            bluetooth.insert(.allowBluetoothHFP)
        } else {
            bluetooth.insert(.allowBluetooth)
        }
        #else
        bluetooth.insert(.allowBluetooth)
        #endif

        // Tiered, most-preferred first. §7.6's exact combination is tier 1, but
        // the OS rejects some option sets outright — `.allowBluetoothA2DP` is
        // contradictory under `.voiceChat`, which forces the HFP route — and a
        // rejected combination surfaces as the same opaque `'!act'` activation
        // failure as a genuinely busy session. Falling through the tiers turns
        // "no audio at all" into "audio, possibly without one nicety", which is
        // the same degrade-don't-die rule the rest of the pipeline follows.
        //
        // Every tier keeps the Bluetooth option, because that is the one that
        // actually matters for the glasses mic.
        let tiers: [(String, AVAudioSession.Category, AVAudioSession.Mode, AVAudioSession.CategoryOptions)] = [
            ("spec: playAndRecord/voiceChat/+a2dp+speaker", .playAndRecord, .voiceChat, bluetooth.union([.allowBluetoothA2DP, .defaultToSpeaker])),
            ("playAndRecord/voiceChat/+speaker",            .playAndRecord, .voiceChat, bluetooth.union([.defaultToSpeaker])),
            ("playAndRecord/voiceChat",                     .playAndRecord, .voiceChat, bluetooth),
            ("playAndRecord/default/+speaker",              .playAndRecord, .default,   bluetooth.union([.defaultToSpeaker])),
            ("record/default",                              .record,        .default,   bluetooth),
        ]

        var lastError: Error?
        for (name, category, mode, options) in tiers {
            do {
                try session.setCategory(category, mode: mode, options: options)
                try await activate(session)
                BridgeState.log("audio session active — \(name)")
                return
            } catch {
                lastError = error
                BridgeState.log("audio session tier rejected [\(name)]: \(Self.describe(error))")
            }
        }
        throw lastError ?? AudioSessionError.allTiersFailed
    }

    /// Retries activation briefly. `setActive(true)` fails with `'!act'` if the
    /// app is not yet foreground-active — and Start can race a camera or
    /// microphone permission alert, which is exactly that situation.
    private func activate(_ session: AVAudioSession) async throws {
        var lastError: Error?
        for attempt in 1...3 {
            do {
                try session.setActive(true)
                return
            } catch {
                lastError = error
                try? await Task.sleep(for: .milliseconds(250 * attempt))
            }
        }
        throw lastError ?? AudioSessionError.allTiersFailed
    }

    enum AudioSessionError: LocalizedError {
        case allTiersFailed
        var errorDescription: String? { "no AVAudioSession configuration was accepted" }
    }

    /// Step 2 — prefer a Bluetooth HFP port; fall back to the built-in mic and
    /// say so loudly.
    ///
    /// TODO(glasses): §11.3 — whether the Ray-Ban Meta glasses expose their mic
    /// as a `.bluetoothHFP` port to a 3P app *while a DAT camera session is
    /// streaming* is the highest-risk unknown in this build. Dump this log line
    /// before, during, and after starting the stream; that single line resolves it.
    /// Measured 2026-11-14 on Ray-Ban Display: while the DAT camera stream runs, the glasses'
    /// HFP mic delivers all-zero samples (the camera and HFP share one Bluetooth radio). So the
    /// iPhone mic is the default; Diagnostics > "Use iPhone microphone" off tries the glasses mic.
    static var usePhoneMic: Bool { UserDefaults.standard.object(forKey: "usePhoneMic") as? Bool ?? true }

    @discardableResult
    private func selectPreferredInput() -> AVAudioSessionPortDescription? {
        let session = AVAudioSession.sharedInstance()
        let inputs = session.availableInputs ?? []

        let hfp = inputs.first { $0.portType == .bluetoothHFP }
        let builtIn = inputs.first { $0.portType == .builtInMic }
        let chosen = Self.usePhoneMic ? (builtIn ?? hfp) : (hfp ?? builtIn)

        guard let chosen else {
            BridgeState.log("!! no audio input ports at all")
            return nil
        }

        // Log only when the selection actually changes. This runs again on every
        // route change, and an unconditional log buries the rest of the error
        // log in identical lines — which is exactly how a route-change loop hid
        // itself during hardware testing.
        let changed = lastLoggedPortUID != chosen.uid
        lastLoggedPortUID = chosen.uid

        if changed {
            BridgeState.log("availableInputs: " + inputs.map {
                "\($0.portName)[\($0.portType.rawValue)]"
            }.joined(separator: ", "))

            if chosen.portType == .builtInMic && hfp != nil {
                BridgeState.log("using \(chosen.portName): glasses mic goes silent while the camera streams")
            } else if hfp == nil {
                // The audio analogue of Android's "degrade to video-only" rule.
                // Demo-quality degradation, not a functional one (§11.3).
                // Expected in mock mode: with no glasses connected as an audio
                // device there is no HFP port to find.
                BridgeState.log("no Bluetooth HFP port — using \(chosen.portName)")
            } else {
                BridgeState.log("*** HFP PORT FOUND: \(chosen.portName) — glasses mic is live (§11.3)")
            }
        }
        do {
            try session.setPreferredInput(chosen)
        } catch {
            BridgeState.logError("setPreferredInput", error)
        }
        BridgeState.setAudioInputPortName(chosen.portName)
        return chosen
    }

    /// Wait until the route actually carries `port` before anyone reads a format.
    ///
    /// `setPreferredInput` is a request, not a commit. The switch lands
    /// asynchronously, and `AVAudioEngine.inputNode.inputFormat(forBus:)` keeps
    /// reporting the OLD route until it does. Reading it too early builds the
    /// converter — and installs the tap — against the built-in mic's 48 kHz while
    /// the glasses are in the middle of becoming the 8/16 kHz HFP input. Nothing
    /// throws. The tap just delivers buffers the graph no longer describes, which
    /// is precisely how you get WAVs of exactly the right length full of
    /// near-zero samples (§11.4).
    ///
    /// Nothing downstream would have caught it either: only
    /// `.oldDeviceUnavailable` / `.newDeviceAvailable` trigger a restart, and
    /// `setPreferredInput` raises neither, so the stale format would persist for
    /// the whole session.
    private func awaitRouteSettled(expecting port: AVAudioSessionPortDescription) async {
        let session = AVAudioSession.sharedInstance()
        let deadline = CACurrentMediaTime() + Self.routeSettleTimeout

        while CACurrentMediaTime() < deadline {
            if session.currentRoute.inputs.contains(where: { $0.uid == port.uid }) { break }
            try? await Task.sleep(for: .milliseconds(50))
        }

        let actual = session.currentRoute.inputs
            .map { "\($0.portName)[\($0.portType.rawValue)]" }
            .joined(separator: ", ")
        let settled = session.currentRoute.inputs.contains { $0.uid == port.uid }
        BridgeState.log("route \(settled ? "settled" : "did NOT settle") on \(port.portName) — "
                        + "currentRoute input: \(actual.isEmpty ? "none" : actual)")
    }

    /// Steps 3–6 — read the LIVE format, build a converter to the canonical
    /// target, tap, start.
    private func installTapAndStart() throws {
        let input = engine.inputNode

        // Step 3 — DO NOT assume 16 kHz. HFP negotiates 8 kHz (CVSD) or 16 kHz
        // (mSBC), and the session may hand you 48 kHz float regardless (§7.6).
        let inputFormat = input.inputFormat(forBus: 0)
        // Format and route logged together on purpose: 48 kHz alongside a
        // bluetoothHFP route means the format was read before the switch landed,
        // which is the stale-converter failure this method used to walk into.
        let routeNow = AVAudioSession.sharedInstance().currentRoute.inputs
            .map { $0.portType.rawValue }
            .joined(separator: ",")
        BridgeState.log(String(
            format: "input format: %.0f Hz, %u ch  (route: %@)",
            inputFormat.sampleRate, inputFormat.channelCount,
            routeNow.isEmpty ? "none" : routeNow
        ))
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            BridgeState.log("!! input format is degenerate — is the session active?")
            return
        }

        // Step 4 — converter to 16 kHz mono Int16 interleaved.
        guard let target = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: Self.targetSampleRate,
            channels: Self.targetChannels,
            interleaved: true
        ) else {
            BridgeState.log("!! could not build target audio format")
            return
        }
        targetFormat = target

        guard let converter = AVAudioConverter(from: inputFormat, to: target) else {
            BridgeState.log("!! could not build AVAudioConverter from the live input format")
            return
        }
        converter.sampleRateConverterQuality = AVAudioQuality.high.rawValue
        self.converter = converter

        // Step 5 — tap. Convert, append, return. No awaits on this thread (§11.7).
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: Self.tapBufferSize, format: inputFormat) {
            [weak self] buffer, _ in
            guard let self else { return }
            guard let data = self.convert(buffer) else { return }
            self.onPCM(data)
        }

        // Step 6
        engine.prepare()
        try engine.start()
    }

    // MARK: - Conversion

    private func convert(_ buffer: AVAudioPCMBuffer) -> Data? {
        guard let converter, let target = targetFormat else { return nil }

        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else {
            return nil
        }

        var consumed = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, statusPtr in
            if consumed {
                statusPtr.pointee = .noDataNow
                return nil
            }
            consumed = true
            statusPtr.pointee = .haveData
            return buffer
        }

        if let error {
            BridgeState.logError("audio convert", error)
            return nil
        }
        guard status != .error, out.frameLength > 0 else { return nil }
        guard let channelData = out.int16ChannelData else { return nil }

        let byteCount = Int(out.frameLength) * MemoryLayout<Int16>.size * Int(target.channelCount)
        return Data(bytes: channelData[0], count: byteCount)
    }

    // MARK: - Route / interruption observers
    //
    // Mandatory, not optional (§7.6, §11.4). Any route change can move the input
    // away from the HFP port with no error: the tap keeps firing with silence,
    // the chunker keeps emitting WAVs, and the server keeps transcribing nothing.

    private func installNotificationObservers() {
        let center = NotificationCenter.default

        observers.append(center.addObserver(
            forName: AVAudioSession.routeChangeNotification, object: nil, queue: nil
        ) { [weak self] note in
            guard let self else { return }
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt ?? 0
            let reason = AVAudioSession.RouteChangeReason(rawValue: raw) ?? .unknown
            BridgeState.log("route change: \(reason)")
            switch reason {
            case .oldDeviceUnavailable, .newDeviceAvailable:
                // ONLY these two, exactly as §7.6 specifies.
                //
                // `.categoryChange` and `.override` must NOT trigger a restart:
                // the restart calls setPreferredInput(), which itself provokes a
                // route change, which fires this notification again — a
                // self-sustaining loop that tears down and rebuilds the audio
                // engine continuously. In mock mode that also interrupts the
                // AVCaptureSession feeding the simulated camera, so video frames
                // stop arriving while `stream state` still reads `.streaming`.
                self.restartForRouteChange()
            default:
                break
            }
        })

        observers.append(center.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: nil
        ) { [weak self] note in
            guard let self else { return }
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt ?? 0
            guard let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
            switch type {
            case .began:
                BridgeState.log("audio interrupted (call or Siri)")
            case .ended:
                let optionsRaw = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
                let options = AVAudioSession.InterruptionOptions(rawValue: optionsRaw)
                if options.contains(.shouldResume) {
                    BridgeState.log("audio interruption ended — resuming")
                    self.restartForRouteChange()
                }
            @unknown default:
                break
            }
        })
    }

    /// Re-assert the preferred input and restart the engine. Without this the
    /// mic goes dead the moment the route flips and never comes back (§7.6).
    private func restartForRouteChange() {
        guard isRunning else { return }

        // Belt and braces on top of narrowing the trigger: never let two
        // restarts overlap, and never restart more than once a second. A
        // restart provokes route changes of its own, so without this an
        // unlucky burst still becomes a loop.
        stateLock.lock()
        let now = CACurrentMediaTime()
        if restarting || now - lastRestartAt < 1.0 {
            stateLock.unlock()
            return
        }
        restarting = true
        lastRestartAt = now
        stateLock.unlock()

        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        Task { [weak self] in
            guard let self else { return }
            // Cleared only after the async work finishes — a `defer` here would
            // fire the instant this Task is launched, not when it completes,
            // which would leave the guard doing nothing.
            defer {
                self.stateLock.lock(); self.restarting = false; self.stateLock.unlock()
            }
            do {
                try await self.activate(AVAudioSession.sharedInstance())
                // Same race as the cold path: the restart is what re-selects the
                // port, so it must also wait for the route before re-tapping.
                let chosen = self.selectPreferredInput()
                if let chosen { await self.awaitRouteSettled(expecting: chosen) }
                try self.installTapAndStart()
                BridgeState.log("audio engine restarted after route change")
            } catch {
                BridgeState.log("!! audio restart failed: \(Self.describe(error))")
            }
        }
    }

    // MARK: - Stop
    //
    // Order matters: remove the tap -> stop the engine -> deactivate. Deactivating
    // before removing the tap can wedge the engine, exactly as tearing down SCO
    // before the recorder does on Android (§7.6).

    func stop() {
        stateLock.lock()
        if !running { stateLock.unlock(); return }
        running = false
        stateLock.unlock()

        engine.inputNode.removeTap(onBus: 0)
        engine.stop()

        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        observers.removeAll()

        do {
            try AVAudioSession.sharedInstance()
                .setActive(false, options: .notifyOthersOnDeactivation)
        } catch {
            BridgeState.logError("audio session deactivate", error)
        }

        converter = nil
        targetFormat = nil
        BridgeState.setAudioInputPortName("—")
        BridgeState.log("audio recorder stopped")
    }
}
