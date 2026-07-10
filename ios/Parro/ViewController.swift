import UIKit
import WebKit

class ViewController: UIViewController {

    // MARK: - Properties

    private var webView: WKWebView!

    // Loading & Error State
    private var loadingIndicator: UIActivityIndicatorView!
    private var errorView: UIView!
    private var loadTimeoutTimer: Timer?
    private let loadTimeoutSeconds: TimeInterval = 15.0

    // MARK: - URL Configuration
    // Primary: public server; fallback: local dev
    private let serverURL = "https://www.xiaofuinfo.com/app/"
    private let backendBaseURL = "https://www.xiaofuinfo.com"

    // MARK: - Token Storage Keys
    private let tokenKey = "com.parro.authToken"
    private let userNameKey = "com.parro.userName"

    // MARK: - Lifecycle

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        setupWebView()
        setupLoadingIndicator()
        loadWebContent()

        // Listen for URL scheme auth callbacks from AppDelegate/SceneDelegate
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleAuthTokenNotification(_:)),
            name: .parroAuthTokenReceived,
            object: nil
        )
    }

    deinit {
        loadTimeoutTimer?.invalidate()
        webView?.configuration.userContentController.removeAllScriptMessageHandlers()
        NotificationCenter.default.removeObserver(self)
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
        userContentController.add(self, name: "parro_share")
        userContentController.add(self, name: "parro_open_url")
        config.userContentController = userContentController

        webView = WKWebView(frame: .zero, configuration: config)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.scrollView.bounces = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never

        // Pull-to-refresh
        let refreshControl = UIRefreshControl()
        refreshControl.tintColor = UIColor(red: 0.77, green: 0.63, blue: 0.42, alpha: 1.0)
        refreshControl.addTarget(self, action: #selector(handleRefresh), for: .valueChanged)
        webView.scrollView.refreshControl = refreshControl

        view.addSubview(webView)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
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
        startLoading()
        webView.load(request)
    }

    private func setupLoadingIndicator() {
        // Loading spinner
        loadingIndicator = UIActivityIndicatorView(style: .large)
        loadingIndicator.translatesAutoresizingMaskIntoConstraints = false
        loadingIndicator.hidesWhenStopped = true
        loadingIndicator.color = UIColor(red: 0.77, green: 0.63, blue: 0.42, alpha: 1.0) // Gold accent
        view.addSubview(loadingIndicator)
        NSLayoutConstraint.activate([
            loadingIndicator.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            loadingIndicator.centerYAnchor.constraint(equalTo: view.centerYAnchor)
        ])

        // Error view (hidden initially)
        errorView = UIView()
        errorView.translatesAutoresizingMaskIntoConstraints = false
        errorView.isHidden = true
        errorView.backgroundColor = .systemBackground
        view.addSubview(errorView)
        NSLayoutConstraint.activate([
            errorView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            errorView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            errorView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            errorView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor)
        ])

        // Error icon
        let errorIcon = UILabel()
        errorIcon.text = "📡"
        errorIcon.font = .systemFont(ofSize: 48)
        errorIcon.textAlignment = .center
        errorIcon.translatesAutoresizingMaskIntoConstraints = false
        errorView.addSubview(errorIcon)

        // Error message
        let errorLabel = UILabel()
        errorLabel.text = "网络连接失败\n请检查网络后重试"
        errorLabel.numberOfLines = 0
        errorLabel.textAlignment = .center
        errorLabel.font = .systemFont(ofSize: 16)
        errorLabel.textColor = .secondaryLabel
        errorLabel.translatesAutoresizingMaskIntoConstraints = false
        errorView.addSubview(errorLabel)

        // Retry button
        let retryButton = UIButton(type: .system)
        retryButton.setTitle("🔄 重新加载", for: .normal)
        retryButton.titleLabel?.font = .systemFont(ofSize: 17, weight: .medium)
        retryButton.backgroundColor = UIColor(red: 0.77, green: 0.63, blue: 0.42, alpha: 1.0)
        retryButton.setTitleColor(.white, for: .normal)
        retryButton.layer.cornerRadius = 10
        retryButton.translatesAutoresizingMaskIntoConstraints = false
        retryButton.addTarget(self, action: #selector(retryLoadWebContent), for: .touchUpInside)
        errorView.addSubview(retryButton)

        NSLayoutConstraint.activate([
            errorIcon.centerXAnchor.constraint(equalTo: errorView.centerXAnchor),
            errorIcon.bottomAnchor.constraint(equalTo: errorLabel.topAnchor, constant: -16),
            errorLabel.centerXAnchor.constraint(equalTo: errorView.centerXAnchor),
            errorLabel.centerYAnchor.constraint(equalTo: errorView.centerYAnchor),
            errorLabel.leadingAnchor.constraint(equalTo: errorView.leadingAnchor, constant: 32),
            errorLabel.trailingAnchor.constraint(equalTo: errorView.trailingAnchor, constant: -32),
            retryButton.centerXAnchor.constraint(equalTo: errorView.centerXAnchor),
            retryButton.topAnchor.constraint(equalTo: errorLabel.bottomAnchor, constant: 24),
            retryButton.widthAnchor.constraint(equalToConstant: 160),
            retryButton.heightAnchor.constraint(equalToConstant: 44)
        ])
    }

    private func startLoading() {
        loadingIndicator.startAnimating()
        errorView.isHidden = true
        webView.isHidden = true
        // Start timeout timer
        loadTimeoutTimer?.invalidate()
        loadTimeoutTimer = Timer.scheduledTimer(withTimeInterval: loadTimeoutSeconds, repeats: false) { [weak self] _ in
            self?.handleLoadTimeout()
        }
    }

    private func stopLoading() {
        loadingIndicator.stopAnimating()
        loadTimeoutTimer?.invalidate()
        loadTimeoutTimer = nil
        webView.isHidden = false
    }

    private func handleLoadTimeout() {
        print("⏰ Web content load timeout after \(loadTimeoutSeconds)s")
        stopLoading()
        webView.stopLoading()
        showErrorState()
    }

    private func showErrorState() {
        errorView.isHidden = false
        loadingIndicator.stopAnimating()
        webView.isHidden = true
    }

    @objc private func retryLoadWebContent() {
        print("🔄 Retrying web content load...")
        errorView.isHidden = true
        startLoading()
        loadWebContent()
    }

    @objc private func handleRefresh() {
        webView.reload()
    }

    // MARK: - Token Injection

    /// Safely escape a string for embedding in a JS single-quoted string literal
    private func escapeForJS(_ str: String) -> String {
        var escaped = ""
        for char in str {
            switch char {
            case "\\": escaped += "\\\\"
            case "'":  escaped += "\\'"
            case "\n": escaped += "\\n"
            case "\r": escaped += "\\r"
            case "\u{2028}": escaped += "\\u2028"
            case "\u{2029}": escaped += "\\u2029"
            default:   escaped.append(char)
            }
        }
        return escaped
    }

    /// Inject the stored JWT token into the WebView's localStorage
    private func injectAuthToken() {
        guard let token = getStoredToken() else { return }
        let safeToken = escapeForJS(token)
        let js = """
        (function() {
            try {
                window.localStorage.setItem('parro_token', '\(safeToken)');
                window.dispatchEvent(new CustomEvent('parro_auth_changed', { detail: { token: '\(safeToken)' } }));
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

    // MARK: - Token Storage (UserDefaults)

    private func saveToken(_ token: String) {
        UserDefaults.standard.set(token, forKey: tokenKey)
        print("🔑 Token saved to UserDefaults")
    }

    private func getStoredToken() -> String? {
        return UserDefaults.standard.string(forKey: tokenKey)
    }

    private func clearToken() {
        UserDefaults.standard.removeObject(forKey: tokenKey)
        UserDefaults.standard.removeObject(forKey: userNameKey)
        print("🗑 Token cleared from UserDefaults")
    }

    // MARK: - Notification Handlers

    /// Handle generic auth token from parro:// URL scheme
    @objc private func handleAuthTokenNotification(_ notification: Notification) {
        guard let token = notification.userInfo?["token"] as? String else { return }
        print("🔑 Received auth token via URL scheme")
        saveToken(token)
        injectAuthToken()
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
            stopLoading()
            webView.scrollView.refreshControl?.endRefreshing()
            // Inject stored token after page load
            injectAuthToken()
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if webView == self.webView {
            print("❌ Web content failed to load: \(error.localizedDescription)")
            stopLoading()
            showErrorState()
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if webView == self.webView {
            print("❌ Provisional navigation failed: \(error.localizedDescription)")
            stopLoading()
            showErrorState()
        }
    }

    /// Default SSL validation — certificate is valid (Let's Encrypt)
    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        completionHandler(.performDefaultHandling, nil)
    }
}

// MARK: - WKUIDelegate (Required for JS dialogs)

extension ViewController: WKUIDelegate {

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(alert, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "确定", style: .default) { _ in completionHandler(true) })
        present(alert, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
        alert.addTextField { textField in
            textField.text = defaultText
        }
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in completionHandler(nil) })
        alert.addAction(UIAlertAction(title: "确定", style: .default) { _ in
            completionHandler(alert.textFields?.first?.text)
        })
        present(alert, animated: true)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        // Handle target="_blank" links by opening in Safari
        if let url = navigationAction.request.url {
            UIApplication.shared.open(url, options: [:]) { success in
                if !success {
                    print("❌ Failed to open URL: \(url)")
                }
            }
        }
        return nil
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

        case "parro_share":
            handleShare(message: message)

        case "parro_open_url":
            handleOpenURL(message: message)

        default:
            print("⚠️ Unknown JS bridge message: \(message.name)")
        }
    }

    /// Return the stored JWT token to the WebView
    private func handleGetToken(message: WKScriptMessage) {
        let token = getStoredToken() ?? ""
        let safeToken = escapeForJS(token)
        let js = "window.__parro_token_callback && window.__parro_token_callback('\(safeToken)');"

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
        print("💾 Token saved via JS Bridge")
    }

    /// Clear the stored token and notify WebView
    private func handleLogout(message: WKScriptMessage) {
        clearToken()
        notifyWebViewLogout()
        print("🚪 Logout via JS Bridge")
    }

    /// Native Share Sheet
    private func handleShare(message: WKScriptMessage) {
        guard let body = message.body as? [String: Any] else { return }
        let title = body["title"] as? String ?? ""
        let url = body["url"] as? String ?? ""
        let text = body["text"] as? String ?? ""

        var items: [Any] = []
        if !title.isEmpty { items.append(title) }
        if !text.isEmpty { items.append(text) }
        if let shareURL = URL(string: url), !url.isEmpty { items.append(shareURL) }

        guard !items.isEmpty else { return }

        let activityVC = UIActivityViewController(activityItems: items, applicationActivities: nil)
        if let popover = activityVC.popoverPresentationController {
            popover.sourceView = view
            popover.sourceRect = CGRect(x: view.bounds.midX, y: view.bounds.midY, width: 0, height: 0)
        }
        present(activityVC, animated: true)
    }

    /// Open URL in Safari (not in WebView)
    private func handleOpenURL(message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let urlString = body["url"] as? String,
              let url = URL(string: urlString) else { return }
        UIApplication.shared.open(url, options: [:]) { success in
            if !success {
                print("❌ Failed to open URL: \(url)")
            }
        }
    }
}
