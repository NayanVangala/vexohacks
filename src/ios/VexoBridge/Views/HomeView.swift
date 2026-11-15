//
//  HomeView.swift
//  VexoBridge
//
//  The phone app. Visual language: a warm canvas, big pastel cards that stack
//  and overlap like a deck, oversized numbers, tiny letter-spaced labels, and
//  one coral action. Colors and shapes live in Theme.swift.
//
//  Tabs: Today, Needs you, To do, Things, Settings
//  (the original bridge controls). The laptop web app is separate.
//

import SwiftUI
import Charts
import CoreImage.CIFilterBuiltins

struct RootView: View {
    let controller: BridgeController
    @AppStorage("seenIntro") private var seenIntro = false
    @AppStorage("appearance") private var appearance = "system"
    /// Launch with `-startTab glasses` (or tasks/things) to open on that tab: screenshots, demo takes.
    @State private var tab = UserDefaults.standard.string(forKey: "startTab") ?? "today"

    var body: some View {
        @Bindable var store = controller.memory
        TabView(selection: $tab) {
            TodayView(controller: controller, replayIntro: { seenIntro = false })
                .tabItem { Label("Today", systemImage: "sparkle") }
                .tag("today")
            NeedsYouView(store: store)
                .tabItem { Label("Needs you", systemImage: "tray.full") }
                .badge(store.inbox.count)
                .tag("needs")
            TasksView(store: store)
                .tabItem { Label("To do", systemImage: "checklist") }
                .badge(store.todoItems.count)
                .tag("tasks")
            ThingsView(store: store)
                .tabItem { Label("Things", systemImage: "key.horizontal") }
                .tag("things")
            GlassesView(controller: controller, replayIntro: { seenIntro = false })
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag("glasses")
        }
        .overlay(alignment: .top) {
            if let notice = store.notice {
                NoticeBanner(text: notice) { store.notice = nil }
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .task(id: notice) {
                        try? await Task.sleep(for: .seconds(6))
                        if store.notice == notice { store.notice = nil }
                    }
            }
        }
        .animation(.spring(response: 0.45, dampingFraction: 0.85), value: store.notice)
        .tint(Theme.ink)
        .preferredColorScheme(Appearance.scheme(appearance))
        // Pops on approve, from the phone OR a pinch on the glasses: the other
        // person scans this to pay.
        .sheet(item: $store.checkout) { _ in CheckoutSheet(store: store) }
        .fullScreenCover(isPresented: Binding(get: { !seenIntro }, set: { seenIntro = !$0 })) {
            IntroView { seenIntro = true }
                .environment(\.colorScheme, .light)   // the coral welcome is designed in one light
        }
    }
}

// MARK: - Today

