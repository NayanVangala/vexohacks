//
//  MockDevicePanel.swift
//  VexoBridge
//
//  Debug-only mock controls. CLAUDE-ios.md §7.8.
//
//  Mirrors the CameraAccess sample's debug menu: pair, power/unfold/don toggles,
//  feed picker, tap / tap-and-hold.
//

#if DEBUG

import SwiftUI

struct MockDevicePanel: View {

    let controller: BridgeController

    @Environment(\.dismiss) private var dismiss
    @State private var feed: MockDeviceController.Feed = .backCamera

    var body: some View {
        NavigationStack {
            Form {
                Section("Device") {
                    LabeledContent("Paired", value: controller.mock.enabled ? "yes" : "no")
                    Button("Pair + power on + unfold + don") {
                        Task { await controller.mock.enable(feed: feed) }
                    }
                    .disabled(controller.mock.enabled)

                    Button("Unpair", role: .destructive) {
                        Task { await controller.mock.disable() }
                    }
                    .disabled(!controller.mock.enabled)
                }

                Section("Wear state") {
                    HStack {
                        Button("don") { controller.mock.don() }
                        Spacer()
                        Button("doff") { controller.mock.doff() }
                        Spacer()
                        Button("unfold") { controller.mock.unfold() }
                        Spacer()
                        Button("fold") { controller.mock.fold() }
                    }
                    .buttonStyle(.bordered)
                    .disabled(!controller.mock.enabled)
                }

                Section("Camera feed") {
                    Picker("Feed", selection: $feed) {
                        ForEach(MockDeviceController.Feed.allCases) { option in
                            Text(option.rawValue).tag(option)
                        }
                    }
                    .onChange(of: feed) { _, newValue in
                        Task { await controller.mock.setFeed(newValue) }
                    }
                    Text("Use the bundled HEVC file in the Simulator — there is no usable camera there (§11.11).")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("Captouch") {
                    // Simulates the gesture. It does NOT prove that real device
                    // taps reach a 3P app — SDK 0.8.0 exposes no such publisher
                    // (§14.3).
                    Button("tap  →  activate") {
                        controller.mock.tap()
                        Task { await controller.activate() }
                    }
                    Button("tapAndHold") { controller.mock.tapAndHold() }
                }
                .disabled(!controller.mock.enabled)
            }
            .navigationTitle("MockDeviceKit")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}

#endif
