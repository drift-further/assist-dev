// chrome.js — collapse the status bar + terminal toggle row to reclaim ~91px
// on a phone. State lives in one body class; localStorage makes it stick.

const _CHROME_KEY = 'assist.chromeCollapsed';
let _chromeLastDotErr = false;

function toggleChrome(force) {
    const collapsed = (force === undefined)
        ? !document.body.classList.contains('chrome-collapsed')
        : !!force;
    document.body.classList.toggle('chrome-collapsed', collapsed);
    try {
        if (collapsed) localStorage.setItem(_CHROME_KEY, '1');
        else localStorage.removeItem(_CHROME_KEY);
    } catch (e) {
        // Private mode / storage disabled: the toggle still works for this
        // session, it just will not survive a reload.
    }
    const btn = document.getElementById('term-chrome-btn');
    if (btn) {
        btn.textContent = collapsed ? '▾' : '▴';
        btn.title = collapsed ? 'Show status bar' : 'Hide status bar';
    }
    if (collapsed) syncChromeGrabber();
}

// Mirror the live DOM rather than keeping a second copy of connection/attention
// state. #status-dot and #studio-badge are already the source of truth, written
// by js/app.js and js/studio.js; reading them here means this cannot drift.
function syncChromeGrabber() {
    const dot = document.getElementById('status-dot');
    const badge = document.getElementById('studio-badge');
    const gDot = document.getElementById('grab-dot');
    const gBadge = document.getElementById('grab-badge');
    if (!gDot || !gBadge) return;

    const err = !!(dot && dot.classList.contains('err'));
    gDot.classList.toggle('err', err);

    const count = (badge && !badge.classList.contains('hidden'))
        ? (badge.textContent || '').trim() : '';
    gBadge.textContent = count;
    gBadge.classList.toggle('visible', !!count);

    // Auto-reveal on the TRANSITION into failure only. A sustained outage polls
    // every 5s; re-expanding each time would fight the operator every time they
    // tried to collapse it again.
    if (err && !_chromeLastDotErr &&
        document.body.classList.contains('chrome-collapsed')) {
        toggleChrome(false);
        if (typeof showFlash === 'function') showFlash('error', 'Connection lost');
    }
    _chromeLastDotErr = err;
}

// The chevron reflects state on load too — the first-paint script in index.html
// sets the class before this module exists.
document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('term-chrome-btn');
    if (!btn) return;
    const collapsed = document.body.classList.contains('chrome-collapsed');
    btn.textContent = collapsed ? '▾' : '▴';
    btn.title = collapsed ? 'Show status bar' : 'Hide status bar';
});
