//
//  WearablesManager.swift
//  VexoBridge
//
//  MWDAT session + stream ownership. CLAUDE-ios.md §7.4, API shapes from §8.
//
//  Nothing here touches audio — MWDAT has no audio API on either platform.
//

import Foundation
import MWDATCore
import MWDATCamera

/// One-time record of whether `Wearables.configure()` succeeded.
///
/// This matters because `Wearables.shared` **traps** — it does not throw — when
/// the SDK was never configured. §7.2 says a configure failure must be logged
/// and the app kept alive, which is only possible if nothing afterwards touches
/// the SDK. Every entry point in WearablesManager checks this first.
enum WearablesRuntime {
    nonisolated(unsafe) private(set) static var isConfigured = false

    static func markConfigured() { isConfigured = true }
}

/// Lock-protected frame tally.
///
/// The `videoFramePublisher` listener runs on the SDK's delivery thread, which
/// must not hop to the main actor and must not await (§7.5, §11.7) — so counting
/// frames there cannot use actor isolation. A plain lock is the same trade-off
/// AudioChunker already makes on the audio tap.
final class FrameCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    /// Returns the post-increment value so a caller can detect the first frame
    /// without a second lock acquisition.
    func increment() -> Int {
        lock.lock(); defer { lock.unlock() }
        count += 1
        return count
    }

    func reset() { lock.lock(); count = 0; lock.unlock() }

    var value: Int {
        lock.lock(); defer { lock.unlock() }
        return count
    }
}

/// Lock-protected last-seen frame size, on the same delivery thread and for the
/// same reason as `FrameCounter`.
///
/// Requested resolution is not delivered resolution. The SDK silently degrades
/// `.medium` to `.low` under Bluetooth pressure, and until this existed the only
/// way to find out was to pull a capture off the server and read its header.
/// Logging every change — not just the first frame — is what makes the degrade
/// visible at the moment it happens.
final class FrameSizeWatch: @unchecked Sendable {
    private let lock = NSLock()
    private var last: (Int, Int)?

    /// Returns the new size when it differs from the last one, else nil.
    func observe(width: Int, height: Int) -> (Int, Int)? {
        lock.lock(); defer { lock.unlock() }
        guard last?.0 != width || last?.1 != height else { return nil }
        last = (width, height)
        return (width, height)
    }

    func reset() { lock.lock(); last = nil; lock.unlock() }
}

@MainActor
final class WearablesManager {

    // MARK: - Constants (Appendix A)

    private static let resolution: StreamingResolution = .medium   // 504x896
    // 2, the SDK's lowest legal value (valid: 2, 7, 15, 24, 30).
    //
    // This is a resolution change disguised as a frame-rate change. Measuring
    // 1031 stored captures (2026-08-02) shows every session opens at the
    // requested 504x896 for a handful of frames and then sits at 360x640 for
    // the rest of its life — the SDK degrading `.medium` to `.low` under
    // Bluetooth pressure. Resolution and frame rate are not independent knobs:
    // they share one bandwidth budget, and the SDK's documented degradation
    // ladder spends it by dropping resolution FIRST and never taking frame rate
    // below 15. At 7 fps the only thing it can take is pixels, so it takes them.
    //
    // Nothing downstream wants 7 fps. `BridgeController.frameIntervalSeconds`
    // throttles to one uploaded frame every 2.5 s (0.4 fps), so 7 fps discarded
    // ~94% of what the link carried. 2 fps still leaves 5x headroom over what
    // the pipeline consumes and asks the radio for a third as much data.
    //
    // Frames per second was never the product; pixels per frame is — Haiku is
    // billed on frame dimensions, not on frame count.
    private static let frameRate: UInt = 2
    // Stay on `.raw`. `.hvc1` was tried as a diagnostic on 2026-07-29 and is
    // strictly worse: the device terminates the session before it ever reaches
    // `.started` ("Session ended by device", then "Device unavailable"), and the
    // glasses drop off the phone entirely — the BluetoothHFP input disappears
    // from `availableInputs` too, so audio silently falls back to the built-in
    // mic. `.raw` at least reaches `.started` and creates a stream.
    //
    // So the streaming failure is NOT codec-specific, and `.hvc1` costs the
    // audio path as well. The §11.x background-decode objection stands on top
    // of that.
    private static let videoCodec: VideoCodec = .raw
    private static let photoTimeout: Duration = .seconds(4)

    #if DEBUG
    /// Stream-recycling experiment (2026-07-30). **Off by default — it answered
    /// its question:** 0 frames across 10 generations, so stream age is not the
    /// variable (docs/MWDAT-STREAM-BUG-REPORT.md).
    ///
    /// Must stay off for any other stream test: truncating every stream at 22 s
    /// would mask one that simply needs longer to reach `.starting`, and would
    /// also suppress the 30 s error that is the baseline symptom. Flip to `true`
    /// to re-run the experiment.
    static let useStreamRecycling = false
    #endif

    /// Recycle the stream this often, comfortably inside the 30.0 s window after
    /// which the SDK self-destructs the stream with "Device is not connected"
    /// (docs/MWDAT-STREAM-BUG-REPORT.md). The question this answers: does a
    /// *fresh* stream deliver anything during its first seconds, before whatever
    /// internal timer expires? 22 s leaves 8 s of margin.
    static let streamRecycleInterval: Duration = .seconds(22)