private struct TodayView: View {
    let controller: BridgeController
    let replayIntro: () -> Void
    private var store: MemoryStore { controller.memory }
    @State private var bridge = BridgeState.shared
    @State private var question = ""
    @State private var busy = false
    @State private var convBusy = false
    @State private var unknownName = ""
    @State private var unknownBusy = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                streamControl
                conversationCard
                unknownFaceCard
                askPill
                if let a = store.lastAnswer {
                    AnswerBubble(answer: a, image: controller.backend.frameURL(a.frame))
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
                if store.leadOffer != nil {
                    LeadCard(store: store)
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
                VStack(spacing: -Theme.overlap) {
                    StackCard(color: Theme.card, index: 0) { TodayCard(today: store.today) }.scrollDepth()
                    StackCard(color: Theme.butter, index: 1) { NeedsYouCard(store: store) }.scrollDepth()
                }
                .padding(.top, 6)
                TodoListView(store: store)
                NotesSection(store: store)
                OrdersSection(store: store)
                ActivitySection(store: store)
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 24)
            .animation(.spring(response: 0.45, dampingFraction: 0.85), value: store.lastAnswer?.question)
        }
        .scrollIndicators(.hidden)
        .background(Theme.canvas.ignoresSafeArea())
        .refreshable { await store.refreshLists() }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Button(action: replayIntro) {
                ZStack {
                    Circle().fill(Theme.action)
                    Star3D(color: Theme.star, depth: 0.28).padding(2).allowsHitTesting(false)
                }
                .frame(width: 50, height: 50)
                .overlay { Twinkles(color: .white).frame(width: 72, height: 72) }
            }
            .accessibilityLabel("Replay intro")
            VStack(alignment: .leading, spacing: 0) {
                Text(greeting).font(.subheadline).foregroundStyle(Theme.inkSoft)
                Text("Your memory").font(.title2.weight(.medium)).foregroundStyle(Theme.ink)
            }
            Spacer()
            StatusChip(online: store.online, glasses: bridge.connection)
        }
        .padding(.top, 8)
    }

    private var greeting: String {
        switch Calendar.current.component(.hour, from: .now) {
        case 5..<12: "Good morning,"
        case 12..<17: "Good afternoon,"
        default: "Good evening,"
        }
    }

    private var askPill: some View {
        HStack(spacing: 10) {
            Image(systemName: "sparkle.magnifyingglass").foregroundStyle(Theme.inkSoft)
            TextField("Ask anything you've seen or heard", text: $question)
                .submitLabel(.search)
                .onSubmit(send)
            if store.asking {
                ProgressView()
            } else if !question.isEmpty {
                Button(action: send) {
                    Image(systemName: "arrow.up").font(.subheadline.weight(.bold))
                        .foregroundStyle(.white).frame(width: 30, height: 30)
                        .background(Theme.ink, in: Circle())
                }
            }
        }
        .padding(.horizontal, 18)
        .frame(height: 52)
        .background(Theme.card.opacity(0.7), in: Capsule())
        .overlay(Capsule().stroke(Theme.ink.opacity(0.08)))
    }

    private func send() {
        let q = question
        question = ""
        Task { await store.ask(q) }
    }

    /// The big Start / Stop streaming control, first thing on the Today tab.
    /// Same start()/stop() pipeline as the Settings button in GlassesView —
    /// this is the same action moved to the front page, not a second pipeline.
    private var streamControl: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 8) {
                LiveDot(color: bridge.isRunning ? Theme.chart : Theme.inkFaint,
                        live: bridge.isRunning)
                Text(streamStateText)
                    .font(.headline)
                    .foregroundStyle(Theme.ink)
                Spacer()
                Text(bridge.connection.rawValue.lowercased())
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.inkSoft)
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(Theme.canvas, in: Capsule())
            }
            Button {
                busy = true
                Task {
                    if bridge.isRunning { await controller.stop() } else { await controller.start() }
                    busy = false
                }
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(busy ? (bridge.isRunning ? "Stopping…" : "Starting…")
                                  : (bridge.isRunning ? "Stop streaming" : "Start streaming"))
                            .font(.title2.weight(.semibold))
                        Text(captureSubtitle).font(.footnote).opacity(0.75)
                    }
                    Spacer()
                    if busy {
                        ProgressView().tint(Theme.ink)
                    } else {
                        Image(systemName: bridge.isRunning ? "stop.fill" : "play.fill")
                            .font(.title3.weight(.bold))
                    }
                }
                .foregroundStyle(Theme.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 20).padding(.vertical, 22)
                .background(Theme.action, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
            }
            .buttonStyle(.squish)
            .disabled(busy)
            .sensoryFeedback(.impact, trigger: bridge.isRunning)
            .accessibilityLabel(bridge.isRunning ? "Stop streaming" : "Start streaming")
            TimelineView(.periodic(from: .now, by: 1)) { _ in
                HStack(spacing: 24) {
                    streamStat("\(controller.framesSent)", "Frames sent")
                    streamStat(bridge.audioInputPortName, "Mic")
                    streamStat(controller.displayState == "none" ? "Off" : controller.displayState.capitalized, "Display")
                }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
    }

    private func streamStat(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink).lineLimit(1).minimumScaleFactor(0.6)
            Text(label).font(.caption).foregroundStyle(Theme.inkSoft)
        }
    }

    private var streamStateText: String {
        if busy { return bridge.isRunning ? "Stopping…" : "Starting…" }
        return bridge.isRunning ? "Streaming" : "Stopped"
    }

    private var captureSubtitle: String {
        if bridge.isRunning {
            if let last = bridge.lastFrameAt { return "Glasses streaming · last frame \(last)" }
            return "Waiting for the first frame…"
        }
        return "Camera and mic stay off until you start"
    }

    /// Conversation Mode: what the glasses are taking notes on right now, with
    /// the same Start / Stop notes control as the lens. Sits under streaming and
    /// never touches it: Start streaming stays the first control on Today.
    private var conversationCard: some View {
        let conv = store.conversation
        let active = conv?.isActive == true
        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                LiveDot(color: active ? Theme.chart : Theme.inkFaint, live: active)
                Text(active ? "Taking notes" : "Conversation notes")
                    .font(.headline).foregroundStyle(Theme.ink)
                Spacer()
                if let s = conv?.speakerName {
                    Text(s).font(.caption.weight(.semibold)).foregroundStyle(Theme.inkSoft)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(Theme.canvas, in: Capsule())
                }
            }
            Text(conversationLine)
                .font(.footnote).foregroundStyle(Theme.inkSoft)
            Button {
                convBusy = true
                Task {
                    if active { await store.stopConversation() } else { await store.startConversation() }
                    convBusy = false
                }
            } label: {
                HStack {
                    Text(convBusy ? (active ? "Stopping…" : "Starting…") : (active ? "Stop notes" : "Start notes"))
                        .font(.subheadline.weight(.semibold))
                    Spacer()
                    if convBusy { ProgressView().tint(Theme.card) }
                    else { Image(systemName: active ? "stop.fill" : "record.circle").font(.subheadline.weight(.bold)) }
                }
                .foregroundStyle(Theme.card)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 16).padding(.vertical, 14)
                .background(Theme.ink, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
            .buttonStyle(.squish)
            .disabled(convBusy)
            .accessibilityLabel(active ? "Stop notes" : "Start notes")
            if !store.conversationAvailable {
                Text("Needs the laptop backend update — the feed below keeps working meanwhile.")
                    .font(.caption).foregroundStyle(Theme.inkSoft)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.mint, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
    }

    /// Who-was-that card: shows only while the backend has an unknown-face prompt
    /// armed (after a conversation ended with an unfamiliar face in it). Type the
    /// name to enroll the saved frame, or Skip to discard it. Never shows during
    /// a conversation: the backend arms it only at conversation end.
    @ViewBuilder
    private var unknownFaceCard: some View {
        if let u = store.unknownFace, u.isPending {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Image(systemName: "person.crop.circle.badge.questionmark")
                        .font(.headline).foregroundStyle(Theme.ink)
                    Text("Who was that?")
                        .font(.headline).foregroundStyle(Theme.ink)
                    Spacer()
                    if let f = u.frame, let url = controller.backend.frameURL(f) {
                        AsyncImage(url: url) { img in
                            img.resizable().scaledToFill()
                        } placeholder: {
                            Image(systemName: "person.fill").foregroundStyle(Theme.inkFaint)
                        }
                        .frame(width: 44, height: 44)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    }
                }
                Text("I saw an unfamiliar face. Say their name, or type it here to remember them next time.")
                    .font(.footnote).foregroundStyle(Theme.inkSoft)
                HStack(spacing: 10) {
                    TextField("Name", text: $unknownName)
                        .submitLabel(.done)
                        .onSubmit { Task { await enrollUnknown() } }
                        .padding(.horizontal, 14).padding(.vertical, 12)
                        .background(Theme.canvas, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    Button {
                        Task { await enrollUnknown() }
                    } label: {
                        Text(unknownBusy ? "Saving…" : "Save")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.card)
                            .padding(.horizontal, 16).padding(.vertical, 12)
                            .background(Theme.ink, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }
                    .buttonStyle(.squish)
                    .disabled(unknownBusy || unknownName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button {
                        Task {
                            unknownBusy = true
                            await store.dismissUnknownFace()
                            unknownName = ""
                            unknownBusy = false
                        }
                    } label: {
                        Text("Skip")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.inkSoft)
                            .padding(.horizontal, 16).padding(.vertical, 12)
                            .background(Theme.canvas, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }
                    .buttonStyle(.squish)
                    .disabled(unknownBusy)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.butter, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
        }
    }

    private func enrollUnknown() async {
        let name = unknownName
        unknownBusy = true
        if await store.enrollUnknownFace(name: name) { unknownName = "" }
        unknownBusy = false
    }

    private var conversationLine: String {
        let conv = store.conversation
        if conv?.isActive == true {
            let tasks = conv?.taskTotal ?? 0
            let lines = conv?.lineTotal ?? 0
            let who = conv?.speakerName.map { "with \($0)" } ?? "speaker unknown"
            return "Listening \(who) · \(tasks) task\(tasks == 1 ? "" : "s") found · \(lines) lines noted"
        }
        if let last = store.lastConversation {
            return "Last conversation ended · \(last.tasks) task\(last.tasks == 1 ? "" : "s") · \(last.lines) lines — full notes in Notes."
        }
        return "Start notes before a conversation and Scene writes down tasks and quotes as you talk."
    }
}

/// "TODAY · Thu, Nov 14" with three counting numbers and the activity line.
private struct TodayCard: View {
    let today: Today?
    @State private var drawn: CGFloat = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Eyebrow("Today")
                Spacer()
                Text(Date.now, format: .dateTime.weekday(.abbreviated).month(.abbreviated).day())
                    .font(.caption)
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .overlay(Capsule().stroke(Theme.ink.opacity(0.15)))
                Image(systemName: "calendar")
                    .font(.caption)
                    .frame(width: 30, height: 30)
                    .overlay(Circle().stroke(Theme.ink.opacity(0.15)))
            }
            .foregroundStyle(Theme.ink)

            HStack(alignment: .lastTextBaseline, spacing: 10) {
                Stat(value: today?.memories ?? 0, label: "Memories", size: 46)
                    .layoutPriority(1)
                Stat(value: today?.openTasks ?? 0, label: "Open tasks", size: 22)
                Stat(value: today?.things ?? 0, label: "Things seen", size: 22)
            }

            Chart {
                ForEach(Array(hours.enumerated()), id: \.offset) { i, n in
                    AreaMark(x: .value("Hour", i), y: .value("Memories", n))
                        .interpolationMethod(.catmullRom)
                        .foregroundStyle(LinearGradient(colors: [Theme.chart.opacity(0.28), Theme.chart.opacity(0)],
                                                        startPoint: .top, endPoint: .bottom))
                    LineMark(x: .value("Hour", i), y: .value("Memories", n))
                        .interpolationMethod(.catmullRom)
                        .foregroundStyle(Theme.chart)
                        .lineStyle(StrokeStyle(lineWidth: 2.5, lineCap: .round))
                }
                if let now = today?.nowHour {
                    RuleMark(x: .value("Now", now)).foregroundStyle(Theme.ink.opacity(0.7)).lineStyle(StrokeStyle(lineWidth: 1))
                }
            }
            .chartXScale(domain: 6...22)
            .chartYAxis(.hidden)
            .chartXAxis {
                AxisMarks(values: [8, 10, 12, 14, 16, 18, 20]) { v in
                    AxisValueLabel { Text(hourLabel(v.as(Int.self) ?? 0)).font(.caption2).foregroundStyle(Theme.inkSoft) }
                }
            }
            .frame(height: 110)
            .padding(.horizontal, -8)
            .mask(alignment: .leading) {
                GeometryReader { g in Rectangle().frame(width: g.size.width * drawn) }
            }
            .onAppear { withAnimation(.easeInOut(duration: 1.4).delay(0.35)) { drawn = 1 } }
        }
    }

    /// Flat line before any data, so the chart still draws in on an empty day.
    private var hours: [Int] { today?.hourly ?? Array(repeating: 0, count: 24) }

    private func hourLabel(_ h: Int) -> String {
        h == 12 ? "12 PM" : h < 12 ? "\(h) AM" : "\(h - 12) PM"
    }
}

