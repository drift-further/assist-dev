// ui.js — Flash feedback, drawer controls, swipe gestures, list rendering

function showFlash(type, text) {
    flash.className = 'flash-overlay ' + type;
    flash.textContent = text;
    flash.classList.add('show');
    if (type === 'error') {
        input.classList.add('shake');
        setTimeout(() => input.classList.remove('shake'), 400);
    }
    setTimeout(() => flash.classList.remove('show'), 1200);
}

// Escapes for HTML text and quoted-attribute contexts (both ' and " quotes).
// NOT sufficient for a value interpolated into a JS string inside an inline
// handler (onclick="f('...')"): the HTML parser decodes entities BEFORE the
// JS parser runs, so &#39; becomes a live quote again. For dynamic values use
// a data-* attribute (escaped here) + a delegated listener, never onclick.
function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/`/g, '&#96;');
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function isFav(text) {
    return _favorites.some(f => f.text === text);
}

const SHELL_CMDS = new Set([
    'cd', 'ls', 'pwd', 'echo', 'cat', 'grep', 'mkdir', 'rm', 'cp', 'mv',
    'git', 'make', 'npm', 'python', 'pip', 'docker', 'tmux', 'kill',
    'clear', 'exit', 'claude', 'bash', 'sudo', 'chmod', 'chown',
    'find', 'awk', 'sed', 'head', 'tail', 'wc', 'which', 'ps', 'df', 'du'
]);

function classifyKind(text) {
    if (!text) return 'prompt';
    const trimmed = text.trim();
    const firstWord = trimmed.split(/\s+/)[0];
    if (SHELL_CMDS.has(firstWord)) return 'command';
    if (trimmed.length < 5 && !/\s/.test(trimmed)) return 'command';
    return 'prompt';
}

let _activeHistTab = 'prompts';

function switchHistTab(tab) {
    _activeHistTab = tab;
    document.querySelectorAll('.drawer-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    const clearBtn = document.getElementById('clear-btn');
    if (clearBtn) clearBtn.style.display = tab === 'favs' ? 'none' : '';
    updateHistTabCounts();
    renderLists();
}

function updateHistTabCounts() {
    const favTexts = new Set(_favorites.map(f => f.text));
    const nonFav = _history.filter(h => !favTexts.has(h.text));
    const promptCount = nonFav.filter(h => classifyKind(h.text) === 'prompt').length;
    const cmdCount = nonFav.filter(h => classifyKind(h.text) === 'command').length;
    document.querySelectorAll('.drawer-tab').forEach(t => {
        if (t.dataset.tab === 'prompts') t.textContent = 'Prompts' + (promptCount ? ' ' + promptCount : '');
        if (t.dataset.tab === 'commands') t.textContent = 'Cmds' + (cmdCount ? ' ' + cmdCount : '');
        if (t.dataset.tab === 'favs') t.textContent = 'Favs' + (_favorites.length ? ' ' + _favorites.length : '');
    });
}

function highlightMatch(text, query) {
    const escaped = escHtml(text);
    if (!query) return escaped;
    const queryLower = query.toLowerCase();
    const textLower = text.toLowerCase();
    const idx = textLower.indexOf(queryLower);
    if (idx === -1) return escaped;
    // Recompute indices on the escaped string by walking through plain text positions.
    // Simpler: split plain text, escape each segment, join with <mark>.
    const before = text.slice(0, idx);
    const match = text.slice(idx, idx + query.length);
    const after = text.slice(idx + query.length);
    return escHtml(before) + '<mark>' + escHtml(match) + '</mark>' + escHtml(after);
}

function renderLists() {
    let html = '';
    const filterLower = _filterText.toLowerCase();
    const matchesFilter = (text) => !filterLower || text.toLowerCase().includes(filterLower);

    if (_activeHistTab === 'favs') {
        const items = _favorites.filter(f => matchesFilter(f.text));
        if (items.length > 0) {
            for (const f of items) {
                const display = f.display || f.text;
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(f.text)}">
                    <span class="list-item-text">${highlightMatch(display, _filterText)}</span>
                    <button class="list-item-star fav">&#9733;</button>
                </div>`;
            }
        } else {
            const msg = _filterText ? `No matches for "${escHtml(_filterText)}"` : 'No favorites yet';
            html = `<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">${msg}</div>`;
        }
    } else {
        const wantKind = _activeHistTab === 'commands' ? 'command' : 'prompt';
        const favTexts = new Set(_favorites.map(f => f.text));
        const items = _history.filter(h =>
            !favTexts.has(h.text) &&
            classifyKind(h.text) === wantKind &&
            matchesFilter(h.text)
        );

        if (items.length > 0) {
            for (const h of items) {
                const display = h.display || h.text;
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(h.text)}">
                    <span class="list-item-text">${highlightMatch(display, _filterText)}</span>
                    <button class="list-item-star unfav">&#9734;</button>
                </div>`;
            }
        } else {
            const emptyBucketMsg = wantKind === 'command' ? 'No commands yet' : 'No prompts yet';
            const msg = _filterText ? `No matches for "${escHtml(_filterText)}"` : emptyBucketMsg;
            html = `<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">${msg}</div>`;
        }
    }

    listArea.innerHTML = html;
}