    /// How long to wait for a device to become eligible before giving up.
    /// Pairing a mock device — or waking real glasses — is asynchronous; the SDK
    /// does not publish it to `AutoDeviceSelector` synchronously.
    private static let deviceWaitTimeout: Duration = .seconds(10)

    // MARK: - Callbacks

    /// Installed by BridgeController once at construction, then read from the
    /// SDK's frame-delivery thread. Deliberately NOT main-actor isolated: the
    /// whole point is that frames never touch the main actor before encoding
    /// (§7.5, §11.7). Written once before `start()`, so the opt-out is safe.
    nonisolated(unsafe) var onFrame: (@Sendable (VideoFrame) -> Void)?

    // MARK: - State

    /// Resolved lazily, NOT at init.
    ///
    /// `Wearables.shared` traps with "Call `configure()` before attempting to
    /// access Wearables!" if touched first. SwiftUI evaluates an `App`'s stored
    /// property initializers *before* its `init()` body runs, so a
    /// `BridgeController()` default would construct this manager — and read
    /// `Wearables.shared` — before `Wearables.configure()` ever executes. That
    /// is a launch crash, and it is why this is an @autoclosure rather than a
    /// stored value. Nothing in `init` may touch the SDK (§6.1 step 1).
    private let wearablesProvider: @Sendable () -> any WearablesInterface
    private var wearables: any WearablesInterface { wearablesProvider() }

    /// Guard for every SDK entry point. Returns nil (with a log) rather than
    /// letting `Wearables.shared` trap.
    private var sdk: (any WearablesInterface)? {
        guard WearablesRuntime.isConfigured else {
            BridgeState.log("!! SDK unavailable: Wearables.configure() did not succeed")
            return nil
        }
        return wearables
    }

    private var session: DeviceSession?
    private var stream: MWDATCamera.Stream?

    static let damEnabled: Bool =
        (Bundle.main.object(forInfoDictionaryKey: "MWDAT") as? [String: Any])?["DAMEnabled"] as? Bool ?? false

    /// Phase 0 display spike. Rides the same session as the camera stream.
    let glassesDisplay = GlassesDisplay()

    /// AnyListenerToken IS the subscription. Drop the reference and frames stop
    /// arriving with no error, no log, and a stream that reports `.streaming`
    /// the whole time (§11.6). Releasing these is how we unsubscribe; the
    /// protocol also exposes an async `cancel()`, which teardown calls.
    private var tokens: [any AnyListenerToken] = []

    /// Link-state probes, deliberately outliving the eligibility wait.
    ///
    /// `awaitEligibleDevice` gives up after `deviceWaitTimeout`, and the single
    /// snapshot `logEligibilityDiagnosis` takes at that moment cannot tell a
    /// wedged link from a merely slow one — both read `connecting`. These
    /// listeners keep logging after the wait has already failed, so a late
    /// `.connected` still shows up in the log. Released by `stop()` and by the
    /// next `attachLinkProbes()`.
    private var linkProbes: [any AnyListenerToken] = []

    /// Stream recycling (see `streamRecycleInterval`).
    private var streamRecycleTask: Task<Void, Never>?
    private var streamGeneration = 0

    /// Frames delivered by the CURRENT generation. Reset on every recycle so each
    /// window is scored independently — a single frame in any window would prove
    /// the transport can move data at all, which is the whole point.
    private let frameCounter = FrameCounter()
    private let frameSizeWatch = FrameSizeWatch()
    private var framesAllGenerations = 0

    /// Frames the SDK has handed us this generation — the most upstream video
    /// signal there is. If this stops climbing while the audio tap keeps firing,
    /// the SDK stopped delivering; if both stop, the freeze is above both.
    var framesReceived: Int { frameCounter.value }

    /// Which session generation we are on. Increments on every auto-recovery
    /// rebuild, so a heartbeat showing `gen` climbing while `rx` restarts from a
    /// low number is recovery working, not a stall.
    var generation: Int { streamGeneration }

    /// Stream state as text, for the heartbeat. `nil` stream reads as "none"
    /// rather than crashing a diagnostic.
    var streamStateText: String {
        guard let stream else { return "none" }
        return String(describing: stream.state)
    }

    private var stateTask: Task<Void, Never>?
    private var errorTask: Task<Void, Never>?

    /// One-shot photo continuation. A second PhotoData must not resume the same
    /// continuation — that is a crash, not a warning (§7.4).
    private var photoContinuation: CheckedContinuation<Data?, Never>?

    private var isStarted = false

    /// Set by `stop()` before anything else, so the session observer can tell a
    /// user-initiated teardown from a device-initiated death. Without this, our
    /// own `session.stop()` emits `.stopped` and auto-recovery would immediately
    /// rebuild the session the user just asked us to shut down.
    private var isTearingDown = false

    /// Session auto-recovery. Measured 2026-07-31: the device ends a working
    /// session on its own ("Session ended by device") after anywhere from 37 s to
    /// ~11 min, with the Bluetooth link still connected and the app still
    /// foreground. `.stopped` is terminal per the SDK — the only remedy is a NEW
    /// session — and previously nothing rebuilt it, so a run went audio-only for
    /// 20 minutes without noticing.
    private var recoveryTask: Task<Void, Never>?
    private var recoveryAttempt = 0

