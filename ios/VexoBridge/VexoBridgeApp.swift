//
//  VexoBridgeApp.swift
//  VexoBridge
//
//  Entry point. CLAUDE-ios.md §7.2.
//
//  Lose your keys. Lose your pills. Lose your mind. We found it.
//

import SwiftUI
import MWDATCore

@main
struct VexoBridgeApp: App {

    @Environment(\.scenePhase) private var scenePhase
    @State private var controller = BridgeController()

    init() {
        // Before any other SDK call, mirroring Android's "initialize the toolkit
        // first" rule. On failure we log and keep running (§7.2) — which is only
        // survivable because WearablesRuntime.isConfigured then keeps every
        // later SDK access away from `Wearables.shared`, whose getter traps
        // rather than throws.
        //
        // Note the ordering trap this init sits inside: SwiftUI evaluates the
        // `controller` stored property BELOW before this body runs, so
        // BridgeController's own init must not touch the SDK. It doesn't —
        // registration observation starts in `onLaunch()`.
        do {
            try Wearables.configure()
            WearablesRuntime.markConfigured()
        } catch {
            BridgeState.logError("Wearables.configure", error)
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView(controller: controller)
                .task {
                    await controller.onLaunch()
                }
                // The registration callback path. Direct equivalent of Android's
                // VIEW/BROWSABLE intent filter on scheme `rokidbridge`.
                .onOpenURL { url in
                    Task { await controller.handleOpenURL(url) }
                }
                // Log every background transition: it explains any gap in the
                // frame timeline, because there is no background capture (§11.1).
                .onChange(of: scenePhase) { _, phase in
                    controller.noteScenePhaseChange(String(describing: phase))
                }
        }
    }
}
