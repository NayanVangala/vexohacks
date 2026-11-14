//
//  GlassesView.swift
//  VexoBridge
//
//  The device's home: are the glasses connected, is anything flowing, where is
//  the laptop. Same stacked-card language as the other tabs; the 3D star is
//  the hero. Every control from the original bridge screen is still here, the
//  diagnostics just live in a drawer at the bottom.
//

import SwiftUI
import MWDATCore

struct GlassesView: View {
    let controller: BridgeController
    @State private var state = BridgeState.shared
    @State private var url = ""
    @State private var busy = false
    @State private var finding = false
    @State private var showLog = false
    @State private var showMockPanel = false

    private var store: MemoryStore { controller.memory }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Glasses").font(.largeTitle.weight(.medium)).foregroundStyle(Theme.ink)
                    Text(summary).font(.subheadline).foregroundStyle(Theme.inkSoft)
                }
                .padding(.top, 12)

                VStack(spacing: -Theme.overlap) {
                    StackCard(color: Theme.rose, index: 0) { hero }.scrollDepth()
                    StackCard(color: Theme.card, index: 1) { live }.scrollDepth()
                    StackCard(color: Theme.sky, index: 2) { laptop }.scrollDepth()
                    startButton.rise(3)
                }
                .padding(.horizontal, -16)

                diagnostics
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 24)
        }
        .scrollIndicators(.hidden)
        .background(Theme.canvas.ignoresSafeArea())
        .onAppear { url = controller.backend.baseURL }
        .sheet(isPresented: $showMockPanel) { MockDevicePanel(controller: controller) }
    }

    // MARK: - Hero: the glasses themselves

    private var hero: some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 10) {
                Eyebrow("Ray-Ban Display")
                Text(status.title)
                    .font(.system(size: 34, weight: .medium))
                    .foregroundStyle(Theme.ink)
                    .contentTransition(.interpolate)
                Text(status.detail).font(.subheadline).foregroundStyle(Theme.ink.opacity(0.7))
                    .fixedSize(horizontal: false, vertical: true)
                if state.registrationState == .available {
                    Button {
                        Task { await controller.connectWearables() }
                    } label: {
                        Text("Connect glasses").font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.card)
                            .padding(.horizontal, 20).padding(.vertical, 12)
                            .background(Theme.ink, in: Capsule())
                    }
                    .buttonStyle(.squish)
                    .padding(.top, 4)
                }
            }
            Spacer(minLength: 0)
            ZStack {
                StarGlow().scaleEffect(1.6)
                Star3D(color: Theme.star)
                Twinkles(color: .white)
            }
            .frame(width: 130, height: 130)
            .accessibilityHidden(true)
        }
        .animation(.spring(response: 0.5, dampingFraction: 0.85), value: status.title)
    }

    private var status: (title: String, detail: String) {
        switch state.registrationState {
        case .unavailable?:
            return ("Meta AI needed", "Install Meta AI and turn on Developer Mode for these glasses.")
        case .available?:
            return ("Not connected", "Connect once, then approve Scene in the Meta AI app.")
        case .registering?:
            return ("Waiting for Meta AI", "Approve Scene in the Meta AI app, then come back.")
        default:
            break
        }
        switch state.connection {
        case .connected:    return ("Streaming", "Seeing and hearing. Answers and cards appear on the display.")
        case .connecting:   return ("Connecting", "Unfold the arms and keep the glasses close to the phone.")
        case .scanning:     return ("Looking", "Searching for your glasses over Bluetooth.")
        case .reconnecting: return ("Reconnecting", "The glasses ended the session. Picking back up on its own.")
        case .disconnected: return ("Ready", "Tap Start to begin remembering.")
        }
    }

    private var summary: String {
        switch (store.online, state.connection) {
        case (false, _): "Can't reach the laptop"
        case (true, .connected): "Live, and the laptop is listening"
        default: "Laptop connected"
        }
    }

    // MARK: - Live numbers (polled once a second)

    private var live: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Eyebrow("Live")
                    Spacer()
                    HStack(spacing: 6) {
                        LiveDot(color: state.connection == .connected ? Theme.chart : Theme.inkFaint,
                                live: state.connection == .connected)
                        Text(state.lastFrameAt.map { "Last frame \($0)" } ?? "No frames yet")
                            .font(.caption).foregroundStyle(Theme.inkSoft)
                    }
                }
                HStack(alignment: .lastTextBaseline, spacing: 26) {
                    number(controller.framesSent, "Frames sent", 46)
                    number(controller.framesFromGlasses, "From glasses", 22)
                    number(controller.reconnects, "Reconnects", 22)
                }
                if state.cameraPermissionText.lowercased() != "granted" {
                    // Audio works without this (it's a Bluetooth headset), the camera does not.
                    // Opens Meta AI's approval sheet for Scene's camera access.
                    Button {
                        Task { await controller.ensureCameraPermission() }
                    } label: {
                        HStack {
                            Image(systemName: "camera.fill")
                            Text("Allow camera in Meta AI").font(.subheadline.weight(.semibold))
                            Spacer()
                            Image(systemName: "arrow.up.right")
                        }
                        .foregroundStyle(Theme.card)
                        .padding(.horizontal, 18).padding(.vertical, 14)
                        .background(Theme.ink, in: Capsule())
                    }
                    .buttonStyle(.squish)
                }
                VStack(spacing: 0) {
                    row("Microphone", state.audioInputPortName)
                    row("Display", controller.displayState == "none" ? "Off" : controller.displayState.capitalized)
                    row("Camera permission", state.cameraPermissionText.capitalized)
                    row("Meta AI", state.registrationText.capitalized, last: true)
                }
            }
        }
    }

    private func number(_ value: Int, _ label: String, _ size: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(value)").font(Theme.number(size)).foregroundStyle(Theme.ink)
                .contentTransition(.numericText(value: Double(value)))
                .animation(.snappy, value: value)
            Text(label).font(.caption).foregroundStyle(Theme.inkSoft)
        }
    }

    private func row(_ label: String, _ value: String, last: Bool = false) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text(label).foregroundStyle(Theme.inkSoft)
                Spacer()
                Text(value).foregroundStyle(Theme.ink).lineLimit(1)
            }
            .font(.subheadline)
            .padding(.vertical, 11)
            if !last { Rectangle().fill(Theme.ink.opacity(0.07)).frame(height: 1) }
        }
    }

    // MARK: - Laptop

    private var laptop: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Eyebrow("Laptop")
                Spacer()
                HStack(spacing: 6) {
                    LiveDot(color: store.online ? Theme.chart : Theme.alert, live: store.online)
                    Text(store.online ? "Memory is listening" : "Not reachable").font(.caption)
                }
                .foregroundStyle(Theme.inkSoft)
            }
            TextField("http://192.168.1.20:8001", text: $url)
                .font(.subheadline.monospaced())
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .submitLabel(.done)
                .onSubmit(save)
                .padding(.horizontal, 16).frame(height: 48)
                .background(Theme.card, in: Capsule())
            HStack(spacing: 10) {
                Button(action: save) {
                    Text("Use this address").font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity).padding(.vertical, 13)
                        .foregroundStyle(Theme.card).background(Theme.ink, in: Capsule())
                }
                .buttonStyle(.squish)
                Button {
                    finding = true
                    Task {
                        let ok = await controller.findLaptop()
                        url = controller.backend.baseURL
                        if !ok { store.notice = "No laptop found on this network. Start Scene on the laptop, or type its address." }
                        finding = false
                    }
                } label: {
                    HStack(spacing: 6) {
                        if finding { ProgressView().controlSize(.small) }
                        Text(finding ? "Looking…" : "Find automatically")
                    }
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity).padding(.vertical, 13)
                    .foregroundStyle(Theme.ink)
                    .overlay(Capsule().stroke(Theme.ink.opacity(0.25)))
                }
                .buttonStyle(.squish)
                .disabled(finding)
            }
        }
    }

    private func save() {
        controller.backend.setBaseURL(url)
        url = controller.backend.baseURL
        Task { await controller.pingBackend() }
    }

    // MARK: - Start / photo

    private var startButton: some View {
        HStack(spacing: 10) {
            Button {
                busy = true
                Task {
                    if state.isRunning { await controller.stop() } else { await controller.start() }
                    busy = false
                }
            } label: {
                VStack(spacing: 4) {
                    Text(state.isRunning ? "Stop remembering" : "Start remembering").font(.title3.weight(.medium))
                    Text(state.isRunning ? "Camera and mic are on" : "Camera and mic stay off until you start")
                        .font(.footnote).opacity(0.75)
                }
                .foregroundStyle(Theme.ink)
                .frame(maxWidth: .infinity).padding(.vertical, 22)
                .background(Theme.action, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
                .overlay(alignment: .trailing) {
                    if busy { ProgressView().tint(Theme.ink).padding(.trailing, 24) }
                }
            }
            .buttonStyle(.squish)
            .disabled(busy)

            if state.isRunning {
                // POST /activate + one full-resolution photo: "remember this" on purpose.
                Button {
                    Task { await controller.activate() }
                } label: {
                    Image(systemName: "camera.aperture").font(.title2)
                        .foregroundStyle(Theme.ink)
                        .frame(width: 76).frame(maxHeight: .infinity)
                        .background(Theme.star, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
                }
                .buttonStyle(.squish)
                .accessibilityLabel("Take a photo to remember")
                .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(.horizontal, 8)
        .animation(.spring(response: 0.45, dampingFraction: 0.8), value: state.isRunning)
    }

    // MARK: - Diagnostics drawer

    private var diagnostics: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button {
                withAnimation(.spring(response: 0.45, dampingFraction: 0.85)) { showLog.toggle() }
            } label: {
                HStack {
                    Text("Diagnostics").font(.headline)
                    Text("\(state.errorLogNewestFirst.count)").font(.caption.monospacedDigit())
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Theme.card.opacity(0.7), in: Capsule())
                    Spacer()
                    Image(systemName: "chevron.down").rotationEffect(.degrees(showLog ? 180 : 0))
                }
                .foregroundStyle(Theme.ink)
                .padding(.vertical, 8)
            }
            .buttonStyle(.plain)

            if showLog {
                #if DEBUG
                VStack(alignment: .leading, spacing: 8) {
                    Toggle("Use phone camera instead (MockDeviceKit)", isOn: Binding(
                        get: { controller.useMockDevice }, set: { controller.useMockDevice = $0 }))
                    Toggle("Glasses microphone", isOn: Binding(
                        get: { controller.useGlassesMic }, set: { controller.useGlassesMic = $0 }))
                    Button("Mock device panel") { showMockPanel = true }.font(.subheadline)
                }
                .font(.subheadline)
                .tint(Theme.action)
                .disabled(state.isRunning)
                #endif

                HStack {
                    Text("Newest first").font(.caption).foregroundStyle(Theme.inkSoft)
                    Spacer()
                    Button("Clear") { state.clearLog() }.font(.caption.weight(.semibold)).foregroundStyle(Theme.ink)
                }
                LazyVStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(state.errorLogNewestFirst.prefix(80).enumerated()), id: \.offset) { _, line in
                        Text(line).font(.system(size: 11, design: .monospaced)).foregroundStyle(Theme.ink.opacity(0.8))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                }
                .padding(14)
                .background(Theme.card.opacity(0.6), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(.top, 8)
    }
}