    /// Capped backoff. Early attempts are fast because the common causes clear in
    /// seconds (a transient companion-app drop); the ceiling stops a genuinely
    /// wedged device — folded glasses, app removed in Meta AI — from being
    /// hammered for the rest of the run.
    static func recoveryDelay(forAttempt attempt: Int) -> Duration {
        switch attempt {
        case 1:  return .seconds(2)
        case 2:  return .seconds(5)
        case 3:  return .seconds(10)
        case 4:  return .seconds(20)
        default: return .seconds(30)
        }
    }

    /// Guards for §6.1 step 5. See `requestCameraPermissionOnce()`.
    ///
    /// Two flags, not one. `inFlight` prevents the overlapping request that
    /// throws `PermissionError.requestInProgress`; `granted` is the only thing
    /// permitted to stop retrying for good. A single latch conflated them, and
    /// the conflation is a real bug: the first `.registered` fires while the
    /// glasses link is still `connecting`, so the request throws "No wearable
    /// devices have been discovered or registered" — and because the latch was
    /// already burned, no later attempt ever ran. The registration stream
    /// re-emits `.registered` on every foreground precisely so it can be retried.
    private var cameraPermissionInFlight = false
    private var cameraPermissionGranted = false

    init(wearables: @escaping @autoclosure @Sendable () -> any WearablesInterface = Wearables.shared) {
        self.wearablesProvider = wearables
    }

    // MARK: - Registration & permission (§6.1 steps 4–5)

    /// Must be called AFTER `Wearables.configure()`, never from an initializer.
    func observeRegistration() {
        guard let wearables = sdk else { return }
        Task {
            for await state in wearables.registrationStateStream() {
                await MainActor.run {
                    BridgeState.shared.registrationState = state
                }
                switch state {
                case .unavailable:
                    // Meta AI is missing or Developer Mode is off. Surface the
                    // text; do not just disable a button (§6.2).
                    BridgeState.log("registration UNAVAILABLE — is Meta AI installed and Developer Mode on?")
                case .available:
                    BridgeState.log("registration: available — tap Connect to register with Meta AI")
                case .registering:
                    BridgeState.log("registration: registering — approve Scene in Meta AI")
                case .registered:
                    BridgeState.log("registration: registered")
                    requestCameraPermissionOnce()
                @unknown default:
                    break
                }
            }
        }
    }

    /// §6.1 step 5: once registered, camera permission follows automatically.
    ///
    /// Fired from the registration stream but deliberately NOT awaited there —
    /// `requestPermission` bounces through Meta AI and only returns after the
    /// user acts, and awaiting it inside `for await` would stall the stream, so
    /// the UI would sit on a stale registration state the whole time.
    ///
    /// Guarded because the stream re-emits `.registered` on every foreground,
    /// and a second request while one is in flight throws
    /// `PermissionError.requestInProgress`.
    private func requestCameraPermissionOnce() {
        guard !cameraPermissionGranted, !cameraPermissionInFlight else { return }
        cameraPermissionInFlight = true
        // The class is @MainActor, so this Task inherits that isolation and the
        // flag reset below needs no hop.
        Task {
            await ensureCameraPermission()
            cameraPermissionInFlight = false
        }
    }

    func startRegistration() async {
        guard let wearables = sdk else { return }
        do {
            try await wearables.startRegistration()
        } catch {
            BridgeState.logError("startRegistration", error)
        }
    }

    /// Check, then request only if needed. Both bounce through the Meta AI app
    /// and return via .onOpenURL -> handleUrl (§6.1).
    func ensureCameraPermission() async {
        guard let wearables = sdk else { return }
        do {
            var status = try await wearables.checkPermissionStatus(.camera)
            BridgeState.log("camera permission: \(status)")
            if status != .granted {
                status = try await wearables.requestPermission(.camera)
                BridgeState.log("camera permission after request: \(status)")
            }
            cameraPermissionGranted = (status == .granted)
            let text = String(describing: status)
            await MainActor.run { BridgeState.shared.cameraPermissionText = text }
        } catch {
            BridgeState.logError("camera permission", error)
            // Previously this path left `cameraPermissionText` reading "unknown"
            // forever, which is indistinguishable from "never asked" in the
            // header. Say that it failed and that a retry is coming.
            await MainActor.run { BridgeState.shared.cameraPermissionText = "failed — will retry" }
            // "No wearable devices have been discovered or registered" is about
            // the device list, not about permission — app-level registration can
            // be `.registered` while Meta AI still has no live link to any
            // glasses. Only `logEligibilityDiagnosis` can say which of the two
            // it is, and until now it ran solely from `start()`, so this path —
            // the one §6.1 step 5 actually reaches first — reported the symptom
            // and threw the cause away.
            logEligibilityDiagnosis()
        }
    }

    // MARK: - Start
    //
    // This ordering is load-bearing. The SDK's own sample gets burned by races
    // otherwise (§7.4, §11.8).

    func start() async {
        guard !isStarted else { return }
        guard sdk != nil else { return }
        isStarted = true
        isTearingDown = false
        recoveryAttempt = 0
        streamGeneration = 0
        framesAllGenerations = 0
        frameCounter.reset()
        frameSizeWatch.reset()
        BridgeState.setConnection(.connecting)

        if await buildSessionAndStream() { return }

        BridgeState.setConnection(.disconnected)
        isStarted = false
    }

