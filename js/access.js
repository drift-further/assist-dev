// access.js — Temporary open-access window: sheet, countdown strip, onboard toast.
//
// The window is a server-side deadline (shared/auth.py). This module only
// mirrors it: /poll carries the authoritative remaining seconds every 5s and a
// local 1s ticker interpolates between them, so the countdown reads smoothly
// without a second endpoint.

let _accessSheetOpen = false;
let _accessRemaining = 0;
let _accessTicker = null;
let _accessNetworks = '';
let _accessSeenOnboard = 0;   // highest onboard id already toasted here

function toggleAccessSheet(force) {
    const sheet = document.getElementById('access-sheet');
    if (!sheet) return;
    _accessSheetOpen = (force === undefined) ? !_accessSheetOpen : !!force;
    sheet.classList.toggle('visible', _accessSheetOpen);
    sheet.setAttribute('aria-hidden', _accessSheetOpen ? 'false' : 'true');
    if (_accessSheetOpen) _renderAccessSheet();
}

function _renderAccessSheet() {
    const nets = document.getElementById('access-networks');
    if (nets) nets.textContent = _accessNetworks || '—';
}

async function accessOpen(minutes) {
    try {
        const resp = await fetch('/access/open', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ minutes: minutes })
        });
        const data = await resp.json();
        if (!data || !data.ok) {
            showToast((data && data.error) || 'Could not open access', 'error');
            return;
        }
        _applyAccessData(data.access);
        toggleAccessSheet(false);
    } catch (e) {
        // The request may well have committed before the connection dropped —
        // claiming failure here would leave a live window the operator thinks
        // never opened. The strip is authoritative; the next poll settles it.
        showToast('Could not confirm — check the strip above', 'error');
    }
}

async function accessClose() {
    try {
        const resp = await fetch('/access/close', { method: 'POST' });
        const data = await resp.json();
        if (data && data.access) _applyAccessData(data.access);
    } catch (e) {
        // Swallowed on purpose: the next /poll re-syncs the strip either way.
    }
    toggleAccessSheet(false);
}

function _applyAccessData(block) {
    if (!block) {
        _accessRemaining = 0;
        _approvePending = [];
        _renderApproveSheet();
        _renderAccessStrip();
        return;
    }
    _accessNetworks = block.networks || '';
    _accessRemaining = block.open ? (block.remaining_sec || 0) : 0;
    _renderAccessStrip();
    if (_accessSheetOpen) _renderAccessSheet();
    // The server republishes the report until it expires; de-duplicate on its
    // id so this fires once here without the server having to destroy it.
    const o = block.last_onboard;
    if (o && o.id > _accessSeenOnboard) {
        _accessSeenOnboard = o.id;
        showToast('Device joined · ' + o.ip + ' · ' + _uaShort(o.ua), 'success');
    }
    // Same at-least-once + de-duplicate-on-id contract as last_onboard above.
    _approvePending = block.pending_requests || [];
    _renderApproveSheet();
    for (const r of _approvePending) {
        if (r.id > _approveSeen) {
            _approveSeen = r.id;
            _notifyApproval(r);
        }
    }
}

// Collapse a User-Agent to "Browser/OS". Order matters: Edge carries "Chrome/"
// and Chrome carries "Safari/", so each must be tested before the one it
// impersonates.
function _uaShort(ua) {
    if (!ua) return 'unknown';
    const os = /iPhone|iPad|iPod/.test(ua) ? 'iOS'
        : /Android/.test(ua) ? 'Android'
        : /Mac OS X/.test(ua) ? 'macOS'
        : /Windows/.test(ua) ? 'Windows'
        : /Linux/.test(ua) ? 'Linux' : '';
    const browser = /Edg\//.test(ua) ? 'Edge'
        : /Chrome\//.test(ua) ? 'Chrome'
        : /Firefox\//.test(ua) ? 'Firefox'
        : /Safari\//.test(ua) ? 'Safari' : 'browser';
    return os ? browser + '/' + os : browser;
}

function _fmtAccessClock(sec) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m + ':' + String(s).padStart(2, '0');
}

function _renderAccessStrip() {
    const strip = document.getElementById('access-strip');
    if (!strip) return;
    const open = _accessRemaining > 0;

    strip.classList.toggle('visible', open);
    strip.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (open) {
        strip.textContent = 'ACCESS OPEN · ' + _fmtAccessClock(_accessRemaining) +
                            ' · tap to close';
    }

    if (open && !_accessTicker) {
        _accessTicker = setInterval(() => {
            _accessRemaining = Math.max(0, _accessRemaining - 1);
            if (_accessRemaining <= 0) {
                clearInterval(_accessTicker);
                _accessTicker = null;
            }
            _renderAccessStrip();
        }, 1000);
    } else if (!open && _accessTicker) {
        clearInterval(_accessTicker);
        _accessTicker = null;
    }
}

