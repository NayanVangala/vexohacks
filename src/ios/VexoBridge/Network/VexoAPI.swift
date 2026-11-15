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
    let speaker: String?
    let quote: String?          // exact words the task came from, verbatim; nil on older rows
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
    let kind: String?     // "chime": play the local "I heard you" ping; "chime-out": play the local "I'm done" ping, nothing to download
}

/// GET /interrupt: the laptop's barge-in sequence number. Decoded with the
/// shared convertFromSnakeCase decoder, so interrupt_id arrives as interruptId.
struct InterruptState: Decodable, Sendable {
    let interruptId: Int
}

/// GET /conversation (and the same shape inside GET /session once the backend
/// adds it there): Conversation Mode state. Every field is optional: an older
/// backend returns none of them, and the phone just keeps polling /feed as today.
struct ConversationState: Decodable, Equatable, Sendable {
    let mode: String?
    let status: String?
    let active: Bool?
    let startedAt: Double?
    let lastSpeechAt: Double?
    let speaker: String?
    let taskCount: Int?
    let tasksFound: Int?
    let notesCount: Int?
    let lines: Int?

    /// "active" / "listening" / "on" / "taking_notes" / "conversation" all mean on.
    /// Anything else (idle/off/ended) means off unless `active` says true.
    var isActive: Bool {
        if active == true { return true }
        let m = (mode ?? status ?? "").lowercased()
        return ["active", "listening", "on", "taking_notes", "conversation"].contains(m)
    }

    var taskTotal: Int { taskCount ?? tasksFound ?? 0 }
    var lineTotal: Int { lines ?? 0 }
    /// A real name only: "" and "Unknown" both mean no name yet.
    var speakerName: String? {
        guard let s = speaker?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty, s != "Unknown" else { return nil }
        return s
    }
}

/// One ended conversation, frozen the moment the mode flips off: what to show
/// on the phone until the next one starts.
struct ConversationSummary: Equatable, Sendable {
    let tasks: Int
    let lines: Int
    let endedAt: Date
}

/// GET /unknown-face: the who-was-that prompt armed when a conversation ended
/// with an unfamiliar face in it. `frame` is the saved frame filename (load it
/// with BackendClient.frameURL); `frameUrl` is the tunnel URL when the laptop
/// has one. Only ever pending after a conversation: never during one.
struct UnknownFaceState: Decodable, Equatable, Sendable {
    let pending: Bool?
    let frame: String?
    let frameUrl: String?
    let askedAt: Double?

    var isPending: Bool { pending == true && frame != nil }
}

/// GET /session, display half: true while the glasses web app is open (the lens
/// beacons POST /display/heartbeat). Other /session keys are ignored here.
struct SessionState: Decodable, Sendable {
    let displayAudioMode: Bool
    let conversation: ConversationState?
    let conversationMode: String?
}

/// What /ask found for a thing: only present when the best memory is an object with a photo.
/// The server currently strips `target` from /ask (server.py), so this is usually nil and the
/// phone falls back to the answer's frame plus the Things row's last-seen time. Decoded anyway,
/// so if the server ever includes it the phone uses its age instead of guessing.
struct LeadTarget: Decodable, Equatable, Sendable {
    let name: String?
    let place: String?
    let frame: String?
    let ageMin: Int?
}

struct AskResult: Decodable, Sendable {
    let question: String
    let answer: String
    let frame: String?
    let target: LeadTarget?
}

/// The lead offer after tapping Lead on a Thing. `canLead` is true only with a photo taken in
/// the last 180 minutes, the same rule the glasses' voice flow uses (memory.LEAD_MAX_AGE_MIN).
/// Never invents a place: place and frame come from the answer, else the Things row.
struct LeadOffer: Equatable, Sendable {
    let name: String
    let place: String?
    let frame: String?
    let answer: String
    let ageMin: Int
    let canLead: Bool
    let askFailed: Bool

