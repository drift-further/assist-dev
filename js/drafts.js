// drafts.js — the composer belongs to the session tab.
//
// Text, attachment tray and Enter lock are per tmux target and live on the
// server (shared/drafts.py), so a draft begun on the desktop is on the phone.
// Switching tabs swaps all three together; unsent text simply IS that tab's
// draft — there is no mode to toggle and no save command.
//
// Keyed by _termTarget (the TAB), never getInputTarget(): input routing can
// point sends at a split pane, but the composer you are typing into still
// belongs to the tab you are looking at.

const _DRAFT_SAVE_MS = 600;          // debounce after typing stops
const _DRAFT_EMPTY = { text: '', attachments: [], enter_armed: true, updated_at: 0 };

let _draftCache = {};                // target -> {text, attachments, enter_armed, updated_at}
let _draftRev = {};                  // target -> updated_at this browser has seen
let _draftMarks = new Set();         // targets the server says hold a draft
let _draftSaveTimer = null;
let _draftDirtyFor = null;           // target with local edits not yet persisted
let _draftInitFor = null;            // target the composer is currently showing
// target -> count of LOCAL mutations (typing, tray, lock, send). loadDraft()
// captures it before its fetch and refuses to apply a response that is older
// than the newest local change — otherwise a GET issued on tab switch can land
// after you have already tapped the lock and quietly undo it, which is exactly
// what a slow phone connection makes routine.
let _draftSeq = {};

function _bumpDraftSeq(target) {
    if (!target) return 0;
    _draftSeq[target] = (_draftSeq[target] || 0) + 1;
    return _draftSeq[target];
}

function _draftTarget() { return _termTarget || ''; }

function _draftEntry(target) {
    return _draftCache[target] || { ..._DRAFT_EMPTY };
}

// Read by BOTH send paths in app.js. Armed by default, so a target this
// browser has never seen keeps today's one-liner reflex.
function draftEnterArmed() {
    return _draftEntry(_draftTarget()).enter_armed !== false;
}

// The tray shape the server stores. An entry still uploading has no path yet,
// so it cannot be restored and is left out; _uploadAttachment() saves again the
// moment it lands.
function _draftTray() {
    return (_attachments || [])
        .filter(a => a.path)
        .map(a => ({ id: a.id, name: a.name, size: a.size, path: a.path }));
}

function _draftHasContent(entry) {
    return !!((entry.text || '').trim() || (entry.attachments || []).length);
}

// ================================================================
// Marker on the tab (spec §6)
// ================================================================
// A CLASS, never a child node — gotcha 881: anything appended to .session-tab
// is picked up by every consumer that recovers a label from textContent. The
// marker is a ::after pseudo-element in css/input.css, which textContent cannot
// see at all, so this cannot reproduce that bug.
function _renderDraftMarks() {
    const local = _draftTarget();
    document.querySelectorAll('.session-tab[data-target]').forEach(tab => {
        const target = tab.dataset.target;
        // The active tab is authoritative about itself: its own composer is on
        // screen, and waiting for the next poll to confirm what you just typed
        // makes the marker feel broken.
        const marked = target === local
            ? _draftHasContent({ text: input.value, attachments: _attachments })
            : _draftMarks.has(target);
        tab.classList.toggle('has-draft', marked);
    });
}

// ================================================================
// Composer <-> draft
// ================================================================
function _applyDraftToComposer(target) {
    const d = _draftEntry(target);
    input.value = d.text || '';
    _attachments = (d.attachments || []).map(a => ({ ...a, uploading: false }));
    _draftInitFor = target;
    renderAttachments();
    if (typeof renderSegChips === 'function') renderSegChips();
    _syncEnterLockUI();
    _renderDraftMarks();
}

// Mirror what is on screen into the cache without touching the network, so a
// tab switch always flushes the latest keystroke rather than the last one the
// debounce happened to catch.
function _draftCapture(target) {
    if (!target) return;
    _draftCache[target] = {
        ..._draftEntry(target),
        text: input.value,
        attachments: _draftTray(),
    };
    _bumpDraftSeq(target);
}

function saveDraftSoon() {
    const target = _draftTarget();
    if (!target) return;
    _draftCapture(target);
    _draftDirtyFor = target;
    _renderDraftMarks();
    if (_draftSaveTimer) clearTimeout(_draftSaveTimer);
    _draftSaveTimer = setTimeout(() => flushDraft(target), _DRAFT_SAVE_MS);
}

async function flushDraft(target) {
    if (_draftSaveTimer) { clearTimeout(_draftSaveTimer); _draftSaveTimer = null; }
    if (!target) return;
    const d = _draftCache[target];
    if (!d) return;
    if (_draftDirtyFor === target) _draftDirtyFor = null;
    try {
        const resp = await fetch('/api/draft', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target: target,
                text: d.text || '',
                attachments: d.attachments || [],
                enter_armed: d.enter_armed !== false,
            }),
            // Survives the page being backgrounded or closed mid-save — the
            // phone case this whole feature exists for.
            keepalive: true,
        });
        const data = await resp.json();
        if (data.ok) {
            _draftCache[target] = data.draft;
            _draftRev[target] = data.draft.updated_at;
            // Mark it here rather than waiting for the next poll to say so.
            // This is the tab you just switched AWAY from, so _renderDraftMarks()
            // no longer treats it as authoritative about itself — without this
            // its marker lags by up to a full poll interval, which reads as the
            // draft not having been kept.
            if (_draftHasContent(data.draft)) _draftMarks.add(target);
            else _draftMarks.delete(target);
            _renderDraftMarks();
        }
    } catch (e) {}
}

