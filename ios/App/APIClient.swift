import Foundation

struct Reminder: Decodable, Identifiable, Sendable, Hashable {
    let id: Int
    let text: String
    let dueAt: Date
    /// The next occurrences, starting with `dueAt` (just that, for a one-off).
    let upcoming: [Date]
    /// The server's own wording ("tomorrow at 09:00"), in the user's timezone.
    let when: String
    let recurrence: String?
    /// The chat app it will arrive on.
    let app: String
    /// Someone else's name, when it was set for them.
    let forPerson: String?

    enum CodingKeys: String, CodingKey {
        case id, text, when, recurrence, app, upcoming
        case dueAt = "due_at"
        case forPerson = "for"
    }
}

struct Me: Decodable, Sendable {
    let name: String
    let timezone: String
    let apps: [String]
}

struct Created: Decodable, Sendable {
    let reminder: Reminder
    let confirmation: String
    let notes: [String]
}

private struct Paired: Decodable { let token: String }
private struct ReminderList: Decodable { let reminders: [Reminder] }
private struct ServerError: Decodable { let error: String }

enum APIError: LocalizedError {
    case unauthorized
    case server(String)
    case badServerURL

    var errorDescription: String? {
        switch self {
        case .unauthorized: "Signed out. Pair again with a new code."
        case .server(let message): message
        case .badServerURL: "That doesn't look like a server address."
        }
    }
}

/// The v1 API, one method per endpoint. See docs/design/users-and-app-api.md.
struct APIClient: Sendable {
    let base: URL
    let token: String?

    static func baseURL(from raw: String) throws -> URL {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), let scheme = url.scheme,
              ["http", "https"].contains(scheme), url.host != nil
        else { throw APIError.badServerURL }
        return url
    }

    func pair(code: String, deviceName: String) async throws -> String {
        let body = ["code": code, "name": deviceName]
        let paired: Paired = try await send("POST", "pair", body: body)
        return paired.token
    }

    func me() async throws -> Me { try await send("GET", "me") }

    func reminders() async throws -> [Reminder] {
        let list: ReminderList = try await send("GET", "reminders")
        return list.reminders
    }

    func create(_ text: String) async throws -> Created {
        try await send("POST", "reminders", body: ["text": text])
    }

    func cancel(_ id: Int) async throws {
        try await sendNoContent("DELETE", "reminders/\(id)")
    }

    func signOut() async throws {
        try await sendNoContent("DELETE", "devices/self")
    }

    // MARK: plumbing

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { decoder in
            let raw = try decoder.singleValueContainer().decode(String.self)
            let withFraction = ISO8601DateFormatter()
            withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = withFraction.date(from: raw) ?? ISO8601DateFormatter().date(from: raw) {
                return date
            }
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "bad date \(raw)"))
        }
        return d
    }()

    private func request(_ method: String, _ path: String, body: [String: String]?) throws
        -> URLRequest
    {
        var req = URLRequest(url: base.appending(path: "api/v1/\(path)"))
        req.httpMethod = method
        req.timeoutInterval = 30  // AI reading on the server can take a few seconds
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(body)
        }
        return req
    }

    private func perform(_ req: URLRequest) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: req)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if (200..<300).contains(status) { return data }
        // A failed pairing is a 401 too, but it carries a message worth showing.
        let message = (try? JSONDecoder().decode(ServerError.self, from: data))?.error
        if status == 401, message == nil || message == "unauthorized" {
            throw APIError.unauthorized
        }
        throw APIError.server(message ?? "Server error (\(status)).")
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: [String: String]? = nil)
        async throws -> T
    {
        let data = try await perform(try request(method, path, body: body))
        return try Self.decoder.decode(T.self, from: data)
    }

    private func sendNoContent(_ method: String, _ path: String) async throws {
        _ = try await perform(try request(method, path, body: nil))
    }
}
