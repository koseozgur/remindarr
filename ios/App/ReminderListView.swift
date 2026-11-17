import SwiftUI

struct ReminderListView: View {
    @Environment(Session.self) private var session
    @Environment(\.scenePhase) private var phase
    @State private var composing = false
    @State private var showingSettings = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                if let error {
                    Text(error).foregroundStyle(.red)
                }
                // Asked here, once there is something to be reminded of, and
                // only when tapped: never as a pop-up on first launch.
                if session.notifications == .notDetermined, session.settings?.delivery == "chat",
                   !session.reminders.isEmpty
                {
                    Button("Notify me on this phone too", systemImage: "bell.badge") {
                        Task {
                            do { try await session.enableNotifications() } catch {
                                self.error = error.localizedDescription
                            }
                        }
                    }
                }
                if session.reminders.isEmpty {
                    Text("Nothing pending.").foregroundStyle(.secondary)
                }
                ForEach(session.reminders) { reminder in
                    ReminderRow(reminder: reminder)
                        .swipeActions {
                            Button("Cancel", role: .destructive) {
                                Task { await cancel(reminder) }
                            }
                        }
                }
            }
            .refreshable { await refresh() }
            .task { await refresh() }
            // Coming back to the app re-reads the list, which also re-schedules
            // the phone's notifications for anything set or cancelled in chat.
            .onChange(of: phase) { _, now in
                if now == .active { Task { await refresh() } }
            }
            .navigationTitle(AppConfig.displayName)
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button("New", systemImage: "plus") { composing = true }
                }
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        if let name = session.userName { Text("Signed in as \(name)") }
                        Button("Where reminders arrive", systemImage: "bell") {
                            showingSettings = true
                        }
                        Button("Sign out", role: .destructive) {
                            Task { await session.signOut() }
                        }
                    } label: {
                        Image(systemName: "person.circle")
                    }
                }
            }
            .sheet(isPresented: $composing) { NewReminderView() }
            .sheet(isPresented: $showingSettings) { SettingsView() }
        }
    }

    private func refresh() async {
        do {
            try await session.refresh()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func cancel(_ reminder: Reminder) async {
        do {
            try await session.cancel(reminder)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct ReminderRow: View {
    let reminder: Reminder

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                if reminder.recurrence != nil { Image(systemName: "repeat") }
                Text(reminder.text).font(.body)
            }
            HStack(spacing: 8) {
                Text(reminder.when)
                Text(destination).foregroundStyle(.tertiary)
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    private var destination: String {
        let app = ["line": "LINE", "discord": "Discord", "telegram": "Telegram"][reminder.app]
            ?? reminder.app
        if let person = reminder.forPerson { return "→ \(person) on \(app)" }
        return "→ \(app)"
    }
}
