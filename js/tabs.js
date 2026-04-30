// tabs.js — Tab pin/reorder: long-press context menu, touch drag reorder, localStorage persistence

const _TAB_LONG_PRESS_MS = 500;
let _tabLongPressTimer = null;
let _tabContextTarget = null;

// Persisted state
let _pinnedTabs = JSON.parse(localStorage.getItem('assist_pinned_tabs') || '[]');
let _tabOrder = JSON.parse(localStorage.getItem('assist_tab_order') || '[]');

let _staleSheetOpen = false;
let _lastStaleCount = 0;  // for new-stale flash detection

function openStaleSheet() {
    const sheet = document.getElementById('stale-sheet');
    const overlay = document.getElementById('drawer-overlay');
    if (!sheet) return;
    _staleSheetOpen = true;
    sheet.classList.add('open');
    sheet.setAttribute('aria-hidden', 'false');
    if (overlay) {
        overlay.classList.add('visible');
        overlay.dataset.closesStaleSheet = '1';
    }
}

function closeStaleSheet() {
    const sheet = document.getElementById('stale-sheet');
    const overlay = document.getElementById('drawer-overlay');
    if (!sheet) return;
    _staleSheetOpen = false;
    sheet.classList.remove('open');
    sheet.setAttribute('aria-hidden', 'true');
    if (overlay && overlay.dataset.closesStaleSheet === '1') {
        overlay.classList.remove('visible');
        delete overlay.dataset.closesStaleSheet;
    }
}

function _staleRowDotClass(tab) {
    if (tab.classList.contains('has-prompt')) return 'amber';
    if (tab.classList.contains('done')) return 'green';
    if (tab.classList.contains('running')) return 'cyan';
    return '';
}

function _onStaleRowTap(target) {
    closeStaleSheet();
    if (typeof selectTab === 'function') selectTab(target);
    // _applyStaleGroup will re-evaluate on the next poll and the freshly
    // active tab will leave the sheet automatically (active tabs are excluded).
}

function _savePinnedTabs() {
    try { localStorage.setItem('assist_pinned_tabs', JSON.stringify(_pinnedTabs)); } catch(e) {}
}
function _saveTabOrder() {
    try { localStorage.setItem('assist_tab_order', JSON.stringify(_tabOrder)); } catch(e) {}
}

// --- Context menu ---

