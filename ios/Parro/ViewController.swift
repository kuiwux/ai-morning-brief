import UIKit
import WebKit
import AuthenticationServices

class ViewController: UIViewController {

    // MARK: - Properties

    private var webView: WKWebView!
    private var signInBar: UIStackView!
    private var appleSignInButton: ASAuthorizationAppleIDButton!
    private var googleSignInButton: UIButton!
    private var signInBarTopConstraint: NSLayoutConstraint!
    private var authOverlayWebView: WKWebView?
    private var authOverlayBackground: UIView?

    // MARK: - URL Configuration
    // Change this to your PWA server URL
    private let serverURL = "http://localhost:8899"
    private let backendBaseURL = "http://localhost:8899"
    private let googleCallbackScheme = "parro"

    // MARK: - Token Storage Keys
    private let tokenKey = "com.parro.authToken"
    private let userNameKey = "com.parro.userName"

    // MARK: - Lifecycle

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        setupSignInBar()
        setupWebView()
        loadWebContent()
        updateSignInBarVisibility()

        // Listen for URL scheme auth callbacks from AppDelegate/SceneDelegate
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleGoogleAuthNotification(_:)),
            name: .parroGoogleAuthCallback,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleAuthTokenNotification(_:)),
            name: .parroAuthTokenReceived,
            object: nil
        )
    }

    deinit {
        webView?.configuration.userContentController.removeAllScriptMessageHandlers()
        NotificationCenter.default.removeObserver(self)
    }

    // MARK: - Sign-In Bar Setup

    private func setupSignInBar() {
        // Container stack for sign-in buttons
        signInBar = UIStackView()
        signInBar.axis = .horizontal
        signInBar.spacing = 12
        signInBar.alignment = .center
        signInBar.distribution = .fillEqually
        signInBar.translatesAutoresizingMaskIntoConstraints = false
        signInBar.isHidden = true
        view.addSubview(signInBar)

        // Apple Sign-In button (system-provided native button)
        appleSignInButton = ASAuthorizationAppleIDButton(type: .signIn, style: .black)
        appleSignInButton.translatesAutoresizingMaskIntoConstraints = false
        appleSignInButton.addTarget(self, action: #selector(handleAppleSignInTapped), for: .touchUpInside)
        appleSignInButton.heightAnchor.constraint(equalToConstant: 44).isActive = true
        signInBar.addArrangedSubview(appleSignInButton)

        // Google Sign-In button (custom styled)
        googleSignInButton = UIButton(type: .system)
        googleSignInButton.translatesAutoresizingMaskIntoConstraints = false
        googleSignInButton.setTitle("使用 Google 登录", for: .normal)
        googleSignInButton.setTitleColor(.white, for: .normal)
        googleSignInButton.titleLabel?.font = .systemFont(ofSize: 16, weight: .medium)
        googleSignInButton.backgroundColor = UIColor(red: 0.26, green: 0.52, blue: 0.96, alpha: 1.0) // Google blue
        googleSignInButton.layer.cornerRadius = 8
        googleSignInButton.heightAnchor.constraint(equalToConstant: 44).isActive = true
        googleSignInButton.addTarget(self, action: #selector(handleGoogleSignInTapped), for: .touchUpInside)
        signInBar.addArrangedSubview(googleSignInButton)

        // Layout: sign-in bar at the top with safe area inset
        signInBarTopConstraint = signInBar.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 16)
        NSLayoutConstraint.activate([
            signInBarTopConstraint,
            signInBar.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 20),
            signInBar.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -20)
        ])
    }

    // MARK: - WebView Setup

    private func setupWebView() {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true

        // Set up JS Bridge message handlers
        let userContentController = WKUserContentController()
        userContentController.add(self, name: "parro_get_token")
        userContentController.add(self, name: "parro_save_token")
        userContentController.add(self, name: "parro_logout")
        userContentController.add(self, name: "parro_apple_sign_in")
        userContentController.add(self, name: "parro_google_sign_in")
        config.userContentController = userContentController

        webView = WKWebView(frame: .zero, configuration: config)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.scrollView.bounces = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never

        view.addSubview(webView)

        // Initial constraint: WebView starts below sign-in bar
        // This constraint will be replaced by updateSignInBarVisibility()
        webViewToSignInBarConstraint = webView.topAnchor.constraint(equalTo: signInBar.bottomAnchor, constant: 16)
        webViewToSignInBarConstraint?.isActive = true

        NSLayoutConstraint.activate([
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor)
        ])
    }

    // MARK: - Web Content Loading

    private func loadWebContent() {
        guard let url = URL(string: serverURL) else {
            print("❌ Invalid server URL: \(serverURL)")
            return
        }
        let request = URLRequest(url: url)
        webView.load(request)
    }

    // MARK: - Token Injection

    /// Inject the stored JWT token into the WebView's localStorage
    private func injectAuthToken() {
        guard let token = getStoredToken() else { return }
        let js = """
        (function() {
            try {
                window.localStorage.setItem('parro_token', '\(token.replacingOccurrences(of: "'", with: "\\'"))');
                window.dispatchEvent(new CustomEvent('parro_auth_changed', { detail: { token: '\(token.replacingOccurrences(of: "'", with: "\\'"))' } }));
                console.log('[Native] Token injected into WebView');
            } catch(e) {
                console.error('[Native] Failed to inject token:', e);
            }
        })();
        """
        webView.evaluateJavaScript(js) { result, error in
            if let error = error {
                print("❌ Failed to inject token: \(error.localizedDescription)")
            } else {
                print("✅ Token injected successfully")
                self.updateSignInBarVisibility()
            }
        }
    }

    /// Send a logout signal to the WebView
    private func notifyWebViewLogout() {
        let js = """
        (function() {
            try {
                window.localStorage.removeItem('parro_token');
                window.dispatchEvent(new CustomEvent('parro_auth_changed', { detail: { token: null } }));
                console.log('[Native] Logout signaled to WebView');
            } catch(e) {
                console.error('[Native] Failed to signal logout:', e);
            }
        })();
        """
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    // WebView constraints that change based on auth state
    private var webViewToSafeAreaConstraint: NSLayoutConstraint?
    private var webViewToSignInBarConstraint: NSLayoutConstraint?

    // MARK: - Sign-In Bar Visibility

    private func updateSignInBarVisibility() {
        let isLoggedIn = getStoredToken() != nil
        signInBar.isHidden = isLoggedIn

        // Deactivate old constraints
        webViewToSafeAreaConstraint?.isActive = false
        webViewToSignInBarConstraint?.isActive = false

        if isLoggedIn {
            // Move WebView to top (no sign-in bar)
            webViewToSafeAreaConstraint = webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor)
            webViewToSafeAreaConstraint?.isActive = true
        } else {
            // Restore WebView position below sign-in bar
            webViewToSignInBarConstraint = webView.topAnchor.constraint(equalTo: signInBar.bottomAnchor, constant: 16)
            webViewToSignInBarConstraint?.isActive = true
        }
        view.layoutIfNeeded()
    }

    // MARK: - Token Storage (UserDefaults)

    private func saveToken(_ token: String) {
        UserDefaults.standard.set(token, forKey: tokenKey)
        UserDefaults.standard.synchronize()
        print("🔑 Token saved to UserDefaults")
    }

    private func getStoredToken() -> String? {
        return UserDefaults.standard.string(forKey: tokenKey)
    }

    private func clearToken() {
        UserDefaults.standard.removeObject(forKey: tokenKey)
        UserDefaults.standard.removeObject(forKey: userNameKey)
        UserDefaults.standard.synchronize()
        print("🗑 Token cleared from UserDefaults")
    }

    // MARK: - Apple Sign-In

    @objc private func handleAppleSignInTapped() {
        let appleIDProvider = ASAuthorizationAppleIDProvider()
        let request = appleIDProvider.createRequest()
        request.requestedScopes = [.fullName, .email]

        let authorizationController = ASAuthorizationController(authorizationRequests: [request])
        authorizationController.delegate = self
        authorizationController.presentationContextProvider = self
        authorizationController.performRequests()
    }

    /// POST Apple identity token to backend and retrieve JWT
    private func completeAppleAuth(identityToken: String, fullName: PersonNameComponents?) {
        guard let url = URL(string: "\(backendBaseURL)/api/auth/apple/callback") else {
            print("❌ Invalid Apple callback URL")
            showAuthError("Apple 登录配置错误")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        var body: [String: Any] = ["identity_token": identityToken]
        if let fullName = fullName {
            body["name"] = [
                "firstName": fullName.givenName ?? "",
                "lastName": fullName.familyName ?? ""
            ]
        }

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: body, options: [])
        } catch {
            print("❌ Failed to serialize Apple auth body: \(error)")
            showAuthError("Apple 登录数据错误")
            return
        }

        print("🍎 Sending Apple identity token to backend...")
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self = self else { return }

            if let error = error {
                print("❌ Apple auth network error: \(error.localizedDescription)")
                DispatchQueue.main.async {
                    self.handleAppleAuthFallback(identityToken: identityToken, fullName: fullName)
                }
                return
            }

            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let token = json["token"] as? String else {
                print("❌ Invalid response from Apple callback")
                DispatchQueue.main.async {
                    self.handleAppleAuthFallback(identityToken: identityToken, fullName: fullName)
                }
                return
            }

            DispatchQueue.main.async {
                self.saveToken(token)
                self.injectAuthToken()
                print("✅ Apple Sign-In complete, token received")
            }
        }.resume()
    }

    /// Fallback: if backend is unreachable, show a simple alert and let the WebView handle it
    private func handleAppleAuthFallback(identityToken: String, fullName: PersonNameComponents?) {
        print("⚠️ Backend unreachable for Apple auth, falling back to WebView injection")
        // Inject the identity token into WebView so PWA can handle it
        let escapedToken = identityToken.replacingOccurrences(of: "'", with: "\\'")
        let js = """
        (function() {
            window.localStorage.setItem('parro_apple_identity_token', '\(escapedToken)');
            window.dispatchEvent(new CustomEvent('parro_apple_auth_fallback', {
                detail: { identityToken: '\(escapedToken)' }
            }));
        })();
        """
        webView.evaluateJavaScript(js, completionHandler: nil)

        let alert = UIAlertController(
            title: "Apple 登录",
            message: "已获取 Apple 身份信息，但无法连接后端服务。请稍后重试。",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "确定", style: .default))
        present(alert, animated: true)
    }

    // MARK: - Google Sign-In (via WebView overlay)

    @objc private func handleGoogleSignInTapped() {
        // Step 1: Fetch Google OAuth URL from backend
        guard let url = URL(string: "\(backendBaseURL)/api/auth/google/url") else {
            print("❌ Invalid Google OAuth URL endpoint")
            showAuthError("Google 登录配置错误")
            return
        }

        print("🔵 Fetching Google OAuth URL...")
        URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            guard let self = self else { return }

            if let error = error {
                print("❌ Failed to fetch Google OAuth URL: \(error.localizedDescription)")
                DispatchQueue.main.async {
                    self.handleGoogleAuthFallback()
                }
                return
            }

            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let oauthURLString = json["url"] as? String else {
                print("❌ Invalid Google OAuth URL response")
                DispatchQueue.main.async {
                    self.handleGoogleAuthFallback()
                }
                return
            }

            DispatchQueue.main.async {
                self.launchGoogleOAuthOverlay(oauthURL: oauthURLString)
            }
        }.resume()
    }

    /// Launch an overlay WebView for Google OAuth flow
    private func launchGoogleOAuthOverlay(oauthURL: String) {
        guard let url = URL(string: oauthURL) else {
            print("❌ Invalid Google OAuth URL")
            showAuthError("Google 登录 URL 无效")
            return
        }

        // Create a semi-transparent background
        let background = UIView(frame: view.bounds)
        background.backgroundColor = UIColor.black.withAlphaComponent(0.5)
        background.alpha = 0
        background.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(background)
        NSLayoutConstraint.activate([
            background.topAnchor.constraint(equalTo: view.topAnchor),
            background.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            background.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            background.trailingAnchor.constraint(equalTo: view.trailingAnchor)
        ])
        authOverlayBackground = background

        // Add a close button
        let closeButton = UIButton(type: .system)
        closeButton.setTitle("✕ 取消", for: .normal)
        closeButton.setTitleColor(.white, for: .normal)
        closeButton.titleLabel?.font = .systemFont(ofSize: 16, weight: .semibold)
        closeButton.translatesAutoresizingMaskIntoConstraints = false
        closeButton.addTarget(self, action: #selector(dismissAuthOverlay), for: .touchUpInside)
        background.addSubview(closeButton)

        // Create overlay WebView for OAuth
        let config = WKWebViewConfiguration()
        let overlayWebView = WKWebView(frame: .zero, configuration: config)
        overlayWebView.navigationDelegate = self
        overlayWebView.translatesAutoresizingMaskIntoConstraints = false
        overlayWebView.layer.cornerRadius = 12
        overlayWebView.clipsToBounds = true
        background.addSubview(overlayWebView)
        authOverlayWebView = overlayWebView

        NSLayoutConstraint.activate([
            closeButton.topAnchor.constraint(equalTo: background.safeAreaLayoutGuide.topAnchor, constant: 12),
            closeButton.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -20),
            closeButton.heightAnchor.constraint(equalToConstant: 44),

            overlayWebView.topAnchor.constraint(equalTo: closeButton.bottomAnchor, constant: 8),
            overlayWebView.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -40),
            overlayWebView.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 20),
            overlayWebView.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -20)
        ])

        // Animate in
        UIView.animate(withDuration: 0.3) {
            background.alpha = 1
        }

        // Load the OAuth URL
        let request = URLRequest(url: url)
        overlayWebView.load(request)
        print("🔵 Google OAuth overlay launched")
    }

    @objc private func dismissAuthOverlay() {
        UIView.animate(withDuration: 0.3, animations: {
            self.authOverlayBackground?.alpha = 0
        }) { _ in
            self.authOverlayWebView?.stopLoading()
            self.authOverlayWebView?.removeFromSuperview()
            self.authOverlayWebView = nil
            self.authOverlayBackground?.removeFromSuperview()
            self.authOverlayBackground = nil
        }
    }

    /// Fallback: if backend is unreachable, let the WebView PWA handle Google OAuth
    private func handleGoogleAuthFallback() {
        print("⚠️ Backend unreachable for Google OAuth URL, falling back to WebView")
        let js = """
        (function() {
            console.log('[Native] Google auth fallback triggered');
            window.dispatchEvent(new CustomEvent('parro_google_auth_fallback'));
        })();
        """
        webView.evaluateJavaScript(js, completionHandler: nil)

        let alert = UIAlertController(
            title: "Google 登录",
            message: "无法连接后端服务，请检查网络后重试。",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "确定", style: .default))
        present(alert, animated: true)
    }

    // MARK: - Notification Handlers (URL Scheme Callbacks)

    /// Handle Google OAuth callback from parro:// URL scheme
    @objc private func handleGoogleAuthNotification(_ notification: Notification) {
        guard let code = notification.userInfo?["code"] as? String else { return }
        print("🔵 Received Google OAuth code via URL scheme")
        handleGoogleOAuthCallback(code: code)
    }

    /// Handle generic auth token from parro:// URL scheme
    @objc private func handleAuthTokenNotification(_ notification: Notification) {
        guard let token = notification.userInfo?["token"] as? String else { return }
        print("🔑 Received auth token via URL scheme")
        saveToken(token)
        injectAuthToken()
    }

    /// Called when Google OAuth callback URL is intercepted with the authorization code
    private func handleGoogleOAuthCallback(code: String) {
        print("🔵 Google OAuth code received, exchanging for token...")
        dismissAuthOverlay()

        guard let url = URL(string: "\(backendBaseURL)/api/auth/google/callback?code=\(code)") else {
            print("❌ Invalid Google callback URL")
            showAuthError("Google 登录回调错误")
            return
        }

        URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            guard let self = self else { return }

            if let error = error {
                print("❌ Google token exchange error: \(error.localizedDescription)")
                DispatchQueue.main.async {
                    self.showAuthError("Google 登录失败，请重试")
                }
                return
            }

            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let token = json["token"] as? String else {
                print("❌ Invalid token response from Google callback")
                DispatchQueue.main.async {
                    self.showAuthError("Google 登录验证失败")
                }
                return
            }

            DispatchQueue.main.async {
                self.saveToken(token)
                self.injectAuthToken()
                print("✅ Google Sign-In complete, token received")
            }
        }.resume()
    }

    // MARK: - Utility

    private func showAuthError(_ message: String) {
        let alert = UIAlertController(
            title: "登录失败",
            message: message,
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "确定", style: .default))
        present(alert, animated: true)
    }

    // MARK: - Status Bar

    override var prefersStatusBarHidden: Bool {
        return false
    }

    override var preferredStatusBarStyle: UIStatusBarStyle {
        return .default
    }
}

