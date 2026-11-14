//
//  LocationReporter.swift
//  VexoBridge
//
//  Sends the phone's position to the laptop (POST /location) so the ordering agent can find
//  "the nearest McDonald's". The server keeps it for 30 minutes; we refresh every 5 minutes or
//  after moving 200 m. Only while Scene is in use (When In Use), and only coarse-ish accuracy.
//

import CoreLocation
import Foundation

@MainActor
final class LocationReporter: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private let backend: BackendClient
    private var lastSent: (CLLocation, Date)?

    init(backend: BackendClient) {
        self.backend = backend
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        manager.distanceFilter = 100
    }

    func start() {
        switch manager.authorizationStatus {
        case .notDetermined: manager.requestWhenInUseAuthorization()
        case .authorizedWhenInUse, .authorizedAlways: manager.startUpdatingLocation()
        default: BridgeState.log("location: not allowed, ordering will use the server's default place")
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in
            if [.authorizedWhenInUse, .authorizedAlways].contains(manager.authorizationStatus) {
                manager.startUpdatingLocation()
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let loc = locations.last else { return }
        Task { @MainActor in self.maybeSend(loc) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        BridgeState.logError("location", error)
    }

    private func maybeSend(_ loc: CLLocation) {
        if let (prev, at) = lastSent, Date().timeIntervalSince(at) < 300, loc.distance(from: prev) < 200 { return }
        lastSent = (loc, Date())
        let body: [String: Double] = ["lat": loc.coordinate.latitude, "lng": loc.coordinate.longitude]
        Task {
            guard let url = URL(string: backend.baseURL + "/location") else { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            if let (_, r) = try? await URLSession.shared.data(for: req), (r as? HTTPURLResponse)?.statusCode == 200 {
                BridgeState.log("location sent (±\(Int(loc.horizontalAccuracy)) m)")
            }
        }
    }
}
