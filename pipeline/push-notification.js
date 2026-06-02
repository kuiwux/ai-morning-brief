/**
 * push-notification.js
 * PWA 前端推送注册 — Web Push + 通知权限
 * ============================================
 * 功能:
 *   1. 请求通知权限 Notification.requestPermission()
 *   2. 注册 Service Worker push 事件
 *   3. 订阅 PushManager → 发送 subscription 到后端
 *   4. 点击通知 → 打开对应文章
 *   5. 通知分组 (tag)
 *
 * 依赖:
 *   - Service Worker: /sw.js (由 server.py 提供)
 *   - 后端 API: /api/v2/push/subscribe
 */

(function () {
  'use strict';

  // ── 配置 ─────────────────────────────────────────────────────────────────
  const API_BASE = window.location.origin;
  const SW_URL = '/sw.js';

  // ── 状态 ─────────────────────────────────────────────────────────────────
  let isSupported = false;
  let isSubscribed = false;
  let currentSubscription = null;

  // ── 检测浏览器支持 ───────────────────────────────────────────────────────
  function checkSupport() {
    if (!('serviceWorker' in navigator)) {
      console.log('[Push] ❌ Service Worker 不支持');
      return false;
    }
    if (!('PushManager' in window)) {
      console.log('[Push] ❌ Push API 不支持');
      return false;
    }
    if (!('Notification' in window)) {
      console.log('[Push] ❌ Notification API 不支持');
      return false;
    }
    isSupported = true;
    console.log('[Push] ✅ 浏览器支持 Web Push');
    return true;
  }

  // ── 注册 Service Worker ──────────────────────────────────────────────────
  async function registerServiceWorker() {
    try {
      const registration = await navigator.serviceWorker.register(SW_URL, {
        scope: '/',
      });
      console.log('[Push] ✅ Service Worker 已注册:', registration.scope);

      // 等待 SW 就绪
      await navigator.serviceWorker.ready;
      return registration;
    } catch (err) {
      console.error('[Push] ❌ Service Worker 注册失败:', err);
      return null;
    }
  }

  // ── URL-safe base64 转 Uint8Array ────────────────────────────────────────
  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding)
      .replace(/\-/g, '+')
      .replace(/_/g, '/');

    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);

    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  // ── 获取 VAPID 公钥 ─────────────────────────────────────────────────────
  async function getVapidPublicKey() {
    try {
      const resp = await fetch(`${API_BASE}/api/v2/push/vapid-key`);
      const data = await resp.json();
      if (data.publicKey && data.available) {
        console.log('[Push] ✅ VAPID 公钥已获取');
        return data.publicKey;
      }
      console.warn('[Push] ⚠️ VAPID 公钥不可用');
      return null;
    } catch (err) {
      console.error('[Push] ❌ 获取 VAPID 公钥失败:', err);
      return null;
    }
  }

  // ── 订阅 Push Manager ────────────────────────────────────────────────────
  async function subscribeToPush(registration) {
    try {
      const vapidPublicKey = await getVapidPublicKey();
      if (!vapidPublicKey) {
        console.warn('[Push] ⚠️ 无 VAPID 公钥，跳过订阅');
        return null;
      }

      const convertedKey = urlBase64ToUint8Array(vapidPublicKey);

      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: convertedKey,
      });

      console.log('[Push] ✅ 已订阅 Push Manager');
      return subscription;
    } catch (err) {
      if (Notification.permission === 'denied') {
        console.warn('[Push] ⚠️ 用户拒绝了通知权限');
      } else {
        console.error('[Push] ❌ 订阅失败:', err);
      }
      return null;
    }
  }

  // ── 发送 subscription 到后端 ─────────────────────────────────────────────
  async function sendSubscriptionToServer(subscription) {
    try {
      const subscriptionJSON = subscription.toJSON();
      const payload = {
        platform: 'web',
        subscription: subscriptionJSON,
        user_id: window._userId || '',
      };

      const resp = await fetch(`${API_BASE}/api/v2/push/subscribe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const data = await resp.json();
      if (data.status === 'ok') {
        console.log('[Push] ✅ 设备已注册到后端');
        return true;
      } else {
        console.error('[Push] ❌ 后端注册失败:', data);
        return false;
      }
    } catch (err) {
      console.error('[Push] ❌ 发送订阅信息失败:', err);
      return false;
    }
  }

  // ── 请求通知权限 ─────────────────────────────────────────────────────────
  async function requestNotificationPermission() {
    if (Notification.permission === 'granted') {
      console.log('[Push] ✅ 通知权限已授权');
      return true;
    }

    if (Notification.permission === 'denied') {
      console.log('[Push] ❌ 通知权限已被拒绝');
      return false;
    }

    try {
      const permission = await Notification.requestPermission();
      if (permission === 'granted') {
        console.log('[Push] ✅ 用户授予通知权限');
        return true;
      } else {
        console.log('[Push] ⚠️ 用户拒绝了通知权限');
        return false;
      }
    } catch (err) {
      console.error('[Push] ❌ 请求通知权限失败:', err);
      return false;
    }
  }

  // ── 取消订阅 ─────────────────────────────────────────────────────────────
  async function unsubscribeFromPush() {
    if (!currentSubscription) {
      console.log('[Push] 无活跃订阅');
      return;
    }

    try {
      await currentSubscription.unsubscribe();

      // 通知后端
      const subJSON = currentSubscription.toJSON();
      await fetch(`${API_BASE}/api/v2/push/unsubscribe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token: subJSON.endpoint,
        }),
      });

      currentSubscription = null;
      isSubscribed = false;
      console.log('[Push] ✅ 已取消订阅');
    } catch (err) {
      console.error('[Push] ❌ 取消订阅失败:', err);
    }
  }

  // ── 检查当前订阅状态 ─────────────────────────────────────────────────────
  async function checkExistingSubscription(registration) {
    try {
      const subscription = await registration.pushManager.getSubscription();
      if (subscription) {
        currentSubscription = subscription;
        isSubscribed = true;
        console.log('[Push] ✅ 已有活跃订阅');
        return true;
      }
      return false;
    } catch (err) {
      console.error('[Push] ❌ 检查订阅状态失败:', err);
      return false;
    }
  }

  // ── 添加推送通知 UI 按钮 ─────────────────────────────────────────────────
  function addPushToggleButton() {
    // 在 stats-bar 右侧添加推送开关
    const statsBar = document.querySelector('.stats-bar');
    if (!statsBar) return;

    const btn = document.createElement('button');
    btn.className = 'global-voice-btn push-toggle-btn';
    btn.id = 'push-toggle-btn';
    btn.title = '推送通知设置';
    updateToggleButton(btn);
    btn.addEventListener('click', handleToggleClick);
    statsBar.appendChild(btn);

    // 监听通知权限变化
    if ('permissions' in navigator) {
      navigator.permissions.query({ name: 'notifications' }).then((status) => {
        status.addEventListener('change', () => updateToggleButton(btn));
      });
    }
  }

  function updateToggleButton(btn) {
    const perm = Notification.permission;
    if (perm === 'granted' && isSubscribed) {
      btn.innerHTML = '🔔 推送已开启';
      btn.style.borderColor = '#66bb6a';
      btn.style.color = '#2e7d32';
    } else if (perm === 'denied') {
      btn.innerHTML = '🔕 推送已阻止';
      btn.style.borderColor = '#ef5350';
      btn.style.color = '#c62828';
    } else {
      btn.innerHTML = '🔔 开启推送';
      btn.style.borderColor = '';
      btn.style.color = '';
    }
  }

  async function handleToggleClick() {
    const btn = document.getElementById('push-toggle-btn');

    if (isSubscribed) {
      // 取消订阅
      await unsubscribeFromPush();
      updateToggleButton(btn);
    } else {
      // 重新订阅
      if (Notification.permission === 'denied') {
        alert('通知权限已被阻止，请在浏览器设置中重新开启。\n\nChrome: 地址栏左侧 🔒 → 通知 → 允许');
        return;
      }
      const ok = await initPushNotifications();
      if (ok) {
        updateToggleButton(btn);
      }
    }
  }

  // ── 主导出函数：初始化推送通知 ───────────────────────────────────────────
  async function initPushNotifications() {
    if (!checkSupport()) {
      return false;
    }

    // 1. 注册 Service Worker
    const registration = await registerServiceWorker();
    if (!registration) {
      return false;
    }

    // 2. 检查已有订阅
    const hasSubscription = await checkExistingSubscription(registration);
    if (hasSubscription) {
      // 重新发送到后端以确保同步
      await sendSubscriptionToServer(currentSubscription);
      return true;
    }

    // 3. 请求通知权限
    const permissionGranted = await requestNotificationPermission();
    if (!permissionGranted) {
      return false;
    }

    // 4. 订阅 Push Manager
    const subscription = await subscribeToPush(registration);
    if (!subscription) {
      return false;
    }

    currentSubscription = subscription;
    isSubscribed = true;

    // 5. 发送订阅到后端
    await sendSubscriptionToServer(subscription);

    return true;
  }

  // ── 服务端推送事件处理（在 SW 注册后设置） ──────────────────────────────
  function setupMessageListener() {
    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data && event.data.type === 'NOTIFICATION_CLICK') {
        const url = event.data.url || '/';
        // 如果页面已经打开则导航，否则打开新窗口
        if (document.hidden) {
          window.location.href = url;
        }
      }
    });
  }

  // ── 通过 postMessage 请求当前 subscription ──────────────────────────────
  function getCurrentSubscriptionJSON() {
    if (currentSubscription) {
      return currentSubscription.toJSON();
    }
    return null;
  }

  // ── 自动初始化 ───────────────────────────────────────────────────────────
  async function autoInit() {
    if (!checkSupport()) {
      console.log('[Push] 浏览器不支持 Web Push，跳过初始化');
      return;
    }

    // 只在用户已授权或未决定时自动初始化
    if (Notification.permission === 'denied') {
      console.log('[Push] 通知权限已拒绝，跳过自动初始化');
      addPushToggleButton();
      return;
    }

    // 延迟初始化，避免影响首屏加载
    setTimeout(async () => {
      await initPushNotifications();
      setupMessageListener();
    }, 3000);
  }

  // ── 暴露到全局 ──────────────────────────────────────────────────────────
  window.PushNotifications = {
    init: initPushNotifications,
    unsubscribe: unsubscribeFromPush,
    getSubscription: getCurrentSubscriptionJSON,
    isSupported: () => isSupported,
    isSubscribed: () => isSubscribed,
  };

  // ── 启动 ─────────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      addPushToggleButton();
      autoInit();
    });
  } else {
    addPushToggleButton();
    autoInit();
  }

  console.log('[Push] 📡 push-notification.js 已加载');
})();
