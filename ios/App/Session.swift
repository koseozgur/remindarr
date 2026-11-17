import Observation
import UIKit
import UserNotifications

/// Whether this install is paired, and the calls that need the token.
@MainActor @Observable
final class Session {
    private(set) var serverURL: URL?
    private(set) var token: String?
    var reminders: [Reminder] = []
    var userName: String?
    private(set) var notifications: UNAuthorizationStatus = .notDetermined

    var isPaired: Bool { token != nil && serverURL != nil }

    init() {
        (serverURL, token) = Self.storedCredentials() ?? (nil, nil)
    }

    /// The Keychain's pairing, else a token baked into a local Debug build
    /// (`Config/Local.xcconfig`) with the build's default server.
    nonisolated static func storedCredentials() -> (URL, String)? {
        if let token = Keychain.read("token"),
           let server = Keychain.read("server").flatMap(URL.init(string:))
        {
            return (server, token)
        }
        if let token = AppConfig.bakedToken, let server = URL(string: AppConfig.defaultServerURL) {
            return (server, token)
        }
        return nil
    }

    /// For App Intents, which run without the UI.
    nonisolated static func storedClient() -> APIClient? {
        storedCredentials().map { APIClient(base: $0.0, token: $0.1) }
    }

    private var client: APIClient? {
        guard let serverURL, let token else { return nil }
        return APIClient(base: serverURL, token: token)
    }

    func pair(server: String, code: String) async throws {
        let base = try APIClient.baseURL(from: server)
        let token = try await APIClient(base: base, token: nil)
            .pair(code: code, deviceName: UIDevice.current.name)
        Keychain.write("server", base.absoluteString)
        Keychain.write("token", token)
        self.serverURL = base
        self.token = token
        try await refresh()
    }

    func refresh() async throws {
        guard let client else { return }
        do {
            async let me = client.me()
            async let list = client.reminders()
            let (who, items) = try await (me, list)
            userName = who.name
            reminders = items
        } catch APIError.unauthorized {
            forget()
            throw APIError.unauthorized
        }
        notifications = await PhoneNotifications.status()
        await PhoneNotifications.sync(reminders)
    }

    /// Asked from the list, when the user taps "Notify me on this phone too".
    func enableNotifications() async {
        _ = await PhoneNotifications.requestPermission()
        notifications = await PhoneNotifications.status()
        await PhoneNotifications.sync(reminders)
    }

    func create(_ text: String) async throws -> Created {
        guard let client else { throw APIError.unauthorized }
        let created = try await client.create(text)
        try? await refresh()
        return created
    }

    func cancel(_ reminder: Reminder) async throws {
        guard let client else { throw APIError.unauthorized }
        try await client.cancel(reminder.id)
        reminders.removeAll { $0.id == reminder.id }
        await PhoneNotifications.sync(reminders)
    }

    func signOut() async {
        try? await client?.signOut()  // best effort: forget locally regardless
        forget()
    }

    private func forget() {
        Keychain.delete("token")
        token = nil
        reminders = []
        userName = nil
        PhoneNotifications.removeAll()
    }
}