    /// "5 minutes ago", "2 hours ago", "3 days ago".
    var ageText: String {
        if ageMin < 1 { return "just now" }
        if ageMin < 60 { return "\(ageMin) minute\(ageMin == 1 ? "" : "s") ago" }
        if ageMin < 1440 {
            let h = ageMin / 60
            return "\(h) hour\(h == 1 ? "" : "s") ago"
        }
        let d = ageMin / 1440
        return "\(d) day\(d == 1 ? "" : "s") ago"
    }
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

/// One line of the Follow-Through transcript, verbatim. The detail screen shows
/// the lines around a promise's quote as its description, never paraphrased.
struct FTSegment: Decodable, Equatable, Sendable {
    let speaker: String?
    let text: String
    let start: Double?
    let end: Double?
}

/// Where Follow-Through proved the quote in the transcript: segment indexes.
struct FTProof: Decodable, Equatable, Sendable {
    let verified: Bool?
    let firstSegment: Int?
    let lastSegment: Int?
}

/// A commitment Follow-Through found in the conversation, in any state (its /api/state items).
struct Promise: Decodable, Identifiable, Equatable {
    let id: String
    let title: String
    let quote: String?
    let status: String          // pending | needs_review | done | drafted | failed | dismissed
    let action: String?         // email | reply | calendar | todo
    let counterpart: String?
    let owner: String?
    let summary: String?
    let body: String?
    let subject: String?
    let to: String?
    let changedFrom: String?
    let durationMin: Int?
    let proof: FTProof?
    let dueLocal: String?       // "YYYY-MM-DDTHH:MM", local time
    let result: String?
    let issues: [String]?
    let link: String?
}

/// One "To do" row. A Follow-Through promise and a backend task are the same
/// thing to Nayan, so the phone merges them into one list. The backend stays
/// split: promises live in Follow-Through, tasks in the receiver.
struct TodoItem: Identifiable, Equatable {
    enum Kind { case promise, task }
    enum Status { case pending, needsReview, failed }
    let id: String
    let kind: Kind
    let title: String
    let meta: String?
    let quote: String?
    let status: Status
    /// Newest first. Tasks carry a real timestamp; Follow-Through sends none,
    /// so an undated promise sorts as just now, newest FT item first.
    let sortKey: Double
    let promiseId: String?
    let taskId: Int?
    /// Detail screen: who said it (never guessed), what kind of follow-up it
    /// is, the one-line summary, the verbatim transcript lines around the
    /// quote, and the draft text (email body / reply) when there is one.
    let speaker: String?
    let actionLabel: String?
    let summary: String?
    let context: [String]
    let detailBody: String?
    let result: String?
    let issues: [String]

    var buttonLabel: String {
        switch (kind, status) {
        case (.promise, .pending): "Approve"
        case (.promise, .needsReview): "Not now"
        case (.promise, .failed): "Try again"
        case (.task, _): "Done"
        }
    }

    var statusText: String {
        switch status {
        case .pending: "Waiting for you"
        case .needsReview: "Needs a look — check the quote first"
        case .failed: "Didn't go through"
        }
    }
}

private struct FTState: Decodable {
    let items: [Promise]
    let segments: [FTSegment]?
}

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
    private(set) var promises: [Promise] = []      // To do list: every promise, any state
    private(set) var ftSegments: [FTSegment] = []  // Follow-Through transcript, verbatim, for detail context
    private(set) var online = false
    /// Display Audio Mode: true while the glasses web app is open (lens heartbeat
    /// -> backend /session -> here). Camera is off, glasses mic is listening.
    private(set) var displayAudioMode = false
    /// Conversation Mode: the live state from GET /conversation (or the /session
    /// copy while the backend still lacks that route), plus the last ended one.
    /// A backend without the route leaves `conversationAvailable` false and the
    /// phone keeps showing the feed exactly as today.
    private(set) var conversation: ConversationState?
    private(set) var conversationAvailable = false
    private(set) var lastConversation: ConversationSummary?
    /// Unknown face: the who-was-that prompt armed at the last conversation's end
    /// (nil when nothing is waiting: no unknown face, already enrolled, skipped,
    /// or expired). The card in HomeView shows only while this is pending.
    private(set) var unknownFace: UnknownFaceState?
    private var sessionConversation: ConversationState?
    private var conversationStartedAt: Double = 0
    /// Fires on every mode change; BridgeController pauses/resumes frames here.
    var onDisplayAudioModeChanged: ((Bool) -> Void)?
    private(set) var lastAnswer: AskResult?
    private(set) var asking = false

    /// Lead me there: the offer (or the honest no-photo note) after tapping Lead on a Thing.
    private(set) var leadOffer: LeadOffer?
    /// Yes was tapped: the phone shows the photo + place and Stop. Spoken step-by-step
    /// guidance still comes from the glasses' voice flow (see startLead).
    private(set) var leadStarted = false

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
    /// Called the moment a real answer is done (spoken answer finished, or a
    /// text-only answer card was shown). The backend queues the chime-out line
    /// behind the answer, so this fires after the answer, never before it.
    var onChimeOut: (() -> Void)?
    /// Called when the laptop reports a barge-in (the wearer started talking
    /// over Jessica): the phone must stop its player and drop queued lines.
    var onInterrupt: (() -> Void)?
    private var lastSpeech = -1   // -1: first poll only catches up, never replays old lines
    private var lastInterruptId = -1   // -1: first poll only catches up, never stops anything

