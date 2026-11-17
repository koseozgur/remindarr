import Foundation
import OSLog
import UserNotifications

/// Local notifications for the user's pending reminders, so they also go off
/// on the phone when the chat app is quiet. Scheduled from the list the server
/// returns; there is no remote push.
///
/// The phone only knows what it saw at the last refresh (app opened, pulled to
/// refresh, a reminder set or cancelled here, or a background refresh). A
/// reminder set or cancelled in chat since then shows up, or stops, at the
/// next one.
enum PhoneNotifications {
    /// iOS keeps at most 64 pending per app and drops the rest silently.
    static let limit = 60
    private static let prefix = "reminder-"
    private static let log = Logger(subsystem: AppConfig.keychainService, category: "notifications")

    /// What to schedule: the soonest `limit` future occurrences, own
    /// reminders only. One set for someone else is theirs to receive.
    static func plan(_ reminders: [Reminder], now: Date, limit: Int = limit)
        -> [(id: String, at: Date, reminder: Reminder)]
    {
        let all = reminders.filter { $0.forPerson == nil }.flatMap { reminder in
            reminder.upcoming.filter { $0 > now }.map { at in
                (id: "\(prefix)\(reminder.id)-\(Int(at.timeIntervalSince1970))",
                 at: at, reminder: reminder)
            }
        }
        return Array(all.sorted { $0.at < $1.at }.prefix(limit))
    }

    static func status() async -> UNAuthorizationStatus {
        await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }

    static func requestPermission() async -> Bool {
        (try? await UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound])) ?? false
    }

    /// Make what's scheduled match what `reminders` says now.
    ///
    /// Only what's no longer wanted is removed: removal is applied
    /// asynchronously, so removing an id and adding it straight back can lose
    /// it. Adding an id that's already pending replaces it.
    /// Returns whether everything wanted fit under iOS's cap; false also
    /// when notifications aren't allowed, so nothing was scheduled.
    @discardableResult
    static func sync(_ reminders: [Reminder], now: Date = .now) async -> Bool {
        let center = UNUserNotificationCenter.current()
        guard await allowed() else { return false }
        let all = plan(reminders, now: now, limit: .max)
        let wanted = Array(all.prefix(limit))
        let wantedIDs = Set(wanted.map(\.id))
        let stale = await center.pendingNotificationRequests()
            .map(\.identifier).filter { $0.hasPrefix(prefix) && !wantedIDs.contains($0) }
        center.removePendingNotificationRequests(withIdentifiers: stale)
        for item in wanted {
            let content = UNMutableNotificationContent()
            content.title = item.reminder.text
            if let recurrence = item.reminder.recurrence { content.body = recurrence }
            content.sound = .default
            var when = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute, .second], from: item.at)
            when.timeZone = .current  // an absolute moment, even after travelling
            let trigger = UNCalendarNotificationTrigger(dateMatching: when, repeats: false)
            do {
                try await center.add(
                    UNNotificationRequest(identifier: item.id, content: content, trigger: trigger))
            } catch {
                log.error("schedule failed: \(error.localizedDescription, privacy: .public)")
            }
        }
        log.info("scheduled \(wanted.count) of \(all.count), removed \(stale.count)")
        return wanted.count == all.count
    }

    static func allowed() async -> Bool {
        [.authorized, .provisional].contains(await status())
    }

    /// The whole round trip, shared by the app, background refresh and Siri:
    /// read the setting and the list, schedule (or clear, under "chat only"),
    /// and tell the server what this phone now covers. "Phone only" skips
    /// chat only for what such a report covers.
    @discardableResult
    static func refresh(using client: APIClient) async throws -> (DeliverySettings, [Reminder]) {
        async let settingsCall = client.settings()
        async let listCall = client.reminders()
        let (settings, list) = try await (settingsCall, listCall)
        var complete = false
        if settings.schedulesOnPhone {
            complete = await sync(list.reminders)
        } else {
            await removeAllNow()
        }
        let notifying = settings.schedulesOnPhone ? await allowed() : false
        try? await client.reportSync(
            asOf: list.asOf, notifications: notifying, complete: complete)
        return (settings, list.reminders)
    }

    static func removeAllNow() async {
        let center = UNUserNotificationCenter.current()
        let ours = await center.pendingNotificationRequests()
            .map(\.identifier).filter { $0.hasPrefix(prefix) }
        center.removePendingNotificationRequests(withIdentifiers: ours)
    }

    static func removeAll() {
        Task { await removeAllNow() }
    }
}

/// Shows a reminder's banner even while the app is open.
final class NotificationPresenter: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }
}
