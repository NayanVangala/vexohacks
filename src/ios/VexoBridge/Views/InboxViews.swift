//
//  InboxViews.swift
//  VexoBridge
//
//  Needs you (everything waiting on the wearer, from /inbox + Follow-Through), meeting notes, and
//  today's activity. Same stacked-card language as the rest of the app (Theme.swift). The server
//  describes each item's buttons, so new kinds of approval need no new phone code.
//

import MapKit
import SwiftUI

// MARK: - Needs you tab

struct NeedsYouView: View {
    let store: MemoryStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Needs you").font(.largeTitle.weight(.medium)).foregroundStyle(Theme.ink)
                    Text(store.inbox.isEmpty ? "Nothing waiting on you" : "\(store.inbox.count) waiting on a yes or no")
                        .font(.subheadline).foregroundStyle(Theme.inkSoft)
                }
                .padding(.top, 12)

                if store.inbox.isEmpty {
                    HStack(alignment: .top, spacing: 12) {
                        FourPointStar().fill(Theme.action).frame(width: 22, height: 22)
                        Text("Emails, calendar events, orders and promises Scene drafts for you show up here. Nothing acts until you tap.")
                            .font(.subheadline).foregroundStyle(Theme.inkSoft)
                    }
                    .padding(22)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.card.opacity(0.6), in: RoundedRectangle(cornerRadius: 28, style: .continuous))
                } else {
                    VStack(spacing: -Theme.overlap) {
                        ForEach(Array(store.inbox.enumerated()), id: \.element.id) { i, item in
                            StackCard(color: InboxStyle.color(item.type), index: i) {
                                InboxCard(item: item, store: store)
                            }
                            .scrollDepth()
                            .transition(.move(edge: .trailing).combined(with: .opacity))
                        }
                    }
                    .animation(.spring(response: 0.5, dampingFraction: 0.85), value: store.inbox.map(\.id))
                }
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 24)
        }
        .scrollIndicators(.hidden)
        .background(Theme.canvas.ignoresSafeArea())
        .refreshable { await store.refreshLists() }
    }
}

enum InboxStyle {
    static func color(_ type: String) -> Color {
        switch type {
        case "payment", "order": Theme.butter
        case "email", "slack": Theme.sky
        case "event": Theme.mint
        case "promise", "question": Theme.rose
        default: Theme.sky
        }
    }

    static func label(_ type: String) -> String {
        switch type {
        case "payment": "Money you're owed"
        case "order": "Order"
        case "email": "Email draft"
        case "slack": "Slack message"
        case "event": "Calendar"
        case "promise": "Promise you made"
        case "question": "Scene asks"
        case "offer": "Scene noticed"
        default: type.capitalized
        }
    }
}

/// One waiting item with its own buttons; shared by the Needs you tab and Today's spotlight.
struct InboxCard: View {
    let item: InboxItem
    let store: MemoryStore
    @Environment(\.openURL) private var openURL
    @State private var busy: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Eyebrow(InboxStyle.label(item.type))
                Spacer()
                if item.type != "promise" {   // Follow-Through doesn't say when; don't invent a time
                    Text(Date(timeIntervalSince1970: item.ts), format: .relative(presentation: .named))
                        .font(.caption).foregroundStyle(Theme.inkFaint)
                }
            }
            Text(item.title).font(.title3.weight(.semibold)).foregroundStyle(Theme.ink)
            if !item.body.isEmpty {
                Text(item.body).font(.subheadline).foregroundStyle(Theme.inkSoft).lineLimit(4)
            }
            if !item.actions.isEmpty {
                HStack(spacing: 10) {
                    ForEach(item.actions, id: \.self) { a in
                        Button {
                            busy = a.label
                            Task { await store.perform(a, on: item); busy = nil }
                        } label: {
                            HStack(spacing: 6) {
                                if busy == a.label { ProgressView().controlSize(.small).tint(a.primary ? Theme.card : Theme.ink) }
                                Text(a.label).font(.subheadline.weight(.semibold))
                            }
                            .frame(maxWidth: .infinity).padding(.vertical, 13)
                            .foregroundStyle(a.primary ? Theme.card : Theme.ink)
                            .background(a.primary ? Theme.ink : .clear, in: Capsule())
                            .overlay(Capsule().stroke(Theme.ink.opacity(a.primary ? 0 : 0.25)))
                        }
                        .buttonStyle(.squish)
                        .disabled(busy != nil)
                    }
                }
            }
            if item.type == "order", let o = store.order(item.ref) {
                OrderMap(order: o)
            }
            if let link = item.link, let url = store.backend.absolute(link) {
                Button { openURL(url) } label: {
                    Label("Open payment page", systemImage: "safari").font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity).padding(.vertical, 13)
                        .foregroundStyle(Theme.card).background(Theme.ink, in: Capsule())
                }
                .buttonStyle(.squish)
            }
        }
    }
}

