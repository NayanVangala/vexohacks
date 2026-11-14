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
        try Self.ensureOK(response, path: path)
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
        try Self.ensureOK(response, path: path)
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

    private static func ensureOK(_ response: URLResponse, path: String) throws {
        guard let http = response as? HTTPURLResponse else { throw BackendError.notHTTP }
        guard (200..<300).contains(http.statusCode) else { throw BackendError.httpStatus(http.statusCode, path: path) }
    }
}

/// For endpoints whose response body we don't need.
struct Ignored: Decodable {}

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
    private(set) var online = false
    private(set) var lastAnswer: AskResult?
    private(set) var asking = false

    /// The approved payment whose QR the other person is scanning. Set on
    /// approve (from the phone OR a pinch on the glasses), polled until paid.
    var checkout: Payment?

    /// Called once per genuinely new card, so the glasses can show it.
    var onNewCard: ((Card) -> Void)?

    private var lastCardId = 0
    private var failures = 0
    private var primed = false
    private var pollTask: Task<Void, Never>?

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

    func refreshLists() async {
        async let t: [OpenTask] = backend.get("/tasks")
        async let i: [Thing] = backend.get("/items")
        async let p: [Payment] = backend.get("/payments")
        async let d: Today = backend.get("/today")
        if let t = try? await t { tasks = t }
        if let i = try? await i { things = i }
        if let p = try? await p { payments = p }
        if let d = try? await d, d != today { today = d }
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
        }
        await refreshLists()
    }

    func reject(_ paymentId: Int) async {
        do { let _: Ignored = try await backend.post("/payments/\(paymentId)/reject") }
        catch { BridgeState.logError("reject payment", error) }
        await refreshLists()
    }

    func complete(_ taskId: Int) async {
        do { let _: Ignored = try await backend.post("/tasks/\(taskId)/done") }
        catch { BridgeState.logError("task done", error) }
        await refreshLists()
    }

    func ask(_ question: String) async {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        asking = true
        defer { asking = false }
        do { lastAnswer = try await backend.post("/ask", form: ["text": q]) }
        catch { BridgeState.logError("ask", error) }
    }
}
