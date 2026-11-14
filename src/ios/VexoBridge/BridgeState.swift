//
//  BridgeState.swift
//  VexoBridge
//
//  Shared observable state. CLAUDE-ios.md §7.1.
//
//  Android twin: `object BridgeState` exposing StateFlows. SwiftUI observes
//  stored properties directly, so there is no flow plumbing here.
//
//  Everything on this type is @MainActor. Callers on SDK delivery threads and
//  on the audio tap thread must hop — the hop is wrapped inside the nonisolated
//  `log*` helpers below so no caller has to remember (§7.1, §11.7).
//

import Foundation
import Observation

import MWDATCore

@Observable
@MainActor
final class BridgeState {

    static let shared = BridgeState()

    // MARK: - Types

    enum Connection: String {
        case disconnected = "DISCONNECTED"
        case scanning     = "SCANNING"
        case connecting   = "CONNECTING"
        case connected    = "CONNECTED"
        /// Distinct from `connecting`: the device ended a session that was
        /// already working, and auto-recovery is rebuilding it. Worth its own
        /// state because "CONNECTING" on a run that has been streaming for ten
        /// minutes reads as a stale indicator rather than an active repair.
        case reconnecting = "RECONNECTING"
    }

    // MARK: - Constants

    /// Bounded memory over a long session. Appendix A: never change.
    static let errorLogCap = 50

    #if DEBUG
    /// Durable run log, for diagnosing anything longer than the 50-line cap.
    ///
    /// Neither existing surface survives a long run: `errorLog` is capped at 50
    /// entries, and `devicectl … --console` has repeatedly detached mid-session
    /// (taking every line with it). This file outlives both. Truncated per
    /// launch, so a pulled copy is exactly one run. Retrieve with:
    ///
    ///   xcrun devicectl device copy from --device <udid> \
    ///     --domain-type appDataContainer --domain-identifier com.vexohacks.bridge \
    ///     --source Documents/vexo-run.log --destination ./vexo-run.log
    private static let runLog: FileHandle? = {
        guard let dir = FileManager.default.urls(
            for: .documentDirectory, in: .userDomainMask
        ).first else { return nil }
        let url = dir.appendingPathComponent("vexo-run.log")
        FileManager.default.createFile(atPath: url.path, contents: nil)
        return try? FileHandle(forWritingTo: url)
    }()

    /// Last scene phase seen, so the heartbeat can report it without the reader
    /// having to correlate against a separate transition line.
    var scenePhaseText: String = "?"
    #endif

    // MARK: - State

    var connection: Connection = .disconnected
    var lastFrameAt: String?
    var errorLog: [String] = []
    var isRunning: Bool = false

    /// Debug-only truth about where the microphone actually is. A silent WAV is
    /// indistinguishable from a quiet room until you look at this (§11.4).
    var audioInputPortName: String = "—"

    /// Registration/permission surface, driven by WearablesManager.
    ///
    /// The enum is stored rather than a string because §6.2 gates UI on it: the
    /// Connect button appears only for `.available`, a spinner only for
    /// `.registering`. `nil` means `registrationStateStream()` has not emitted
    /// yet — distinct from `.unavailable`, which is a real answer.
    var registrationState: RegistrationState?
    var cameraPermissionText: String = "unknown"

    /// Display text. `RegistrationState` is an `@objc` Int-backed enum that does
    /// not conform to `CustomStringConvertible`, so interpolating it directly
    /// prints `RegistrationState(rawValue: 1)`. `label` is the readable mapping.
    var registrationText: String { registrationState?.label ?? "unknown" }

    private init() {}

    // MARK: - Formatting

    /// Fixed-format, locale-independent. A user's 12-hour locale otherwise
    /// reorders the log (§7.1). This is the `Locale.US` equivalent.
    private static let timestampFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    static func timestamp(_ date: Date = Date()) -> String {
        timestampFormatter.string(from: date)
    }

    // MARK: - Mutation (main actor)

    func append(_ message: String) {
        let line = "\(Self.timestamp())  \(message)"
        errorLog.append(line)
        if errorLog.count > Self.errorLogCap {
            errorLog.removeFirst(errorLog.count - Self.errorLogCap)
        }
        // The in-app list is capped at 50 and never leaves the process, so a
        // laptop cannot read it mid-session. stdout IS relayed by
        // `devicectl device process launch --console`. os_log would not help:
        // macOS `log` dropped iOS device streaming, so it is Console.app or
        // nothing. DEBUG-only — a diagnostic surface, not release behaviour.
        #if DEBUG
        if let handle = Self.runLog,
           let bytes = ("[vexo] " + line + "\n").data(using: .utf8) {
            try? handle.write(contentsOf: bytes)
        }
        print("[vexo] \(line)")
        // Mandatory, not tidiness. stdout is a PIPE under
        // `devicectl --console`, not a TTY, so it is block-buffered: lines sit in
        // a 4 KB buffer and reach the reader only when the buffer fills or the
        // process exits. A live session therefore looks completely silent while
        // the app is logging normally — which is indistinguishable from a dead
        // console attachment.
        fflush(stdout)
        #endif
    }

    func clearLog() {
        errorLog.removeAll()
    }

    func markFrameSent() {
        lastFrameAt = Self.timestamp()
    }

    /// Newest-first, for the UI. Storage stays oldest-first so the cap trims the
    /// oldest entries.
    var errorLogNewestFirst: [String] {
        errorLog.reversed()
    }
}

// MARK: - Thread-hopping façade
//
// Call these from anywhere: the frame callback, the audio tap, a URLSession
// completion. The @MainActor hop happens here, once, rather than at every
// call site (§7.1, §11.7).

extension BridgeState {

    nonisolated static func log(_ message: String) {
        Task { @MainActor in shared.append(message) }
    }

    nonisolated static func logError(_ context: String, _ error: Error) {
        // `localizedDescription` for anything a user reads; DatError also
        // exposes `description`, which is always English and meant for logs (§8.4).
        Task { @MainActor in shared.append("\(context): \(error.localizedDescription)") }
    }

    nonisolated static func setConnection(_ value: Connection) {
        Task { @MainActor in shared.connection = value }
    }

    nonisolated static func markFrame() {
        Task { @MainActor in shared.markFrameSent() }
    }

    nonisolated static func setAudioInputPortName(_ name: String) {
        Task { @MainActor in shared.audioInputPortName = name }
    }

    nonisolated static func setRegistrationState(_ value: RegistrationState) {
        Task { @MainActor in shared.registrationState = value }
    }
}
