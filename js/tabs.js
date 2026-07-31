// tabs.js — Tab pin/reorder: long-press context menu, touch drag reorder,
// server-side persistence (shared/tab_state.py) so phone and desktop agree.

const _TAB_LONG_PRESS_MS = 500;
let _tabLongPressTimer = null;
let _tabContextTarget = null;

// Server-owned state, refreshed from every /poll. Pin and order are keyed by
// SESSION (a session's panes stay contiguous so team-lead/agent grouping
// survives a reorder); snooze is keyed by TARGET so one agent pane can be
// tucked away on its own.
//
// A snoozed tab is forced into the zZ (idle) sheet regardless of idle time. It
// leaves when tapped, or when the server's wake sweep sees the pane's
// busy/quiet state flip (shared/tab_state.py: sweep_wakes).
let _tabState = { pinned: [], order: [], snoozed: {}, sort: 'manual' };

// Cycled from the tab context menu. 'manual' honours the dragged order; the
// other two are recomputed server-side every poll, so a session opened later
// lands in the right place instead of on the end.
const _SORT_MODES = ['manual', 'name', 'created'];
const _SORT_LABELS = { manual: 'Manual', name: 'Name', created: 'Opened' };

function _sessionOf(target) { return (target || '').split(':')[0]; }
function _isPinned(target) { return _tabState.pinned.includes(_sessionOf(target)); }
function _isSnoozed(target) { return !!(target && _tabState.snoozed[target]); }

// Called from the poll handler with the doc the server shipped alongside the
// session list.
function _applyTabState(doc) {
    if (!doc) return;
    _tabState = {
        pinned: doc.pinned || [],
        order: doc.order || [],
        snoozed: doc.snoozed || {},
        sort: doc.sort || 'manual',
    };
}

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
    // Tapping a snoozed row wakes it immediately (no one-poll lag).
    _wakeSnoozed(target);
    if (typeof selectTab === 'function') selectTab(target);
    // _applyStaleGroup will re-evaluate on the next poll and the freshly
    // active tab will leave the sheet automatically (active tabs are excluded).
}

// --- Server writes ---

// Pin and order changes re-sort the strip, and the server owns that sort, so
// each write is followed by a poll rather than a second copy of the sort in JS.
function _saveLists(patch) {
    Object.assign(_tabState, patch);
    return fetch('/api/tab-state', {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(patch),
    }).then(r => r.json()).then(d => {
        if (d.ok) _applyTabState(d.tab_state);
        if (typeof consolidatedPoll === 'function') consolidatedPoll();
    }).catch(() => {});
}

// Snooze doesn't reorder anything, so update locally first — the zZ sheet
// reacts on the tap instead of five seconds later.
function _saveSnooze(target, on) {
    if (on) _tabState.snoozed[target] = {at: Date.now() / 1000, was_busy: false};
    else delete _tabState.snoozed[target];
    return fetch('/api/tab-state/snooze', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target: target, on: !!on}),
    }).then(r => r.json()).then(d => {
        if (d.ok) _applyTabState(d.tab_state);
    }).catch(() => {});
}

// Session order as the strip currently shows it — the basis for both reorder
// gestures, since order is per session, not per pane.
function _sessionOrderFromDom() {
    const container = document.getElementById('session-tabs');
    if (!container) return _tabState.order.slice();
    const seen = new Set();
    const sessions = [];
    for (const t of container.querySelectorAll('.session-tab')) {
        const s = _sessionOf(t.dataset.target);
        if (s && !seen.has(s)) { seen.add(s); sessions.push(s); }
    }
    return sessions;
}

// Wake a snoozed tab (idempotent): called on tap/select.
function _wakeSnoozed(target) {
    if (_isSnoozed(target)) _saveSnooze(target, false);
}

// --- Context menu ---

