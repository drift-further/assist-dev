// chrome.js — collapse the status bar while keeping the session rail reachable.
// State lives in one body class; localStorage makes it stick.

const _CHROME_KEY = 'assist.chromeCollapsed';
const _TABWRAP_KEY = 'assist.tabsWrap';
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
        btn.textContent = collapsed ? 'Show bar' : 'Hide bar';
        btn.dataset.glyph = collapsed ? '▾' : '▴';
        btn.title = collapsed ? 'Show status bar' : 'Hide status bar';
    }
    if (collapsed) syncChromeGrabber();
}

// Wrapping session rail: the tabs share row 1 with the status bar and spill
// onto a second row, instead of scrolling sideways in a rail of their own.
// Off by default — the whole feature is one body class, so nothing about the
// default chrome changes for anyone who never turns it on. Same storage shape
// as the collapse above, and index.html reads it before first paint because it
// changes the header's height.
function toggleTabWrap(force) {
    const on = (force === undefined)
        ? !document.body.classList.contains('tabs-wrap')
        : !!force;
    document.body.classList.toggle('tabs-wrap', on);
    try {
        if (on) localStorage.setItem(_TABWRAP_KEY, '1');
        else localStorage.removeItem(_TABWRAP_KEY);
    } catch (e) {
        // Private mode / storage disabled: still works for this session.
    }
    syncTabWrapBtn();
    // The ≡ pill floats onto row 1 while wrapping and is sticky-right in the
    // default rail, so its position in the strip differs between the two. It
    // is only rebuilt on a poll, which is up to 5s away.
    if (typeof _applyStaleGroup === 'function') _applyStaleGroup();
    // .status-bar has no box while wrapping, so its offsetHeight goes to 0 and
    // --status-bar-h (left drawer + notification offsets) has to be re-taken.
    if (typeof measureStatusBar === 'function') measureStatusBar();
}

function syncTabWrapBtn() {
    const btn = document.getElementById('btn-tabwrap');
    if (!btn) return;
    btn.classList.toggle('is-on', document.body.classList.contains('tabs-wrap'));
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
    syncTabWrapBtn();
    const btn = document.getElementById('term-chrome-btn');
    if (!btn) return;
    const collapsed = document.body.classList.contains('chrome-collapsed');
    btn.textContent = collapsed ? 'Show bar' : 'Hide bar';
    btn.dataset.glyph = collapsed ? '▾' : '▴';
    btn.title = collapsed ? 'Show status bar' : 'Hide status bar';
});