// MARK: - Meeting notes (Today section + detail sheet)

struct NotesSection: View {
    let store: MemoryStore
    @State private var open: NoteSummary?

    var body: some View {
        if !store.notes.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Eyebrow("Meeting notes").padding(.top, 10)
                ForEach(store.notes.prefix(3)) { n in
                    Button { open = n } label: {
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: "note.text").font(.footnote.weight(.semibold))
                                .frame(width: 32, height: 32).background(Theme.mint, in: Circle())
                            VStack(alignment: .leading, spacing: 3) {
                                Text(n.summary).font(.subheadline.weight(.medium)).lineLimit(2).multilineTextAlignment(.leading)
                                Text("\(n.decisions) decisions, \(n.actionItems) action items")
                                    .font(.caption).foregroundStyle(Theme.inkSoft)
                            }
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(Theme.inkFaint)
                        }
                        .foregroundStyle(Theme.ink)
                        .padding(14)
                        .background(Theme.card.opacity(0.7), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                    }
                    .buttonStyle(.squish)
                }
            }
            .sheet(item: $open) { n in NoteSheet(summary: n, store: store) }
        }
    }
}

private struct NoteSheet: View {
    let summary: NoteSummary
    let store: MemoryStore
    @State private var detail: NoteDetail?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Eyebrow("Meeting notes")
                Text(detail?.summary.isEmpty == false ? detail!.summary : summary.summary)
                    .font(.title3.weight(.medium)).foregroundStyle(Theme.ink)
                if let d = detail, !d.decisions.isEmpty {
                    section("Decisions", d.decisions.map { ($0, nil) })
                }
                if let d = detail, !d.actionItems.isEmpty {
                    section("Action items", d.actionItems.map { ($0.text, $0.quote) })
                }
                if detail == nil { ProgressView().tint(Theme.ink) }
            }
            .padding(28)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.mint.ignoresSafeArea())
        .presentationDetents([.medium, .large])
        .task { detail = await store.noteDetail(summary.id) }
    }

    private func section(_ title: String, _ rows: [(String, String?)]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Eyebrow(title)
            ForEach(Array(rows.enumerated()), id: \.offset) { _, r in
                VStack(alignment: .leading, spacing: 3) {
                    Text(r.0).font(.body.weight(.medium)).foregroundStyle(Theme.ink)
                    if let q = r.1 { Text("\u{201C}\(q)\u{201D}").font(.footnote.italic()).foregroundStyle(Theme.inkSoft) }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.card.opacity(0.75), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
        }
    }
}

// MARK: - Activity (Today section)

struct ActivitySection: View {
    let store: MemoryStore

    var body: some View {
        if !store.activity.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Eyebrow("Today's activity").padding(.bottom, 8).padding(.top, 10)
                ForEach(store.activity.prefix(15)) { a in
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: icon(a)).font(.footnote.weight(.semibold))
                            .frame(width: 30, height: 30).background(Theme.card, in: Circle())
                        Text(a.text).font(.subheadline).foregroundStyle(Theme.ink).lineLimit(2)
                        Spacer(minLength: 0)
                        Text(Date(timeIntervalSince1970: a.ts), format: .relative(presentation: .named))
                            .font(.caption).foregroundStyle(Theme.inkFaint)
                    }
                    .padding(.vertical, 7)
                }
            }
        }
    }

    private func icon(_ a: ActivityItem) -> String {
        switch a.kind {
        case "task": a.status == "done" ? "checkmark.circle" : "checklist"
        case "notes": "note.text"
        default:
            a.text.hasPrefix("Paid") || a.text.hasPrefix("Approved") ? "checkmark.seal" :
            a.text.hasPrefix("Receipt") ? "receipt" : "eye"
        }
    }
}

// MARK: - Follow-through (every promise Follow-Through found, and what happened to it)

struct PromisesSection: View {
    let store: MemoryStore

    var body: some View {
        if !store.promises.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Eyebrow("Promises you made")
                VStack(spacing: -Theme.overlap) {
                    ForEach(Array(store.promises.enumerated()), id: \.element.id) { i, p in
                        StackCard(color: color(p), index: i) { PromiseCard(promise: p, store: store) }
                            .scrollDepth()
                    }
                }
                .animation(.spring(response: 0.5, dampingFraction: 0.85), value: store.promises)
            }
        }
    }

    private func color(_ p: Promise) -> Color {
        switch p.status {
        case "done", "drafted": Theme.mint
        case "failed", "needs_review": Theme.butter
        default: Theme.rose
        }
    }
}

