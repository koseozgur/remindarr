import BackgroundTasks
import SwiftUI
import UserNotifications

@main
struct AppMain: App {
    @State private var session = Session()
    @Environment(\.scenePhase) private var phase
    private let presenter = NotificationPresenter()

    init() {
        UNUserNotificationCenter.current().delegate = presenter
    }

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
        .onChange(of: phase) { _, now in
            if now == .background { Self.scheduleRefresh() }
        }
        // Keeps the phone's notifications current when the app isn't opened
        // for a while. iOS decides when (and whether) this runs.
        .backgroundTask(.appRefresh(AppConfig.refreshTaskID)) {
            Self.scheduleRefresh()
            guard let client = Session.storedClient(),
                  let reminders = try? await client.reminders()
            else { return }
            await PhoneNotifications.sync(reminders)
        }
    }

    nonisolated static func scheduleRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: AppConfig.refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 4 * 3600)
        try? BGTaskScheduler.shared.submit(request)
    }
}
