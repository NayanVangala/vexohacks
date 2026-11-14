//
//  ContentView.swift
//  VexoBridge
//
//  Control surface. CLAUDE-ios.md §7.2.
//
//  Mirrors the Android layout one control at a time: status text · last-frame
//  text · backend URL field · MockDeviceKit toggle · button
//  row (Save URL, Start/Stop, Activate) · error-log header with Clear · scrollable log.
//

import SwiftUI

import MWDATCore

struct ContentView: View {

    let controller: BridgeController

    @State private var state = BridgeState.shared
    @State private var urlText: String = ""
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            registrationRow
            backendField
            #if DEBUG
            debugToggles
            #endif
            buttonRow
            Divider()
            logHeader
            logList
        }
        .padding()
        .onAppear { urlText = controller.backend.baseURL }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Vexo Bridge")
                .font(.largeTitle.bold())
            Text(state.connection.rawValue)
                .font(.headline)
                .foregroundStyle(statusColor)

            // Auto-recovery, said out loud.
            //
            // The device ends a working session on its own (§ WearablesManager
            // — measured between 37 s and ~11 min) and the manager rebuilds it
            // with a capped backoff. Without this line RECONNECTING is a bare
            // status word, and a bare status word during a demo reads as a
            // fault. A spinner plus "resumes automatically" reads as the system
            // doing its job, which is what is actually happening.
            if state.connection == .reconnecting {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Rebuilding the session — capture resumes automatically.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            Text("Last frame: \(state.lastFrameAt ?? "—")")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            // A silent WAV is indistinguishable from a quiet room until you look
            // at the port name (§11.4).
            // `registrationText` maps through RegistrationState.label — the raw
            // enum interpolates as `RegistrationState(rawValue: 1)`.
            Text("Mic: \(state.audioInputPortName)   ·   Reg: \(state.registrationText)   ·   Cam: \(state.cameraPermissionText)")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// Red is reserved for DISCONNECTED — a state that needs a human. Recovery
    /// in progress is amber, deliberately the same amber as CONNECTING: the
    /// system is working, nobody needs to do anything, and a red indicator on a
    /// projector is the difference between "self-healing" and "broken".
    private var statusColor: Color {
        switch state.connection {
        case .connected:    return .green
        case .connecting,
             .reconnecting,
             .scanning:     return .orange
        case .disconnected: return .red
        }
    }

    // MARK: - Registration (§6.1 steps 4–5, §6.2)
    //
    // Show the Connect button only for `.available`, a spinner for
    // `.registering`, and the reason for `.unavailable` — §6.2 is explicit that
    // an unavailable state must be explained, not expressed as a dead button.
    // `.registered` shows nothing: the header already carries the state, and
    // camera permission is requested automatically from there.

    @ViewBuilder
    private var registrationRow: some View {
        switch state.registrationState {
        case .some(.available):
            VStack(alignment: .leading, spacing: 4) {
                Button("Connect glasses") {
                    Task { await controller.connectWearables() }
                }
                .buttonStyle(.borderedProminent)
                Text("Registers Vexo Bridge with Meta AI. Nothing can stream until this is done.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

        case .some(.registering):
            HStack(spacing: 8) {
                ProgressView()
                Text("Registering — approve Vexo Bridge in the Meta AI app.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

        case .some(.unavailable):
            Text("Meta AI unavailable. Install Meta AI, then turn on Developer Mode for these glasses.")
                .font(.footnote)
                .foregroundStyle(.red)

        case .some(.registered), .none:
            EmptyView()
        }
    }

    // MARK: - Backend URL

    private var backendField: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Backend").font(.caption).foregroundStyle(.secondary)
            TextField("http://10.0.0.137:8000", text: $urlText)
                .textFieldStyle(.roundedBorder)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
        }
    }

    // MARK: - Mock

    #if DEBUG
    @State private var showMockPanel = false

    /// Both debug switches live in one view so the body's VStack stays under
    /// ViewBuilder's 10-subview limit.
    private var debugToggles: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle("Use MockDeviceKit", isOn: Binding(
                get: { controller.useMockDevice },
                set: { controller.useMockDevice = $0 }
            ))
            .disabled(state.isRunning)

            Toggle("Glasses mic (HFP)", isOn: Binding(
                get: { controller.useGlassesMic },
                set: { controller.useGlassesMic = $0 }
            ))
            .disabled(state.isRunning)

            Text("Mic off = no HFP claim on the Bluetooth radio. If video then reaches "
                 + "'streaming', audio and camera are contending (§11.3).")
                .font(.caption)
                .foregroundStyle(.secondary)

            Button("Mock device panel…") { showMockPanel = true }
                .font(.footnote)
        }
        .sheet(isPresented: $showMockPanel) {
            MockDevicePanel(controller: controller)
        }
    }
    #endif

    // MARK: - Buttons

    private var buttonRow: some View {
        HStack(spacing: 10) {
            Button("Save URL") {
                controller.backend.setBaseURL(urlText)
                urlText = controller.backend.baseURL
                Task { await controller.pingBackend() }
            }
            .buttonStyle(.bordered)

            Button(state.isRunning ? "Stop" : "Start") {
                busy = true
                Task {
                    if state.isRunning { await controller.stop() }
                    else { await controller.start() }
                    busy = false
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy)

            // POST /activate + one full-resolution photo tagged source=activation.
            Button("Activate") {
                Task { await controller.activate() }
            }
            .buttonStyle(.bordered)
            .disabled(!state.isRunning)
        }
    }

    // MARK: - Log

    private var logHeader: some View {
        HStack {
            Text("Errors").font(.headline)
            Spacer()
            Button("Clear") { state.clearLog() }
                .font(.footnote)
        }
    }

    private var logList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 2) {
                // Newest-first.
                ForEach(Array(state.errorLogNewestFirst.enumerated()), id: \.offset) { _, line in
                    Text(line)
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .frame(maxHeight: .infinity)
    }
}

#Preview {
    ContentView(controller: BridgeController())
}
