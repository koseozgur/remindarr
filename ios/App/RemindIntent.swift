import AppIntents

/// "Remind me in <app>": Siri asks what and when, the server reads it exactly
/// as it would a chat message, and Siri says the confirmation (which states
/// the resolved time, so a misread is audible at once).
///
/// App Intents need no entitlement, unlike SiriKit, so this works with free
/// signing.
struct RemindIntent: AppIntent {
    static let title: LocalizedStringResource = "Set a reminder"
    static let description = IntentDescription("Sets a reminder, read as the chat bot reads it.")

    @Parameter(
        title: "Reminder",
        requestValueDialog: "What should I remind you about, and when?")
    var text: String

    func perform() async throws -> some ReturnsValue<String> & ProvidesDialog {
        guard let client = Session.storedClient() else {
            throw IntentFailure("Open the app and pair it first.")
        }
        do {
            let created = try await client.create(text)
            if let reminders = try? await client.reminders() {
                await PhoneNotifications.sync(reminders)
            }
            return .result(value: created.confirmation, dialog: "\(created.confirmation)")
        } catch {
            throw IntentFailure(error.localizedDescription)
        }
    }
}

struct IntentFailure: Error, CustomLocalizedStringResourceConvertible {
    let message: String
    init(_ message: String) { self.message = message }
    var localizedStringResource: LocalizedStringResource { "\(message)" }
}

struct RemindShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: RemindIntent(),
            phrases: [
                "Remind me in \(.applicationName)",
                "Set a reminder in \(.applicationName)",
            ],
            shortTitle: "Set a reminder",
            systemImageName: "bell")
    }
}
