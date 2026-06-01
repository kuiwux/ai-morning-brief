// ==================== 硅谷AI晨报 · Service Worker ====================
const CACHE_NAME = 'ai-morning-news-v1';
const OFFLINE_URL = '/offline.html';

// Resources to pre-cache on install
const PRECACHE_RESOURCES = [
  '/',
  '/index.html',
  '/offline.html',
  '/privacy.html',
  '/manifest.json',
  '/push-notification.js',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png',
  '/icons/favicon-32.png'
];

// ==================== INSTALL ====================
self.addEventListener('install', (event) => {
  console.log('[SW] Installing Service Worker...');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        console.log('[SW] Pre-caching resources...');
        return cache.addAll(PRECACHE_RESOURCES);
      })
      .then(() => {
        console.log('[SW] Pre-cache complete');
        return self.skipWaiting();
      })
      .catch((err) => {
        console.error('[SW] Pre-cache failed:', err);
      })
  );
});

// ==================== ACTIVATE ====================
self.addEventListener('activate', (event) => {
  console.log('[SW] Activating Service Worker...');
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => {
            console.log('[SW] Deleting old cache:', name);
            return caches.delete(name);
          })
      );
    }).then(() => {
      console.log('[SW] Claiming clients...');
      return self.clients.claim();
    })
  );
});

// ==================== FETCH: Network First ====================
self.addEventListener('fetch', (event) => {
  // Only handle GET requests
  if (event.request.method !== 'GET') return;

  // Skip chrome-extension and non-http(s) requests
  const url = new URL(event.request.url);
  if (!url.protocol.startsWith('http')) return;

  event.respondWith(networkFirst(event.request));
});

async function networkFirst(request) {
  const url = new URL(request.url);

  // For navigation requests (HTML pages), try network first, fallback to offline page
  if (request.mode === 'navigate') {
    try {
      const networkResponse = await fetch(request, { cache: 'no-store' });
      // Cache the fresh response
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, networkResponse.clone());
      return networkResponse;
    } catch (error) {
      console.log('[SW] Network failed for navigation, serving offline page:', url.pathname);
      // Try cached version first
      const cachedResponse = await caches.match(request);
      if (cachedResponse) return cachedResponse;
      // Fallback to offline page
      return caches.match(OFFLINE_URL);
    }
  }

  // For other resources (JS, CSS, images, API), network first with cache fallback
  try {
    const networkResponse = await fetch(request, { cache: 'no-store' });

    // Cache successful responses
    if (networkResponse && networkResponse.status === 200) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, networkResponse.clone());
    }

    return networkResponse;
  } catch (error) {
    console.log('[SW] Network failed, trying cache:', url.pathname);
    const cachedResponse = await caches.match(request);

    if (cachedResponse) {
      return cachedResponse;
    }

    // For API requests that aren't cached, return a JSON error
    if (url.pathname.startsWith('/api/')) {
      return new Response(
        JSON.stringify({ error: 'offline', message: '需要网络连接' }),
        {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        }
      );
    }

    // For images, return a placeholder
    if (request.destination === 'image') {
      return new Response(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect fill="#E5E0D8" width="200" height="200"/><text fill="#A0A0A5" x="50%" y="50%" text-anchor="middle" dy=".3em" font-size="14">需要网络加载</text></svg>',
        { headers: { 'Content-Type': 'image/svg+xml' } }
      );
    }

    // Default error
    return new Response('Network error', { status: 408 });
  }
}

// ==================== PUSH NOTIFICATIONS ====================
self.addEventListener('push', (event) => {
  console.log('[SW] Push event received:', event);

  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = {
        title: '硅谷AI晨报',
        body: event.data.text(),
        icon: '/icons/icon-192.png'
      };
    }
  }

  const options = {
    body: data.body || '有新的AI资讯',
    icon: data.icon || '/icons/icon-192.png',
    badge: '/icons/favicon-32.png',
    tag: data.tag || 'ai-news',
    data: {
      url: data.url || '/',
      ...data
    },
    vibrate: [200, 100, 200],
    requireInteraction: data.requireInteraction || false,
    actions: data.actions || [],
    // Group similar notifications
    renotify: data.renotify || false
  };

  event.waitUntil(
    self.registration.showNotification(
      data.title || '硅谷AI晨报',
      options
    )
  );
});

// ==================== NOTIFICATION CLICK ====================
self.addEventListener('notificationclick', (event) => {
  console.log('[SW] Notification clicked:', event);

  event.notification.close();

  const urlToOpen = event.notification.data?.url || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((clientList) => {
        // If a window is already open, focus it and navigate
        for (const client of clientList) {
          if (client.url.includes(self.location.origin) && 'focus' in client) {
            client.focus();
            client.postMessage({
              type: 'NOTIFICATION_CLICK',
              url: urlToOpen,
              data: event.notification.data
            });
            return;
          }
        }
        // Otherwise open a new window
        if (clients.openWindow) {
          return clients.openWindow(urlToOpen);
        }
      })
  );
});

// ==================== MESSAGE HANDLING ====================
self.addEventListener('message', (event) => {
  console.log('[SW] Message received:', event.data);

  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }

  if (event.data && event.data.type === 'CACHE_URLS') {
    const urls = event.data.urls || [];
    event.waitUntil(
      caches.open(CACHE_NAME).then((cache) => {
        return cache.addAll(urls);
      })
    );
  }
});

console.log('[SW] Service Worker registered successfully!');
