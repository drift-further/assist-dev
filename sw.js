// sw.js — Service worker for Assist (stale-while-revalidate for static assets)
const VERSION = 'assist-v2-001';
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
    const url = new URL(event.request.url);

    // Network-only for API, poll, terminal, WebSocket
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
        url.pathname.startsWith('/autoyes/') ||
        url.pathname.startsWith('/api/') ||
        url.pathname.startsWith('/health')
    ) {
        return;  // let browser handle normally (network-only)
    }

    // Stale-while-revalidate for static assets
    event.respondWith(
        caches.open(STATIC_CACHE).then(cache =>
            cache.match(event.request).then(cached => {
                const fetched = fetch(event.request).then(response => {
                    if (response.ok) cache.put(event.request, response.clone());
                    return response;
                }).catch(() => cached);
                return cached || fetched;
            })
        )
    );
});
