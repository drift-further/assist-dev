"""shared/tab_state.py — Server-side tab order, pinning, and snoozing.

These three used to live in browser localStorage, so a phone and a desktop
disagreed about which tabs appeared where and which were tucked into the zZ
sheet. They are server state now; the browser is a renderer.

Keying is deliberately mixed: **order and pin are per session, snooze is per
target**. Pinning a single target lets a team lead float away from its agent
panes, which breaks the `is_subpane` / team-lead adjacency the UI derives from
pane order. Snooze stays per target so one agent pane can be tucked away on its
own.
"""

import copy
import json
import threading
import time

from shared import state

TAB_STATE_FILE = state.DATA_DIR / "tab_state.json"

# A pane counts as busy while its last content change is newer than this — the
# same 30s boundary routes/poll.py uses to call a pane idle.
QUIET_SEC = 30

# A snooze always sticks for at least this long. Without it, snoozing a pane
# that is a second away from crossing the busy/quiet line would wake it again
# on the very next poll.
MIN_SNOOZE_SEC = 30

# How the strip is ordered. "manual" honours the explicit order list (what
# dragging a tab writes); the other two are computed fresh every poll, so a new
# session lands in the right place instead of at the end.
SORT_MODES = ("manual", "name", "created")

_lock = threading.Lock()
_tab_state = {"pinned": [], "order": [], "snoozed": {}, "sort": "manual"}


def _dedup(values):
    """Strings only, first occurrence wins, order preserved.

    Anything that isn't a list becomes []. A bare string would otherwise
    iterate character by character and turn a hand-edited tab_state.json into
    a list of letters.
    """
    if not isinstance(values, (list, tuple)):
        return []
    seen = set()
    out = []
    for v in values:
        if isinstance(v, str) and v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _normalize(data):
    """Coerce a loaded or patched doc into the canonical shape."""
    if not isinstance(data, dict):
        data = {}
    snoozed = {}
    raw = data.get("snoozed")
    if isinstance(raw, dict):
        for target, meta in raw.items():
            if not isinstance(target, str) or not target or not isinstance(meta, dict):
                continue
            try:
                at = float(meta.get("at", 0.0))
            except (TypeError, ValueError):
                at = 0.0
            snoozed[target] = {"at": at, "was_busy": bool(meta.get("was_busy"))}
    sort = data.get("sort")
    return {
        "pinned": _dedup(data.get("pinned")),
        "order": _dedup(data.get("order")),
        "snoozed": snoozed,
        "sort": sort if sort in SORT_MODES else "manual",
    }


def load_tab_state():
    """Load from disk. Called once at import."""
    global _tab_state
    try:
        data = json.loads(TAB_STATE_FILE.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}
    with _lock:
        _tab_state = _normalize(data)


def _save_locked():
    """Write to disk. Caller must hold _lock."""
    try:
        state.atomic_write_json(TAB_STATE_FILE, _tab_state)
    except OSError:
        pass


def get_tab_state():
    """Return the full doc (deep copy)."""
    with _lock:
        return copy.deepcopy(_tab_state)


def set_lists(pinned=None, order=None, sort=None):
    """Replace the pinned and/or order lists (session names), and/or sort mode.

    Writing an explicit order implies manual sort — otherwise the drag you just
    made would be silently overruled by a name or date sort still in force.
    """
    global _tab_state
    with _lock:
        merged = dict(_tab_state)
        if pinned is not None:
            merged["pinned"] = pinned
        if order is not None:
            merged["order"] = order
            if sort is None:
                sort = "manual"
        if sort is not None:
            merged["sort"] = sort
        _tab_state = _normalize(merged)
        _save_locked()
        return copy.deepcopy(_tab_state)


def _is_busy(target, now=None):
    """True while the pane's last content change is under QUIET_SEC old.

    Takes state._activity_lock, so it must never be called while holding
    _lock — the lock order in this module is always _lock then _activity_lock.
    """
    now = now if now is not None else time.time()
    with state._activity_lock:
        last = state.pane_last_activity.get(target)
    if last is None:
        return False
    return (now - last) < QUIET_SEC


def set_snooze(target, on):
    """Snooze or wake one target.

    Snoozing stamps the pane's current busy/quiet state so sweep_wakes() can
    spot the transition later.
    """
    global _tab_state
    # Outside the lock — _is_busy takes state._activity_lock.
    entry = {"at": time.time(), "was_busy": _is_busy(target)} if on else None
    with _lock:
        snoozed = dict(_tab_state["snoozed"])
        if entry is None:
            snoozed.pop(target, None)
        else:
            snoozed[target] = entry
        _tab_state = {**_tab_state, "snoozed": snoozed}
        _save_locked()
        return copy.deepcopy(_tab_state)