/// The butter card: a payment waiting on you, or "all clear" + things recently seen.
private struct NeedsYouCard: View {
    let store: MemoryStore

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if pending != nil || store.inbox.isEmpty {
                Eyebrow(pending == nil ? "All clear" : "Needs you")
            }
            if pending == nil, let item = store.inbox.first(where: { $0.type != "payment" }) {
                InboxCard(item: item, store: store)
            } else if let p = pending {
                HStack(alignment: .center, spacing: 10) {
                    Text(p.amountText).font(Theme.number(44)).foregroundStyle(Theme.ink)
                        .lineLimit(1).minimumScaleFactor(0.5)
                    star
                    Spacer()
                }
                Text("from \(p.who) · \(p.memo)").foregroundStyle(Theme.inkSoft)
                if p.status == "proposed" {
                    HStack(spacing: 10) {
                        pill("Approve", filled: true) { Task { await store.approve(p.id) } }
                        pill("Not now", filled: false) { Task { await store.reject(p.id) } }
                    }
                } else {
                    pill("Show QR to get paid", filled: true) { store.checkout = p }
                }
            } else {
                // No payment, no inbox item: the To do list right below this
                // card is the one place for tasks, so this card says nothing
                // about counts instead of repeating it in a second number.
                Text("Nothing to approve right now.")
                    .font(.subheadline).foregroundStyle(Theme.inkSoft)
            }
            if let card = store.cards.first {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "waveform").font(.footnote).frame(width: 26, height: 26)
                        .overlay(Circle().stroke(Theme.ink.opacity(0.2)))
                    Text(card.kind == "answer" ? "\u{201C}\(card.title)\u{201D} — \(card.body)" : card.body)
                        .font(.footnote).foregroundStyle(Theme.ink.opacity(0.8)).lineLimit(2)
                }
            }
        }
        .animation(.spring(response: 0.5, dampingFraction: 0.8), value: pending?.id)
    }

    private var pending: Payment? {
        store.payments.first { $0.status == "proposed" } ?? store.payments.first { $0.status == "approved" }
    }

    private var star: some View {
        Star3D(color: Theme.action, depth: 0.3).frame(width: 44, height: 44).accessibilityHidden(true)
    }

    private func pill(_ title: String, filled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.subheadline.weight(.semibold))
                .frame(maxWidth: .infinity).padding(.vertical, 14)
                .foregroundStyle(filled ? Theme.card : Theme.ink)
                .background(filled ? Theme.ink : .clear, in: Capsule())
                .overlay(Capsule().stroke(Theme.ink.opacity(filled ? 0 : 0.25)))
        }
        .buttonStyle(.squish)
        .sensoryFeedback(.success, trigger: filled ? pending?.status : nil)
    }
}