function _createContextMenu(tab, x, y) {
    _removeContextMenu();
    const target = tab.dataset.target;
    const session = target.split(':')[0];
    const isPinned = _isPinned(target);
    const isSnoozed = _isSnoozed(target);

    const menu = document.createElement('div');
    menu.className = 'tab-context-menu';
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';

    const pinBtn = document.createElement('button');
    pinBtn.className = 'tab-ctx-item';
    pinBtn.textContent = isPinned ? 'Unpin' : 'Pin';
    pinBtn.onclick = function(e) {
        e.stopPropagation();
        // Pinning is per session — pinning one pane of a team would otherwise
        // tear the lead away from its agents.
        const pinned = isPinned
            ? _tabState.pinned.filter(s => s !== session)
            : _tabState.pinned.concat([session]);
        _saveLists({pinned: pinned});
        _applyPinMarkers();
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
                // Pin/order/snooze refs are rewritten server-side (see
                // routes/terminal.py: terminal_rename), so every device gets the
                // fix-up; the poll below brings the new doc down.
                const oldPrefix = session + ':';
                const newPrefix = data.new + ':';
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

    // One entry that cycles Manual -> Name -> Opened rather than three, so the
    // menu stays thumb-sized and always shows which mode is in force.
    const sortBtn = document.createElement('button');
    sortBtn.className = 'tab-ctx-item tab-ctx-sort';
    sortBtn.textContent = 'Sort: ' + (_SORT_LABELS[_tabState.sort] || 'Manual');
    sortBtn.onclick = function(e) {
        e.stopPropagation();
        const next = _SORT_MODES[(_SORT_MODES.indexOf(_tabState.sort) + 1) % _SORT_MODES.length];
        _saveLists({sort: next});
        showFlash('sent', 'Sort: ' + _SORT_LABELS[next]);
        _removeContextMenu();
    };

    // Deliberate fit — TUI panes only (they're the ones that used to auto-shrink).
    const isTui = !!(typeof _paneTui !== 'undefined' && _paneTui[target]);
    let fitBtn = null;
    if (isTui) {
        fitBtn = document.createElement('button');
        fitBtn.className = 'tab-ctx-item';
        fitBtn.textContent = 'Fit to screen';
        fitBtn.onclick = function(e) {
            e.stopPropagation();
            _removeContextMenu();
            if (typeof fitTuiToScreen === 'function') fitTuiToScreen(target);
        };
    }

    const snoozeBtn = document.createElement('button');
    snoozeBtn.className = 'tab-ctx-item tab-ctx-snooze';
    snoozeBtn.textContent = isSnoozed ? 'Unsnooze' : 'Snooze';
    snoozeBtn.onclick = function(e) {
        e.stopPropagation();
        _saveSnooze(target, !isSnoozed);
        if (typeof _applyStaleGroup === 'function') _applyStaleGroup();
        _removeContextMenu();
    };

    menu.appendChild(pinBtn);
    menu.appendChild(renameBtn);
    menu.appendChild(reorderBtn);
    menu.appendChild(sortBtn);
    if (fitBtn) menu.appendChild(fitBtn);
    menu.appendChild(dupeBtn);
    menu.appendChild(snoozeBtn);
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
            // Offset the menu below the touch point so the synthesized click
            // on release doesn't land on the first menu item ("Pin").
            _createContextMenu(tab, touch.clientX, touch.clientY + 12);
            // Mark so touchend swallows the synthesized click
            tab._longPressTriggered = true;
        }, _TAB_LONG_PRESS_MS);
    }, {passive: true});

    container.addEventListener('touchmove', function() {
        if (_tabLongPressTimer) {
            clearTimeout(_tabLongPressTimer);
            _tabLongPressTimer = null;
        }
    }, {passive: true});

    container.addEventListener('touchend', function(e) {
        if (_tabLongPressTimer) {
            clearTimeout(_tabLongPressTimer);
            _tabLongPressTimer = null;
        }
        // Consume the long-press flag: swallow the click the browser
        // synthesizes right after this touchend so it can't hit the menu
        // or re-select the tab.
        const tab = e.target.closest('.session-tab');
        if (tab && tab._longPressTriggered) {
            tab._longPressTriggered = false;
            const swallow = function(ev) {
                ev.preventDefault();
                ev.stopPropagation();
            };
            document.addEventListener('click', swallow, {capture: true, once: true});
            // Disarm if no synthesized click arrives (some browsers skip it)
            setTimeout(function() {
                document.removeEventListener('click', swallow, {capture: true});
            }, 500);
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
            // Tab may have been removed (poll rebuild, session died) — bail
            // rather than reorder with -1 math.
            if (!container.contains(tab)) return;
            // A drag nudges the whole SESSION one slot, not one pane: order is
            // per session, and the server keeps a session's panes contiguous.
            const sessions = _sessionOrderFromDom();
            const idx = sessions.indexOf(_sessionOf(tab.dataset.target));
            if (idx === -1) return;
            const direction = _dragOffsetX > 0 ? 1 : -1;
            const newIdx = Math.max(0, Math.min(sessions.length - 1, idx + direction));
            if (newIdx !== idx) {
                sessions.splice(newIdx, 0, sessions.splice(idx, 1)[0]);
                _saveLists({order: sessions});
            }
        }
    }, {passive: true});

    // touchcancel (call, browser gesture takeover) — clean up so _dragTab
    // can't stay set and block the poll's tab rebuild forever.
    container.addEventListener('touchcancel', function() {
        if (_dragHoldTimer) { clearTimeout(_dragHoldTimer); _dragHoldTimer = null; }
        if (_dragTab) {
            _dragTab.style.opacity = '';
            _dragTab.style.transform = '';
            _dragTab.style.boxShadow = '';
            _dragTab = null;
        }
        _dragEnabled = false;
    }, {passive: true});
}

