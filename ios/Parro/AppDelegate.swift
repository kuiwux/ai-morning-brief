import UIKit

@main
class AppDelegate: UIResponder, UIApplicationDelegate {

    // MARK: - Shared Auth Token (accessible from anywhere)
    static var pendingAuthToken: String?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        return true
    }

    // MARK: UISceneSession Lifecycle

    func application(_ application: UIApplication, configurationForConnecting connectingSceneSession: UISceneSession, options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        return UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
    }

    func application(_ application: UIApplication, didDiscardSceneSessions sceneSessions: Set<UISceneSession>) {
    }

    // MARK: - URL Scheme Handling (parro://)
    // Handle URL scheme callbacks for OAuth flows
    func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey : Any] = [:]) -> Bool {
        return handleParroURL(url)
    }

    /// Parse parro:// URLs and extract auth parameters
    /// Supported formats:
    ///   parro://auth?token=xxx
    private func handleParroURL(_ url: URL) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            print("❌ Invalid parro:// URL: \(url)")
            return false
        }

        print("🔗 Received parro:// URL: \(url)")

        // Handle generic auth token
        if let token = components.queryItems?.first(where: { $0.name == "token" })?.value {
            AppDelegate.pendingAuthToken = token
            NotificationCenter.default.post(name: .parroAuthTokenReceived, object: nil, userInfo: ["token": token])
            print("🔑 Auth token received via URL scheme")
            return true
        }

        print("⚠️ Unrecognized parro:// URL format: \(url)")
        return false
    }
}

// MARK: - Notification Names for Auth Events

extension Notification.Name {
    static let parroAuthTokenReceived = Notification.Name("parroAuthTokenReceived")
}