// Delegated star clicks — inline onclick broke on multi-line/backslash
// history entries (raw text injected into a JS string literal). Capture
// phase so the parent .list-item's loadText onclick never fires.
listArea.addEventListener('click', function(e) {
    const star = e.target.closest('.list-item-star');
    if (!star) return;
    e.preventDefault();
    e.stopPropagation();
    const item = star.closest('.list-item');
    if (item && item.dataset.text !== undefined) toggleFavorite(item.dataset.text);
}, true);

function loadText(el) {
    input.value = el.dataset.text;
    closeBottomDrawer();
    input.focus();
}

function clearHistFilter() {
    _filterText = '';
    const el = document.getElementById('hist-filter');
    if (el) el.value = '';
    document.getElementById('hist-filter-clear').style.display = 'none';
    renderLists();
}

(function attachHistFilter() {
    const el = document.getElementById('hist-filter');
    if (!el) return;
    el.addEventListener('input', () => {
        _filterText = el.value;
        document.getElementById('hist-filter-clear').style.display = el.value ? 'block' : 'none';
        renderLists();
    });
})();

// ================================================================
// Drawer controls (left: more keys, right: keys, bottom: history)
// ================================================================
function toggleLeftDrawer() {
    const drawer = document.getElementById('drawer-left');
    const overlay = document.getElementById('drawer-overlay');
    const isOpen = drawer.classList.contains('open');
    if (isOpen) {
        closeLeftDrawer();
    } else {
        closeBottomDrawer();
        drawer.classList.add('open');
        overlay.classList.add('visible');
    }
}

function closeLeftDrawer() {
    document.getElementById('drawer-left').classList.remove('open');
    if (!document.getElementById('drawer-bottom').classList.contains('open')) {
        document.getElementById('drawer-overlay').classList.remove('visible');
    }
}

function toggleBottomDrawer() {
    const drawer = document.getElementById('drawer-bottom');
    const overlay = document.getElementById('drawer-overlay');
    const isOpen = drawer.classList.contains('open');
    if (isOpen) {
        closeBottomDrawer();
    } else {

        closeLeftDrawer();
        drawer.classList.remove('half');
        drawer.classList.add('open');
        overlay.classList.add('visible');
        loadHistory();
        updateHistTabCounts();
    }
}

function closeBottomDrawer() {
    const drawer = document.getElementById('drawer-bottom');
    drawer.classList.remove('open', 'half');
    if (!document.getElementById('drawer-left').classList.contains('open')) {
        document.getElementById('drawer-overlay').classList.remove('visible');
    }
    if (typeof clearHistFilter === 'function') clearHistFilter();
}

function toggleKeysPanel() {
    const panel = document.getElementById('keys-panel');
    const btn = document.getElementById('btn-keys');
    const visible = panel.classList.contains('visible');
    panel.classList.toggle('visible', !visible);
    btn.classList.toggle('active', !visible);
}

function closeDrawers() {
    document.getElementById('drawer-left').classList.remove('open');
    document.getElementById('drawer-bottom').classList.remove('open');
    document.getElementById('drawer-overlay').classList.remove('visible');
    if (typeof closeStaleSheet === 'function') closeStaleSheet();
}

function togglePlusMenu() {
    const menu = document.getElementById('plus-menu');
    const btn = document.getElementById('btn-plus');
    const visible = menu.classList.contains('visible');
    menu.classList.toggle('visible', !visible);
    btn.classList.toggle('active', !visible);
}

// Swipe gestures for drawers
(function() {
    let startX = 0, startY = 0;
    const left = document.getElementById('drawer-left');
    const bottom = document.getElementById('drawer-bottom');

    left.addEventListener('touchstart', e => { startX = e.touches[0].clientX; startY = e.touches[0].clientY; }, {passive: true});
    left.addEventListener('touchend', e => {
        const dx = e.changedTouches[0].clientX - startX;
        const dy = Math.abs(e.changedTouches[0].clientY - startY);
        if (dx < -60 && dy < 80) closeLeftDrawer();
    }, {passive: true});

    bottom.addEventListener('touchstart', e => { startX = e.touches[0].clientX; startY = e.touches[0].clientY; }, {passive: true});
    bottom.addEventListener('touchend', e => {
        const dy = e.changedTouches[0].clientY - startY;
        const dx = Math.abs(e.changedTouches[0].clientX - startX);
        if (dx > 80) return;  // horizontal swipe, ignore

        const drawerHeight = bottom.offsetHeight;
        const swipeRatio = dy / drawerHeight;

        if (swipeRatio > 0.6) {
            // Full swipe down — close
            bottom.classList.remove('half');
            closeBottomDrawer();
        } else if (swipeRatio > 0.3) {
            // Partial swipe — snap to half height
            if (!bottom.classList.contains('half')) {
                bottom.classList.add('half');
            } else {
                // Already half — close
                bottom.classList.remove('half');
                closeBottomDrawer();
            }
        }
        // <30% — do nothing, stay in current position
    }, {passive: true});
})();