function _createContextMenu(tab, x, y) {
    _removeContextMenu();
    const target = tab.dataset.target;
    const session = target.split(':')[0];
    const isPinned = _pinnedTabs.includes(target);

    const menu = document.createElement('div');
    menu.className = 'tab-context-menu';
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';

    const pinBtn = document.createElement('button');
    pinBtn.className = 'tab-ctx-item';
    pinBtn.textContent = isPinned ? 'Unpin' : 'Pin';
    pinBtn.onclick = function(e) {
        e.stopPropagation();
        if (isPinned) {
            _pinnedTabs = _pinnedTabs.filter(t => t !== target);
        } else {
            _pinnedTabs.push(target);
        }
        _savePinnedTabs();
        _reorderTabsDom();
        _removeContextMenu();
    };

    const renameBtn = document.createElement('button');
    renameBtn.className = 'tab-ctx-item';
    renameBtn.textContent = 'Rename';
    renameBtn.onclick = function(e) {
        e.stopPropagation();
        _removeContextMenu();
        const newName = prompt('Rename session:', shortName(session));
        if (!newName || newName.trim() === '' || newName.trim() === session) return;
        fetch('/terminal/rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({session: session, name: newName.trim()}),
        }).then(r => r.json()).then(data => {
            if (data.ok) {
                // Update pinned tabs refs
                const oldPrefix = session + ':';
                const newPrefix = data.new + ':';
                _pinnedTabs = _pinnedTabs.map(t => t.startsWith(oldPrefix) ? newPrefix + t.slice(oldPrefix.length) : t);
                _savePinnedTabs();
                _tabOrder = _tabOrder.map(t => t.startsWith(oldPrefix) ? newPrefix + t.slice(oldPrefix.length) : t);
                _saveTabOrder();
                // Update active target
                if (_termTarget && _termTarget.startsWith(oldPrefix)) {
                    _termTarget = newPrefix + _termTarget.slice(oldPrefix.length);
                    try { localStorage.setItem('term_target', _termTarget); } catch(e) {}
                    updateTmuxIndicator();
                }
                showFlash('sent', 'Renamed to ' + data.new);
                consolidatedPoll();
            } else {
                showFlash('error', data.error || 'Rename failed');
            }
        }).catch(() => showFlash('error', 'Offline'));
    };

    const dupeBtn = document.createElement('button');
    dupeBtn.className = 'tab-ctx-item';
    dupeBtn.textContent = 'Duplicate';
    dupeBtn.onclick = function(e) {
        e.stopPropagation();
        _removeContextMenu();
        const suggestedName = session + '-2';
        const newName = prompt('New session name:', suggestedName);
        if (!newName || newName.trim() === '') return;
        fetch('/terminal/duplicate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({session: session, name: newName.trim(), skip_init: true}),
        }).then(r => r.json()).then(data => {
            if (data.ok) {
                showFlash('sent', 'Created ' + data.session);
                _termTarget = data.target;
                try { localStorage.setItem('term_target', _termTarget); } catch(e) {}
                updateTmuxIndicator();
                consolidatedPoll();
                // Switch WS to new target
                if (_termWs && _termWsConnected) {
                    _termWs.send(JSON.stringify({type: 'subscribe', target: data.target, lines: _termLines}));
                } else {
                    captureTerminal();
                }
                // Prompt to run init command
                if (data.init_cmd && confirm('Run setup commands?\n\n' + data.init_cmd)) {
                    fetch('/terminal/run-init', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({session: data.session}),
                    }).then(r => r.json()).then(rd => {
                        if (rd.ok && !rd.skipped) showFlash('sent', 'Running: ' + rd.init_cmd);
                    }).catch(() => {});
                }
            } else {
                showFlash('error', data.error || 'Duplicate failed');
            }
        }).catch(() => showFlash('error', 'Offline'));
    };

    const killBtn = document.createElement('button');
    killBtn.className = 'tab-ctx-item tab-ctx-kill';
    killBtn.textContent = 'Kill';
    killBtn.onclick = function(e) {
        e.stopPropagation();
        _removeContextMenu();
        if (confirm('End session "' + session + '"?')) {
            fetch('/terminal/kill', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({session: session}),
            }).then(() => { if (typeof consolidatedPoll === 'function') consolidatedPoll(); });
        }
    };

    const reorderBtn = document.createElement('button');
    reorderBtn.className = 'tab-ctx-item';
    reorderBtn.textContent = 'Reorder';
    reorderBtn.onclick = function(e) {
        e.stopPropagation();
        _removeContextMenu();
        _enterReorderMode(tab);
    };

    menu.appendChild(pinBtn);
    menu.appendChild(renameBtn);
    menu.appendChild(reorderBtn);
    menu.appendChild(dupeBtn);
    menu.appendChild(killBtn);
    document.body.appendChild(menu);

    // Ensure menu stays on screen
    requestAnimationFrame(() => {
        const rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
        if (rect.bottom > window.innerHeight) menu.style.top = (y - rect.height) + 'px';
    });

    // Close on outside tap
    setTimeout(() => {
        document.addEventListener('touchstart', _onOutsideTap, {once: true});
        document.addEventListener('click', _onOutsideTap, {once: true});
    }, 50);
}

function _onOutsideTap(e) {
    const menu = document.querySelector('.tab-context-menu');
    if (menu && !menu.contains(e.target)) {
        _removeContextMenu();
    }
}

function _removeContextMenu() {
    const menu = document.querySelector('.tab-context-menu');
    if (menu) menu.remove();
    document.removeEventListener('touchstart', _onOutsideTap);
    document.removeEventListener('click', _onOutsideTap);
}

// --- Long-press detection ---

