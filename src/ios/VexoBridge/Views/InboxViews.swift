//
//  InboxViews.swift
//  VexoBridge
//
//  Needs you (everything waiting on the wearer, from /inbox + Follow-Through), meeting notes, and
//  today's activity. Same stacked-card language as the rest of the app (Theme.swift). The server
//  describes each item's buttons, so new kinds of approval need no new phone code.
//

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
        case "email": Theme.sky
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