    private var lastCardId = 0
    private var failures = 0
    private var primed = false
    private var pollTask: Task<Void, Never>?
    private var speechTask: Task<Void, Never>?
    private var fetchingSpeech = false
    /// MP3 downloads run here, not in the poll loop: a slow download used to hold
    /// `fetchingSpeech` and stall discovery of the next line (and any chime)
    /// behind it. Lines wait in `speechPending` in id order; one serial drain
    /// task downloads them so answers still play in order.
    private var speechDownloadTask: Task<Void, Never>?
    private var speechPending: [Utterance] = []

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
            // Display Audio Mode, polled with everything else: the lens beacons
            // the backend, the backend answers in /session, the controller
            // pauses frames and keeps the mic on the change. A backend that
            // predates the mode returns no key, so this just stays false.
            if let s: SessionState = try? await backend.get("/session") {
                if s.displayAudioMode != displayAudioMode {
                    displayAudioMode = s.displayAudioMode
                    onDisplayAudioModeChanged?(s.displayAudioMode)
                }
                // Conversation Mode via /session, for backends that added it there
                // but not GET /conversation yet (see below).
                sessionConversation = s.conversation ?? s.conversationMode.map {
                    ConversationState(mode: $0, status: nil, active: nil, startedAt: nil, lastSpeechAt: nil,
                                      speaker: nil, taskCount: nil, tasksFound: nil, notesCount: nil, lines: nil)
                }
            }
            // Conversation Mode: GET /conversation when the backend has it. A 404
            // just means the laptop predates it: fall back to the /session copy
            // above, and otherwise keep polling /feed as today.
            if let c: ConversationState = try? await backend.get("/conversation") {
                conversationAvailable = true
                applyConversation(c)
            } else {
                conversationAvailable = false
                if let c = sessionConversation { applyConversation(c) }
            }
            // Unknown face: GET /unknown-face when the backend has it. A 404 just
            // means the laptop predates it: the spoken prompt + feed card still work,
            // only the tap-to-type card stays hidden.
            if let u: UnknownFaceState = try? await backend.get("/unknown-face") {
                unknownFace = u.isPending ? u : nil
            }
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

    /// Polls /speech (small JSON, fast) and returns immediately: chime-in lines
    /// fire inline, everything else queues for the background drain below. The
    /// old shape awaited each MP3 download inside this loop while holding
    /// `fetchingSpeech`, so one slow MP3 stretched the 0.5 s poll into seconds
    /// and a chime posted meanwhile could age past its 5 s window unheard.
    /// Downloads no longer gate discovery; the loop keeps its 0.5 s cadence
    /// whatever the LAN does.
    private func playNewSpeech() async {
        guard !fetchingSpeech else { return }
        fetchingSpeech = true
        defer { fetchingSpeech = false }
        // Barge-in: if the laptop stopped Jessica mid-sentence (the wearer
        // started talking), stop the phone player and drop every line queued
        // behind her, BEFORE appending this poll's lines -- the new wake chime
        // is queued server-side after the interrupt, so it still plays.
        if let state: InterruptState = try? await backend.get("/interrupt") {
            if lastInterruptId < 0 {
                lastInterruptId = state.interruptId   // first poll: catch up, don't stop anything
            } else if state.interruptId != lastInterruptId {
                lastInterruptId = state.interruptId
                speechPending.removeAll()
                speechDownloadTask?.cancel()
                speechDownloadTask = nil
                onInterrupt?()
            }
        }
        guard let lines: [Utterance] = try? await backend.get("/speech?after=\(max(lastSpeech, 0))") else { return }
        let catchingUp = lastSpeech < 0
        lastSpeech = max(lastSpeech, lines.map(\.id).max() ?? 0, 0)
        guard !catchingUp else { return }
        let now = Date().timeIntervalSince1970
        for line in lines where now - line.ts < 30 {   // stale lines aren't worth saying
            if line.kind == "chime" {
                // No download: the chime is a local WAV, so it fires the moment
                // the poll sees it instead of waiting behind MP3 downloads.
                if now - line.ts < 5 { onChime?() }   // a late ping is just confusing
                continue
            }
            // Answers AND chime-outs queue in server order for the serial drain:
            // the drain hands each answer to AudioPlayer before it reaches the
            // chime-out behind it, so the chime-out still sounds the moment the
            // answer ends, never before it. (Chime-in stays inline above: a wake
            // ack must preempt, not wait behind downloads.)
            speechPending.append(line)
        }
        startSpeechDownloadsIfNeeded()
    }