// --- Device approval -------------------------------------------------------
//
// The mirror image of the strip above: instead of the operator opening a
// window, a device asks and the operator answers. Cards are built with DOM
// APIs rather than innerHTML so an IP that arrived in a header is never parsed
// as markup, and each card node is kept alive across ticks so the 1s countdown
// cannot swap a button out from under a tap.

let _approveSeen = 0;            // highest request id already notified here
let _approvePending = [];        // last payload from /poll, ticked down locally
let _approveNodes = new Map();   // id -> {card, when}, so ticks touch text only
let _approveTicker = null;

function _approveCard(r) {
    const card = document.createElement('div');
    card.className = 'approve-card';

    const code = document.createElement('div');
    code.className = 'approve-code';
    code.textContent = r.code;

    const who = document.createElement('div');
    who.className = 'approve-meta';
    who.textContent = r.ip + ' · ' + _uaShort(r.ua);

    const when = document.createElement('div');
    when.className = 'approve-meta';
    when.textContent = 'expires in ' + _fmtAccessClock(r.remaining_sec);

    const actions = document.createElement('div');
    actions.className = 'approve-actions';
    const deny = document.createElement('button');
    deny.className = 'approve-deny';
    deny.textContent = 'DENY';
    deny.onclick = () => accessDecide(r.id, false);
    const ok = document.createElement('button');
    ok.className = 'approve-ok';
    ok.textContent = 'APPROVE';
    ok.onclick = () => accessDecide(r.id, true);
    actions.appendChild(deny);
    actions.appendChild(ok);

    card.appendChild(code);
    card.appendChild(who);
    card.appendChild(when);
    card.appendChild(actions);
    return { card, when };
}

function _renderApproveSheet() {
    const sheet = document.getElementById('approve-sheet');
    const backdrop = document.getElementById('approve-backdrop');
    const body = document.getElementById('approve-sheet-body');
    if (!sheet || !body) return;

    const live = new Set(_approvePending.map(r => r.id));
    for (const [id, node] of _approveNodes) {
        if (!live.has(id)) {
            node.card.remove();
            _approveNodes.delete(id);
        }
    }
    for (const r of _approvePending) {
        let node = _approveNodes.get(r.id);
        if (!node) {
            node = _approveCard(r);
            _approveNodes.set(r.id, node);
            body.appendChild(node.card);
        }
        node.when.textContent = 'expires in ' + _fmtAccessClock(r.remaining_sec);
    }

    const open = _approvePending.length > 0;
    sheet.classList.toggle('visible', open);
    sheet.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (backdrop) {
        backdrop.classList.toggle('visible', open);
        backdrop.setAttribute('aria-hidden', open ? 'false' : 'true');
    }

    if (open && !_approveTicker) {
        _approveTicker = setInterval(() => {
            _approvePending.forEach(r => {
                r.remaining_sec = Math.max(0, r.remaining_sec - 1);
            });
            _approvePending = _approvePending.filter(r => r.remaining_sec > 0);
            _renderApproveSheet();
        }, 1000);
    } else if (!open && _approveTicker) {
        clearInterval(_approveTicker);
        _approveTicker = null;
    }
}

// Same guard shape as sendPromptNotification in monitor.js: a backgrounded
// Assist tab still polls, so this is what actually reaches the phone.
function _notifyApproval(r) {
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    try {
        new Notification('Assist — approve new device?', {
            body: 'code ' + r.code + ' · ' + r.ip + ' · ' + _uaShort(r.ua),
            tag: 'assist-approve-' + r.id
        });
    } catch (e) {
        // Some browsers refuse a constructed Notification outside a service
        // worker. The modal is the real surface, so this is not worth raising.
    }
}

async function accessDecide(id, approve) {
    // Drop it locally first so a double-tap cannot fire two verdicts. The next
    // poll is authoritative either way, and the server refuses a second
    // verdict on the same record regardless.
    _approvePending = _approvePending.filter(r => r.id !== id);
    _renderApproveSheet();
    try {
        const resp = await fetch('/access/decide', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id, action: approve ? 'approve' : 'deny' })
        });
        const data = await resp.json();
        if (data && data.access) _applyAccessData(data.access);
    } catch (e) {
        showToast('Could not send the verdict — it will reappear', 'error');
    }
}
