//
//  VexoAPI.swift
//  VexoBridge
//
//  The memory side of the backend (receiver/server.py): feed, tasks, things,
//  payments, ask. MemoryStore polls it and is the single source of truth for
//  both screens: the phone app and the glasses display.
//

import Foundation
import Observation

// MARK: - Wire types (server JSON is snake_case; decoder converts)

struct Card: Decodable, Identifiable, Equatable, Sendable {
    let id: Int
    let kind: String          // answer | task | payment
    let title: String
    let body: String
    let ref: Int?             // task id or payment id
    let frame: String?
    let ts: Double
}

struct OpenTask: Decodable, Identifiable, Equatable, Sendable {
    let id: Int
    let text: String
    let ts: Double
}

struct Thing: Decodable, Identifiable, Equatable, Sendable {
    let id: Int
    let name: String
    let place: String?
    let frame: String?
    let ts: Double
}

struct Payment: Decodable, Identifiable, Equatable, Sendable {
    let id: Int
    let counterparty: String
    let amountCents: Int
    let memo: String
    let status: String        // proposed | approved | paid | rejected
    let url: String?
    let ts: Double

    var amountText: String { String(format: "$%.2f", Double(amountCents) / 100) }
    var who: String { counterparty.isEmpty ? "someone" : counterparty }
}

struct Today: Decodable, Equatable, Sendable {
    let memories: Int
    let openTasks: Int
    let things: Int
    let hourly: [Int]      // 24 buckets, laptop's local time
    let nowHour: Int
}

struct Utterance: Decodable, Sendable {
    let id: Int
    let text: String
    let ts: Double
    let kind: String?     // "chime": play the local "I heard you" ping, nothing to download
}

struct AskResult: Decodable, Sendable {
    let question: String
    let answer: String
    let frame: String?
}

// MARK: - Endpoints

extension BackendClient {

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    func get<T: Decodable>(_ path: String) async throws -> T {
        let (data, response) = try await URLSession.shared.data(from: try endpoint(path))
        try Self.ensureOK(response, data: data, path: path)
        return try Self.decoder.decode(T.self, from: data)
    }

    /// POST with an optional urlencoded form (FastAPI `Form(...)` accepts it).
    @discardableResult
    func post<T: Decodable>(_ path: String, form: [String: String] = [:]) async throws -> T {
        var req = URLRequest(url: try endpoint(path))
        req.httpMethod = "POST"
        req.timeoutInterval = 30   // /ask waits on a model
        if !form.isEmpty {
            var c = URLComponents()
            c.queryItems = form.map { URLQueryItem(name: $0.key, value: $0.value) }
            req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            req.httpBody = c.percentEncodedQuery?.data(using: .utf8)
        }
        let (data, response) = try await URLSession.shared.data(for: req)
        try Self.ensureOK(response, data: data, path: path)
        return try Self.decoder.decode(T.self, from: data)
    }

    /// Frames are served by the laptop over plain HTTP; the phone may load them
    /// (NSAllowsLocalNetworking), the glasses may not (they need HTTPS).
    func frameURL(_ frame: String?) -> URL? {
        guard let frame else { return nil }
        return URL(string: "\(baseURL)/frames/\(frame)")
    }

    private func endpoint(_ path: String) throws -> URL {
        guard let u = URL(string: baseURL + path) else { throw BackendError.badURL(baseURL + path) }
        return u
    }

    private static func ensureOK(_ response: URLResponse, data: Data, path: String) throws {
        guard let http = response as? HTTPURLResponse else { throw BackendError.notHTTP }
        guard (200..<300).contains(http.statusCode) else {
            struct Detail: Decodable { let detail: String }
            if let d = try? JSONDecoder().decode(Detail.self, from: data) { throw BackendError.server(d.detail) }
            throw BackendError.httpStatus(http.statusCode, path: path)
        }
    }
}

/// For endpoints whose response body we don't need.
struct Ignored: Decodable {}

// MARK: - Needs you, notes, activity (Srijan's backend: /inbox, /notes, /activity)

/// One button on a waiting item: the server says exactly which call it makes.
struct InboxAction: Decodable, Hashable, Sendable {
    let label: String
    let method: String
    let path: String
    let primary: Bool
    let json: [String: String]?
}