private struct Stat: View {
    let value: Int
    let label: String
    let size: CGFloat
    @State private var shown = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(shown)").font(Theme.number(displaySize)).foregroundStyle(Theme.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentTransition(.numericText(value: Double(shown)))
            Text(label).font(.caption).foregroundStyle(Theme.inkSoft)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear { countTo(value, delay: 0.4) }
        .onChange(of: value) { _, v in countTo(v, delay: 0) }
    }

    /// Size the number from its digit count so it fits its column on one
    /// line at the biggest readable size. A monospaced digit is ~0.6em wide
    /// and a stat column is ~95pt, so font <= 95 / (digits * 0.62).
    /// The old 0.62x shrink made 1000 tiny (28pt) and it still wrapped,
    /// because the Text had no bounded width for minimumScaleFactor to
    /// work against. The full number always shows, never "1k".
    /// 1000 / 9999 -> 38pt, 10000 -> 30pt, 999 and below -> full size.
    private var displaySize: CGFloat {
        let digits = max(1, String(value).count)
        return min(size, 95 / (CGFloat(digits) * 0.62))
    }

    private func countTo(_ v: Int, delay: Double) {
        withAnimation(.snappy(duration: 0.9).delay(delay)) { shown = v }
    }
}

private struct StatusChip: View {
    let online: Bool
    let glasses: BridgeState.Connection

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(dot).frame(width: 7, height: 7)
            Text(text).font(.caption)
        }
        .foregroundStyle(Theme.ink)
        .padding(.horizontal, 10).padding(.vertical, 7)
        .background(Theme.card.opacity(0.6), in: Capsule())
    }

    private var dot: Color { !online ? Theme.alert : glasses == .connected ? Theme.chart : Theme.star }
    private var text: String { !online ? "Laptop offline" : glasses == .connected ? "Live" : "Glasses off" }
}

private struct AnswerBubble: View {
    let answer: AskResult
    let image: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Eyebrow(answer.question)
            Text(answer.answer).font(.title3.weight(.medium)).foregroundStyle(Theme.ink)
            if let image {
                AsyncImage(url: image) { $0.resizable().scaledToFit() } placeholder: { Color.clear.frame(height: 1) }
                    .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.mint, in: RoundedRectangle(cornerRadius: 28, style: .continuous))
    }
}

private struct FeedList: View {
    let cards: [Card]

    var body: some View {
        if !cards.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Eyebrow("Heard and seen").padding(.bottom, 8).padding(.top, 10)
                ForEach(cards.prefix(12)) { card in
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: icon(card.kind)).font(.footnote.weight(.semibold))
                            .frame(width: 30, height: 30)
                            .background(Theme.card, in: Circle())
                        VStack(alignment: .leading, spacing: 2) {
                            Text(card.title).font(.subheadline.weight(.medium))
                            Text(card.body).font(.footnote).foregroundStyle(Theme.inkSoft).lineLimit(2)
                        }
                        Spacer(minLength: 0)
                        Ago(ts: card.ts)
                    }
                    .foregroundStyle(Theme.ink)
                    .padding(.vertical, 8)
                }
            }
        }
    }

    private func icon(_ kind: String) -> String {
        switch kind {
        case "payment": "dollarsign"
        case "task": "checkmark"
        default: "text.bubble"
        }
    }
}

