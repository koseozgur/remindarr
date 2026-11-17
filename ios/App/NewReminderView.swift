import SwiftUI

/// One text field, read by the server exactly as a chat message would be.
struct NewReminderView: View {
    @Environment(Session.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var error: String?
    @State private var confirmation: String?
    @State private var busy = false
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("call mum tomorrow at 9", text: $text, axis: .vertical)
                        .focused($focused)
                        .submitLabel(.send)
                        .onSubmit { Task { await send() } }
                } footer: {
                    Text("Say what and when, as you would in chat: “bins every monday at 7pm”.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .navigationTitle("New reminder")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(busy ? "Sending…" : "Set") { Task { await send() } }
                        .disabled(busy || text.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .onAppear { focused = true }
            // The confirmation states the resolved time, so a misread shows at once.
            .alert(
                "Set",
                isPresented: Binding(
                    get: { confirmation != nil }, set: { if !$0 { confirmation = nil } })
            ) {
                Button("OK") { dismiss() }
            } message: {
                Text(confirmation ?? "")
            }
        }
    }

    private func send() async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let created = try await session.create(trimmed)
            error = nil
            confirmation = ([created.confirmation] + created.notes).joined(separator: "\n")
        } catch {
            self.error = error.localizedDescription
        }
    }
}