// --- Desktop drag reorder (HTML5 DnD; touch uses the hold-drag above) ---

let _dndSession = null;  // session being dragged with a mouse

function _initTabDesktopDrag() {
    const container = document.getElementById('session-tabs');
    if (!container) return;

    container.addEventListener('dragstart', function(e) {
        const tab = e.target.closest('.session-tab');
        if (!tab) return;
        _dndSession = _sessionOf(tab.dataset.target);
        tab.classList.add('dnd-source');
        e.dataTransfer.effectAllowed = 'move';
        // Firefox refuses to start a drag without payload.
        try { e.dataTransfer.setData('text/plain', _dndSession); } catch (err) {}
    });

    container.addEventListener('dragover', function(e) {
        if (!_dndSession) return;
        e.preventDefault();  // required, or the drop never fires
        e.dataTransfer.dropEffect = 'move';
        const tab = e.target.closest('.session-tab');
        _clearDropMarkers();
        if (!tab || _sessionOf(tab.dataset.target) === _dndSession) return;
        // Drop before or after, depending on which half the cursor is over.
        const box = tab.getBoundingClientRect();
        tab.classList.add(e.clientX < box.left + box.width / 2 ? 'dnd-before' : 'dnd-after');
    });

    container.addEventListener('drop', function(e) {
        if (!_dndSession) return;
        e.preventDefault();
        const marked = container.querySelector('.dnd-before, .dnd-after');
        const source = _dndSession;
        _endDesktopDrag();
        if (!marked) return;
        const anchor = _sessionOf(marked.dataset.target);
        if (anchor === source) return;
        const after = marked.classList.contains('dnd-after');
        const sessions = _sessionOrderFromDom().filter(s => s !== source);
        let at = sessions.indexOf(anchor);
        if (at === -1) return;
        sessions.splice(after ? at + 1 : at, 0, source);
        _saveLists({order: sessions});   // implies sort: manual, server-side
        showFlash('sent', 'Tab moved');
    });

    container.addEventListener('dragend', _endDesktopDrag);
}

function _clearDropMarkers() {
    document.querySelectorAll('.dnd-before, .dnd-after')
        .forEach(t => t.classList.remove('dnd-before', 'dnd-after'));
}

function _endDesktopDrag() {
    _dndSession = null;
    _clearDropMarkers();
    document.querySelectorAll('.dnd-source').forEach(t => t.classList.remove('dnd-source'));
}

// --- Click-to-place reorder mode ---

let _reorderModeTab = null;  // the tab being repositioned
let _reorderCleanup = null;  // function to tear down listeners + visuals

// True while a tab drag or reorder placement is in progress — the 5s poll
// checks this and skips its tab-strip rebuild (app.js _applySessionsData).
function _tabsInteractionActive() {
    return !!(_dragTab || _reorderModeTab || _dndSession);
}

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
    banner.innerHTML = 'Tap a slot to place <b>' + escHtml((tab.textContent || '').trim().split('\n')[0]) +
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
    // Zones sit between tabs, so slot i means "in front of whichever tab was at
    // index i". Resolve that to a SESSION, since order is per session.
    const sourceSession = _sessionOf(sourceTab.dataset.target);
    const before = insertIdx < tabsAtTimeOfBuild.length
        ? _sessionOf(tabsAtTimeOfBuild[insertIdx].dataset.target)
        : null;

    const sessions = _sessionOrderFromDom().filter(s => s !== sourceSession);
    let at = (before && before !== sourceSession) ? sessions.indexOf(before) : -1;
    if (at === -1) at = sessions.length;  // dropped past the end, or onto itself
    sessions.splice(at, 0, sourceSession);

    _saveLists({order: sessions});
    _exitReorderMode();

    showFlash('sent', 'Tab moved');
}

// --- Pin markers ---
// The server hands the session list down already sorted (shared/tab_state.py:
// apply_order), so the DOM is in the right order by the time it is built. All
// that is left here is the pinned styling and the divider.

