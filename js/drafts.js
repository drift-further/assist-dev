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
// The text the SERVER is known to hold for the target on screen — set when this
// module paints the composer from a draft, and again when a save is
// acknowledged. Anything else in the box was typed by the user and has not been
// persisted yet, which is the one thing a background write must never destroy.
let _composerSynced = { target: null, text: '' };

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
// Marker on the tab
// ================================================================
// A CLASS, never a child node: anything appended to .session-tab
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
// TYPING ALWAYS WINS. Every caller below is asynchronous — the first poll after
// a page load, a GET landing, another device's edit, a draft the sweep dropped —
// and each one used to write straight over whatever was in the box. Type while
// one is in flight and the characters vanished; the first poll fires within a
// second of load, which on a phone is exactly when you start typing.
//
// Switching TABS is not this case. There the text on screen belongs to the tab
// you are leaving, selectTab has already captured and flushed it, and swapping
// it out IS the point — so that one path passes force.
function _composerHoldsUnsavedInput(target) {
    const live = input.value;
    if (!live) return false;   // nothing to lose
    return !(_composerSynced.target === target && _composerSynced.text === live);
}

function _applyDraftToComposer(target, force) {
    const d = _draftEntry(target);
    if (!force && _composerHoldsUnsavedInput(target)) {
        // Keep what is on screen and ADOPT it as this tab's draft: claim the
        // target so the poll stops calling this "first sight", then capture and
        // persist, so the copy we just refused is overwritten rather than left
        // to reappear on the next poll. The tray stays put too — it belongs
        // with the text somebody is still typing.
        const refused = (d.text || '').trim();
        _draftInitFor = target;
        _syncEnterLockUI();
        saveDraftSoon(target);
        if (refused && refused !== input.value.trim() && typeof showFlash === 'function') {
            showFlash('ok', 'Kept what you typed');
        }
        return;
    }
    input.value = d.text || '';
    _composerSynced = { target: target, text: input.value };
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
    // A password prompt is on screen, so the composer holds a secret. Drafts are
    // persisted server-side and restored on the next visit, so capturing here
    // would leave the password in plaintext in the draft store and put it back in
    // the composer later. Keep whatever was already cached and skip this capture.
    if (typeof _isPasswordPrompt === 'function' && _isPasswordPrompt()) return;
    _draftCache[target] = {
        ..._draftEntry(target),
        text: input.value,
        attachments: _draftTray(),
    };
    _bumpDraftSeq(target);
}

// `target` defaults to the tab on screen. It is passed explicitly only when the
// composer is being adopted by a tab _termTarget has not moved to yet — an
// automatic switch, where selectTab calls in before it updates the target.
function saveDraftSoon(target) {
    target = target || _draftTarget();
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
            // The server now holds this text, so the composer is no longer
            // carrying anything unsaved and a remote copy may replace it again.
            // Still keyed on what is on screen: keystrokes made while the PUT
            // was in flight leave the two different, and stay protected.
            if (target === _draftTarget()) {
                _composerSynced = { target: target, text: data.draft.text || '' };
            }
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

// `force` says this is a deliberate tab switch, so the composer may be replaced
// even mid-word. It applies to the opening paint only — by the time the fetch
// lands the user may be typing into the tab they just opened, and that typing is
// protected like any other.
async function loadDraft(target, force) {
    if (!target) return;
    // Paint the cached copy first so a tab switch is never a flash of empty.
    _applyDraftToComposer(target, force);
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

// Called from selectTab() BEFORE _termTarget moves. `deliberate` is false when
// the app moved the tab itself (dead-pane recovery, a session it just
// launched) — then unsent text stays on screen and follows you, because nobody
// chose to leave the tab it was written for.
function onTabSwitchDraft(prevTarget, nextTarget, deliberate) {
    if (prevTarget === nextTarget) return;
    if (prevTarget) {
        _draftCapture(prevTarget);
        flushDraft(prevTarget);
    }
    loadDraft(nextTarget, deliberate !== false);
}

// A successful send clears text and tray but KEEPS the Enter lock: the lock is
// a property of the tab, not of the message. The server drops the row entirely
// when the lock is back to its armed default, so a plain tab still stores
// nothing.
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
// Poll sync — markers, and the second-device case
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
// The Enter lock — three things move together or it lies on a phone
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
// Wrapped, not passed by reference: saveDraftSoon takes an optional target and
// a listener would hand it the Event.
input.addEventListener('input', () => saveDraftSoon());
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