async function loadDraft(target) {
    if (!target) return;
    // Paint the cached copy first so a tab switch is never a flash of empty.
    _applyDraftToComposer(target);
    const seq = _draftSeq[target] || 0;
    try {
        const resp = await fetch('/api/draft?target=' + encodeURIComponent(target));
        const data = await resp.json();
        if (!data.ok) return;
        // Switched away, or changed this draft locally, while the fetch was in
        // flight — either way the server copy is no longer what to show.
        if (target !== _draftTarget() || (_draftSeq[target] || 0) !== seq) return;
        _draftCache[target] = data.draft;
        _draftRev[target] = data.draft.updated_at;
        _applyDraftToComposer(target);
    } catch (e) {}
}

// Called from selectTab() BEFORE _termTarget moves.
function onTabSwitchDraft(prevTarget, nextTarget) {
    if (prevTarget === nextTarget) return;
    if (prevTarget) {
        _draftCapture(prevTarget);
        flushDraft(prevTarget);
    }
    loadDraft(nextTarget);
}

// A successful send clears text and tray but KEEPS the Enter lock: the lock is
// a property of the tab, not of the message. The server drops the row entirely
// when the lock is back to its armed default, so a plain tab still stores
// nothing (spec §7).
// Called at the START of a send. A debounced save still armed for this target
// would otherwise fire mid-flight and — since the two PUTs race — could land
// AFTER the clear and resurrect as a draft the very text that was just sent.
function draftCancelPendingSave(target) {
    if (_draftSaveTimer && _draftDirtyFor === target) {
        clearTimeout(_draftSaveTimer);
        _draftSaveTimer = null;
        _draftDirtyFor = null;
    }
}

async function clearDraftAfterSend(target) {
    if (!target) return;
    draftCancelPendingSave(target);
    _draftCache[target] = { ..._draftEntry(target), text: '', attachments: [] };
    _bumpDraftSeq(target);
    _draftMarks.delete(target);
    _renderDraftMarks();
    await flushDraft(target);
}

// ================================================================
// Poll sync — markers, and the second-device case (spec §9.3)
// ================================================================
function _applyDraftsData(block) {
    if (!block) return;
    _draftMarks = new Set(block.marks || []);
    const rev = block.rev || {};
    const target = _draftTarget();

    if (target && _draftInitFor !== target) {
        // First sight of this target in this page's life — covers page load
        // and every path that moves _termTarget without going through
        // selectTab() (poll fallback, dead-pane recovery).
        loadDraft(target);
    } else if (target && _draftDirtyFor !== target) {
        const known = _draftRev[target] || 0;
        if (Object.prototype.hasOwnProperty.call(rev, target)) {
            // Another device wrote this tab's draft.
            if (rev[target] > known) loadDraft(target);
        } else if (known > 0) {
            // Another device sent from this tab, or the sweep expired it.
            _draftRev[target] = 0;
            _draftCache[target] = { ..._DRAFT_EMPTY };
            _applyDraftToComposer(target);
        }
    }
    _renderDraftMarks();
}

// ================================================================
// The Enter lock (spec §4) — three things move together or it lies on a phone
// ================================================================
// 1. app.js keydown stops calling doPaste() and lets Enter insert a newline
// 2. app.js's 150ms IME poller goes FULLY INERT for this target — it currently
//    EATS trailing newlines, which are legitimate content once Enter means
//    newline
// 3. enterkeyhint flips send -> enter, so the phone keyboard stops advertising
//    a Send key that no longer sends
// This function owns 3; app.js reads draftEnterArmed() for 1 and 2.
function _syncEnterLockUI() {
    const armed = draftEnterArmed();
    input.setAttribute('enterkeyhint', armed ? 'send' : 'enter');
    const btn = document.getElementById('enter-lock');
    if (!btn) return;
    btn.classList.toggle('unlocked', !armed);
    btn.setAttribute('aria-pressed', armed ? 'false' : 'true');
    btn.title = armed
        ? 'Enter sends — tap for multi-line'
        : 'Enter makes a newline — tap to send on Enter';
}

function toggleEnterLock() {
    const target = _draftTarget();
    if (!target) {
        showFlash('error', 'No session');
        return;
    }
    const armed = draftEnterArmed();
    _draftCapture(target);
    _draftCache[target] = { ..._draftEntry(target), enter_armed: !armed };
    _bumpDraftSeq(target);
    _syncEnterLockUI();
    _renderDraftMarks();
    // A deliberate tap, not typing — persist immediately rather than debounced.
    flushDraft(target);
    // A keyboard already on screen read enterkeyhint when it opened, so the
    // Send/Enter key label only changes if focus cycles. Costs a keyboard
    // flicker on the tab you just tapped; the alternative is a key that lies.
    if (document.activeElement === input) {
        input.blur();
        input.focus();
    }
}

// ================================================================
// Wiring
// ================================================================
input.addEventListener('input', saveDraftSoon);
input.addEventListener('blur', () => {
    const target = _draftTarget();
    if (target && _draftDirtyFor === target) {
        _draftCapture(target);
        flushDraft(target);
    }
});
// Backgrounding a phone browser is the common way a session ends. visibility
// change fires reliably where unload does not.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'hidden') return;
    const target = _draftTarget();
    if (target && _draftDirtyFor === target) {
        _draftCapture(target);
        flushDraft(target);
    }
});
