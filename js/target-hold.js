// target-hold.js — a tab list built before a pane existed must not move you off it.
//
// Both tab strips (_applySessionsData in app.js on every /poll, loadSessions in
// terminal.js after launch/kill) read "the pane on screen is not in this list" as
// "it closed" and fall back to another pane, saving that choice. Right for a
// pane that exited; wrong for one this browser just created. /poll lists panes
// FIRST and then spends its time on the per-pane scan, so a poll in flight when
// Duplicate or Launch returns lands after the switch carrying a list without the
// new session, and the fallback dropped you on the first tab.
//
// Two guards, both on the client's own clock:
// - a list requested before this browser last chose its target cannot move it;
// - a pane created here is held until a list shows it, or the hold runs out.

const _TARGET_HOLD_MS = 15000;
let _targetChosenAt = 0;   // when this browser last moved _termTarget itself
let _heldTarget = null;    // a pane created here that no list has shown yet
let _heldUntil = 0;

// Call wherever this browser moves _termTarget on purpose. `created` marks a
// pane that did not exist a moment ago.
function noteTargetChosen(target, created) {
    _targetChosenAt = Date.now();
    if (created) {
        _heldTarget = target;
        _heldUntil = _targetChosenAt + _TARGET_HOLD_MS;
    }
}

// True when a tab list lacking `current` must NOT be read as it having closed.
// `listRequestedAt` is Date.now() taken just before the list was fetched.
function targetMissingIsStale(current, listRequestedAt) {
    if (listRequestedAt <= _targetChosenAt) return true;
    return !!current && current === _heldTarget && Date.now() < _heldUntil;
}

// A list that contains the held pane releases the hold.
function noteTargetListed(target) {
    if (target === _heldTarget) _heldTarget = null;
}