    /// Steps 2-8: selector, eligibility wait, session, listeners, stream.
    ///
    /// Extracted from `start()` so auto-recovery rebuilds through the *same*
    /// sequence rather than a parallel copy that can drift. Returns false on any
    /// failure and deliberately does NOT touch `isStarted` or the connection
    /// indicator — the caller owns those, because "failed to start" and "failed to
    /// recover, will retry" need different handling.
    private func buildSessionAndStream() async -> Bool {
        guard let wearables = sdk else { return false }

        do {
            // 2 — selector
            let selector = AutoDeviceSelector(wearables: wearables)

            // 2a — WAIT for a device to actually be eligible.
            //
            // `createSession` throws `.noEligibleDevice` if no device is visible
            // *at that instant*. Pairing is asynchronous on both paths: a mock
            // device does not appear to the selector the moment `pairGlasses()`
            // returns, and real glasses need their Bluetooth link up. Without
            // this wait, Start fails immediately with `.noEligibleDevice` even
            // though the device shows up milliseconds later — which is exactly
            // what happens when MockDeviceKit is enabled in the same tap.
            //
            // The SDK's own guidance is to monitor availability before starting
            // work, rather than infer it.
            guard await awaitEligibleDevice(selector) else {
                // The reason itself is logged by awaitEligibleDevice's
                // diagnosis, which can see state the selector never exposes.
                BridgeState.log("!! no eligible device after \(Self.deviceWaitTimeout)")
                return false
            }

            // 2b — the link is provably up only here, so this is the first moment
            // a camera-permission request can succeed. §6.1 step 5 fires it off
            // the registration stream instead, which is too early: at that point
            // the link is still `connecting` and the request throws "All
            // discovered devices are powered off or disconnected". Registration
            // does not re-emit when the *link* changes, so nothing retried it.
            //
            // Deliberately not awaited — `requestPermission` bounces through
            // Meta AI and only returns once the user acts, and blocking the
            // stream setup on a human would strand the session (§6.1 step 5).
            // The grant may therefore land mid-session; Start again to use it.
            requestCameraPermissionOnce()

            // 3 — session
            let session = try wearables.createSession(deviceSelector: selector)
            self.session = session

            // 4 — grab the streams BEFORE start(). stateStream() does not buffer
            //     past events; subscribing after start() can miss `.started`.
            let stateStream = session.stateStream()
            let errorStream = session.errorStream()
            observe(stateStream: stateStream)
            observe(errorStream: errorStream)

            // 5 — start
            try session.start()

            // 6 — check `state` FIRST, then await. A session can reach `.started`
            //     before a `for await` begins iterating, and you deadlock waiting
            //     for an event that already fired (§11.8).
            if session.state != .started {
                var reachedStarted = false
                for await state in session.stateStream() {
                    if state == .started { reachedStarted = true; break }
                    if state == .stopped {
                        BridgeState.log("session stopped before reaching .started")
                        break
                    }
                }
                guard reachedStarted || session.state == .started else {
                    return false
                }
            }

            // 7 — attach the stream capability. Returns nil if the session is
            //     not `.started`.
            let config = StreamConfiguration(
                videoCodec: Self.videoCodec,
                resolution: Self.resolution,
                frameRate: Self.frameRate
            )
            guard let stream = try session.addStream(config: config) else {
                BridgeState.log("addStream returned nil — session was not .started")
                return false
            }
            self.stream = stream

            // 8 — listeners first, store every token, THEN start.
            //
            // Accumulate before resetting: a recovery rebuild is a new generation,
            // not a new run, and zeroing here would make the final tally under-
            // report every frame delivered before the last session death.
            framesAllGenerations += frameCounter.value
            frameCounter.reset()
            // Each generation renegotiates bandwidth, so the delivered size is
            // worth re-logging even when it lands on the same value as before.
            frameSizeWatch.reset()
            streamGeneration += 1

            attachListeners(to: stream)
            // `await`: Stream.start()/stop() are async in SDK 0.7.0, synchronous
            // in 0.8.0. Same at every call site below.
            await stream.start()

            BridgeState.log("stream started: \(Self.videoCodec) \(Self.resolution) @\(Self.frameRate)fps")

            // 9 — display, AFTER the stream is up, so a Display failure can only
            //     cost us the card, never the camera.
            // Only with DAM on: Display requires it, and DAM is the one change that
            // could affect the camera. Flip MWDAT.DAMEnabled in Info.plist to test
            // camera-only vs camera+display on real glasses.
            // DAM (the Device Access Toolkit app model) is on because Ray-Ban Display sessions may
            // require it, but the phone-driven display stays off: the glasses' own Scene web app
            // (src/glasses) draws the cards. Launch with -sceneDATDisplay YES to attach it anyway.
            if Self.damEnabled && UserDefaults.standard.bool(forKey: "sceneDATDisplay") {
                await glassesDisplay.attach(to: session)
            } else {
                BridgeState.log("DISPLAY skipped: cards come from the glasses web app (DAM \(Self.damEnabled ? "on" : "off"))")
            }

            #if DEBUG
            // Recreate before the 30 s self-destruct, so no single stream ever
            // reaches it. See `useStreamRecycling` — off by default.
            if Self.useStreamRecycling {
                startStreamRecycling()
                BridgeState.log("stream recycling every \(Self.streamRecycleInterval)")
            }
            #endif
            return true
        } catch {
            // No connection/isStarted mutation here — the caller distinguishes
            // "failed to start" from "failed to recover, will retry".
            BridgeState.logError("wearables build", error)
            return false
        }
    }

