//
//  HomeView.swift
//  VexoBridge
//
//  The phone app. Visual language: a warm canvas, big pastel cards that stack
//  and overlap like a deck, oversized numbers, tiny letter-spaced labels, and
//  one coral action. Colors and shapes live in Theme.swift.
//
//  Tabs: Today (numbers, what needs you, start/stop), Tasks, Things, Glasses
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
                .tabItem { Label("Follow-through", systemImage: "checklist") }
                .badge(store.tasks.count + store.promises.filter { $0.status == "pending" || $0.status == "needs_review" }.count)
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

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                askPill
                if let a = store.lastAnswer {
                    AnswerBubble(answer: a, image: controller.backend.frameURL(a.frame))
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
                VStack(spacing: -Theme.overlap) {
                    StackCard(color: Theme.card, index: 0) { TodayCard(today: store.today) }.scrollDepth()
                    StackCard(color: Theme.butter, index: 1) { NeedsYouCard(store: store) }.scrollDepth()
                    captureButton.rise(2)
                }
                .padding(.top, 6)
                NotesSection(store: store)
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

    /// The coral action, tucked under the stack like the reference's "Start Focus Session".
    private var captureButton: some View {
        Button {
            busy = true
            Task {
                if bridge.isRunning { await controller.stop() } else { await controller.start() }
                busy = false
            }
        } label: {
            VStack(spacing: 4) {
                Text(bridge.isRunning ? "Stop remembering" : "Start remembering")
                    .font(.title3.weight(.medium))
                Text(captureSubtitle).font(.footnote).opacity(0.75)
            }
            .foregroundStyle(Theme.ink)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 22)
            .background(Theme.action, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
            .overlay {
                if busy { ProgressView().tint(Theme.ink).frame(maxWidth: .infinity, alignment: .trailing).padding(.trailing, 28) }
            }
        }
        .buttonStyle(.squish)
        .disabled(busy)
        .sensoryFeedback(.impact, trigger: bridge.isRunning)
    }

    private var captureSubtitle: String {
        if bridge.isRunning {
            if let last = bridge.lastFrameAt { return "Glasses streaming · last frame \(last)" }
            return "Waiting for the first frame…"
        }
        return "Glasses \(bridge.connection.rawValue.lowercased())"
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

            HStack(alignment: .lastTextBaseline, spacing: 26) {
                Stat(value: today?.memories ?? 0, label: "Memories", size: 46)
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
                HStack(alignment: .center, spacing: 10) {
                    Text("\(store.tasks.count)").font(Theme.number(44)).foregroundStyle(Theme.ink)
                        .contentTransition(.numericText(value: Double(store.tasks.count)))
                    star
                    Spacer()
                    recentThings
                }
                Text(store.tasks.isEmpty ? "Nothing waiting. Say \u{201C}hey scene\u{201D} to ask." : "open task\(store.tasks.count == 1 ? "" : "s") · nothing to approve")
                    .foregroundStyle(Theme.inkSoft)
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

    /// Overlapping circles of the last few things seen, like the reference's app-icon row.
    private var recentThings: some View {
        HStack(spacing: -10) {
            ForEach(store.things.prefix(4)) { t in
                AsyncImage(url: store.backend.frameURL(t.frame)) { $0.resizable().scaledToFill() } placeholder: {
                    Text(t.name.prefix(1).uppercased()).font(.caption.weight(.semibold))
                }
                .frame(width: 34, height: 34)
                .background(Theme.card)
                .clipShape(Circle())
                .overlay(Circle().stroke(Theme.card, lineWidth: 2.5))
            }
        }
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
            Text("\(shown)").font(Theme.number(size)).foregroundStyle(Theme.ink)
                .contentTransition(.numericText(value: Double(shown)))
            Text(label).font(.caption).foregroundStyle(Theme.inkSoft)
        }
        .onAppear { countTo(value, delay: 0.4) }
        .onChange(of: value) { _, v in countTo(v, delay: 0) }
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
                PageTitle("Follow-through", subtitle: subtitle)
                PromisesSection(store: store)
                if !store.promises.isEmpty { Eyebrow("To-dos").padding(.top, 6) }
                if store.tasks.isEmpty {
                    EmptyNote("Nothing open. When someone asks you to do something, it lands here.")
                } else {
                    VStack(spacing: -Theme.overlap) {
                        ForEach(Array(store.tasks.enumerated()), id: \.element.id) { i, task in
                            StackCard(color: Theme.pastels[i % Theme.pastels.count], index: i) {
                                TaskRow(task: task) { Task { await store.complete(task.id) } }
                            }
                            .scrollDepth()
                            .transition(.move(edge: .trailing).combined(with: .opacity))
                        }
                    }
                    .animation(.spring(response: 0.5, dampingFraction: 0.85), value: store.tasks)
                }
            }
            .padding(.horizontal, 16)
        }
        .background(Theme.canvas.ignoresSafeArea())
        .refreshable { await store.refreshLists() }
    }

    private var subtitle: String {
        let waiting = store.promises.filter { $0.status == "pending" || $0.status == "needs_review" }.count
        return "\(waiting) promise\(waiting == 1 ? "" : "s") waiting, \(store.tasks.count) to-do\(store.tasks.count == 1 ? "" : "s")"
    }
}

private struct TaskRow: View {
    let task: OpenTask
    let done: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Text(parts.title).font(.headline).foregroundStyle(Theme.ink)
                HStack(spacing: 6) {
                    if let meta = parts.meta { Text(meta) }
                    Ago(ts: task.ts)
                }
                .font(.footnote).foregroundStyle(Theme.inkSoft)
            }
            Spacer()
            Button(action: done) {
                Image(systemName: "checkmark").font(.subheadline.weight(.bold)).foregroundStyle(Theme.ink)
                    .frame(width: 48, height: 48).background(Theme.card, in: Circle())
            }
            .buttonStyle(.squish)
            .accessibilityLabel("Mark done")
            .sensoryFeedback(.success, trigger: task.id)
        }
    }

    /// "Send the deck (from Sam, due 5pm)" -> title + "from Sam, due 5pm".
    private var parts: (title: String, meta: String?) {
        guard task.text.hasSuffix(")"), let r = task.text.range(of: " (", options: .backwards) else { return (task.text, nil) }
        return (String(task.text[..<r.lowerBound]), String(task.text[r.upperBound...].dropLast()))
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
                if store.things.isEmpty {
                    EmptyNote("Nothing seen yet. Keys, wallet, laptop: wherever the glasses last saw them.")
                } else {
                    VStack(spacing: -Theme.overlap) {
                        ForEach(Array(store.things.enumerated()), id: \.element.id) { i, thing in
                            StackCard(color: Theme.pastels[i % Theme.pastels.count], index: i) {
                                ThingRow(thing: thing, image: store.backend.frameURL(thing.frame), open: open == thing.id)
                            }
                            .scrollDepth()
                            .onTapGesture {
                                withAnimation(.spring(response: 0.5, dampingFraction: 0.82)) {
                                    open = open == thing.id ? nil : thing.id
                                }
                            }
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