/// Anything waiting on you: email, calendar event, order, notes offer, the agent's question, a payment,
/// or a Follow-Through promise. Ids are numbers from Scene and strings from Follow-Through.
struct InboxItem: Decodable, Identifiable, Sendable {
    let type: String
    let ref: String
    let title: String
    let body: String
    let ts: Double
    let actions: [InboxAction]
    let link: String?
    var id: String { "\(type)-\(ref)" }

    enum CodingKeys: String, CodingKey { case type, id, title, body, ts, actions, link }

    init(type: String, ref: String, title: String, body: String, ts: Double, actions: [InboxAction], link: String? = nil) {
        (self.type, self.ref, self.title, self.body, self.ts, self.actions, self.link) = (type, ref, title, body, ts, actions, link)
    }

    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        ref = (try? c.decode(String.self, forKey: .id)) ?? (try? c.decode(Int.self, forKey: .id)).map(String.init) ?? "?"
        title = try c.decode(String.self, forKey: .title)
        body = (try? c.decode(String.self, forKey: .body)) ?? ""
        ts = (try? c.decode(Double.self, forKey: .ts)) ?? 0
        actions = (try? c.decode([InboxAction].self, forKey: .actions)) ?? []
        link = try? c.decode(String.self, forKey: .link)
    }
}

struct NoteSummary: Decodable, Identifiable, Sendable {
    let id: Int
    let summary: String
    let ts: Double
    let decisions: Int
    let actionItems: Int
    let slides: Int
}

struct NoteDetail: Decodable, Sendable {
    struct Item: Decodable, Sendable { let text: String; let quote: String? }
    let summary: String
    let decisions: [String]
    let actionItems: [Item]

    enum CodingKeys: String, CodingKey { case summary, decisions, actionItems }
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        summary = (try? c.decode(String.self, forKey: .summary)) ?? ""
        decisions = (try? c.decode([String].self, forKey: .decisions)) ?? []
        actionItems = (try? c.decode([Item].self, forKey: .actionItems)) ?? []
    }
}

struct ActivityItem: Decodable, Identifiable, Sendable {
    let id: Int
    let kind: String
    let text: String
    let status: String?
    let ts: Double
}

/// Follow-Through's state, just the parts the phone shows.
/// An order from the dining agent, any stage (its /orders list): where it is and where to pick it up.
struct OrderInfo: Decodable, Identifiable, Equatable, Sendable {
    let id: Int
    let placeName: String
    let address: String
    let status: String          // proposed | approved | paid | rejected
    let stage: String           // awaiting_approval | awaiting_payment | received | preparing | ready | rejected
    let totalCents: Int
}

struct OKReply: Decodable { let ok: Bool? }

/// A commitment Follow-Through found in the conversation, in any state (its /api/state items).
struct Promise: Decodable, Identifiable, Equatable {
    let id: String
    let title: String
    let quote: String?
    let status: String          // pending | needs_review | done | drafted | failed | dismissed
    let action: String?         // email | reply | calendar | todo
    let counterpart: String?
    let dueLocal: String?       // "YYYY-MM-DDTHH:MM", local time
    let result: String?
    let issues: [String]?
    let link: String?
}

private struct FTState: Decodable { let items: [Promise] }

extension BackendClient {
    /// Run an inbox action exactly as the server described it (JSON body when it gave one).
    func perform(_ a: InboxAction) async throws -> Data {
        guard let url = URL(string: baseURL + a.path) else { throw BackendError.badURL(baseURL + a.path) }
        var req = URLRequest(url: url)
        req.httpMethod = a.method
        req.timeoutInterval = 30
        if let json = a.json {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw BackendError.notHTTP }
        guard (200..<300).contains(http.statusCode) else {
            struct Detail: Decodable { let detail: String }
            if let d = try? JSONDecoder().decode(Detail.self, from: data) { throw BackendError.server(d.detail) }
            throw BackendError.httpStatus(http.statusCode, path: a.path)
        }
        return data
    }

    /// The server gives links as absolute URLs when PUBLIC_BASE_URL is set, else as paths.
    func absolute(_ link: String) -> URL? {
        URL(string: link.hasPrefix("http") ? link : baseURL + link)
    }
}

// MARK: - Store

@Observable
@MainActor
final class MemoryStore {

    static let pollInterval: Duration = .seconds(2)

    let backend: BackendClient