    /// True once the selector has an active device, false on timeout.
    ///
    /// Checks `activeDevice` BEFORE awaiting the stream — same trap as
    /// `stateStream()` in §11.8: the stream does not replay past values, so a
    /// device that became active before iteration begins would never be seen.
    private func awaitEligibleDevice(_ selector: AutoDeviceSelector) async -> Bool {
        if selector.activeDevice != nil { return true }

        BridgeState.log("waiting for an eligible device…")
        attachLinkProbes()

        let eligible = await withTaskGroup(of: Bool.self) { group in
            group.addTask {
                for await device in selector.activeDeviceStream() where device != nil {
                    return true
                }
                return false
            }
            group.addTask {
                try? await Task.sleep(for: Self.deviceWaitTimeout)
                return false
            }
            let result = await group.next() ?? false
            group.cancelAll()
            return result
        }

        if eligible {
            // Probes deliberately NOT cancelled here any more. The stream dies
            // with "Device is not connected: <identifier>" about 30 s in, and only
            // a live link listener can distinguish "the link dropped at that
            // instant" from "the link held and the camera transport failed on its
            // own". `stop()` releases them.
            //
            // The identifier is logged raw so it can be compared against the one
            // in that error: `nameOrId()` returns the NAME when there is one, so a
            // stream bound to a different device than the selector picked would
            // look identical in the log until now.
            BridgeState.log("eligible — selector activeDevice: \(selector.activeDevice ?? "nil")")
        } else {
            logEligibilityDiagnosis()
            // Left attached on purpose — see `linkProbes`. Anything logged from
            // here on arrived AFTER this Start already gave up.
            BridgeState.log("   probes still attached past the timeout — a later "
                            + "'link: connected' means slow, not stuck. Tap Start again if one appears.")
        }
        return eligible
    }

    /// Why `awaitEligibleDevice` gave up.
    ///
    /// `AutoDeviceSelector` exposes exactly one fact — `activeDevice:
    /// DeviceIdentifier?` — so a timeout on its own cannot distinguish "the SDK
    /// cannot see any device" from "it sees one and rejects it". These checks
    /// can, using state the selector does not surface: registration (§6.2), the
    /// device list, and per-device `linkState` / `compatibility()`.
    private func logEligibilityDiagnosis() {
        guard let wearables = sdk else { return }

        // 1 — registration. An unregistered app is shown no devices at all, so
        //     this must be ruled out before anything else means anything.
        let registration = wearables.registrationState
        guard registration == .registered else {
            BridgeState.log("!! cause: registration is '\(registration.label)', not 'registered'")
            switch registration {
            case .available:
                BridgeState.log("   fix: tap Connect, then approve Scene in the Meta AI app (§6.1 step 4)")
            case .unavailable:
                BridgeState.log("   fix: install Meta AI, or turn Developer Mode on for these glasses (§6.2)")
            case .registering:
                BridgeState.log("   fix: registration is still in flight — finish it in Meta AI, then Start again")
            case .registered:
                break
            @unknown default:
                break
            }
            return
        }

        // 2 — registered, but is anything visible?
        let identifiers = wearables.devices
        guard !identifiers.isEmpty else {
            BridgeState.log("!! cause: registered, but Meta AI reports zero linked devices")
            BridgeState.log("   fix: are the glasses awake, unfolded, and linked in Meta AI?")
            return
        }

        // 3 — visible but rejected. Compatibility and link state are the two
        //     reasons the SDK will not hand a device to a session.
        BridgeState.log("registered; \(identifiers.count) device(s) visible to the SDK:")
        for identifier in identifiers {
            guard let device = wearables.deviceForIdentifier(identifier) else {
                BridgeState.log("   ? \(identifier) — no Device object behind this identifier")
                continue
            }

            let compatibility = device.compatibility()
            BridgeState.log("   • \(device.nameOrId()) [\(device.deviceType().rawValue)] "
                            + "link: \(device.linkState)  compat: \(compatibility)")

            switch compatibility {
            case .deviceUpdateRequired:
                BridgeState.log("   fix: glasses firmware is older than MWDAT 0.8.0 needs — update in Meta AI")
            case .sdkUpdateRequired:
                BridgeState.log("   fix: firmware is newer than MWDAT 0.8.0 supports — bump the SDK")
            case .undefined:
                BridgeState.log("   note: compatibility undefined — usually means the link is not up yet")
            case .compatible:
                break
            @unknown default:
                break
            }

            if device.linkState != .connected {
                BridgeState.log("   fix: link is \(device.linkState), not connected. Bluetooth audio pairing to "
                                + "the phone is a different link and does not count.")
            }
        }

        BridgeState.log("   camera permission: \(BridgeState.shared.cameraPermissionText)")
    }

    // MARK: - Link probes
    //
    // `logEligibilityDiagnosis` answers "why did it fail" once. These answer the
    // question a single sample cannot: is the link moving at all? MWDATCore
    // exposes no way to drive a link — `linkState` is get-only and there is no
    // connect/reconnect call anywhere in the interface — so observing the
    // transitions is the entire available surface.

