import SwiftUI
import UserNotifications

/// Where reminders arrive. The same setting as `/deliver` in chat: the server
/// keeps it, so changing it in either place shows in both.
struct SettingsView: View {
    @Environment(Session.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var error: String?
    @State private var busy = false

    private static let modes = [
        ("phone", "Phone only"), ("both", "Phone and chat"), ("chat", "Chat only"),
    ]
    private static let appNames = ["line": "LINE", "discord": "Discord", "telegram": "Telegram"]

    var body: some View {
        NavigationStack {
            Form {
                if let settings = session.settings {
                    Section {
                        Picker("Arrive on", selection: binding(\.delivery) { mode in
                            await change(delivery: mode)
                        }) {
                            ForEach(Self.modes, id: \.0) { Text($0.1).tag($0.0) }
                        }
                        .pickerStyle(.inline)
                        .labelsHidden()
                    } header: {
                        Text("Reminders arrive")
                    } footer: {
                        Text(modeExplanation(settings.delivery))
                    }
                    if settings.delivery != "phone" || !settings.apps.isEmpty {
                        Section("Chat app") {
                            Picker("Chat app", selection: binding(\.chatApp) { app in
                                await change(chatApp: .some(app))
                            }) {
                                Text("Wherever I set them").tag(String?.none)
                                ForEach(settings.apps, id: \.self) { app in
                                    Text(Self.appNames[app] ?? app).tag(String?.some(app))
                                }
                            }
                            .pickerStyle(.inline)
                            .labelsHidden()
                        }
                    }
                    Section {
                        Text(settings.summary)
                        if settings.schedulesOnPhone, session.notifications == .denied {
                            Text("Notifications are off for this app in iOS Settings, so "
                                 + "reminders keep coming by chat.")
                                .foregroundStyle(.red)
                        }
                    }
                } else {
                    ProgressView()
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .disabled(busy)
            .navigationTitle("Where reminders arrive")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .task { try? await session.refresh() }
        }
    }

    private func modeExplanation(_ mode: String) -> String {
        switch mode {
        case "phone":
            "Phone notifications instead of chat. If this phone hasn't checked in "
                + "within a day, has notifications off, or hasn't seen a reminder yet "
                + "(say, one you just set in chat), that reminder still comes by chat, "
                + "so nothing is missed."
        case "both": "Every reminder comes twice: on this phone and in chat."
        default: "Only in chat. This phone schedules nothing."
        }
    }

    /// A binding that shows the server's value and sends changes to it.
    private func binding<Value: Sendable>(
        _ key: KeyPath<DeliverySettings, Value>, set: @escaping @MainActor (Value) async -> Void
    ) -> Binding<Value> {
        Binding(
            get: { session.settings![keyPath: key] },
            set: { value in Task { await set(value) } })
    }

    private func change(delivery: String? = nil, chatApp: String?? = nil) async {
        busy = true
        defer { busy = false }
        do {
            try await session.updateSettings(delivery: delivery, chatApp: chatApp)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
