// sw.js — Service worker for Assist (network-first for static assets, cache fallback when offline)
const VERSION = 'assist-v3-001';
const STATIC_CACHE = 'assist-static-' + VERSION;
const STATIC_URLS = [
    '/',
    '/css/fonts.css',
    '/css/base.css',
    '/css/status-bar.css',
    '/css/input.css',
    '/css/terminal.css',
    '/css/drawers.css',
    '/css/widgets.css',
    '/css/commands.css',
    '/js/state.js',
    '/js/ui.js',
    '/js/input.js',
    '/js/terminal.js',
    '/js/actions.js',
    '/js/commands.js',
    '/js/monitor.js',
    '/js/app.js',
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(STATIC_CACHE).then(cache => cache.addAll(STATIC_URLS))
    );
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== STATIC_CACHE).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', event => {
    // Never intercept non-GET requests
    if (event.request.method !== 'GET') return;

    const url = new URL(event.request.url);

    // Network-only for API, poll, terminal, sudo password, WebSocket
    if (
        url.pathname.startsWith('/poll') ||
        url.pathname.startsWith('/terminal/') ||
        url.pathname.startsWith('/type') ||
        url.pathname.startsWith('/paste') ||
        url.pathname.startsWith('/copy') ||
        url.pathname.startsWith('/key') ||
        url.pathname.startsWith('/upload') ||
        url.pathname.startsWith('/history') ||
        url.pathname.startsWith('/favorite') ||
        url.pathname.startsWith('/sudo-password') ||
        url.pathname.startsWith('/autoyes/') ||
        url.pathname.startsWith('/api/') ||
        url.pathname.startsWith('/health')
    ) {
        return;  // let browser handle normally (network-only)
    }

    // Network-first with cache fallback for the app shell + static assets
    // (/, /index.html, /js/, /css/). This is a LAN tool: stale-while-
    // revalidate meant every deploy's first load verified stale code.
    event.respondWith(
        caches.open(STATIC_CACHE).then(cache =>
            fetch(event.request).then(response => {
                if (response.ok) cache.put(event.request, response.clone());
                return response;
            }).catch(() => cache.match(event.request, {ignoreSearch: true}))
        )
    );
});
