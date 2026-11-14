//
//  GlassesDisplay.swift
//  VexoBridge
//
//  What the wearer sees on the Ray-Ban Display. Rides the SAME DeviceSession as
//  the camera stream (attached after the stream is up, failures swallowed, so it
//  can never take the camera down).
//
//  Every card comes from MemoryStore's feed. Buttons are pinched with the Neural
//  Band; a payment only ever moves on an Approve pinch (or a tap on the phone).
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
    private(set) var stateText = "none"

    /// Set by BridgeController. Weak: the store outlives nothing here.
    weak var store: MemoryStore?

    /// Shown when the display (re)starts, so a reconnect lands on the latest card.
    private var current: Card?

    func attach(to session: DeviceSession) async {
        do {
            let display = try session.addDisplay()
            self.display = display
            stateToken = display.statePublisher.listen { [weak self] state in
                Task { @MainActor in
                    guard let self else { return }
                    self.stateText = String(describing: state)
                    BridgeState.log("DISPLAY state: \(state)")
                    if state == .started { await self.render() }
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

    /// New card from the feed. Safe to call with no display attached: it is
    /// remembered and drawn once the display starts.
    func show(_ card: Card?) {
        current = card
        Task { await render() }
    }

    // MARK: - Rendering

    private func render() async {
        guard let display, stateText == "started" else { return }
        do {
            try await display.send(view(for: current))
        } catch {
            BridgeState.logError("DISPLAY send", error)
        }
    }

    private func view(for card: Card?) -> FlexBox {
        guard let card else { return idle() }
        switch card.kind {
        case "payment": return paymentCard(card)
        case "task":    return taskCard(card)
        default:        return answerCard(card)
        }
    }

    private func idle() -> FlexBox {
        let open = store?.tasks.count ?? 0
        return panel {
            Icon(name: .metaAi)
            Text("Scene is remembering", style: .heading)
            Text(open == 0 ? "Say \u{201C}hey scene\u{201D} to ask" : "\(open) open task\(open == 1 ? "" : "s") · say \u{201C}hey scene\u{201D}",
                 style: .body, color: .secondary)
        }
    }

    private func answerCard(_ card: Card) -> FlexBox {
        panel {
            Text(card.title, style: .meta, color: .secondary)
            Text(card.body, style: .heading)
            Button(label: "OK", style: .secondary, iconName: .checkmark, onClick: { [weak self] in
                Task { @MainActor in self?.show(nil) }
            })
        }
    }

    private func taskCard(_ card: Card) -> FlexBox {
        panel {
            Text("New task", style: .meta, color: .secondary)
            Text(card.body, style: .heading)
            FlexBox(direction: .row, spacing: 12) {
                Button(label: "Done", style: .primary, iconName: .checkmark, onClick: { [weak self] in
                    Task { @MainActor in
                        if let id = card.ref { await self?.store?.complete(id) }
                        self?.show(nil)
                    }
                })
                Button(label: "Later", style: .secondary, iconName: .clock, onClick: { [weak self] in
                    Task { @MainActor in self?.show(nil) }
                })
            }
        }
    }

    private func paymentCard(_ card: Card) -> FlexBox {
        // Re-check live status: it may have been approved or rejected on the phone.
        let status = store?.payment(card.ref)?.status ?? "proposed"
        guard status == "proposed" else {
            return panel {
                Text(status == "rejected" ? "Request dropped" : "Requested", style: .heading)
                Text(status == "rejected" ? "Nothing was sent." : "Show the QR on your phone to get paid.",
                     style: .body, color: .secondary)
                Button(label: "OK", style: .secondary, iconName: .checkmark, onClick: { [weak self] in
                    Task { @MainActor in self?.show(nil) }
                })
            }
        }
        return panel {
            Text(card.title, style: .heading)
            Text(card.body, style: .body, color: .secondary)
            FlexBox(direction: .row, spacing: 12) {
                Button(label: "Approve", style: .primary, iconName: .checkmark, onClick: { [weak self] in
                    Task { @MainActor in
                        guard let self, let id = card.ref else { return }
                        BridgeState.log("PINCH approve payment #\(id)")
                        await self.store?.approve(id)
                        await self.render()   // flips to "Requested"
                    }
                })
                Button(label: "Reject", style: .secondary, iconName: .x, onClick: { [weak self] in
                    Task { @MainActor in
                        guard let self, let id = card.ref else { return }
                        await self.store?.reject(id)
                        self.show(nil)
                    }
                })
            }
        }
    }

    private func panel(@ComponentBuilder _ content: () -> [any ViewComponent]) -> FlexBox {
        FlexBox(direction: .column, spacing: 12, content: content)
            .padding(24)
            .background(.card)
    }
}