function _initTabLongPress() {
    const container = document.getElementById('session-tabs');
    if (!container) return;

    container.addEventListener('touchstart', function(e) {
        const tab = e.target.closest('.session-tab');
        if (!tab) return;
        _tabContextTarget = tab;
        const touch = e.touches[0];
        _tabLongPressTimer = setTimeout(function() {
            _tabLongPressTimer = null;
            _createContextMenu(tab, touch.clientX, touch.clientY);
            // Prevent the tap from also firing onclick
            tab._longPressTriggered = true;
        }, _TAB_LONG_PRESS_MS);
    }, {passive: true});

    container.addEventListener('touchmove', function() {
        if (_tabLongPressTimer) {
            clearTimeout(_tabLongPressTimer);
            _tabLongPressTimer = null;
        }
    }, {passive: true});

    container.addEventListener('touchend', function() {
        if (_tabLongPressTimer) {
            clearTimeout(_tabLongPressTimer);
            _tabLongPressTimer = null;
        }
    }, {passive: true});

    // Desktop: right-click context menu
    container.addEventListener('contextmenu', function(e) {
        const tab = e.target.closest('.session-tab');
        if (!tab) return;
        e.preventDefault();
        _createContextMenu(tab, e.clientX, e.clientY);
    });
}

// --- Drag reorder (hold-to-drag: 400ms hold required before drag starts) ---

let _dragTab = null;
let _dragStartX = 0;
let _dragOffsetX = 0;
let _dragHoldTimer = null;
let _dragEnabled = false;  // only true after hold delay

const _DRAG_HOLD_MS = 400;

function _initTabDragReorder() {
    const container = document.getElementById('session-tabs');
    if (!container) return;

    container.addEventListener('touchstart', function(e) {
        const tab = e.target.closest('.session-tab');
        if (!tab) return;
        _dragTab = tab;
        _dragStartX = e.touches[0].clientX;
        _dragOffsetX = 0;
        _dragEnabled = false;
        // Start hold timer — drag only enabled after holding
        _dragHoldTimer = setTimeout(function() {
            _dragHoldTimer = null;
            _dragEnabled = true;
            tab.style.opacity = '0.6';
            tab.style.boxShadow = '0 0 12px rgba(0, 255, 65, 0.4)';
        }, _DRAG_HOLD_MS);
    }, {passive: true});

    container.addEventListener('touchmove', function(e) {
        if (!_dragTab) return;
        const dx = e.touches[0].clientX - _dragStartX;

        // If moved before hold completes, cancel drag (it's a scroll)
        if (!_dragEnabled && Math.abs(dx) > 10) {
            if (_dragHoldTimer) { clearTimeout(_dragHoldTimer); _dragHoldTimer = null; }
            _dragTab = null;
            return;
        }

        if (!_dragEnabled) return;

        _dragOffsetX = dx;
        _dragTab.style.transform = 'translateX(' + dx + 'px)';
    }, {passive: true});

    container.addEventListener('touchend', function() {
        if (_dragHoldTimer) { clearTimeout(_dragHoldTimer); _dragHoldTimer = null; }
        if (!_dragTab) return;
        const tab = _dragTab;
        const wasDragging = _dragEnabled;
        _dragTab = null;
        _dragEnabled = false;

        tab.style.opacity = '';
        tab.style.transform = '';
        tab.style.boxShadow = '';

        // Only reorder if hold-drag was active and dragged enough
        if (wasDragging && Math.abs(_dragOffsetX) > 40) {
            const tabs = Array.from(container.querySelectorAll('.session-tab'));
            const idx = tabs.indexOf(tab);
            const direction = _dragOffsetX > 0 ? 1 : -1;
            const newIdx = Math.max(0, Math.min(tabs.length - 1, idx + direction));
            if (newIdx !== idx) {
                const targets = tabs.map(t => t.dataset.target);
                const [moved] = targets.splice(idx, 1);
                targets.splice(newIdx, 0, moved);
                _tabOrder = targets;
                _saveTabOrder();
                _reorderTabsDom();
            }
        }
    }, {passive: true});
}