private struct PromiseCard: View {
    let promise: Promise
    let store: MemoryStore
    @Environment(\.openURL) private var openURL
    @State private var busy: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Eyebrow(state)
                Spacer()
                Image(systemName: icon).font(.footnote.weight(.semibold)).foregroundStyle(Theme.inkSoft)
            }
            Text(promise.title).font(.title3.weight(.semibold)).foregroundStyle(Theme.ink)
            if let meta { Text(meta).font(.footnote).foregroundStyle(Theme.inkSoft) }
            if let q = promise.quote, !q.isEmpty {
                Text("\u{201C}\(q)\u{201D}").font(.subheadline.italic()).foregroundStyle(Theme.inkSoft).lineLimit(3)
            }
            if promise.status == "needs_review", let issues = promise.issues, !issues.isEmpty {
                Label(issues.joined(separator: "; ").capitalizedFirst, systemImage: "exclamationmark.circle")
                    .font(.footnote).foregroundStyle(Theme.ink)
            }
            if let r = promise.result, !r.isEmpty, ["done", "drafted", "failed"].contains(promise.status) {
                Text(r).font(.footnote).foregroundStyle(Theme.ink.opacity(0.75))
            }
            HStack(spacing: 10) {
                switch promise.status {
                case "pending":
                    button("Approve", "approve", primary: true)
                    button("Not now", "dismiss")
                case "needs_review":
                    button("Not now", "dismiss")
                case "failed":
                    button("Try again", "retry", primary: true)
                default:
                    EmptyView()
                }
            }
            if let link = promise.link, let url = URL(string: link), promise.status == "drafted" {
                Button { openURL(url) } label: {
                    Label("Open draft", systemImage: "arrow.up.right").font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.ink)
                }
                .buttonStyle(.squish)
            }
        }
    }

    private var state: String {
        switch promise.status {
        case "pending": "Waiting on your yes"
        case "needs_review": "Needs a look"
        case "done": "Done"
        case "drafted": "Drafted"
        case "failed": "Didn\u{2019}t go through"
        default: promise.status
        }
    }

    private var icon: String {
        switch promise.action {
        case "email", "reply": "envelope"
        case "calendar": "calendar"
        default: "checklist"
        }
    }

    /// "To Sam · Tue 4:00 PM"
    private var meta: String? {
        var bits: [String] = []
        if let c = promise.counterpart, !c.isEmpty { bits.append("With \(c)") }
        if let d = promise.dueLocal, !d.isEmpty {
            let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd'T'HH:mm"
            bits.append(f.date(from: d).map { $0.formatted(.dateTime.weekday(.abbreviated).hour().minute()) } ?? d)
        }
        return bits.isEmpty ? nil : bits.joined(separator: ", ")
    }

    private func button(_ label: String, _ verb: String, primary: Bool = false) -> some View {
        Button {
            busy = verb
            Task { await store.promise(promise.id, verb); busy = nil }
        } label: {
            HStack(spacing: 6) {
                if busy == verb { ProgressView().controlSize(.small).tint(primary ? Theme.card : Theme.ink) }
                Text(label).font(.subheadline.weight(.semibold))
            }
            .frame(maxWidth: .infinity).padding(.vertical, 13)
            .foregroundStyle(primary ? Theme.card : Theme.ink)
            .background(primary ? Theme.ink : .clear, in: Capsule())
            .overlay(Capsule().stroke(Theme.ink.opacity(primary ? 0 : 0.25)))
        }
        .buttonStyle(.squish)
        .disabled(busy != nil)
    }
}

private extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}

// MARK: - Orders: where to pick it up, on a map