// MARK: - Tasks (stacked pastel cards)

private struct TasksView: View {
    let store: MemoryStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                PageTitle("To do", subtitle: "\(store.todoItems.count) thing\(store.todoItems.count == 1 ? "" : "s") waiting")
                TodoListView(store: store, showTitle: false)
            }
            .padding(.horizontal, 16)
        }
        .background(Theme.canvas.ignoresSafeArea())
        .refreshable { await store.refreshLists() }
    }
}

// MARK: - To do (the one list: promises + tasks, newest first)
//
// Follow-Through promises and backend tasks used to live in two places with
// two names. They are one list now: "To do". The backend is unchanged; the
// merge and de-dup happen in MemoryStore.todoItems. One row, one button:
// Approve for a promise, Done for a task, Not now / Try again otherwise.

private struct TodoListView: View {
    let store: MemoryStore
    var showTitle = true
    @State private var selected: TodoItem?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if showTitle {
                HStack {
                    Eyebrow("To do")
                    Spacer()
                    Text("\(store.todoItems.count) waiting").font(.subheadline).foregroundStyle(Theme.inkSoft)
                }
            }
            if store.todoItems.isEmpty {
                TodoEmptyState()
            } else {
                VStack(spacing: 12) {
                    ForEach(store.todoItems) { item in
                        TodoRow(item: item, store: store) { selected = item }
                    }
                }
            }
        }
        .sheet(item: $selected) { item in TodoDetailSheet(item: item, store: store) }
    }
}

private struct TodoRow: View {
    let item: TodoItem
    let store: MemoryStore
    var onTap: (() -> Void)? = nil
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(item.title).font(.headline).foregroundStyle(Theme.ink)
                    if let meta = item.meta { Text(meta).font(.footnote).foregroundStyle(Theme.inkSoft) }
                    Text(item.statusText).font(.caption).foregroundStyle(Theme.inkSoft)
                }
                Spacer()
                Button {
                    busy = true
                    Task { await store.todoAction(item); busy = false }
                } label: {
                    Text(busy ? "\u{2026}" : item.buttonLabel)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.card)
                        .padding(.horizontal, 16).padding(.vertical, 10)
                        .background(Theme.ink, in: Capsule())
                }
                .buttonStyle(.squish)
                .disabled(busy)
                .accessibilityLabel("\(item.buttonLabel): \(item.title)")
            }
            if let q = item.quote, !q.isEmpty {
                Text("\u{201C}\(q)\u{201D}").font(.subheadline.italic()).foregroundStyle(Theme.inkSoft).lineLimit(2)
            }
            Text("Tap for full detail \u{203A}").font(.caption).foregroundStyle(Theme.inkFaint)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .contentShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        .onTapGesture { onTap?() }
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel("Details: \(item.title)")
    }
}

// MARK: - To do detail (tap a row: what was actually said)
//
// Title, status, who said it, when, the exact quote verbatim, and the
// description built from the verbatim transcript lines around the quote.
// Nothing here is paraphrased or invented: no quote saved means the screen
// says so. Same action button as the row, plus a Dismiss for failed promises.
struct TodoDetailSheet: View {
    let item: TodoItem
    let store: MemoryStore
    @Environment(\.dismiss) private var dismiss
    @State private var busy = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Eyebrow(item.kind == .promise ? (item.actionLabel ?? "Promise") : "Task")
                Text(item.title).font(.title2.weight(.semibold)).foregroundStyle(Theme.ink)
                HStack(spacing: 8) {
                    Text(item.statusText).font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.ink)
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Theme.butter, in: Capsule())
                    if let meta = item.meta { Text(meta).font(.subheadline).foregroundStyle(Theme.inkSoft) }
                }
                if let who = item.speaker {
                    Text("\(who) said this").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                } else {
                    Text("Who said it was not recorded.").font(.subheadline).foregroundStyle(Theme.inkSoft)
                }
                Text(Date(timeIntervalSince1970: item.sortKey), format: .dateTime.weekday(.abbreviated).month().day().hour().minute())
                    .font(.subheadline).foregroundStyle(Theme.inkSoft)
                VStack(alignment: .leading, spacing: 6) {
                    Eyebrow("Exactly what was said")
                    if let q = item.quote, !q.isEmpty {
                        Text("\u{201C}\(q)\u{201D}").font(.body.italic()).foregroundStyle(Theme.ink)
                    } else {
                        Text("No exact quote saved for this one.").font(.body).foregroundStyle(Theme.inkSoft)
                    }
                }
                VStack(alignment: .leading, spacing: 8) {
                    Eyebrow("Description")
                    if let s = item.summary, !s.isEmpty {
                        Text(s).font(.body).foregroundStyle(Theme.ink)
                    }
                    if !item.context.isEmpty {
                        ForEach(item.context, id: \.self) { line in
                            Text(line).font(.subheadline).foregroundStyle(Theme.inkSoft)
                        }
                    } else if (item.summary ?? "").isEmpty && (item.detailBody ?? "").isEmpty {
                        Text("No conversation around this one was saved.").font(.body).foregroundStyle(Theme.inkSoft)
                    }
                    if let d = item.detailBody, !d.isEmpty {
                        Text(d).font(.body).foregroundStyle(Theme.ink)
                    }
                    if let r = item.result, !r.isEmpty {
                        Text("Result: \(r)").font(.subheadline).foregroundStyle(Theme.inkSoft)
                    }
                    if !item.issues.isEmpty {
                        Text("Needs a look: \(item.issues.joined(separator: "; "))").font(.subheadline).foregroundStyle(Theme.inkSoft)
                    }
                }
                HStack(spacing: 10) {
                    Button {
                        busy = true
                        Task { await store.todoAction(item); busy = false; dismiss() }
                    } label: {
                        Text(busy ? "\u{2026}" : item.buttonLabel).font(.headline)
                            .frame(maxWidth: .infinity).padding(.vertical, 14)
                            .foregroundStyle(Theme.card).background(Theme.ink, in: Capsule())
                    }
                    .buttonStyle(.squish).disabled(busy)
                    if item.kind == .promise && item.status == .failed, let id = item.promiseId {
                        Button {
                            busy = true
                            Task { await store.promise(id, "dismiss"); busy = false; dismiss() }
                        } label: {
                            Text("Dismiss").font(.headline)
                                .frame(maxWidth: .infinity).padding(.vertical, 14)
                                .foregroundStyle(Theme.ink)
                                .overlay(Capsule().stroke(Theme.ink.opacity(0.25)))
                        }
                        .buttonStyle(.squish).disabled(busy)
                    }
                    Button { dismiss() } label: {
                        Text("Close").font(.headline)
                            .frame(maxWidth: .infinity).padding(.vertical, 14)
                            .foregroundStyle(Theme.ink)
                            .overlay(Capsule().stroke(Theme.ink.opacity(0.25)))
                    }
                    .buttonStyle(.squish).disabled(busy)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.canvas.ignoresSafeArea())
        .presentationDetents([.medium, .large])
    }
}