// --- Click-to-place reorder mode ---

let _reorderModeTab = null;  // the tab being repositioned
let _reorderCleanup = null;  // function to tear down listeners + visuals

function _enterReorderMode(tab) {
    // Cancel any existing reorder mode
    if (_reorderModeTab) _exitReorderMode();

    _reorderModeTab = tab;
    tab.classList.add('reorder-source');

    const container = document.getElementById('session-tabs');
    if (!container) return;

    // Build drop zones between each tab
    _buildDropZones(container, tab);

    // Show cancel banner
    const banner = document.createElement('div');
    banner.className = 'reorder-banner';
    banner.innerHTML = 'Tap a slot to place <b>' + (tab.textContent || '').trim().split('\n')[0] +
        '</b> &mdash; <span class="reorder-cancel">cancel</span>';
    banner.querySelector('.reorder-cancel').onclick = () => _exitReorderMode();
    container.parentElement.insertBefore(banner, container);

    // ESC to cancel (desktop)
    const onKey = (e) => { if (e.key === 'Escape') _exitReorderMode(); };
    document.addEventListener('keydown', onKey);

    _reorderCleanup = () => {
        tab.classList.remove('reorder-source');
        banner.remove();
        document.removeEventListener('keydown', onKey);
        // Remove all drop zones
        container.querySelectorAll('.reorder-drop-zone').forEach(z => z.remove());
        _reorderModeTab = null;
        _reorderCleanup = null;
    };
}

function _exitReorderMode() {
    if (_reorderCleanup) _reorderCleanup();
}

function _buildDropZones(container, sourceTab) {
    // Remove old zones
    container.querySelectorAll('.reorder-drop-zone').forEach(z => z.remove());

    // Get all non-stale tabs in main area (stale tabs are now in the bottom sheet)
    const tabs = Array.from(container.querySelectorAll(':scope > .session-tab, :scope > .pin-divider'));
    const sourceTarget = sourceTab.dataset.target;

    // Insert a drop zone before each tab and after the last one
    const allTabs = Array.from(container.querySelectorAll(':scope > .session-tab'));

    for (let i = 0; i <= allTabs.length; i++) {
        const zone = document.createElement('div');
        zone.className = 'reorder-drop-zone';
        zone.dataset.insertIndex = i;

        // Don't show zones immediately adjacent to the source tab (no-op positions)
        const sourceIdx = allTabs.indexOf(sourceTab);
        if (i === sourceIdx || i === sourceIdx + 1) {
            zone.classList.add('reorder-zone-hidden');
        }

        zone.onclick = (e) => {
            e.stopPropagation();
            _placeTabAtIndex(container, sourceTab, parseInt(zone.dataset.insertIndex, 10), allTabs);
        };

        if (i < allTabs.length) {
            container.insertBefore(zone, allTabs[i]);
        } else {
            // After last tab but before stale pill (if present)
            const stalePill = container.querySelector('.stale-pill-wrap');
            if (stalePill) {
                container.insertBefore(zone, stalePill);
            } else {
                container.appendChild(zone);
            }
        }
    }
}

function _placeTabAtIndex(container, sourceTab, insertIdx, tabsAtTimeOfBuild) {
    // Compute new order from current tab positions, excluding the source
    const currentTabs = Array.from(container.querySelectorAll('.session-tab'));
    const targets = currentTabs.filter(t => t !== sourceTab).map(t => t.dataset.target);

    // Adjust insert index for removal of source
    const sourceWasAt = tabsAtTimeOfBuild.indexOf(sourceTab);
    let adjustedIdx = insertIdx;
    if (sourceWasAt !== -1 && sourceWasAt < insertIdx) adjustedIdx--;
    adjustedIdx = Math.max(0, Math.min(targets.length, adjustedIdx));

    targets.splice(adjustedIdx, 0, sourceTab.dataset.target);

    // Merge with full tab order (preserve any tabs not currently visible)
    const mainTabs = document.getElementById('session-tabs');
    const allTargets = Array.from(mainTabs.querySelectorAll('.session-tab')).map(t => t.dataset.target);
    // Build complete order: tabs in `targets` first at their positions, others keep relative order
    const ordered = [];
    const seen = new Set();
    // Put the container's tabs in their new order
    for (const t of targets) {
        ordered.push(t);
        seen.add(t);
    }
    // Append any tabs from other containers (stale group etc) not already placed
    for (const t of allTargets) {
        if (!seen.has(t)) {
            ordered.push(t);
            seen.add(t);
        }
    }

    _tabOrder = ordered;
    _saveTabOrder();

    _exitReorderMode();
    _reorderTabsDom();
    _applyStaleGroup();

    showFlash('sent', 'Tab moved');
}

