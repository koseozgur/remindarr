import SwiftUI

struct PairView: View {
    @Environment(Session.self) private var session
    @State private var server = Keychain.read("server") ?? AppConfig.defaultServerURL
    @State private var code = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Send /pair to the bot on LINE or Discord, then enter the code it gives you.")
                        .font(.callout)
                }
                Section("Server") {
                    TextField("https://…", text: $server)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                Section("Code") {
                    TextField("XXXX-XXXX", text: $code)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        .font(.title3.monospaced())
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button(busy ? "Pairing…" : "Pair") { Task { await pair() } }
                        .disabled(busy || code.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .navigationTitle(AppConfig.displayName)
            .task { await autoPairForDebugging() }
        }
    }

    /// Debug builds only: `-devPairCode XXXX-XXXX` as a launch argument pairs
    /// without typing, for headless checks against the local dev server.
    private func autoPairForDebugging() async {
        #if DEBUG
        if let devCode = UserDefaults.standard.string(forKey: "devPairCode"), code.isEmpty {
            code = devCode
            await pair()
        }
        #endif
    }

    private func pair() async {
        busy = true
        defer { busy = false }
        do {
            try await session.pair(server: server, code: code)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