// MARK: - WKNavigationDelegate

extension ViewController: WKNavigationDelegate {

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView == self.webView {
            print("✅ Web content loaded successfully")
            // Inject stored token after page load
            injectAuthToken()
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if webView == self.webView {
            print("❌ Web content failed to load: \(error.localizedDescription)")
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if webView == self.webView {
            print("❌ Provisional navigation failed: \(error.localizedDescription)")
        }
    }

    /// Intercept navigation in the Google OAuth overlay to capture the authorization code
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // Only intercept in the auth overlay, not the main WebView
        guard webView == authOverlayWebView else {
            decisionHandler(.allow)
            return
        }

        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }

        let urlString = url.absoluteString
        print("🔍 Auth overlay navigating to: \(urlString)")

        // Check if this is the Google OAuth callback (contains authorization code)
        // The backend callback URL typically looks like:
        // http://localhost:8899/api/auth/google/callback?code=xxx&...
        if urlString.contains("/api/auth/google/callback") || urlString.contains("code=") {
            // Parse the authorization code from URL query parameters
            if let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
               let codeItem = components.queryItems?.first(where: { $0.name == "code" }),
               let code = codeItem.value {
                decisionHandler(.cancel)
                handleGoogleOAuthCallback(code: code)
                return
            }

            // Also try parsing from URL fragment or other patterns
            if let regex = try? NSRegularExpression(pattern: "[?&]code=([^&]+)", options: []),
               let match = regex.firstMatch(in: urlString, options: [], range: NSRange(urlString.startIndex..., in: urlString)),
               let range = Range(match.range(at: 1), in: urlString) {
                let code = String(urlString[range])
                decisionHandler(.cancel)
                handleGoogleOAuthCallback(code: code)
                return
            }
        }