// --- Reorder DOM tabs based on pinned + order state ---

function _reorderTabsDom() {
    const container = document.getElementById('session-tabs');
    if (!container) return;
    const tabs = Array.from(container.querySelectorAll('.session-tab'));
    if (tabs.length === 0) return;

    // Sort: pinned first, then by saved order, then by current DOM order
    tabs.sort((a, b) => {
        const aPin = _pinnedTabs.includes(a.dataset.target) ? 0 : 1;
        const bPin = _pinnedTabs.includes(b.dataset.target) ? 0 : 1;
        if (aPin !== bPin) return aPin - bPin;

        const aIdx = _tabOrder.indexOf(a.dataset.target);
        const bIdx = _tabOrder.indexOf(b.dataset.target);
        if (aIdx !== -1 && bIdx !== -1) return aIdx - bIdx;
        if (aIdx !== -1) return -1;
        if (bIdx !== -1) return 1;
        return 0;
    });

    // Update pin visual markers
    let hadPinned = false;
    for (const tab of tabs) {
        const isPinned = _pinnedTabs.includes(tab.dataset.target);
        tab.classList.toggle('pinned', isPinned);
        if (isPinned) hadPinned = true;
        container.appendChild(tab);
    }

    // Add/remove pin divider
    let divider = container.querySelector('.pin-divider');
    if (hadPinned) {
        const firstUnpinned = tabs.find(t => !_pinnedTabs.includes(t.dataset.target));
        if (firstUnpinned) {
            if (!divider) {
                divider = document.createElement('span');
                divider.className = 'pin-divider';
            }
            container.insertBefore(divider, firstUnpinned);
        }
    } else if (divider) {
        divider.remove();
    }
}

function _getStaleThreshold() { return SETTINGS ? SETTINGS.ui.stale_tab_threshold_sec : 3600; }

