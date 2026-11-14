//
//  GlassesDisplay.swift
//  VexoBridge
//
//  Phase 0 display spike: attach MWDATDisplay to the SAME DeviceSession the
//  camera stream uses, show one card, count pinches. Proves three things in one
//  run: Display attaches on SDK 0.7.0 (DAM), Neural Band pinch reaches onClick,
//  and frames keep flowing (watch the HB line's rx while pinching).
//
//  No SwiftUI import here on purpose: MWDATDisplay's Text/Button/Image would
//  collide with SwiftUI's.
//

import Foundation
import MWDATCore
import MWDATDisplay

@MainActor
final class GlassesDisplay {
    private var display: Display?
    private var stateToken: (any AnyListenerToken)?
    private var pinches = 0
    private(set) var stateText = "none"

    /// Best-effort: a Display failure is logged and swallowed so it can never
    /// take the camera stream down with it.
    func attach(to session: DeviceSession) async {
        do {
            let display = try session.addDisplay()
            self.display = display
            stateToken = display.statePublisher.listen { [weak self] state in
                Task { @MainActor in
                    guard let self else { return }
                    self.stateText = String(describing: state)
                    BridgeState.log("DISPLAY state: \(state)")
                    if state == .started { await self.showCard() }
                }
            }
            await display.start()
            BridgeState.log("DISPLAY attached")
        } catch DeviceSessionError.datAppOnTheGlassesUpdateRequired {
            BridgeState.log("!! DISPLAY: DAT app on the glasses needs an update")
            try? await Wearables.shared.openDATGlassesAppUpdate()
        } catch {
            BridgeState.logError("DISPLAY attach", error)
        }
    }

    func detach() async {
        await display?.stop()
        if let stateToken { await stateToken.cancel() }
        stateToken = nil
        display = nil
        stateText = "none"
    }

    private func showCard() async {
        guard let display else { return }
        let n = pinches
        do {
            try await display.send(
                FlexBox(direction: .column, spacing: 12) {
                    Text("Vexo memory", style: .heading)
                    Text(n == 0 ? "Pinch the button" : "Pinches: \(n)", style: .body, color: .secondary)
                    Button(label: "Pinch me", style: .primary, iconName: .checkmark, onClick: { [weak self] in
                        Task { @MainActor in await self?.pinched() }
                    })
                }
                .padding(24)
                .background(.card)
            )
        } catch {
            BridgeState.logError("DISPLAY send", error)
        }
    }

    private func pinched() async {
        pinches += 1
        BridgeState.log("PINCH ✓ #\(pinches)")
        await showCard()
    }
}
