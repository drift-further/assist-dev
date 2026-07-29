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