function _applyStaleGroup() {
    const container = document.getElementById('session-tabs');
    const sheetBody = document.getElementById('stale-sheet-body');
    const sheetCount = document.getElementById('stale-sheet-count');
    if (!container || !sheetBody) return;

    // Remove existing pill (we re-render it each pass)
    const existingPill = container.querySelector('.stale-pill-wrap');
    if (existingPill) existingPill.remove();

    // Find stale tabs in the strip:
    //   idle >= threshold AND not active AND not pinned AND not currently running
    // Team-lead grouping: if a team-lead is stale and any agent child is
    // running, the lead and ALL its children stay in the strip.
    const threshold = _getStaleThreshold();
    const allTabs = Array.from(container.querySelectorAll('.session-tab'));

    // Build a session -> [tabs] map so we can check "any child running"
    const bySession = {};
    allTabs.forEach(t => {
        const session = (t.dataset.target || '').split(':')[0];
        if (!bySession[session]) bySession[session] = [];
        bySession[session].push(t);
    });
    const sessionHasRunning = {};
    Object.keys(bySession).forEach(s => {
        sessionHasRunning[s] = bySession[s].some(t => t.classList.contains('running'));
    });

    const staleTabs = allTabs.filter(t => {
        if (t.classList.contains('active')) return false;
        if (t.classList.contains('running')) return false;
        if (_pinnedTabs.includes(t.dataset.target)) return false;
        const idle = parseInt(t.dataset.idleSeconds || '0', 10);
        if (idle < threshold) return false;
        // Team-lead/agent unit: if the session has any running child, keep
        // the whole group in the strip.
        const session = (t.dataset.target || '').split(':')[0];
        if (sessionHasRunning[session]) return false;
        return true;
    });

    // Sort by idle-time descending (most-recently-stale first)
    staleTabs.sort((a, b) => {
        const ai = parseInt(a.dataset.idleSeconds || '0', 10);
        const bi = parseInt(b.dataset.idleSeconds || '0', 10);
        return ai - bi;  // smaller idle = more recent
    });

    // Move stale tabs out of strip into sheet body
    sheetBody.innerHTML = '';
    staleTabs.forEach(tab => {
        const target = tab.dataset.target || '';
        const session = target.split(':')[0];
        const row = document.createElement('div');
        row.className = 'stale-sheet-row';
        row.dataset.target = target;

        const dotClass = _staleRowDotClass(tab);
        if (dotClass) {
            const dot = document.createElement('span');
            dot.className = 'row-dot ' + dotClass;
            row.appendChild(dot);
        }

        const name = document.createElement('span');
        name.className = 'row-name';
        // Reuse the tab's label text (first text node), strip badges/idle-time spans.
        const label = tab.cloneNode(true);
        Array.from(label.querySelectorAll('.tab-badge, .tab-idle-time, .tab-dot')).forEach(n => n.remove());
        name.textContent = label.textContent.trim() || session;
        row.appendChild(name);

        const idleSec = parseInt(tab.dataset.idleSeconds || '0', 10);
        const idle = document.createElement('span');
        idle.className = 'row-idle';
        idle.textContent = (typeof _formatIdleTime === 'function')
            ? _formatIdleTime(idleSec)
            : Math.floor(idleSec / 60) + 'm';
        row.appendChild(idle);

        row.onclick = () => _onStaleRowTap(target);
        sheetBody.appendChild(row);

        // Remove the tab from the strip
        tab.remove();
    });

    if (sheetCount) sheetCount.textContent = String(staleTabs.length);

    // If the sheet is open and no stale tabs remain, close it.
    if (_staleSheetOpen && staleTabs.length === 0) closeStaleSheet();

    if (staleTabs.length === 0) {
        _lastStaleCount = 0;
        return;
    }

    // Render pill
    const wrap = document.createElement('div');
    wrap.className = 'stale-pill-wrap';
    wrap.onclick = () => {
        if (_staleSheetOpen) closeStaleSheet(); else openStaleSheet();
    };
    const pill = document.createElement('span');
    pill.className = 'stale-pill';
    if (staleTabs.length > _lastStaleCount) pill.classList.add('flash');
    pill.innerHTML = '<span class="stale-pill-glyph">zZ</span>' +
        '<span class="stale-pill-count">' + staleTabs.length + '</span>';
    wrap.appendChild(pill);
    container.appendChild(wrap);

    // Clear flash after 250ms so it only fires on count increase
    if (pill.classList.contains('flash')) {
        setTimeout(() => pill.classList.remove('flash'), 260);
    }
    _lastStaleCount = staleTabs.length;
}

// Call reorder after session data is applied
const _origApplySessionsData = typeof _applySessionsData === 'function' ? _applySessionsData : null;

// Hook into tab rendering — called after each poll updates tabs
function _postTabRender() {
    _reorderTabsDom();
    _applyStaleGroup();
    // Restore reorder mode if it was active (poll rebuilds DOM every 5s)
    if (_reorderModeTab) {
        const target = _reorderModeTab.dataset.target;
        const container = document.getElementById('session-tabs');
        const restored = container ? container.querySelector('.session-tab[data-target="' + target + '"]') : null;
        if (restored) {
            // Clean up old state (banner was destroyed by innerHTML='')
            _reorderModeTab = null;
            if (_reorderCleanup) { _reorderCleanup = null; }
            _enterReorderMode(restored);
        } else {
            // Tab no longer exists
            _reorderModeTab = null;
            _reorderCleanup = null;
        }
    }
}

// Initialize on load
_initTabLongPress();
_initTabDragReorder();
