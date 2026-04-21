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

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML.replace(/"/g, '&quot;');
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function isFav(text) {
    return _favorites.some(f => f.text === text);
}

let _activeHistTab = 'history';

function switchHistTab(tab) {
    _activeHistTab = tab;
    document.querySelectorAll('.drawer-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    const clearBtn = document.getElementById('clear-btn');
    if (clearBtn) clearBtn.style.display = tab === 'history' ? '' : 'none';
    updateHistTabCounts();
    renderLists();
}

function updateHistTabCounts() {
    const favTexts = new Set(_favorites.map(f => f.text));
    const histCount = _history.filter(h => !favTexts.has(h.text)).length;
    document.querySelectorAll('.drawer-tab').forEach(t => {
        if (t.dataset.tab === 'history') t.textContent = 'History' + (histCount ? ' ' + histCount : '');
        if (t.dataset.tab === 'favs') t.textContent = 'Favs' + (_favorites.length ? ' ' + _favorites.length : '');
    });
}

function renderLists() {
    let html = '';

    if (_activeHistTab === 'favs') {
        if (_favorites.length > 0) {
            for (const f of _favorites) {
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(f.text)}">
                    <span class="list-item-text">${escHtml(f.display || f.text)}</span>
                    <button class="list-item-star fav" onclick="toggleFavorite('${escHtml(f.text).replace(/'/g, "\\'")}', event)">&#9733;</button>
                </div>`;
            }
        } else {
            html = '<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">No favorites yet</div>';
        }
    } else {
        const favTexts = new Set(_favorites.map(f => f.text));
        const filtered = _history.filter(h => !favTexts.has(h.text));

        if (filtered.length > 0) {
            for (const h of filtered) {
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(h.text)}">
                    <span class="list-item-text">${escHtml(h.display || h.text)}</span>
                    <button class="list-item-star unfav" onclick="toggleFavorite('${escHtml(h.text).replace(/'/g, "\\'")}', event)">&#9734;</button>
                </div>`;
            }
        } else {
            html = '<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">No history yet</div>';
        }
    }

    listArea.innerHTML = html;
}

function loadText(el) {
    input.value = el.dataset.text;
    closeBottomDrawer();
    input.focus();
}

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