/// A small map of the pickup address with the kitchen's stage. Tap to open Apple Maps. Hidden when the
/// address can't be found (the demo kitchen is simulated).
struct OrderMap: View {
    let order: OrderInfo
    @State private var place: CLLocationCoordinate2D?
    private static var cache: [String: CLLocationCoordinate2D] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let place {
                Button { open(place) } label: {
                    Map(initialPosition: .region(MKCoordinateRegion(center: place, latitudinalMeters: 700, longitudinalMeters: 700)),
                        interactionModes: []) {
                        Marker(order.placeName, coordinate: place).tint(Theme.action)
                    }
                    .frame(height: 130)
                    .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                    .overlay(alignment: .bottomTrailing) {
                        Label("Open in Maps", systemImage: "arrow.up.right").font(.caption.weight(.semibold))
                            .padding(.horizontal, 10).padding(.vertical, 6)
                            .background(.regularMaterial, in: Capsule()).padding(8)
                    }
                }
                .buttonStyle(.squish)
                .accessibilityLabel("Open \(order.placeName) in Maps")
            }
            Text("\(stage) · \(order.address)").font(.footnote).foregroundStyle(Theme.inkSoft).lineLimit(2)
        }
        .task(id: order.address) { place = await Self.locate(order) }
    }

    private var stage: String {
        switch order.stage {
        case "awaiting_approval": "Waiting on your OK"
        case "awaiting_payment": "Waiting on payment"
        case "received": "Order received"
        case "preparing": "Being made"
        case "ready": "Ready for pickup"
        default: order.stage.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private static func locate(_ o: OrderInfo) async -> CLLocationCoordinate2D? {
        if let c = cache[o.address] { return c }
        let q = o.address.localizedCaseInsensitiveContains(o.placeName) ? o.address : "\(o.placeName), \(o.address)"
        guard let hit = try? await CLGeocoder().geocodeAddressString(q).first?.location?.coordinate else { return nil }
        cache[o.address] = hit
        return hit
    }

    private func open(_ c: CLLocationCoordinate2D) {
        let item = MKMapItem(placemark: MKPlacemark(coordinate: c))
        item.name = order.placeName
        item.openInMaps(launchOptions: [MKLaunchOptionsDirectionsModeKey: MKLaunchOptionsDirectionsModeDriving])
    }
}

/// Orders already paid: kitchen stage and pickup map, until you collect it.
struct OrdersSection: View {
    let store: MemoryStore
    private var live: [OrderInfo] { store.orders.filter { ["received", "preparing", "ready"].contains($0.stage) } }

    var body: some View {
        if !live.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Eyebrow("Your orders").padding(.top, 10)
                ForEach(live) { o in
                    VStack(alignment: .leading, spacing: 10) {
                        Text(o.placeName).font(.title3.weight(.semibold)).foregroundStyle(Theme.ink)
                        OrderMap(order: o)
                    }
                    .padding(20)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.butter, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
                }
            }
        }
    }
}

// MARK: - Contacts (who "email Maya" or "Slack Sam" can reach)

struct ContactsSection: View {
    let store: MemoryStore
    @State private var book: [String: String] = [:]
    @State private var adding = false
    @State private var name = ""
    @State private var email = ""
    @State private var slack = ""
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Eyebrow("Contacts").padding(.top, 10)
            VStack(alignment: .leading, spacing: 14) {
                Text("Scene only ever emails or Slacks people in this list, and nothing sends until you tap.")
                    .font(.caption).foregroundStyle(Theme.inkSoft)
                ForEach(book.keys.sorted(), id: \.self) { n in
                    HStack {
                        Text(n).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                        Spacer()
                        Text(book[n] ?? "").font(.caption).foregroundStyle(Theme.inkSoft).lineLimit(1)
                    }
                }
                if book.isEmpty { Text("No contacts yet.").font(.subheadline).foregroundStyle(Theme.inkSoft) }
                if adding {
                    field("Name", $name)
                    field("Email", $email, keyboard: .emailAddress)
                    field("Slack member ID (optional, U012ABCDEF)", $slack)
                    if let error { Text(error).font(.caption).foregroundStyle(Theme.alert) }
                    HStack(spacing: 10) {
                        Button("Cancel") { adding = false; error = nil }
                            .frame(maxWidth: .infinity).padding(.vertical, 12)
                            .overlay(Capsule().stroke(Theme.ink.opacity(0.25)))
                        Button("Save") { Task { await save() } }
                            .frame(maxWidth: .infinity).padding(.vertical, 12)
                            .foregroundStyle(Theme.card).background(Theme.ink, in: Capsule())
                    }
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink).buttonStyle(.squish)
                } else {
                    Button { adding = true } label: {
                        Label("Add contact", systemImage: "plus").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                    }
                    .buttonStyle(.squish)
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
        }
        .task { await load() }
        .animation(.spring(response: 0.4, dampingFraction: 0.85), value: adding)
    }

    private func field(_ title: String, _ text: Binding<String>, keyboard: UIKeyboardType = .default) -> some View {
        TextField(title, text: text)
            .keyboardType(keyboard).textInputAutocapitalization(keyboard == .default ? .words : .never).autocorrectionDisabled()
            .padding(14).foregroundStyle(Theme.ink)
            .background(Theme.canvas, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private func load() async { if let b: [String: String] = try? await store.backend.get("/contacts") { book = b } }

    private func save() async {
        var form = ["name": name, "email": email]
        if !slack.isEmpty { form["slack"] = slack }
        do {
            let _: OKReply = try await store.backend.post("/contacts", form: form)
            (name, email, slack, error, adding) = ("", "", "", nil, false)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
