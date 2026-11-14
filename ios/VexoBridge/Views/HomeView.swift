//
//  HomeView.swift
//  VexoBridge
//
//  The phone app: Now (ask + cards + payments that need you), Tasks, Things,
//  and Glasses (the original bridge controls). The laptop web app is separate.
//

import SwiftUI
import CoreImage.CIFilterBuiltins

struct RootView: View {
    let controller: BridgeController

    var body: some View {
        @Bindable var store = controller.memory
        TabView {
            NowView(controller: controller)
                .tabItem { Label("Now", systemImage: "sparkles") }
            TasksView(store: store)
                .tabItem { Label("Tasks", systemImage: "checklist") }
                .badge(store.tasks.count)
            ThingsView(store: store)
                .tabItem { Label("Things", systemImage: "key.horizontal") }
            NavigationStack { ContentView(controller: controller) }
                .tabItem { Label("Glasses", systemImage: "eyeglasses") }
        }
        .tint(.mint)
        // Pops on approve, from the phone OR a pinch on the glasses: the other
        // person scans this to pay.
        .sheet(item: $store.checkout) { _ in CheckoutSheet(store: store) }
    }
}

// MARK: - Now

private struct NowView: View {
    let controller: BridgeController
    private var store: MemoryStore { controller.memory }
    @State private var bridge = BridgeState.shared
    @State private var question = ""
    @State private var busy = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    statusStrip
                    askBar
                    if let a = store.lastAnswer { AnswerCard(answer: a, image: controller.backend.frameURL(a.frame)) }
                    needsYou
                    feed
                }
                .padding()
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Vexo")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button(bridge.isRunning ? "Stop" : "Start") {
                        busy = true
                        Task {
                            if bridge.isRunning { await controller.stop() } else { await controller.start() }
                            busy = false
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(bridge.isRunning ? .red : .mint)
                    .disabled(busy)
                }
            }
            .refreshable { await store.refreshLists() }
        }
    }

    private var statusStrip: some View {
        HStack(spacing: 16) {
            StatusDot(label: "Memory", on: store.online)
            StatusDot(label: "Glasses \(bridge.connection.rawValue.lowercased())", on: bridge.connection == .connected)
            Spacer()
        }
        .font(.footnote)
        .foregroundStyle(.secondary)
    }

    private var askBar: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            TextField("Ask your memory…", text: $question)
                .submitLabel(.search)
                .onSubmit(send)
            if store.asking {
                ProgressView()
            } else if !question.isEmpty {
                Button(action: send) { Image(systemName: "arrow.up.circle.fill").font(.title2) }
            }
        }
        .padding(14)
        .background(.background, in: RoundedRectangle(cornerRadius: 16))
    }

    private func send() {
        let q = question
        question = ""
        Task { await store.ask(q) }
    }

    @ViewBuilder
    private var needsYou: some View {
        let open = store.payments.filter { $0.status == "proposed" || $0.status == "approved" }
        if !open.isEmpty {
            Section {
                ForEach(open) { PaymentCard(payment: $0, store: store) }
            } header: {
                SectionTitle("Needs you")
            }
        }
    }

    @ViewBuilder
    private var feed: some View {
        Section {
            if store.cards.isEmpty {
                Text("Nothing yet. Start the glasses and go about your day. Say \u{201C}hey vexo\u{201D} to ask.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                    .background(.background, in: RoundedRectangle(cornerRadius: 16))
            }
            ForEach(store.cards) { FeedRow(card: $0) }
        } header: {
            SectionTitle("Heard and seen")
        }
    }
}

// MARK: - Tasks

private struct TasksView: View {
    let store: MemoryStore

    var body: some View {
        NavigationStack {
            List {
                ForEach(store.tasks) { task in
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Button {
                            Task { await store.complete(task.id) }
                        } label: {
                            Image(systemName: "circle").font(.title3)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.mint)
                        .accessibilityLabel("Mark done")
                        VStack(alignment: .leading, spacing: 4) {
                            Text(task.text)
                            Ago(ts: task.ts)
                        }
                    }
                    .swipeActions {
                        Button("Done") { Task { await store.complete(task.id) } }.tint(.mint)
                    }
                }
            }
            .overlay {
                if store.tasks.isEmpty {
                    ContentUnavailableView("No open tasks", systemImage: "checklist",
                                           description: Text("When someone asks you to do something, it lands here."))
                }
            }
            .navigationTitle("Tasks")
            .refreshable { await store.refreshLists() }
        }
    }
}

// MARK: - Things

private struct ThingsView: View {
    let store: MemoryStore