    private(set) var cards: [Card] = []          // newest first
    private(set) var tasks: [OpenTask] = []
    private(set) var things: [Thing] = []
    private(set) var payments: [Payment] = []
    private(set) var today: Today?
    private(set) var inbox: [InboxItem] = []       // Needs you: Scene's /inbox + Follow-Through's pending promises
    private(set) var notes: [NoteSummary] = []
    private(set) var activity: [ActivityItem] = []
    private(set) var orders: [OrderInfo] = []
    private(set) var promises: [Promise] = []      // Follow-through tab: every promise, any state
    private(set) var online = false
    private(set) var lastAnswer: AskResult?
    private(set) var asking = false

    /// The approved payment whose QR the other person is scanning. Set on
    /// approve (from the phone OR a pinch on the glasses), polled until paid.
    var checkout: Payment?

    /// Shown as a banner: what failed and what to do about it.
    var notice: String?

    /// Called once per genuinely new card, so the glasses can show it.
    var onNewCard: ((Card) -> Void)?

    /// Called with each new spoken line's audio (ElevenLabs MP3), to play on the glasses' speakers.
    var onSpeech: ((Data) -> Void)?
    /// Called the moment the laptop recognizes "hey scene".
    var onChime: (() -> Void)?
    private var lastSpeech = -1   // -1: first poll only catches up, never replays old lines

    private var lastCardId = 0
    private var failures = 0
    private var primed = false
    private var pollTask: Task<Void, Never>?
    private var speechTask: Task<Void, Never>?
    private var fetchingSpeech = false

