import UIKit

class SceneDelegate: UIResponder, UIWindowSceneDelegate {

    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = (scene as? UIWindowScene) else { return }
        
        let window = UIWindow(windowScene: windowScene)
        let viewController = ViewController()
        window.rootViewController = viewController
        window.makeKeyAndVisible()
        self.window = window

        // Handle URL context if app was launched via URL scheme
        if let urlContext = connectionOptions.urlContexts.first {
            handleURLContext(urlContext)
        }
    }

    /// Handle URL scheme callback when app is already running
    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        for context in URLContexts {
            handleURLContext(context)
        }
    }

    private func handleURLContext(_ urlContext: UIOpenURLContext) {
        let url = urlContext.url
        print("🔗 SceneDelegate received URL: \(url)")

        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return }

        // Route generic auth token
        if let token = components.queryItems?.first(where: { $0.name == "token" })?.value {
            AppDelegate.pendingAuthToken = token
            NotificationCenter.default.post(name: .parroAuthTokenReceived, object: nil, userInfo: ["token": token])
        }
    }

    func sceneDidDisconnect(_ scene: UIScene) {
    }

    func sceneDidBecomeActive(_ scene: UIScene) {
        // Check for pending auth token when app becomes active
        if let token = AppDelegate.pendingAuthToken {
            AppDelegate.pendingAuthToken = nil
            NotificationCenter.default.post(name: .parroAuthTokenReceived, object: nil, userInfo: ["token": token])
        }
    }

    func sceneWillResignActive(_ scene: UIScene) {
    }

    func sceneWillEnterForeground(_ scene: UIScene) {
    }

    func sceneDidEnterBackground(_ scene: UIScene) {
    }
}
