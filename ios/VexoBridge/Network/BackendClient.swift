//
//  BackendClient.swift
//  VexoBridge
//
//  HTTP to the laptop. CLAUDE-ios.md §7.9.
//
//  Android twin: BackendClient (OkHttp multipart).
//
//  One session with a 10 s timeout for fire-and-forget uploads.
//
//  `waitsForConnectivity = false` is load-bearing. The default parks a request
//  until the network returns, which silently converts fire-and-forget uploads
//  into an unbounded backlog — the exact failure mode invariant #3 exists to
//  prevent. Fail fast, drop the frame.
//

import Foundation

enum BackendError: LocalizedError {
    case badURL(String)
    case httpStatus(Int, path: String)
    case notHTTP

    var errorDescription: String? {
        switch self {
        case .badURL(let s):              return "bad backend URL: \(s)"
        case .httpStatus(let c, let p):   return "HTTP \(c) from \(p)"
        case .notHTTP:                    return "non-HTTP response"
        }
    }
}

final class BackendClient: @unchecked Sendable {

    // MARK: - Constants (Appendix A)

    private static let defaultsKey = "backend_base_url"
    private static let hostnameMigrationKey = "backend_base_url_migrated_to_hostname_v1"

    /// An mDNS hostname, deliberately NOT an IP.
    ///
    /// The laptop's DHCP address moved twice inside one test session
    /// (10.0.0.137 → 10.0.0.176). Every move silently breaks every upload — the
    /// symptom is "Could not connect to the server" on each POST with nothing in
    /// the app hinting that the address is stale — and the only repair was
    /// retyping the field mid-test. A `.local` name is resolved by mDNS at
    /// request time, so it survives a new lease.
    ///
    /// Needs the Local Network permission, which Info.plist already declares via
    /// `NSLocalNetworkUsageDescription` + `NSBonjourServices`. If mDNS ever fails
    /// on a hostile network, typing a raw IP into the UI still overrides this.
    ///
    /// Note the previous value also pointed at port 8000, where nothing has
    /// listened since the stray server was killed; the live server is on 8001.
    static let fallbackBaseURL = "http://Nayans-MacBook-Air.local:8001"

    private static let uploadTimeout: TimeInterval = 10

    // MARK: - State

    private let lock = NSLock()
    private var _baseURL: String

    private let uploadSession: URLSession

    var baseURL: String {
        get { lock.withLock { _baseURL } }
    }

    /// True for `http://10.0.0.176:8001`, false for `http://host.local:8001`.
    /// Four dot-separated octets, each fitting in a UInt8 — deliberately strict,
    /// so anything else is treated as a hostname and left untouched.
    private static func isIPLiteral(_ urlString: String) -> Bool {
        guard let host = URLComponents(string: urlString)?.host else { return false }
        let octets = host.split(separator: ".")
        return octets.count == 4 && octets.allSatisfy { UInt8($0) != nil }
    }

    // MARK: - Init

    init(defaults: UserDefaults = .standard) {
        let stored = defaults.string(forKey: Self.defaultsKey)

        // One-time upgrade of a persisted IP literal to the mDNS hostname.
        //
        // Changing `fallbackBaseURL` alone fixes nothing on an existing install:
        // the stored value wins, and a stored IP outlives the DHCP lease that
        // made it valid. Narrow on purpose — only bare IPv4 literals are touched,
        // so a hostname typed by hand is left alone, and because the flag is set
        // at the same time, any later manual override is never overwritten.
        if let stored, Self.isIPLiteral(stored), !defaults.bool(forKey: Self.hostnameMigrationKey) {
            defaults.set(Self.fallbackBaseURL, forKey: Self.defaultsKey)
            defaults.set(true, forKey: Self.hostnameMigrationKey)
            _baseURL = Self.normalize(Self.fallbackBaseURL)
            BridgeState.log("backend URL migrated \(stored) -> \(Self.fallbackBaseURL) (IP is DHCP-fragile)")
        } else {
            _baseURL = Self.normalize(stored ?? Self.fallbackBaseURL)
        }

        let uploadConfig = URLSessionConfiguration.default
        uploadConfig.timeoutIntervalForRequest = Self.uploadTimeout
        uploadConfig.waitsForConnectivity = false
        uploadConfig.requestCachePolicy = .reloadIgnoringLocalCacheData
        uploadSession = URLSession(configuration: uploadConfig)
    }

    // MARK: - Base URL

    /// Strips trailing slashes before persisting. Paths are concatenated
    /// directly, so a stored trailing slash yields a double slash and a 404 (§7.9).
    private static func normalize(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }

    func setBaseURL(_ raw: String, defaults: UserDefaults = .standard) {
        let normalized = Self.normalize(raw)
        lock.withLock { _baseURL = normalized }
        defaults.set(normalized, forKey: Self.defaultsKey)
        BridgeState.log("backend URL set to \(normalized)")
    }

    private func url(_ path: String) throws -> URL {
        let base = baseURL
        guard let u = URL(string: base + path) else { throw BackendError.badURL(base + path) }
        return u
    }

    // MARK: - Endpoints

    /// Cheap GET fired once at launch purely to make iOS raise the Local Network
    /// permission prompt while the user is still looking at the screen (§11.2).
    /// There is no API to query that permission's status, so a thrown error here
    /// is a hint, not a verdict.
    @discardableResult
    func getStats() async throws -> Data {
        var request = URLRequest(url: try url("/stats"))
        request.httpMethod = "GET"
        let (data, response) = try await uploadSession.data(for: request)
        try Self.check(response, path: "/stats")
        return data
    }

    /// multipart, part name `photo`, filename `<epochMs>.jpg`, type `image/jpeg`.
    /// Part name is frozen — it is the server's FastAPI parameter name.
    func postPhoto(_ jpeg: Data, source: String? = nil) async throws {
        var fields: [String: String] = [:]
        if let source { fields["source"] = source }
        let body = MultipartBuilder.build(
            partName: "photo",
            filename: "\(Self.epochMs()).jpg",
            contentType: "image/jpeg",
            data: jpeg,
            extraFields: fields
        )
        try await post(body: body, path: "/capture", session: uploadSession)
    }

    /// multipart, part name `audio`, filename `<epochMs>.wav`, type `audio/wav`.
    func postAudio(_ wav: Data) async throws {
        let body = MultipartBuilder.build(
            partName: "audio",
            filename: "\(Self.epochMs()).wav",
            contentType: "audio/wav",
            data: wav
        )
        try await post(body: body, path: "/capture", session: uploadSession)
    }

    /// Empty body.
    func postActivate() async throws {
        var request = URLRequest(url: try url("/activate"))
        request.httpMethod = "POST"
        request.httpBody = Data()
        let (_, response) = try await uploadSession.data(for: request)
        try Self.check(response, path: "/activate")
    }

    // MARK: - Plumbing

    private func post(body: MultipartBody, path: String, session: URLSession) async throws {
        var request = URLRequest(url: try url(path))
        request.httpMethod = "POST"
        request.setValue(body.contentType, forHTTPHeaderField: "Content-Type")
        request.httpBody = body.data
        let (_, response) = try await session.data(for: request)
        try Self.check(response, path: path)
    }

    /// Non-2xx throws. Callers catch and route to BridgeState. No retry logic
    /// anywhere — a dropped frame is cheaper than an unbounded queue on a flaky
    /// venue LAN (§7.9).
    private static func check(_ response: URLResponse, path: String) throws {
        guard let http = response as? HTTPURLResponse else { throw BackendError.notHTTP }
        guard (200..<300).contains(http.statusCode) else {
            throw BackendError.httpStatus(http.statusCode, path: path)
        }
    }

    static func epochMs() -> Int64 {
        Int64(Date().timeIntervalSince1970 * 1000)
    }
}

// MARK: - Multipart

struct MultipartBody {
    let data: Data
    let contentType: String
}

/// Hand-built multipart. CRLF everywhere — LF-only line endings are the classic
/// hand-rolled-multipart bug and produce a 422 that looks like a missing
/// dependency (§7.9).
enum MultipartBuilder {

    static func build(
        partName: String,
        filename: String,
        contentType: String,
        data: Data,
        extraFields: [String: String] = [:]
    ) -> MultipartBody {
        let boundary = UUID().uuidString
        var body = Data()

        // Simple form fields first — order is irrelevant to FastAPI.
        for (key, value) in extraFields.sorted(by: { $0.key < $1.key }) {
            body.appendASCII("--\(boundary)\r\n")
            body.appendASCII("Content-Disposition: form-data; name=\"\(key)\"\r\n\r\n")
            body.appendASCII("\(value)\r\n")
        }

        body.appendASCII("--\(boundary)\r\n")
        body.appendASCII(
            "Content-Disposition: form-data; name=\"\(partName)\"; filename=\"\(filename)\"\r\n"
        )
        body.appendASCII("Content-Type: \(contentType)\r\n\r\n")
        body.append(data)
        body.appendASCII("\r\n")
        body.appendASCII("--\(boundary)--\r\n")

        return MultipartBody(
            data: body,
            contentType: "multipart/form-data; boundary=\(boundary)"
        )
    }
}

private extension Data {
    mutating func appendASCII(_ string: String) {
        // Header bytes are ASCII by construction; UTF-8 is a safe superset for
        // any filename we generate.
        append(Data(string.utf8))
    }
}