    var body: some View {
        NavigationStack {
            List(store.things) { thing in
                HStack(spacing: 14) {
                    AsyncImage(url: store.backend.frameURL(thing.frame)) { img in
                        img.resizable().scaledToFill()
                    } placeholder: {
                        Image(systemName: "photo").foregroundStyle(.tertiary)
                    }
                    .frame(width: 64, height: 64)
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 12))

                    VStack(alignment: .leading, spacing: 4) {
                        Text(thing.name.capitalized).font(.headline)
                        if let place = thing.place, !place.isEmpty {
                            Text(place).font(.subheadline).foregroundStyle(.secondary)
                        }
                        Ago(ts: thing.ts)
                    }
                }
                .padding(.vertical, 4)
            }
            .overlay {
                if store.things.isEmpty {
                    ContentUnavailableView("Nothing seen yet", systemImage: "key.horizontal",
                                           description: Text("Keys, wallet, laptop: wherever the glasses last saw them."))
                }
            }
            .navigationTitle("Things")
            .refreshable { await store.refreshLists() }
        }
    }
}

// MARK: - Cards

private struct PaymentCard: View {
    let payment: Payment
    let store: MemoryStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(payment.amountText).font(.system(.largeTitle, design: .rounded).bold())
                Spacer()
                Text(payment.status == "approved" ? "Waiting to be paid" : "Request?")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(payment.status == "approved" ? .orange : .secondary)
            }
            Text("from \(payment.who) · \(payment.memo)").foregroundStyle(.secondary)
            if payment.status == "proposed" {
                HStack {
                    Button {
                        Task { await store.approve(payment.id) }
                    } label: {
                        Label("Approve", systemImage: "checkmark").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    Button(role: .destructive) {
                        Task { await store.reject(payment.id) }
                    } label: {
                        Text("Reject").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                }
                .controlSize(.large)
            } else {
                Button {
                    store.checkout = payment
                } label: {
                    Label("Show QR to get paid", systemImage: "qrcode").frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .controlSize(.large)
            }
        }
        .padding()
        .background(.background, in: RoundedRectangle(cornerRadius: 20))
    }
}

private struct AnswerCard: View {
    let answer: AskResult
    let image: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(answer.question).font(.subheadline).foregroundStyle(.secondary)
            Text(answer.answer).font(.title3.weight(.semibold))
            if let image {
                AsyncImage(url: image) { $0.resizable().scaledToFit() } placeholder: { Color.clear.frame(height: 1) }
                    .clipShape(RoundedRectangle(cornerRadius: 12))
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.mint.opacity(0.12), in: RoundedRectangle(cornerRadius: 20))
    }
}

private struct FeedRow: View {
    let card: Card

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .font(.body.weight(.semibold))
                .foregroundStyle(.mint)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 4) {
                Text(card.title).font(.subheadline.weight(.semibold))
                Text(card.body).font(.subheadline).foregroundStyle(.secondary)
                Ago(ts: card.ts)
            }
            Spacer(minLength: 0)
        }
        .padding()
        .background(.background, in: RoundedRectangle(cornerRadius: 16))
    }

    private var icon: String {
        switch card.kind {
        case "payment": "dollarsign.circle"
        case "task":    "checklist"
        default:        "text.bubble"
        }
    }
}

// MARK: - Checkout (the other person scans this)

private struct CheckoutSheet: View {
    let store: MemoryStore

    var body: some View {
        VStack(spacing: 20) {
            if let p = store.checkout {
                Text(p.amountText).font(.system(size: 56, weight: .bold, design: .rounded))
                Text("\(p.memo) · \(p.who)").foregroundStyle(.secondary)
                if p.status == "paid" {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 120))
                        .foregroundStyle(.mint)
                        .symbolEffect(.bounce, value: p.status)
                    Text("Paid").font(.title.bold())
                } else if let url = p.url, let qr = Self.qr(url) {
                    Image(uiImage: qr)
                        .interpolation(.none)
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: 280)
                        .padding()
                        .background(.white, in: RoundedRectangle(cornerRadius: 20))
                    Label("Scan to pay with Stripe", systemImage: "camera.viewfinder")
                        .foregroundStyle(.secondary)
                    ProgressView("Waiting for payment…")
                }
                Button("Close") { store.checkout = nil }
                    .padding(.top)
            }
        }
        .padding(32)
        .presentationDetents([.large])
    }

    static func qr(_ text: String) -> UIImage? {
        let f = CIFilter.qrCodeGenerator()
        f.message = Data(text.utf8)
        f.correctionLevel = "M"
        guard let out = f.outputImage?.transformed(by: CGAffineTransform(scaleX: 10, y: 10)),
              let cg = CIContext().createCGImage(out, from: out.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}

// MARK: - Bits

private struct SectionTitle: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text).font(.title3.bold()).padding(.top, 4)
    }
}

private struct StatusDot: View {
    let label: String
    let on: Bool
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(on ? Color.mint : Color.orange).frame(width: 8, height: 8)
            Text(label)
        }
        .accessibilityElement(children: .combine)
    }
}

private struct Ago: View {
    let ts: Double
    var body: some View {
        Text(Date(timeIntervalSince1970: ts), format: .relative(presentation: .named))
            .font(.caption)
            .foregroundStyle(.tertiary)
    }
}