// MARK: - Things (stacked pastel cards, tap to expand)

private struct ThingsView: View {
    let store: MemoryStore
    @State private var open: Int?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                PageTitle("Things", subtitle: "Where the glasses last saw them")
                if store.leadOffer != nil {
                    LeadCard(store: store)
                }
                if store.things.isEmpty {
                    EmptyNote("Nothing seen yet. Keys, wallet, laptop: wherever the glasses last saw them.")
                } else {
                    VStack(spacing: -Theme.overlap) {
                        ForEach(Array(store.things.enumerated()), id: \.element.id) { i, thing in
                            StackCard(color: Theme.pastels[i % Theme.pastels.count], index: i) {
                                ThingRow(thing: thing, image: store.backend.frameURL(thing.frame), open: open == thing.id, store: store) {
                                    withAnimation(.spring(response: 0.5, dampingFraction: 0.82)) {
                                        open = open == thing.id ? nil : thing.id
                                    }
                                }
                            }
                            .scrollDepth()
                        }
                    }
                }
            }
            .padding(.horizontal, 16)
        }
        .background(Theme.canvas.ignoresSafeArea())
        .refreshable { await store.refreshLists() }
    }
}

private struct ThingRow: View {
    let thing: Thing
    let image: URL?
    let open: Bool
    let store: MemoryStore
    /// Expand/collapse. Lives on the header row only, so tapping Lead me there
    /// below never also collapses the card.
    let onToggle: () -> Void
    @State private var leading = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .center, spacing: 14) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(thing.name.capitalized).font(.headline)
                    Ago(ts: thing.ts)
                }
                Spacer()
                Image(systemName: "chevron.down").font(.footnote.weight(.semibold))
                    .rotationEffect(.degrees(open ? 180 : 0))
                    .frame(width: 34, height: 34).background(Theme.card.opacity(0.5), in: Circle())
                AsyncImage(url: image) { $0.resizable().scaledToFill() } placeholder: {
                    Image(systemName: "eye").foregroundStyle(Theme.inkSoft)
                }
                .frame(width: 48, height: 48)
                .background(Theme.card)
                .clipShape(Circle())
                .overlay(Circle().stroke(Theme.card, lineWidth: 3))
            }
            .contentShape(Rectangle())
            .onTapGesture(perform: onToggle)
            if open {
                if let place = thing.place, !place.isEmpty {
                    Text(place).font(.footnote.weight(.medium))
                        .padding(.horizontal, 12).padding(.vertical, 7)
                        .background(Theme.card.opacity(0.6), in: Capsule())
                }
                AsyncImage(url: image) { $0.resizable().scaledToFit() } placeholder: {
                    RoundedRectangle(cornerRadius: 20).fill(Theme.card.opacity(0.4)).frame(height: 180)
                }
                .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                .transition(.opacity.combined(with: .scale(scale: 0.96, anchor: .top)))
                if thing.frame != nil {
                    Button {
                        leading = true
                        Task {
                            await store.leadTo(thing)
                            leading = false
                        }
                    } label: {
                        HStack(spacing: 6) {
                            if leading { ProgressView().controlSize(.small).tint(Theme.card) }
                            Image(systemName: "location.fill")
                            Text(leading ? "Asking Scene..." : "Lead me there")
                        }
                        .font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity).padding(.vertical, 13)
                        .foregroundStyle(Theme.card)
                        .background(Theme.ink, in: Capsule())
                    }
                    .buttonStyle(.squish)
                    .disabled(leading || store.asking)
                    .accessibilityLabel("Lead me to \(thing.name)")
                } else {
                    Text("No photo of this one, so Scene can't lead you to it.")
                        .font(.footnote).foregroundStyle(Theme.inkSoft)
                }
            } else if let place = thing.place, !place.isEmpty {
                Text(place).font(.footnote).foregroundStyle(Theme.inkSoft).lineLimit(1)
            }
        }
        .foregroundStyle(Theme.ink)
        .contentShape(Rectangle())
    }
}

// MARK: - Intro (kinetic type + star, first launch)

private struct IntroView: View {
    let done: () -> Void

