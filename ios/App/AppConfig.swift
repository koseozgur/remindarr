import Foundation

/// Identity comes from `Config/App.xcconfig`, never from string literals, so
/// renaming the product is a one-file change.
enum AppConfig {
    static var displayName: String {
        info("CFBundleDisplayName") ?? info("CFBundleName") ?? "App"
    }

    /// Keychain items are namespaced by bundle id, so a rename starts clean.
    static var keychainService: String {
        Bundle.main.bundleIdentifier ?? "app"
    }

    /// The background refresh task, declared in Info.plist from the bundle id.
    static var refreshTaskID: String { "\(keychainService).refresh" }

    /// Prefilled on the pair screen: the real server in Release, the local
    /// dev server (`scripts/dev_api.py serve`) in Debug. Set per build in
    /// `App.xcconfig`, overridable in `Local.xcconfig`.
    static var defaultServerURL: String {
        info("AppServerURL") ?? "http://localhost:8787"
    }

    /// A token from `Local.xcconfig`, so the owner's own builds start signed
    /// in. Debug builds only: a Release build (TestFlight, the App Store)
    /// never reads it, and the xcconfig leaves it empty there anyway.
    static var bakedToken: String? {
        #if DEBUG
        return info("AppBakedToken")
        #else
        return nil
        #endif
    }

    private static func info(_ key: String) -> String? {
        guard let value = Bundle.main.object(forInfoDictionaryKey: key) as? String else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? nil : trimmed
    }
}
