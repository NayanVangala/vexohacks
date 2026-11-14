//
//  MockDeviceController.swift
//  VexoBridge
//
//  MockDeviceKit wrapper. CLAUDE-ios.md §7.8, API shapes from §8.3.
//
//  This is the demo insurance: if the glasses fail at the venue, MockDeviceKit
//  driving the phone's own back camera runs the whole pipeline end to end.
//  Rehearse at least one full run in mock mode.
//
//  The entire file is gated so no mock code reaches a release build.
//

#if DEBUG

import Foundation
import MWDATMockDevice

@MainActor
final class MockDeviceController {

    enum Feed: String, CaseIterable, Identifiable {
        case backCamera = "Phone back camera"
        case file       = "Bundled HEVC file"
        var id: String { rawValue }
    }

    private(set) var enabled = false
    // SDK 0.7.0 spelling. 0.8.0 renames this protocol to `MockGlasses` and
    // replaces `pairRaybanMeta()` with a throwing `pairGlasses(model:)`.
    private(set) var device: (any MockRaybanMeta)?

    /// Bundled fallback feed for the Simulator, where there is no usable camera
    /// (§11.11). Must be H.265/HEVC.
    /// TODO(assets): drop a short HEVC clip named `mock_feed.mov` into the app
    /// bundle to make Simulator runs work; the back-camera feed needs a device.
    ///
    /// `-mockFeedPath /abs/path.mov` overrides the bundle lookup, so an
    /// unattended Simulator run needs no asset in the app (Simulator apps can
    /// read host paths).
    var fileFeedURL: URL? {
        if let path = UserDefaults.standard.string(forKey: "mockFeedPath") {
            return URL(fileURLWithPath: path)
        }
        return Bundle.main.url(forResource: "mock_feed", withExtension: "mov")
    }

    /// The Simulator has no usable camera (§11.11), so default to the file feed there.
    nonisolated static var defaultFeed: Feed {
        #if targetEnvironment(simulator)
        return .file
        #else
        return .backCamera
        #endif
    }

    // MARK: - Enable
    //
    // The physical lifecycle must be simulated IN ORDER. The device is not
    // "wearable-ready" until worn, and streams will not start otherwise.

    func enable(feed: Feed = MockDeviceController.defaultFeed) async {
        guard !enabled else { return }

        // 1 — initiallyRegistered/initialPermissionsGranted skip the Meta AI
        //     registration handoff entirely. This is what makes mock mode
        //     work with no hardware and no Developer Center account.
        MockDeviceKit.shared.enable(
            config: MockDeviceKitConfig(
                initiallyRegistered: true,
                initialPermissionsGranted: true
            )
        )

        // 2 — `pairRaybanMeta()` does not throw under 0.7.0, so there is no
        //     do/catch here: Swift warns on an unreachable catch block. 0.8.0's
        //     `pairGlasses(model:)` does throw — restore both together.
        let device = MockDeviceKit.shared.pairRaybanMeta()
        self.device = device

        // 3 — all three, in this order.
        device.powerOn()
        device.unfold()
        device.don()

        // 4 — feed
        await setFeed(feed)

        enabled = true
        BridgeState.log("mock device paired and worn (feed: \(feed.rawValue))")
    }

    func setFeed(_ feed: Feed) async {
        guard let device else { return }
        let camera = device.services.camera
        switch feed {
        case .backCamera:
            // Requires NSCameraUsageDescription and a physical device.
            await camera.setCameraFeed(cameraFacing: .back)
            BridgeState.log("mock feed: phone back camera")
        case .file:
            guard let url = fileFeedURL else {
                BridgeState.log("!! mock file feed missing — add mock_feed.mov (HEVC) to the bundle")
                return
            }
            camera.setCameraFeed(fileURL: url)
            BridgeState.log("mock feed: \(url.lastPathComponent)")
        }
    }

    /// The still returned by capturePhoto. JPEG or PNG.
    func setCapturedImage(_ url: URL) {
        guard let device else { return }
        device.services.camera.setCapturedImage(fileURL: url)
        BridgeState.log("mock captured image: \(url.lastPathComponent)")
    }

    // MARK: - Captouch
    //
    // iOS-only capability with no Android equivalent, and the only way to
    // exercise a tap-driven activation path before hardware arrives. Note §14.3:
    // this simulates the gesture, it does NOT prove real device taps reach a 3P
    // app — SDK 0.8.0 exposes no such publisher.

    func tap() {
        device?.services.captouch.tap()
        BridgeState.log("mock captouch: tap")
    }

    func tapAndHold() {
        device?.services.captouch.tapAndHold()
        BridgeState.log("mock captouch: tapAndHold")
    }

    // MARK: - Lifecycle toggles (surfaced in MockDevicePanel)

    func doff() { device?.doff(); BridgeState.log("mock: doff") }
    func don()  { device?.don();  BridgeState.log("mock: don") }
    func fold() { device?.fold(); BridgeState.log("mock: fold") }
    func unfold() { device?.unfold(); BridgeState.log("mock: unfold") }

    // MARK: - Disable — idempotent in both directions via `enabled`.

    func disable() async {
        if let device {
            MockDeviceKit.shared.unpairDevice(device)
        }
        MockDeviceKit.shared.disable()
        device = nil
        enabled = false
        BridgeState.log("mock device disabled")
    }
}

#endif