    /// Each word is a thing Scene remembers; tapping it says how.
    private let words: [(word: String, what: String)] = [
        ("Keys", "Glance at them once. Later: \u{201C}Hey Scene, where are my keys?\u{201D}"),
        ("Wallet", "Wherever you set things down: wallet, phone, laptop, headphones."),
        ("Promises", "\u{201C}I\u{2019}ll send it by four\u{201D} becomes a follow-up you approve with a pinch."),
        ("Deadlines", "Due times come from what people actually said, quote and all."),
        ("Receipts", "It reads the total and remembers who owes you what."),
        ("Errands", "What you were asked to do lands in Tasks, and closes when you say it\u{2019}s done."),
        ("Names", "Who asked for what, with their exact words as proof."),
        ("Places", "\u{201C}On the kitchen counter, next to the sink,\u{201D} not just \u{201C}seen.\u{201D}"),
        ("Moments", "Meetings become notes: summary, decisions, action items."),
        ("Remember.", "Scene remembers so you don\u{2019}t have to. Nothing acts without your OK."),
    ]
    @State private var lit = 0
    @State private var picked: Int?          // a word the wearer tapped: shows its line, stops the auto-cycle
    @State private var bloom = false

    private var focus: Int { picked ?? lit }

    var body: some View {
        ZStack(alignment: .topLeading) {
            Theme.action.ignoresSafeArea()

            // The big star blooms in behind the words and sways in place. Not touchable,
            // so it never steals a tap from the words or Get started.
            Star3D(color: Theme.star, depth: 0.16)
                .frame(width: 560, height: 560)
                .scaleEffect(bloom ? 1 : 0.25)
                .rotationEffect(.degrees(bloom ? 0 : -45))
                .position(x: 150, y: 470)
                .allowsHitTesting(false)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: -4) {
                ForEach(Array(words.enumerated()), id: \.offset) { i, w in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(w.word)
                            .font(.system(size: 44, weight: .regular))
                            .tracking(-1.5)
                            .foregroundStyle(Theme.ink.opacity(i == focus ? 1 : 0.22))
                            .scaleEffect(i == focus ? 1.04 : 1, anchor: .leading)
                        if picked == i {
                            Text(w.what)
                                .font(.callout.weight(.medium))
                                .foregroundStyle(Theme.ink.opacity(0.78))
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.bottom, 10)
                                .transition(.opacity.combined(with: .move(edge: .top)))
                        }
                    }
                    .contentShape(Rectangle())
                    .onTapGesture {
                        withAnimation(.spring(response: 0.45, dampingFraction: 0.82)) { picked = picked == i ? nil : i }
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityAddTraits(.isButton)
                    .accessibilityHint(w.what)
                }
            }
            .padding(.horizontal, 22)
            .padding(.top, 30)

            VStack {
                Spacer()
                Text("Tap a word to see what Scene does with it")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(Theme.ink.opacity(0.6))
                    .frame(maxWidth: .infinity)
                    .contentTransition(.opacity)
                Button(action: done) {
                    HStack {
                        Text("Get started").font(.headline)
                        Spacer()
                        Image(systemName: "chevron.right")
                    }
                    .foregroundStyle(Theme.ink)
                    .padding(.horizontal, 28).padding(.vertical, 20)
                    .overlay(Capsule().stroke(Theme.ink, lineWidth: 1.2))
                }
                .buttonStyle(.squish)
                .padding(.horizontal, 22).padding(.bottom, 22).padding(.top, 8)
            }
        }
        .task {
            withAnimation(.spring(response: 1.4, dampingFraction: 0.75)) { bloom = true }
            for i in words.indices {
                try? await Task.sleep(for: .milliseconds(230))
                guard picked == nil else { break }        // the wearer took over
                withAnimation(.easeOut(duration: 0.2)) { lit = i }
            }
        }
    }
}

// MARK: - Checkout (the other person scans this)

private struct CheckoutSheet: View {
    let store: MemoryStore

    var body: some View {
        VStack(spacing: 18) {
            if let p = store.checkout {
                Eyebrow(p.status == "paid" ? "Paid" : "Scan to pay")
                Text(p.amountText).font(Theme.number(60)).foregroundStyle(Theme.ink)
                Text("\(p.memo) · \(p.who)").foregroundStyle(Theme.inkSoft)
                if p.status == "paid" {
                    Star3D(color: Theme.chart).frame(width: 170, height: 170)
                        .transition(.scale.combined(with: .opacity))
                    Text("Thanks, all settled.").font(.title3.weight(.medium))
                } else if let url = p.url, let qr = Self.qr(url) {
                    Image(uiImage: qr)
                        .interpolation(.none).resizable().scaledToFit()
                        .frame(maxWidth: 260).padding(22)
                        .background(Theme.card, in: RoundedRectangle(cornerRadius: 28, style: .continuous))
                    Label("Stripe Checkout · card 4242 4242 4242 4242 in test mode", systemImage: "camera.viewfinder")
                        .font(.caption).foregroundStyle(Theme.inkSoft).multilineTextAlignment(.center)
                    ProgressView("Waiting for payment…").tint(Theme.ink)
                }
                Spacer()
                Button("Close") { store.checkout = nil }
                    .font(.headline).foregroundStyle(Theme.ink)
                    .frame(maxWidth: .infinity).padding(.vertical, 18)
                    .overlay(Capsule().stroke(Theme.ink.opacity(0.25)))
            }
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.butter.ignoresSafeArea())
        .animation(.spring(response: 0.5, dampingFraction: 0.7), value: store.checkout?.status)
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

/// To do with nothing yet: say what it is and how to test it, so the list never
/// looks broken or dead. Shown only when there are no promises AND no open tasks.
private struct TodoEmptyState: View {
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            FourPointStar().fill(Theme.action).frame(width: 22, height: 22)
            VStack(alignment: .leading, spacing: 8) {
                Text("Nothing to do yet").font(.headline).foregroundStyle(Theme.ink)
                Text("When someone asks you to do something, it shows up here for you to approve. Nothing sends until you tap.")
                    .font(.subheadline).foregroundStyle(Theme.inkSoft)
                Text("Try it: say 'I will send Srijan the deck by 4', wait 30s, and it appears here.")
                    .font(.subheadline).foregroundStyle(Theme.ink)
            }
        }
        .padding(22)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card.opacity(0.6), in: RoundedRectangle(cornerRadius: 28, style: .continuous))
    }
}