        decisionHandler(.allow)
    }
}

// MARK: - WKScriptMessageHandler (JS Bridge)

extension ViewController: WKScriptMessageHandler {

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        switch message.name {
        case "parro_get_token":
            handleGetToken(message: message)

        case "parro_save_token":
            handleSaveToken(message: message)

        case "parro_logout":
            handleLogout(message: message)

        case "parro_apple_sign_in":
            // WebView requested Apple Sign-In
            DispatchQueue.main.async {
                self.handleAppleSignInTapped()
            }

        case "parro_google_sign_in":
            // WebView requested Google Sign-In
            DispatchQueue.main.async {
                self.handleGoogleSignInTapped()
            }

        default:
            print("⚠️ Unknown JS bridge message: \(message.name)")
        }
    }

    /// Return the stored JWT token to the WebView
    private func handleGetToken(message: WKScriptMessage) {
        let token = getStoredToken() ?? ""
        let js = "window.__parro_token_callback && window.__parro_token_callback('\(token.replacingOccurrences(of: "'", with: "\\'"))');"

        webView.evaluateJavaScript(js) { _, error in
            if let error = error {
                print("❌ Failed to return token via callback: \(error.localizedDescription)")
            }
        }
        print("📤 Token requested by WebView: \(token.isEmpty ? "none" : "returned")")
    }

    /// Save a JWT token sent from the WebView
    private func handleSaveToken(message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let token = body["token"] as? String else {
            print("❌ Invalid save_token payload")
            return
        }
        saveToken(token)
        updateSignInBarVisibility()
        print("💾 Token saved via JS Bridge")
    }

    /// Clear the stored token and notify WebView
    private func handleLogout(message: WKScriptMessage) {
        clearToken()
        notifyWebViewLogout()
        updateSignInBarVisibility()
        print("🚪 Logout via JS Bridge")
    }
}