    /// Log every link-state and compatibility transition for each visible device.
    ///
    /// The token IS the subscription (§11.6): these must be retained or the
    /// callbacks stop with no error and no log. Listeners fire on SDK threads, so
    /// the bodies go through the nonisolated `BridgeState` façade instead of
    /// touching main-actor state (§7.1, §11.7).
    private func attachLinkProbes() {
        guard let wearables = sdk else { return }
        cancelLinkProbes()

        // Monotonic: a wall-clock adjustment mid-handshake cannot make elapsed
        // time run backwards. `Instant` is Sendable, so the closures may capture it.
        let start = ContinuousClock.now

        for identifier in wearables.devices {
            guard let device = wearables.deviceForIdentifier(identifier) else { continue }

            // Resolved to a String up front. `nameOrId()` is a call into an
            // SDK-owned Device, and these closures do not run on the main actor.
            let name = device.nameOrId()
            BridgeState.log("probe on \(name) [id \(identifier)] — "
                            + "entry link: \(device.linkState) compat: \(device.compatibility())")

            linkProbes.append(device.addLinkStateListener { state in
                BridgeState.log("  [\(Self.elapsed(since: start))] \(name) link: \(state)")
            })
            linkProbes.append(device.addCompatibilityListener { compatibility in
                BridgeState.log("  [\(Self.elapsed(since: start))] \(name) compat: \(compatibility)")
            })
        }

        if linkProbes.isEmpty {
            BridgeState.log("probe: nothing to watch — device list is empty")
        }
    }

    /// One line per visible device: identifier, link, compatibility. Cheap enough
    /// to call from an error path, and it is the only way to tell a dropped link
    /// apart from a live link whose camera transport died.
    private func logLinkSnapshot(context: String) {
        guard let wearables = sdk else { return }
        let identifiers = wearables.devices
        guard !identifiers.isEmpty else {
            BridgeState.log("   link snapshot \(context): device list is EMPTY")
            return
        }
        for identifier in identifiers {
            guard let device = wearables.deviceForIdentifier(identifier) else {
                BridgeState.log("   link snapshot \(context): [id \(identifier)] no Device object")
                continue
            }
            BridgeState.log("   link snapshot \(context): \(device.nameOrId()) [id \(identifier)] "
                            + "link: \(device.linkState) compat: \(device.compatibility())")
        }
    }

    private func cancelLinkProbes() {
        guard !linkProbes.isEmpty else { return }
        let expired = linkProbes
        linkProbes.removeAll()
        Task { for token in expired { await token.cancel() } }
    }

    /// Seconds since `start`. `String(format:)` without a locale argument formats
    /// C-style, so this cannot pick up a decimal comma the way a locale-aware
    /// formatter would — the same rule §7.1 applies to log timestamps.
    private nonisolated static func elapsed(since start: ContinuousClock.Instant) -> String {
        let components = (ContinuousClock.now - start).components
        let seconds = Double(components.seconds) + Double(components.attoseconds) / 1e18
        return String(format: "%6.2fs", seconds)
    }

    // MARK: - Stream recycling
    //
    // Hypothesis under test: the stream's 30 s self-destruct is a fixed internal
    // timer, and a fresh stream might deliver frames during its first seconds
    // before that timer expires. Recreating every 22 s never lets any single
    // stream reach 30 s.
    //
    // There is no `removeStream()` in MWDATCamera 0.8.0 — `addStream(config:)` is
    // the only stream API on DeviceSession. Teardown is therefore `stop()` plus
    // releasing the listener tokens, which IS the unsubscribe (§11.6), then
    // dropping the reference. Whether a second `addStream` on the same session is
    // even permitted is itself unknown; a nil return is logged explicitly.

