//
//  Discovery.swift
//  VexoBridge
//
//  Finds the laptop receiver without typing an IP. receiver/run.sh advertises
//  `_scene._tcp` over Bonjour (macOS dns-sd); this browses for it and resolves
//  the first hit to an IPv4 URL. Used only when the saved URL doesn't answer,
//  so a hand-typed address always wins while it works.
//

import Foundation
import Network

enum Discovery {

    static let serviceType = "_scene._tcp"

    /// Resolves to e.g. "http://172.17.29.149:8001", or nil after `timeout`.
    static func findReceiver(timeout: Duration = .seconds(6)) async -> String? {
        let browser = NWBrowser(for: .bonjour(type: serviceType, domain: nil), using: .tcp)
        defer { browser.cancel() }

        let endpoints = AsyncStream<NWEndpoint> { continuation in
            browser.browseResultsChangedHandler = { results, _ in
                results.forEach { continuation.yield($0.endpoint) }
            }
            browser.start(queue: .main)
        }

        return await withTaskGroup(of: String?.self) { group in
            group.addTask {
                for await endpoint in endpoints {
                    if let url = await resolve(endpoint) { return url }
                }
                return nil
            }
            group.addTask {
                try? await Task.sleep(for: timeout)
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first
        }
    }

    /// Bonjour gives a service name, not an address: connect once to learn it.
    /// IPv4 only, so the URL never carries a `%en0` link-local suffix.
    private static func resolve(_ endpoint: NWEndpoint) async -> String? {
        let params = NWParameters.tcp
        (params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options)?.version = .v4
        let connection = NWConnection(to: endpoint, using: params)
        defer { connection.cancel() }

        return await withCheckedContinuation { continuation in
            let once = OnceFlag()
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    if case .hostPort(let host, let port)? = connection.currentPath?.remoteEndpoint,
                       once.claim() {
                        // NWEndpoint prints "172.17.29.149%en0"; the scope breaks a URL.
                        let ip = "\(host)".split(separator: "%").first.map(String.init) ?? "\(host)"
                        continuation.resume(returning: "http://\(ip):\(port)")
                    }
                case .failed, .cancelled:
                    if once.claim() { continuation.resume(returning: nil) }
                default:
                    break
                }
            }
            connection.start(queue: .main)
        }
    }
}

/// A continuation must resume exactly once; NWConnection can report several states.
private final class OnceFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false
    func claim() -> Bool { lock.withLock { defer { done = true }; return !done } }
}