// MARK: - Lead me there (Things tab + Today)
//
// The offer after tapping Lead on a Thing. Offered only when there is a photo taken in the
// last 3 hours; otherwise the card says plainly why not. Yes shows the photo + place on the
// phone with Stop. Spoken step-by-step guidance still comes from the glasses' voice flow,
// because the backend has no phone endpoint that starts a lead (lead.py starts from a spoken
// yes after a voice question). The card states that one voice step instead of faking it.
private struct LeadCard: View {
    let store: MemoryStore

    var body: some View {
        if let offer = store.leadOffer {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Eyebrow(store.leadStarted ? "Leading you" : "Lead me there")
                    Spacer()
                    Image(systemName: "location.fill").font(.footnote.weight(.semibold)).foregroundStyle(Theme.inkSoft)
                }
                Text(offer.answer).font(.title3.weight(.medium)).foregroundStyle(Theme.ink)
                if let image = store.backend.frameURL(offer.frame) {
                    AsyncImage(url: image) { $0.resizable().scaledToFit() } placeholder: {
                        RoundedRectangle(cornerRadius: 20).fill(Theme.card.opacity(0.4)).frame(height: 180)
                    }
                    .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                }
                if store.leadStarted {
                    Text("Walk slowly. For spoken steps, ask the glasses 'Hey Scene, where are my \(offer.name)?' then say 'Yes' when Scene asks to lead you there.")
                        .font(.subheadline).foregroundStyle(Theme.inkSoft)
                    pill("Stop", filled: true) { store.stopLead() }
                } else if offer.canLead {
                    Text("Want me to lead you there?")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                    HStack(spacing: 10) {
                        pill("Yes, lead me", filled: true) { store.startLead() }
                        pill("Not now", filled: false) { store.dismissLead() }
                    }
                } else {
                    Text(noLeadLine(offer)).font(.subheadline).foregroundStyle(Theme.inkSoft)
                    pill("Got it", filled: false) { store.dismissLead() }
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.mint, in: RoundedRectangle(cornerRadius: 28, style: .continuous))
        }
    }

    /// "I saw it at [place] [time ago], no photo to lead from". Never invents a place.
    private func noLeadLine(_ offer: LeadOffer) -> String {
        if offer.askFailed { return offer.answer }
        if offer.frame == nil {
            if let place = offer.place, !place.isEmpty {
                return "I saw it \(placePhrase(place)) \(offer.ageText), no photo to lead from."
            }
            return "I last saw your \(offer.name) \(offer.ageText), but there's no photo to lead from."
        }
        if let place = offer.place, !place.isEmpty {
            return "I saw it \(placePhrase(place)) \(offer.ageText). Too long ago to lead from, it may have moved."
        }
        return "I last saw your \(offer.name) \(offer.ageText). Too long ago to lead from, it may have moved."
    }

    /// Places already read like "on the kitchen counter", so "at" would double up.
    private func placePhrase(_ place: String) -> String {
        let lower = place.lowercased()
        if lower.hasPrefix("on ") || lower.hasPrefix("in ") || lower.hasPrefix("at ") ||
           lower.hasPrefix("next to ") || lower.hasPrefix("by ") || lower.hasPrefix("under ") {
            return place
        }
        return "at \(place)"
    }

    private func pill(_ title: String, filled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.subheadline.weight(.semibold))
                .frame(maxWidth: .infinity).padding(.vertical, 13)
                .foregroundStyle(filled ? Theme.card : Theme.ink)
                .background(filled ? Theme.ink : .clear, in: Capsule())
                .overlay(Capsule().stroke(Theme.ink.opacity(filled ? 0 : 0.25)))
        }
        .buttonStyle(.squish)
    }
}

// MARK: - Bits

private struct PageTitle: View {
    let title: String
    let subtitle: String
    init(_ title: String, subtitle: String) { self.title = title; self.subtitle = subtitle }
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.largeTitle.weight(.medium)).foregroundStyle(Theme.ink)
            Text(subtitle).font(.subheadline).foregroundStyle(Theme.inkSoft)
        }
        .padding(.top, 12)
    }
}

private struct EmptyNote: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            FourPointStar().fill(Theme.action).frame(width: 22, height: 22)
            Text(text).font(.subheadline).foregroundStyle(Theme.inkSoft)
        }
        .padding(22)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card.opacity(0.6), in: RoundedRectangle(cornerRadius: 28, style: .continuous))
    }
}

private struct Ago: View {
    let ts: Double
    var body: some View {
        Text(Date(timeIntervalSince1970: ts), format: .relative(presentation: .named))
            .font(.caption)
            .foregroundStyle(Theme.inkFaint)
    }
}