    /// Serial MP3 drain: downloads pending lines in id order and hands each to
    /// `onSpeech` the moment it lands, so the first line plays without waiting
    /// for later ones. Chime-out lines carry no audio and fire here, in order,
    /// once the answer ahead of them is already queued. A stalled download
    /// fails fast (10 s, like the upload timeout) instead of wedging the queue
    /// on URLSession's 60 s default, and a line that waited so long it went
    /// stale is skipped rather than spoken to a question from a minute ago.
    /// The poll loop never awaits this task.
    private func startSpeechDownloadsIfNeeded() {
        guard speechDownloadTask == nil, !speechPending.isEmpty else { return }
        speechDownloadTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled, let line = self.speechPending.first {
                self.speechPending.removeFirst()
                if line.kind == "chime-out" {
                    // A late ping is just confusing; on time, it fires after the
                    // answer ahead of it was already handed to AudioPlayer.
                    if Date().timeIntervalSince1970 - line.ts < 5 { self.onChimeOut?() }
                    continue
                }
                guard Date().timeIntervalSince1970 - line.ts < 30 else { continue }
                guard let url = URL(string: "\(self.backend.baseURL)/speech/\(line.id)") else { continue }
                var req = URLRequest(url: url)
                req.timeoutInterval = 10
                guard let (data, response) = try? await URLSession.shared.data(for: req),
                      (response as? HTTPURLResponse)?.statusCode == 200 else { continue }
                guard Date().timeIntervalSince1970 - line.ts < 30 else { continue }
                self.onSpeech?(data)
            }
            self.speechDownloadTask = nil
            // A poll may have queued lines after the last check; restart if so.
            if !self.speechPending.isEmpty { self.startSpeechDownloadsIfNeeded() }
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
            ftSegments = ft.segments ?? []
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

    /// Detail screen lookups. Nothing here invents text: the quote comes from
    /// the promise / task row, the context lines are verbatim transcript
    /// segments around the quote (two before, two after, skipping the quote
    /// segment itself), formatted "Name: what was said".
    func promise(id: String) -> Promise? { promises.first { $0.id == id } }

    func ftContext(for p: Promise) -> [String] {
        guard let first = p.proof?.firstSegment, let last = p.proof?.lastSegment,
              !ftSegments.isEmpty, first >= 0, last >= first else { return [] }
        let lo = max(0, first - 2), hi = min(ftSegments.count - 1, last + 2)
        return (lo...hi).filter { $0 < first || $0 > last }.map { i in
            let s = ftSegments[i]
            let who = (s.speaker ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return who.isEmpty ? s.text : "\(who): \(s.text)"
        }
    }

    /// Speaker for a promise: counterpart first, then the owner who made it.
    /// Empty / "Unknown" means no name: the detail screen says so, never guesses.
    static func promiseSpeaker(_ p: Promise) -> String? {
        for n in [p.counterpart, p.owner] {
            if let n = n?.trimmingCharacters(in: .whitespacesAndNewlines), !n.isEmpty, n != "Unknown" { return n }
        }
        return nil
    }

    /// To do row buttons: approve | dismiss | retry, straight to Follow-Through.
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

    // MARK: - Conversation Mode

    /// Fold a fresh ConversationState in, freezing a summary on the active->off flip.
    /// Counts prefer the backend's own numbers; the feed cards are the fallback
    /// (capped at the newest 50, so a long conversation can undercount from here).
    private func applyConversation(_ c: ConversationState) {
        let wasActive = conversation?.isActive == true
        if c.isActive, !wasActive {
            lastConversation = nil
            conversationStartedAt = c.startedAt ?? Date().timeIntervalSince1970
        } else if !c.isActive, wasActive {
            let from = conversationStartedAt
            let tasks = max(c.taskTotal, cards.filter { $0.kind == "task" && $0.ts >= from }.count)
            let lines = max(c.lineTotal, cards.filter { $0.kind == "transcript" && $0.ts >= from }.count)
            lastConversation = ConversationSummary(tasks: tasks, lines: lines, endedAt: .now)
        }
        conversation = c
    }

    /// Start notes from the phone. The response body is ignored on purpose: its
    /// shape is the backend's to choose, and the next GET /conversation (one tick
    /// away) is the confirmation. Needs the Conversation endpoints on the laptop
    /// (and a backend restart after the update) or this just says so.
    func startConversation() async {
        do {
            let _: Ignored = try await backend.post("/conversation/start")
            conversationAvailable = true
            if let c: ConversationState = try? await backend.get("/conversation") { applyConversation(c) }
            BridgeState.log("CONVERSATION started")
        } catch {
            BridgeState.logError("start conversation", error)
            notice = "Couldn't start notes: \(error.localizedDescription). Check the laptop backend is updated and restarted."
        }
    }

    func stopConversation() async {
        do {
            let _: Ignored = try await backend.post("/conversation/stop")
            conversationAvailable = true
            if let c: ConversationState = try? await backend.get("/conversation") { applyConversation(c) }
            BridgeState.log("CONVERSATION stopped")
        } catch {
            BridgeState.logError("stop conversation", error)
            notice = "Couldn't stop notes: \(error.localizedDescription). Check the laptop backend is updated and restarted."
        }
    }

    // MARK: - Unknown face (enroll after the conversation)

    /// Enroll the saved unknown frame under `name`. The laptop enrolls its own
    /// saved frame: the phone only sends the name, never a photo. No name, no
    /// enroll: an empty name just shows the notice and sends nothing. Returns
    /// true when the laptop confirmed the enrollment.
    func enrollUnknownFace(name: String) async -> Bool {
        let clean = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { notice = "Type a name first."; return false }
        do {
            let _: Ignored = try await backend.post("/unknown-face/enroll", form: ["name": clean])
            unknownFace = nil
            BridgeState.log("UNKNOWN FACE enrolled \(clean)")
            return true
        } catch {
            BridgeState.logError("enroll unknown face", error)
            notice = "Couldn't save that name: \(error.localizedDescription)"
            return false
        }
    }

    /// Discard the saved unknown frame without enrolling. Never asks again for that conversation.
    func dismissUnknownFace() async {
        do {
            let _: Ignored = try await backend.post("/unknown-face/dismiss")
            unknownFace = nil
            BridgeState.log("UNKNOWN FACE dismissed")
        } catch {
            BridgeState.logError("dismiss unknown face", error)
            notice = "Couldn't dismiss: \(error.localizedDescription)"
        }
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

    /// The single To do list: actionable promises + open tasks, newest first,
    /// de-duplicated by title. A promise and a task saying the same thing show
    /// once (the promise wins: approving is the gate). Done and drafted
    /// promises are out: they are finished, not to do.
    var todoItems: [TodoItem] {
        var items: [TodoItem] = []
        let now = Date().timeIntervalSince1970
        for (r, p) in promises.reversed().enumerated() {
            let status: TodoItem.Status?
            switch p.status {
            case "pending": status = .pending
            case "needs_review": status = .needsReview
            case "failed": status = .failed
            default: status = nil
            }
            guard let status else { continue }
            let due = p.dueLocal.flatMap(Self.date(fromDue:))
            let detail = [p.subject.map { "Subject: \($0)" }, p.body].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: "\n\n")
            items.append(TodoItem(id: "promise-\(p.id)", kind: .promise, title: p.title,
                                  meta: Self.promiseMeta(p), quote: p.quote, status: status,
                                  sortKey: due?.timeIntervalSince1970 ?? now - Double(r) * 60,
                                  promiseId: p.id, taskId: nil,
                                  speaker: Self.promiseSpeaker(p),
                                  actionLabel: p.action.map { $0.capitalized },
                                  summary: p.summary?.isEmpty == false ? p.summary : nil,
                                  context: ftContext(for: p),
                                  detailBody: detail.isEmpty ? nil : detail,
                                  result: p.result?.isEmpty == false ? p.result : nil,
                                  issues: p.issues ?? []))
        }
        for t in tasks {
            let (title, meta) = Self.splitTaskText(t.text)
            items.append(TodoItem(id: "task-\(t.id)", kind: .task, title: title, meta: meta,
                                  quote: t.quote, status: .pending, sortKey: t.ts,
                                  promiseId: nil, taskId: t.id,
                                  speaker: (t.speaker?.isEmpty == false && t.speaker != "Unknown") ? t.speaker : nil,
                                  actionLabel: nil, summary: nil, context: [],
                                  detailBody: nil, result: nil, issues: []))
        }
        items.sort { $0.sortKey > $1.sortKey }
        // One row per thing to do: the same title twice (promise + task, or
        // Follow-Through finding the same promise twice) shows once.
        var seen = Set<String>()
        return items.filter { seen.insert(Self.normTitle($0.title)).inserted }
    }

    /// The one button on a To do row. Promise approve / task done / dismiss /
    /// retry all go through the existing calls; the list refreshes after.
    func todoAction(_ item: TodoItem) async {
        switch (item.kind, item.status) {
        case (.promise, .pending): if let id = item.promiseId { await promise(id, "approve") }
        case (.promise, .needsReview): if let id = item.promiseId { await promise(id, "dismiss") }
        case (.promise, .failed): if let id = item.promiseId { await promise(id, "retry") }
        case (.task, _): if let id = item.taskId { await complete(id) }
        }
    }

    private static func date(fromDue d: String) -> Date? {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd'T'HH:mm"
        return f.date(from: d)
    }

    private static func promiseMeta(_ p: Promise) -> String? {
        var bits: [String] = []
        if let c = p.counterpart, !c.isEmpty { bits.append("With \(c)") }
        if let d = p.dueLocal, !d.isEmpty {
            bits.append(date(fromDue: d).map { $0.formatted(.dateTime.weekday(.abbreviated).hour().minute()) } ?? d)
        }
        return bits.isEmpty ? nil : bits.joined(separator: " · ")
    }

    private static func normTitle(_ s: String) -> String {
        s.lowercased().components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty }.joined(separator: " ")
    }

    /// "Send the deck (from Sam, due 5pm)" -> title + "from Sam, due 5pm".
    private static func splitTaskText(_ text: String) -> (String, String?) {
        guard text.hasSuffix(")"), let r = text.range(of: " (", options: .backwards) else { return (text, nil) }
        return (String(text[..<r.lowerBound]), String(text[r.upperBound...].dropLast()))
    }

    @discardableResult
    func ask(_ question: String) async -> Bool {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return false }
        asking = true
        defer { asking = false }
        do {
            lastAnswer = try await backend.post("/ask", form: ["text": q])
            return true
        } catch {
            BridgeState.logError("ask", error)
            notice = "Couldn't reach your memory. Check the laptop address in the Glasses tab."
            return false
        }
    }

