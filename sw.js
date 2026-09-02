// sw.js — Service worker for Assist (network-first for static assets, cache fallback when offline)
// Browsers register this only in a secure context (with a loopback exception),
// so it is inert on the current plain-HTTP phone origin until TLS lands.
const VERSION = 'assist-v3-036';
const STATIC_CACHE = 'assist-static-' + VERSION;
const STATIC_URLS = [
    '/',
    '/icons/assist-dev-a.png?v=1',
    '/css/fonts.css',
    '/css/base.css?v=1',
    '/css/status-bar.css?v=4',
    '/css/input.css?v=8',
    '/css/terminal.css?v=12',
    '/css/chrome.css?v=5',
    '/css/studio.css?v=4',
    '/css/drawers.css?v=2',
    '/css/widgets.css?v=1',
    '/css/commands.css?v=1',
    '/js/state.js?v=4',
    '/js/ui.js?v=6',
    '/js/vault.js?v=3',
    '/js/input.js?v=13',
    '/js/drafts.js?v=4',
    '/js/terminal.js?v=25',
    '/js/actions.js?v=15',
    '/js/commands.js?v=4',
    '/js/monitor.js?v=6',
    '/js/chrome.js?v=3',
    '/js/app.js?v=16',
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

    // Network-only for API, poll, terminal, WebSocket
    if (
        url.pathname.startsWith('/poll') ||
        url.pathname.startsWith('/terminal/') ||
        url.pathname.startsWith('/type') ||
        url.pathname.startsWith('/key') ||
        url.pathname.startsWith('/upload') ||
        url.pathname.startsWith('/history') ||
        url.pathname.startsWith('/favorite') ||
        url.pathname.startsWith('/complete/') ||
        url.pathname.startsWith('/segments/') ||
        url.pathname.startsWith('/autoyes/') ||
        url.pathname.startsWith('/access/') ||
        url.pathname.startsWith('/studio/') ||
        url.pathname.startsWith('/login') ||
        url.pathname.startsWith('/api/') ||
        url.pathname.startsWith('/health')
    ) {
        return;  // let browser handle normally (network-only)
    }

    // Network-first with cache fallback for every remaining GET. This is a LAN
    // tool: stale-while-revalidate meant every deploy's first load verified
    // stale code.
    event.respondWith(
        caches.open(STATIC_CACHE).then(cache =>
            fetch(event.request).then(response => {
                if (response.ok) cache.put(event.request, response.clone());
                return response;
            }).catch(() => cache.match(event.request))
        )
    );
});
