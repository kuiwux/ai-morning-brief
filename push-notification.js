// ==================== 硅谷AI晨报 · 推送通知前端逻辑 ====================

const PushNotifications = {
  _vapidPublicKey: null,
  _isSubscribed: false,
  _subscription: null,

  // ==================== INIT ====================
  async init() {
    // Check if browser supports push notifications
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      console.log('[Push] Push notifications not supported');
      return false;
    }

    // Get VAPID public key from server
    try {
      const resp = await fetch('/api/vapid-public-key');
      const data = await resp.json();
      this._vapidPublicKey = data.publicKey;
      console.log('[Push] VAPID public key loaded');
    } catch (err) {
      console.error('[Push] Failed to get VAPID public key:', err);
      return false;
    }

    // Check existing subscription
    await this.checkSubscription();

    return true;
  },

  // ==================== CHECK SUBSCRIPTION ====================
  async checkSubscription() {
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.getSubscription();
      this._isSubscribed = !!subscription;
      this._subscription = subscription;
      console.log('[Push] Subscription status:', this._isSubscribed);
      return this._isSubscribed;
    } catch (err) {
      console.error('[Push] Check subscription failed:', err);
      return false;
    }
  },

  // ==================== REQUEST PERMISSION & SUBSCRIBE ====================
  async subscribe() {
    // Step 1: Request notification permission
    const permission = await this._requestPermission();
    if (permission !== 'granted') {
      console.log('[Push] Permission denied');
      return false;
    }

    // Step 2: Subscribe to push
    try {
      const registration = await navigator.serviceWorker.ready;

      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: this._urlBase64ToUint8Array(this._vapidPublicKey)
      });

      console.log('[Push] Subscribed successfully:', subscription);

      // Step 3: Send subscription to server
      await this._sendSubscriptionToServer(subscription);

      this._isSubscribed = true;
      this._subscription = subscription;

      // Save preference to localStorage
      localStorage.setItem('push_enabled', 'true');

      return true;
    } catch (err) {
      console.error('[Push] Subscribe failed:', err);
      return false;
    }
  },

  // ==================== UNSUBSCRIBE ====================
  async unsubscribe() {
    try {
      if (this._subscription) {
        await this._subscription.unsubscribe();
        await this._sendUnsubscriptionToServer(this._subscription);
        console.log('[Push] Unsubscribed');
      }
      this._isSubscribed = false;
      this._subscription = null;
      localStorage.setItem('push_enabled', 'false');
      return true;
    } catch (err) {
      console.error('[Push] Unsubscribe failed:', err);
      return false;
    }
  },

  // ==================== TOGGLE ====================
  async toggle(enabled) {
    if (enabled) {
      return await this.subscribe();
    } else {
      return await this.unsubscribe();
    }
  },

  // ==================== PRIVATE METHODS ====================

  async _requestPermission() {
    try {
      const result = await Notification.requestPermission();
      console.log('[Push] Permission result:', result);
      return result;
    } catch (err) {
      console.error('[Push] Request permission failed:', err);
      return 'denied';
    }
  },

  async _sendSubscriptionToServer(subscription) {
    try {
      const response = await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          endpoint: subscription.endpoint,
          keys: {
            p256dh: this._arrayBufferToBase64(subscription.getKey('p256dh')),
            auth: this._arrayBufferToBase64(subscription.getKey('auth'))
          },
          userAgent: navigator.userAgent,
          timestamp: new Date().toISOString()
        })
      });

      if (response.ok) {
        console.log('[Push] Subscription sent to server');
      } else {
        console.error('[Push] Server rejected subscription');
      }
    } catch (err) {
      console.error('[Push] Failed to send subscription:', err);
    }
  },

  async _sendUnsubscriptionToServer(subscription) {
    try {
      await fetch('/api/push/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          endpoint: subscription.endpoint
        })
      });
      console.log('[Push] Unsubscription sent to server');
    } catch (err) {
      console.error('[Push] Failed to send unsubscription:', err);
    }
  },

  // ==================== UTILITY: Convert VAPID key ====================
  _urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding)
      .replace(/\-/g, '+')
      .replace(/_/g, '/');

    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);

    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  },

  _arrayBufferToBase64(buffer) {
    let binary = '';
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < bytes.byteLength; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  },

  // ==================== GETTERS ====================
  get isSubscribed() {
    return this._isSubscribed;
  },

  get permissionState() {
    return Notification.permission;
  }
};

// ==================== EXPORT ====================
if (typeof window !== 'undefined') {
  window.PushNotifications = PushNotifications;
}