def sweep_wakes(live_targets, now=None):
    """Auto-wake snoozed panes whose busy/quiet state flipped, and GC dead ones.

    A snoozed pane wakes on the first busy<->quiet transition after it was
    snoozed: snooze a working pane and it returns when it finishes; snooze a
    quiet one and it returns when it starts up again.

    Waking on raw content change instead does not work — routes/poll.py hashes
    the entire capture including ANSI, so a TUI spinner re-triggers it every
    poll and snoozing a busy pane would be a no-op.
    """
    global _tab_state
    now = now if now is not None else time.time()

    with _lock:
        targets = list(_tab_state["snoozed"])
    if not targets:
        return

    # Computed with _lock released — _is_busy takes state._activity_lock.
    busy_now = {t: _is_busy(t, now) for t in targets}

    with _lock:
        snoozed = dict(_tab_state["snoozed"])
        changed = False
        for target in targets:
            meta = snoozed.get(target)
            if meta is None:
                continue  # woken by a request between the two lock windows
            if target not in live_targets:
                # The pane is gone; there is nothing left to come back to.
                snoozed.pop(target, None)
                changed = True
            elif now - meta["at"] < MIN_SNOOZE_SEC:
                continue
            elif busy_now[target] != meta["was_busy"]:
                snoozed.pop(target, None)
                changed = True
        if changed:
            _tab_state = {**_tab_state, "snoozed": snoozed}
            _save_locked()


def apply_order(panes):
    """Sort panes into display order and stamp `pinned` / `snoozed` on each.

    Panes are grouped by session and the groups are sorted; a session's panes
    stay contiguous and in their original tmux order, so the adjacency the UI
    reads for is_subpane and team-lead grouping survives the sort.

    Pinned sessions lead in every sort mode — a pin is a statement about where
    you want a session, and an automatic sort shouldn't overrule it.

    Mutates the pane dicts in place and returns the reordered list.
    """
    doc = get_tab_state()
    pinned = set(doc["pinned"])
    order = doc["order"]
    snoozed = doc["snoozed"]
    sort = doc.get("sort", "manual")
    rank = {session: i for i, session in enumerate(order)}

    groups = []  # [(session, [panes])] in first-seen tmux order
    index = {}
    for pane in panes:
        session = pane.get("session", "")
        if session not in index:
            index[session] = len(groups)
            groups.append((session, []))
        groups[index[session]][1].append(pane)

    def within(session, group, position):
        """The sort key inside a pin tier."""
        if sort == "name":
            return (session.lower(), session)
        if sort == "created":
            # Oldest session first, so the strip reads as opening order.
            return (group[0].get("created", 0) or 0, session.lower())
        # manual: explicit order first, everything else in tmux order behind it
        return (rank.get(session, len(order)), position)

    def sort_key(item):
        position, (session, group) = item
        return (0 if session in pinned else 1, within(session, group, position))

    ordered = []
    for _, (session, group) in sorted(enumerate(groups), key=sort_key):
        is_pinned = session in pinned
        for pane in group:
            pane["pinned"] = is_pinned
            pane["snoozed"] = pane.get("target") in snoozed
            ordered.append(pane)
    return ordered


def rename_session(old, new):
    """Rewrite pin/order/snooze references after a tmux session rename.

    Server-side so a rename fixes up every connected device, not just the one
    that issued it.
    """
    global _tab_state
    if not old or not new or old == new:
        return
    old_prefix = old + ":"
    new_prefix = new + ":"
    with _lock:
        doc = _tab_state
        _tab_state = _normalize(
            {
                "sort": doc.get("sort", "manual"),
                "pinned": [new if s == old else s for s in doc["pinned"]],
                "order": [new if s == old else s for s in doc["order"]],
                "snoozed": {
                    (
                        new_prefix + t[len(old_prefix) :]
                        if t.startswith(old_prefix)
                        else t
                    ): meta
                    for t, meta in doc["snoozed"].items()
                },
            }
        )
        _save_locked()


def is_empty():
    """True when nothing has been pinned, ordered, snoozed, or sorted yet."""
    with _lock:
        return not (
            _tab_state["pinned"]
            or _tab_state["order"]
            or _tab_state["snoozed"]
            or _tab_state.get("sort", "manual") != "manual"
        )


def import_legacy(pinned_targets, order_targets, snoozed_targets):
    """One-time import of the old per-device localStorage lists.

    Pin and order were keyed by target; map each onto its session, first
    occurrence winning. Refuses to run once the server doc holds anything, so
    the first device to sync wins and a second one cannot clobber it.

    Returns (doc, imported).
    """
    global _tab_state
    if not is_empty():
        return get_tab_state(), False

    def sessions_of(targets):
        return _dedup(
            [t.split(":")[0] for t in targets or [] if isinstance(t, str) and t]
        )

    now = time.time()
    snoozed = {
        t: {"at": now, "was_busy": _is_busy(t, now)}
        for t in snoozed_targets or []
        if isinstance(t, str) and t
    }
    with _lock:
        if (
            _tab_state["pinned"]
            or _tab_state["order"]
            or _tab_state["snoozed"]
            or _tab_state.get("sort", "manual") != "manual"
        ):
            return copy.deepcopy(_tab_state), False
        _tab_state = _normalize(
            {
                "pinned": sessions_of(pinned_targets),
                "order": sessions_of(order_targets),
                "snoozed": snoozed,
            }
        )
        _save_locked()
        return copy.deepcopy(_tab_state), True


load_tab_state()