    private func startStreamRecycling() {
        streamRecycleTask?.cancel()
        streamRecycleTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: Self.streamRecycleInterval)
                guard !Task.isCancelled else { break }
                await self?.recycleStream()
            }
        }
    }

    private func stopStreamRecycling() {
        streamRecycleTask?.cancel()
        streamRecycleTask = nil
    }

    private func recycleStream() async {
        guard isStarted, let session else { return }

        let delivered = frameCounter.value
        framesAllGenerations += delivered
        BridgeState.log("recycle: generation \(streamGeneration) delivered \(delivered) frame(s) "
                        + "in \(Self.streamRecycleInterval) — total \(framesAllGenerations)")

        // Teardown, in the same order as `stop()`. async under 0.7.0.
        await stream?.stop()
        let expired = tokens
        tokens.removeAll()
        for token in expired { await token.cancel() }
        stream = nil

        // A recycle is pointless if the session itself has died — addStream would
        // just return nil. Say so rather than logging a bare nil.
        guard session.state == .started else {
            BridgeState.log("!! recycle abandoned: session state is \(session.state), not .started")
            stopStreamRecycling()
            return
        }

        let config = StreamConfiguration(
            videoCodec: Self.videoCodec,
            resolution: Self.resolution,
            frameRate: Self.frameRate
        )

        do {
            guard let fresh = try session.addStream(config: config) else {
                BridgeState.log("!! recycle: addStream returned nil on a .started session — "
                                + "the SDK may permit only one stream per session")
                stopStreamRecycling()
                return
            }
            streamGeneration += 1
            frameCounter.reset()
            frameSizeWatch.reset()
            stream = fresh
            attachListeners(to: fresh)
            await fresh.start()
            BridgeState.log("recycle: generation \(streamGeneration) started")
        } catch {
            BridgeState.logError("recycle addStream", error)
            stopStreamRecycling()
        }
    }

    // MARK: - Listeners

    private func attachListeners(to stream: MWDATCamera.Stream) {
        // Captured now, on the main actor, so the listener never reads actor state
        // from the delivery thread.
        let counter = frameCounter
        let sizeWatch = frameSizeWatch
        let generation = streamGeneration

        tokens.append(stream.videoFramePublisher.listen { [weak self] frame in
            // Stays on the SDK's background thread on purpose — JPEG encoding
            // belongs here, not on the main actor (§7.5, §11.7).
            let n = counter.increment()

            // The single most important line in the log. Everything else measures
            // failure; this one would mean the transport moved real data. Logged
            // only on the first frame of each generation — at 7-24 fps, logging
            // every frame would flush the 50-line cap in seconds.
            if n == 1 {
                BridgeState.log("*** FIRST VIDEO FRAME — generation \(generation)")
            }

            // Only fires when the size actually changes, so this costs one line
            // at stream open plus one at each degrade — safe against the 50-line
            // cap even though it is evaluated on every frame.
            if let (w, h) = ImageEncoder.dimensions(of: frame).flatMap({
                sizeWatch.observe(width: $0.0, height: $0.1)
            }) {
                BridgeState.log("frame size: \(w)x\(h) (frame \(n), generation \(generation))")
            }

            self?.onFrame?(frame)
        })

        tokens.append(stream.statePublisher.listen { state in
            // Stream state is logged only, same as Android. `.stopped` is
            // terminal for the session — release and create a new one.
            BridgeState.log("stream state: \(state)")
        })

        tokens.append(stream.errorPublisher.listen { [weak self] error in
            BridgeState.logError("stream error", error)
            // Snapshot every device's link the moment the error lands. "Device is
            // not connected" is ambiguous on its own: it reads the same whether
            // the link genuinely dropped or the camera transport failed while the
            // link was still up, and those point at completely different causes.
            Task { @MainActor in self?.logLinkSnapshot(context: "at stream error") }
        })

        tokens.append(stream.photoDataPublisher.listen { [weak self] photoData in
            Task { @MainActor in self?.deliverPhoto(photoData.data) }
        })
    }

    private func observe(stateStream: AsyncStream<DeviceSessionState>) {
        stateTask?.cancel()
        stateTask = Task { [weak self] in
            for await state in stateStream {
                BridgeState.log("session state: \(state)")
                switch state {
                case .started:
                    BridgeState.setConnection(.connected)
                case .stopping:
                    BridgeState.setConnection(.disconnected)
                case .stopped:
                    BridgeState.setConnection(.disconnected)
                    // Terminal per the SDK: the only remedy is a NEW session.
                    // No `await` needed — this Task inherits the class's
                    // @MainActor isolation, so the call is already on-actor.
                    self?.handleUnexpectedSessionStop()
                case .paused:
                    // The device owns this transition. Do NOT restart while
                    // paused — wait for .started or .stopped. Restarting during
                    // pause is how you get a session that never recovers (§7.4).
                    BridgeState.log("session paused — holding, not restarting")
                case .idle, .starting:
                    break   // leave connection untouched
                @unknown default:
                    break
                }
                _ = self
            }
        }
    }

    private func observe(errorStream: AsyncStream<DeviceSessionError>) {
        errorTask?.cancel()
        errorTask = Task {
            for await error in errorStream {
                // Thermal / battery cases get an unmistakable prefix so a
                // mid-demo shutdown has its reason on screen, not a mystery
                // disconnect (§11.9).
                BridgeState.log("!! SESSION ERROR: \(error)")
            }
        }
    }

    // MARK: - Photo capture
    //
    // The biggest API-shape difference from Android. capturePhoto(format:)
    // returns Bool — it reports only whether the request was *submitted*. The
    // image arrives later on photoDataPublisher (§7.4).

    /// - Parameter logFailures: pass `false` from the 2.5 s passive poll. Each
    ///   failure reason below is worth one log line for a user-triggered
    ///   activation, but at poll cadence they would emit 24 lines a minute and
    ///   flush the whole 50-line error log (`BridgeState.errorLogCap`). The poll
    ///   loop counts failures and logs its own throttled summary instead.
    func capturePhotoJPEG(logFailures: Bool = true) async -> Data? {
        guard let stream else {
            if logFailures { BridgeState.log("capturePhoto: no active stream") }
            return nil
        }
        guard photoContinuation == nil else {
            if logFailures { BridgeState.log("capturePhoto: a capture is already in flight, skipping") }
            return nil
        }

        // Verified against MWDATCamera 0.8.0: `@discardableResult func
        // capturePhoto(format: PhotoCaptureFormat) -> Bool`, exactly as §8.2
        // records it. The Bool is submission only; the image arrives later on
        // photoDataPublisher.
        let submitted = stream.capturePhoto(format: .jpeg)
        guard submitted else {
            if logFailures { BridgeState.log("capturePhoto: submission refused by SDK") }
            return nil
        }

        // Timeout resumes with nil — a lost capture would otherwise hang
        // activation forever (§7.4).
        let timeout = Task { [weak self] in
            try? await Task.sleep(for: Self.photoTimeout)
            guard !Task.isCancelled else { return }
            await MainActor.run {
                guard let self, self.photoContinuation != nil else { return }
                if logFailures { BridgeState.log("capturePhoto: timed out after 4s") }
                self.deliverPhoto(nil)
            }
        }
        defer { timeout.cancel() }

        return await withCheckedContinuation { (continuation: CheckedContinuation<Data?, Never>) in
            photoContinuation = continuation
        }
    }

    /// One-shot guard: a second PhotoData cannot resume the same continuation.
    private func deliverPhoto(_ data: Data?) {
        guard let continuation = photoContinuation else { return }
        photoContinuation = nil
        continuation.resume(returning: data)
    }

    // MARK: - Session auto-recovery
    //
    // Same shape as the dormant stream-recycling code: cancel, tear the artifacts
    // down, rebuild through the normal path, log each generation. The difference
    // is the trigger — the device decides, not a timer — and that recovery is
    // load-bearing rather than an experiment.

    /// Called for every `.stopped`. Filters out the ones we caused ourselves.
    private func handleUnexpectedSessionStop() {
        guard isStarted, !isTearingDown else { return }   // user pressed Stop
        guard recoveryTask == nil else { return }         // already recovering
        BridgeState.log("!! session ended by device — auto-recovery starting")
        recoveryTask = Task { [weak self] in await self?.runRecovery() }
    }

    private func runRecovery() async {
        while isStarted, !isTearingDown {
            recoveryAttempt += 1
            let delay = Self.recoveryDelay(forAttempt: recoveryAttempt)
            BridgeState.setConnection(.reconnecting)
            BridgeState.log("recovery: attempt \(recoveryAttempt) in \(delay)")

            try? await Task.sleep(for: delay)
            guard !Task.isCancelled, isStarted, !isTearingDown else { break }

            await teardownSessionArtifacts()

            if await buildSessionAndStream() {
                BridgeState.log("recovery: session rebuilt on attempt \(recoveryAttempt)")
                recoveryAttempt = 0
                break
            }
            BridgeState.log("recovery: attempt \(recoveryAttempt) failed")
        }
        // Cleared last so `handleUnexpectedSessionStop` cannot start a second
        // loop while this one is still winding down.
        recoveryTask = nil
    }

    /// Release the session and stream without touching `isStarted`.
    ///
    /// Shared by `stop()` and recovery. Observer tasks are cancelled FIRST so the
    /// `.stopped` our own `session.stop()` emits cannot re-enter
    /// `handleUnexpectedSessionStop`. Stream before session, per §7.4.
    private func teardownSessionArtifacts() async {
        stateTask?.cancel(); stateTask = nil
        errorTask?.cancel(); errorTask = nil

        await glassesDisplay.detach()  // capabilities before their session, like the stream
        await stream?.stop()
        session?.stop()

        let expired = tokens
        tokens.removeAll()
        for token in expired { await token.cancel() }

        stream = nil
        session = nil
        deliverPhoto(nil)
    }

    // MARK: - Teardown
    //
    // `DeviceSession.stop()` is synchronous, but `Stream.stop()` is **async under
    // SDK 0.7.0** (it is synchronous in 0.8.0) — which is why this method is
    // async, and why BridgeController must `await wearables.stop()`.
    //
    // Async rather than wrapping the stream stop in a detached Task: the teardown
    // order below is load-bearing (stream before session, §7.4), and a Task would
    // let `session?.stop()` run first.

    func stop() async {
        // Before the guard: a failed Start leaves `isStarted == false` and no
        // session, so the guard below would return early and strand the probes
        // still logging. An explicit Stop must always silence them.
        // FIRST, before any await: this is what tells the session observer that
        // the `.stopped` about to arrive is ours, not the device's. Set it late
        // and auto-recovery races us and rebuilds the session we are killing.
        isTearingDown = true
        recoveryTask?.cancel()
        recoveryTask = nil

        // Before the guard: a failed Start leaves `isStarted == false` and no
        // session, so the guard below would return early and strand the probes
        // still logging. An explicit Stop must always silence them.
        cancelLinkProbes()
        stopStreamRecycling()

        guard isStarted || session != nil else { return }
        isStarted = false

        // Final tally, including the generation that was live at teardown.
        let finalTotal = framesAllGenerations + frameCounter.value
        BridgeState.log("video frames delivered this run: \(finalTotal) "
                        + "across \(streamGeneration + 1) stream generation(s), "
                        + "\(recoveryAttempt) recovery attempt(s) outstanding")

        await teardownSessionArtifacts()

        BridgeState.setConnection(.disconnected)
        BridgeState.log("wearables stopped")
    }
}

// MARK: - Readable state names
//
// RegistrationState is declared `@objc(MWDATRegistrationState) @frozen enum
// RegistrationState: Int`. It exposes a `description` property but does NOT
// conform to CustomStringConvertible, so `String(describing:)` and "\(state)"
// both fall back to the @objc Int printing and produce
// `RegistrationState(rawValue: 1)` instead of a case name.
//
// The enum is @frozen, so this switch cannot go stale.

extension RegistrationState {
    var label: String {
        switch self {
        case .unavailable: return "unavailable"   // rawValue 0
        case .available:   return "available"     // rawValue 1
        case .registering: return "registering"   // rawValue 2
        case .registered:  return "registered"    // rawValue 3
        @unknown default:  return "unknown(\(rawValue))"
        }
    }
}