    init(backend: BackendClient) {
        self.backend = backend
    }

    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(for: Self.pollInterval)
            }
        }
        // Speech gets its own fast loop: an answer shouldn't wait behind the lists' refresh.
        speechTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.playNewSpeech()
                try? await Task.sleep(for: .milliseconds(500))
            }
        }
    }

    private func tick() async {
        do {
            let new: [Card] = try await backend.get("/feed?after=\(lastCardId)")
            online = true
            failures = 0
            if !new.isEmpty {
                lastCardId = new.map(\.id).max() ?? lastCardId
                cards.insert(contentsOf: new.reversed(), at: 0)
                cards = Array(cards.prefix(50))
                // First poll after launch returns the backlog: show only the
                // newest on the glasses, not a burst of stale cards.
                if primed { new.forEach { onNewCard?($0) } } else if let last = new.last { onNewCard?(last) }
            }
            primed = true
            await refreshLists()
            if let c = checkout, c.status == "approved" {
                checkout = try await backend.get("/payments/\(c.id)")
            }
        } catch {
            online = false
            failures += 1
            // Server started after the app, or the laptop changed IP: look for it
            // over Bonjour every ~10 s instead of waiting for someone to type a URL.
            if failures % 5 == 0, let found = await Discovery.findReceiver(timeout: .seconds(3)),
               found != backend.baseURL {
                backend.setBaseURL(found)
                BridgeState.log("found receiver via Bonjour at \(found)")
            }
        }
    }

    private func playNewSpeech() async {
        guard !fetchingSpeech else { return }
        fetchingSpeech = true
        defer { fetchingSpeech = false }
        guard let lines: [Utterance] = try? await backend.get("/speech?after=\(max(lastSpeech, 0))") else { return }
        let catchingUp = lastSpeech < 0
        lastSpeech = max(lastSpeech, lines.map(\.id).max() ?? 0, 0)
        guard !catchingUp else { return }
        for line in lines where Date().timeIntervalSince1970 - line.ts < 30 {   // stale lines aren't worth saying
            if line.kind == "chime" {
                if Date().timeIntervalSince1970 - line.ts < 5 { onChime?() }   // a late ping is just confusing
                continue
            }
            guard let url = URL(string: "\(backend.baseURL)/speech/\(line.id)"),
                  let (data, response) = try? await URLSession.shared.data(from: url),
                  (response as? HTTPURLResponse)?.statusCode == 200 else { continue }
            onSpeech?(data)
        }
    }

    func refreshLists() async {
        async let t: [OpenTask] = backend.get("/tasks")
        async let i: [Thing] = backend.get("/items")
        async let p: [Payment] = backend.get("/payments")
        async let d: Today = backend.get("/today")
        if let t = try? await t { tasks = t }
        if let i = try? await i { things = i }
        if let p = try? await p { payments = p }
        if let d = try? await d, d != today { today = d }
        await refreshInbox()
    }

    func refreshInbox() async {
        async let ib: [InboxItem] = backend.get("/inbox")
        async let n: [NoteSummary] = backend.get("/notes?limit=10")
        async let a: [ActivityItem] = backend.get("/activity?limit=30")
        async let ft: FTState = backend.get("/ft/api/state")
        async let od: [OrderInfo] = backend.get("/orders?limit=10")
        var items = (try? await ib) ?? inbox.filter { $0.type != "promise" }
        if let ft = try? await ft {   // Follow-Through is optional: missing just means no promises
            promises = ft.items.filter { $0.status != "dismissed" }
            let now = Date().timeIntervalSince1970
            items += ft.items.filter { $0.status == "pending" }.map { i in
                InboxItem(type: "promise", ref: i.id, title: i.title, body: i.quote.map { "\u{201C}\($0)\u{201D}" } ?? "",
                          ts: now - 1, actions: [
                            InboxAction(label: "Approve", method: "POST", path: "/ft/api/items/\(i.id)/approve", primary: true, json: nil),
                            InboxAction(label: "Not now", method: "POST", path: "/ft/api/items/\(i.id)/dismiss", primary: false, json: nil)])
            }
        }
        inbox = items
        if let n = try? await n { notes = n }
        if let a = try? await a { activity = a }
        if let od = try? await od { orders = od }
    }

    func order(_ ref: String) -> OrderInfo? { orders.first { String($0.id) == ref } }

    /// Follow-through tab buttons: approve | dismiss | retry, straight to Follow-Through.
    func promise(_ id: String, _ verb: String) async {
        do {
            _ = try await backend.perform(InboxAction(label: verb, method: "POST", path: "/ft/api/items/\(id)/\(verb)",
                                                      primary: false, json: nil))
            BridgeState.log("FOLLOW-THROUGH \(id): \(verb)")
        } catch {
            BridgeState.logError("follow-through \(verb)", error)
            notice = "Couldn\u{2019}t \(verb) that promise: \(error.localizedDescription)"
        }
        await refreshInbox()
    }

    /// One tap on an inbox button. Payments go through `approve` so the QR sheet opens.
    func perform(_ action: InboxAction, on item: InboxItem) async {
        if item.type == "payment", action.primary, let id = Int(item.ref) {
            await approve(id)
            return
        }
        do {
            _ = try await backend.perform(action)
            BridgeState.log("NEEDS YOU \(item.type) #\(item.ref): \(action.label)")
        } catch {
            BridgeState.logError("inbox action", error)
            notice = "Couldn\u{2019}t \(action.label.lowercased()): \(error.localizedDescription)"
        }
        await refreshLists()
    }

    func noteDetail(_ id: Int) async -> NoteDetail? {
        try? await backend.get("/notes/\(id)")
    }

    func payment(_ id: Int?) -> Payment? {
        guard let id else { return nil }
        return payments.first { $0.id == id } ?? (checkout?.id == id ? checkout : nil)
    }

    // MARK: - Actions (the human gate: every one of these is a tap or a pinch)

    func approve(_ paymentId: Int) async {
        do {
            let p: Payment = try await backend.post("/payments/\(paymentId)/approve")
            checkout = p
            BridgeState.log("PAY approved #\(paymentId)")
        } catch {
            BridgeState.logError("approve payment", error)
            let why = error.localizedDescription
            notice = why.contains("test-mode") || why.contains("STRIPE")
                ? "Payments aren't set up yet. Add a Stripe test key (sk_test_…) to receiver/.env on the laptop, then restart it."
                : "Couldn't request the money: \(why)"
        }
        await refreshLists()
    }

    func reject(_ paymentId: Int) async {
        do { let _: Ignored = try await backend.post("/payments/\(paymentId)/reject") }
        catch { BridgeState.logError("reject payment", error); notice = "Couldn't dismiss that request. Is the laptop reachable?" }
        await refreshLists()
    }

    func complete(_ taskId: Int) async {
        do { let _: Ignored = try await backend.post("/tasks/\(taskId)/done") }
        catch { BridgeState.logError("task done", error); notice = "Couldn't mark that done. Is the laptop reachable?" }
        await refreshLists()
    }

    func ask(_ question: String) async {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        asking = true
        defer { asking = false }
        do { lastAnswer = try await backend.post("/ask", form: ["text": q]) }
        catch { BridgeState.logError("ask", error); notice = "Couldn't reach your memory. Check the laptop address in the Glasses tab." }
    }
}
