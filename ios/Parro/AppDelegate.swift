import UIKit

@main
class AppDelegate: UIResponder, UIApplicationDelegate {

    // MARK: - Shared Auth Token (accessible from anywhere)
    static var pendingAuthCode: String?

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
    ///   parro://google-auth?code=xxx
    ///   parro://auth?token=xxx
    private func handleParroURL(_ url: URL) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            print("❌ Invalid parro:// URL: \(url)")
            return false
        }

        print("🔗 Received parro:// URL: \(url)")

        // Handle Google OAuth callback
        if url.host == "google-auth", let code = components.queryItems?.first(where: { $0.name == "code" })?.value {
            AppDelegate.pendingAuthCode = code
            // Post notification so ViewController can handle it
            NotificationCenter.default.post(name: .parroGoogleAuthCallback, object: nil, userInfo: ["code": code])
            print("🔵 Google OAuth code received: \(code.prefix(10))...")
            return true
        }

        // Handle generic auth token
        if let token = components.queryItems?.first(where: { $0.name == "token" })?.value {
            AppDelegate.pendingAuthCode = token
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
    static let parroGoogleAuthCallback = Notification.Name("parroGoogleAuthCallback")
    static let parroAuthTokenReceived = Notification.Name("parroAuthTokenReceived")
}