// MARK: - ASAuthorizationControllerDelegate (Apple Sign-In)

extension ViewController: ASAuthorizationControllerDelegate {

    func authorizationController(controller: ASAuthorizationController, didCompleteWithAuthorization authorization: ASAuthorization) {
        if let appleIDCredential = authorization.credential as? ASAuthorizationAppleIDCredential {
            // Extract identity token
            guard let identityTokenData = appleIDCredential.identityToken,
                  let identityToken = String(data: identityTokenData, encoding: .utf8) else {
                print("❌ Failed to extract Apple identity token")
                showAuthError("无法获取 Apple 身份信息")
                return
            }

            let fullName = appleIDCredential.fullName
            let email = appleIDCredential.email

            // Log user info (email is only available on first sign-in)
            if let email = email {
                print("🍎 Apple Sign-In: email=\(email)")
            }
            if let givenName = fullName?.givenName {
                print("🍎 Apple Sign-In: name=\(givenName) \(fullName?.familyName ?? "")")
                let displayName = "\(givenName) \(fullName?.familyName ?? "")".trimmingCharacters(in: .whitespaces)
                if !displayName.isEmpty {
                    UserDefaults.standard.set(displayName, forKey: userNameKey)
                }
            }

            print("🍎 Apple identity token received, sending to backend...")
            completeAppleAuth(identityToken: identityToken, fullName: fullName)
        }
    }

    func authorizationController(controller: ASAuthorizationController, didCompleteWithError error: Error) {
        let nsError = error as NSError
        if nsError.code == ASAuthorizationError.canceled.rawValue {
            print("🍎 Apple Sign-In cancelled by user")
        } else {
            print("❌ Apple Sign-In error: \(error.localizedDescription)")
            showAuthError("Apple 登录出错：\(error.localizedDescription)")
        }
    }
}

// MARK: - ASAuthorizationControllerPresentationContextProviding

extension ViewController: ASAuthorizationControllerPresentationContextProviding {

    func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
        return view.window!
    }
}
