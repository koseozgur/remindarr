import SwiftUI

@main
struct AppMain: App {
    @State private var session = Session()

    var body: some Scene {
        WindowGroup {
            Group {
                if session.isPaired {
                    ReminderListView()
                } else {
                    PairView()
                }
            }
            .environment(session)
            // A light retro nod; the visual style is tentative.
            .fontDesign(.monospaced)
        }
    }
}
