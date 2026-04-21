// selection.js — Terminal line selection: long-press to anchor, drag or second long-press to extend

let _selStart = -1;        // first selected line index
let _selEnd = -1;          // last selected line index
let _selActive = false;    // selection is active (lines are highlighted)
let _selDragging = false;  // user is dragging to extend
let _selHoldTimer = null;
let _selStartX = 0;
let _selStartY = 0;

const _SEL_HOLD_MS = 1200;

function _selLineFromY(clientY) {
    const pre = document.getElementById('term-content');
    if (!pre || !pre.firstChild) return -1;
    const text = pre.textContent;
    if (!text) return -1;
    const lines = text.split('\n');
    if (lines.length === 0) return -1;

    const style = getComputedStyle(pre);
    const lh = parseFloat(style.lineHeight);
    const lineHeight = isNaN(lh) ? parseFloat(style.fontSize) * 1.35 : lh;

    const rect = pre.getBoundingClientRect();
    // getBoundingClientRect is viewport-relative, no need to add scrollTop
    const y = clientY - rect.top;
    const idx = Math.floor(y / lineHeight);
    return Math.max(0, Math.min(idx, lines.length - 1));
}

// Apply visual highlights to rendered content (call after each innerHTML update)
function applySelectionHighlights() {
    if (!_selActive || _selStart < 0) return;
    const pre = document.getElementById('term-content');
    if (!pre) return;

    const html = pre.innerHTML;
    const lines = html.split('\n');
    const lo = Math.min(_selStart, _selEnd >= 0 ? _selEnd : _selStart);
    const hi = Math.max(_selStart, _selEnd >= 0 ? _selEnd : _selStart);

    let changed = false;
    for (let i = lo; i <= hi && i < lines.length; i++) {
        lines[i] = '<span class="sel-line">' + lines[i] + '</span>';
        changed = true;
    }
    if (changed) {
        pre.innerHTML = lines.join('\n');
    }

    _showCopyBtn(hi - lo + 1);
}

function _showCopyBtn(count) {
    const btn = document.getElementById('sel-copy-btn');
    const bar = document.getElementById('sel-bar');
    if (btn) btn.textContent = 'COPY ' + count + ' LINE' + (count > 1 ? 'S' : '');
    if (bar) bar.classList.add('visible');
}

function clearSelection() {
    _selStart = -1;
    _selEnd = -1;
    _selActive = false;
    _selDragging = false;
    _selSetScrollLock(false);
    const bar = document.getElementById('sel-bar');
    if (bar) bar.classList.remove('visible');
    // Strip highlight spans immediately
    const pre = document.getElementById('term-content');
    if (pre) {
        const spans = pre.querySelectorAll('.sel-line');
        spans.forEach(s => s.replaceWith(...s.childNodes));
    }
}

// Lock/unlock scroll on term-display during drag selection
function _selSetScrollLock(lock) {
    const display = document.getElementById('term-display');
    if (!display) return;
    if (lock) {
        display.style.overflowY = 'hidden';
        display.style.touchAction = 'none';
    } else {
        display.style.overflowY = '';
        display.style.touchAction = '';
    }
}

function _copySelected() {
    const pre = document.getElementById('term-content');
    if (!pre || _selStart < 0) return;
    const text = pre.textContent;
    const lines = text.split('\n');
    const lo = Math.min(_selStart, _selEnd >= 0 ? _selEnd : _selStart);
    const hi = Math.max(_selStart, _selEnd >= 0 ? _selEnd : _selStart);
    const selected = lines.slice(lo, hi + 1).join('\n');

    // Try sync copy first (works on mobile HTTP with user gesture)
    let copied = false;
    try {
        const ta = document.createElement('textarea');
        ta.value = selected;
        ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px;opacity:0';
        document.body.appendChild(ta);
        ta.focus();
        ta.setSelectionRange(0, selected.length);
        copied = document.execCommand('copy');
        document.body.removeChild(ta);
    } catch(e) {}

    // Async clipboard API as backup
    if (!copied && navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(selected).catch(() => {});
        copied = true;
    }

    if (copied) {
        showFlash('copied', 'Copied ' + (hi - lo + 1) + ' lines');
    } else {
        showFlash('error', 'Copy failed');
    }
    clearSelection();
}

// Attach listeners
(function() {
    const display = document.getElementById('term-display');
    if (!display) return;

    // Long-press to start or extend selection
    display.addEventListener('touchstart', function(e) {
        if (e.target.closest('#sel-copy-btn')) return;
        if (e.target.closest('.term-load-more') || e.target.closest('.term-new-output')) return;

        const touch = e.touches[0];
        _selStartX = touch.clientX;
        _selStartY = touch.clientY;
        const startClientY = touch.clientY;

        _selHoldTimer = setTimeout(function() {
            _selHoldTimer = null;
            const idx = _selLineFromY(startClientY);
            if (idx < 0) return;

            if (navigator.vibrate) navigator.vibrate(30);

            if (_selActive && _selStart >= 0) {
                // Already have an anchor — extend selection to this line
                _selEnd = idx;
            } else {
                // New selection — set anchor
                _selStart = idx;
                _selEnd = idx;
                _selActive = true;
            }
            _selDragging = true;
            _selSetScrollLock(true);  // prevent scroll while dragging
            applySelectionHighlights();
        }, _SEL_HOLD_MS);
    }, {passive: true});

    // Drag to extend selection (scroll is locked during drag)
    display.addEventListener('touchmove', function(e) {
        // Cancel hold if moved in any direction before hold completes
        const _dx = Math.abs(e.touches[0].clientX - _selStartX);
        const _dy = Math.abs(e.touches[0].clientY - _selStartY);
        if (_selHoldTimer && (_dx > 15 || _dy > 15)) {
            clearTimeout(_selHoldTimer);
            _selHoldTimer = null;
            return;
        }
        if (!_selDragging) return;

        const idx = _selLineFromY(e.touches[0].clientY);
        if (idx >= 0 && idx !== _selEnd) {
            _selEnd = idx;
            applySelectionHighlights();
        }
    }, {passive: true});

    // Release: finalize selection, restore scroll
    display.addEventListener('touchend', function() {
        if (_selHoldTimer) {
            clearTimeout(_selHoldTimer);
            _selHoldTimer = null;
        }
        if (_selDragging) {
            _selDragging = false;
            _selSetScrollLock(false);
        }
    }, {passive: true});

    // Copy button
    const copyBtn = document.getElementById('sel-copy-btn');
    if (copyBtn) {
        copyBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            _copySelected();
        });
    }

    // Tap outside to clear
    document.addEventListener('touchstart', function(e) {
        if (!_selActive) return;
        if (e.target.closest('#term-display') || e.target.closest('#sel-bar')) return;
        clearSelection();
    }, {passive: true});
})();
