import Foundation

/// Identity comes from `Config/App.xcconfig`, never from string literals, so
/// renaming the product is a one-file change.
enum AppConfig {
    static var displayName: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String
            ?? Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String
            ?? "App"
    }

    /// Keychain items are namespaced by bundle id, so a rename starts clean.
    static var keychainService: String {
        Bundle.main.bundleIdentifier ?? "app"
    }

    /// Prefilled on the pair screen. The simulator reaches the Mac's
    /// localhost directly; the dev server is `scripts/dev_api.py serve`.
    static let defaultServerURL = "http://localhost:8787"
}