    // MARK: - Lead me there (Things tab)

    /// Lead button on a Thing: ask where it is, then offer to lead, or say plainly why not.
    /// The frame comes from the answer (its target, else its frame), else the Things row;
    /// the age comes from the answer's target when the server sends one, else the row's
    /// last-seen time. No location is ever invented: with no frame there is no offer.
    func leadTo(_ thing: Thing) async {
        let rowAgeMin = Int(max(0, Date().timeIntervalSince1970 - thing.ts) / 60)
        let ok = await ask("Where is my \(thing.name)?")
        leadStarted = false
        guard ok, let a = lastAnswer else {
            leadOffer = LeadOffer(name: thing.name, place: thing.place, frame: thing.frame,
                                  answer: "Couldn't reach your memory. Check the laptop address in the Glasses tab.",
                                  ageMin: rowAgeMin, canLead: false, askFailed: true)
            return
        }
        let frame = a.target?.frame ?? a.frame ?? thing.frame
        let place = (a.target?.place).flatMap { $0.isEmpty ? nil : $0 } ?? thing.place
        let age = a.target?.ageMin ?? rowAgeMin
        leadOffer = LeadOffer(name: thing.name, place: place, frame: frame, answer: a.answer,
                              ageMin: age, canLead: frame != nil && age <= 180, askFailed: false)
        if leadOffer?.canLead == true {
            BridgeState.log("LEAD offer for \(thing.name) (photo, \(age) min ago)")
        }
    }

    /// Yes on the lead offer. There is no phone endpoint that starts the backend lead loop
    /// (lead.py only starts from a spoken yes after a voice question), so the phone shows the
    /// photo + place and the card states the one voice step that starts spoken guidance.
    /// Nothing here pretends live steps are running.
    func startLead() {
        guard leadOffer?.canLead == true else { return }
        leadStarted = true
        BridgeState.log("LEAD showing the way to \(leadOffer?.name ?? "?") - spoken steps via glasses voice flow")
    }

    func stopLead() {
        if leadStarted { BridgeState.log("LEAD stopped for \(leadOffer?.name ?? "?")") }
        leadOffer = nil
        leadStarted = false
    }

    func dismissLead() {
        leadOffer = nil
        leadStarted = false
    }
}