function _applyPinMarkers() {
    const container = document.getElementById('session-tabs');
    if (!container) return;
    const tabs = Array.from(container.querySelectorAll('.session-tab'));
    if (tabs.length === 0) return;

    let hadPinned = false;
    for (const tab of tabs) {
        const isPinned = _isPinned(tab.dataset.target);
        tab.classList.toggle('pinned', isPinned);
        if (isPinned) hadPinned = true;
        // Desktop drag-to-reorder. Set here rather than in the two renderers so
        // both paths get it from one place.
        tab.draggable = true;
    }

    // Add/remove pin divider
    let divider = container.querySelector('.pin-divider');
    if (hadPinned) {
        const firstUnpinned = tabs.find(t => !_isPinned(t.dataset.target));
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
        const target = t.dataset.target;
        if (_isPinned(target)) return false;
        // Manual snooze forces the tab into ZZ even when it's the active or a
        // running tab — snoozing the tab you're looking at is the main use
        // case. A snoozed tab leaves ZZ when tapped/selected (_onStaleRowTap /
        // selectTab), or when the server's wake sweep sees the pane's
        // busy/quiet state flip: snooze a working pane and it returns when it
        // finishes, snooze a quiet one and it returns when it starts up again
        // (shared/tab_state.py: sweep_wakes).
        if (_isSnoozed(target)) return true;
        if (t.classList.contains('active')) return false;
        if (t.classList.contains('running')) return false;
        const idle = parseInt(t.dataset.idleSeconds || '0', 10);
        if (idle < threshold) return false;
        // Team-lead/agent unit: if the session has any running child, keep
        // the whole group in the strip.
        const session = (target || '').split(':')[0];
        if (sessionHasRunning[session]) return false;
        return true;
    });

    // Sort by idle-time descending (most-recently-stale first)
    staleTabs.sort((a, b) => {
        const ai = parseInt(a.dataset.idleSeconds || '0', 10);
        const bi = parseInt(b.dataset.idleSeconds || '0', 10);
        return ai - bi;  // smaller idle = more recent
    });

    // Hide/show tabs via class — never .remove() — so this function stays
    // idempotent across calls (poll-driven and activity-decay-driven). If we
    // removed stale tabs from the DOM, a later between-poll call (which only
    // inspects tabs currently in the strip) would lose track of them when it
    // rebuilds the sheet, causing stale rows to flicker out.
    const staleSet = new Set(staleTabs);
    allTabs.forEach(t => t.classList.toggle('stale-tucked', staleSet.has(t)));

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
        const label = tab.cloneNode(true);
        Array.from(label.querySelectorAll('.tab-badge, .tab-idle-time, .tab-dot')).forEach(n => n.remove());
        name.textContent = label.textContent.trim() || session;
        row.appendChild(name);

        const idleSec = parseInt(tab.dataset.idleSeconds || '0', 10);
        const idle = document.createElement('span');
        if (_isSnoozed(target)) {
            // Manually snoozed — a real idle time would read misleadingly low
            // (e.g. "0m") in an idle list, so show the snooze glyph instead.
            idle.className = 'row-snooze';
            idle.textContent = 'zZ';
        } else {
            idle.className = 'row-idle';
            idle.textContent = (typeof _formatIdleTime === 'function')
                ? _formatIdleTime(idleSec)
                : Math.floor(idleSec / 60) + 'm';
        }
        row.appendChild(idle);

        row.onclick = () => _onStaleRowTap(target);
        sheetBody.appendChild(row);
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

// Hook into tab rendering — called after each poll updates tabs
function _postTabRender() {
    _applyPinMarkers();
    // Stale group is applied later, after _applyStatesData populates
    // dataset.idleSeconds — calling it here would always see idle=0 and
    // bounce stale tabs back into the strip on every poll.
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

// --- One-time migration off localStorage ---
// Pin/order/snooze used to be per-device. Hand whatever this browser still
// holds to the server once, then drop the keys. The server ignores the import
// if it already has state, so the first device to sync wins and a second one
// can't clobber it.

const _LEGACY_TAB_KEYS = ['assist_pinned_tabs', 'assist_tab_order', 'assist_snoozed_tabs'];

function _dropLegacyTabKeys() {
    _LEGACY_TAB_KEYS.forEach(k => { try { localStorage.removeItem(k); } catch(e) {} });
}

function _migrateLegacyTabState() {
    const read = (key) => {
        try {
            const parsed = JSON.parse(localStorage.getItem(key) || '[]');
            return Array.isArray(parsed) ? parsed.filter(v => typeof v === 'string' && v) : [];
        } catch(e) { return []; }
    };
    const payload = {
        pinned: read('assist_pinned_tabs'),
        order: read('assist_tab_order'),
        snoozed: read('assist_snoozed_tabs'),
    };
    if (!payload.pinned.length && !payload.order.length && !payload.snoozed.length) {
        _dropLegacyTabKeys();
        return;
    }
    fetch('/api/tab-state/import', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(d => {
        if (!d.ok) return;
        _applyTabState(d.tab_state);
        _dropLegacyTabKeys();
        if (typeof consolidatedPoll === 'function') consolidatedPoll();
    }).catch(() => {});  // keep the keys; retry on the next load
}

// Initialize on load
_initTabLongPress();
_initTabDragReorder();
_initTabDesktopDrag();
_migrateLegacyTabState();
