"""shared/drafts.py — Per-target composer drafts (text + attachments + Enter lock).

The composer used to hold exactly one message, globally, and sending was the
only way to clear it. It is per tmux target now: switching tabs swaps text,
attachment tray and Enter lock together, and unsent text simply IS that tab's
draft — there is no mode to toggle and no save command.

Server-side rather than localStorage (spec 2023 §3): Assist is phone-first and
a draft begun on the desktop has to be on the phone. Shaped after
shared/tab_state.py — one lock, one atomically-written JSON file, normalized on
every load so a hand-edited file cannot poison the app.

Keyed by TARGET, not session: a draft belongs to the pane you were typing at.

**An entry is deleted when it carries nothing worth remembering** (spec §7:
"empty drafts are deleted, not stored") — but a disarmed Enter lock IS worth
remembering, so `is_default()` requires an armed lock as well as empty content.
Disarming the lock on an empty composer and then typing must not lose the lock.
The tab marker keys off `has_content()`, never off entry existence, so the
marker and the stored map still cannot disagree.
"""

import copy
import json
import threading
import time
from pathlib import Path

from shared import state

DRAFTS_FILE = state.DATA_DIR / "drafts.json"

# A draft untouched for this long is dropped, along with any uploads only it
# referenced. Without it both the map and /tmp grow without bound, because with
# per-tab drafts the tray is no longer freed by clearing on send.
# 30 days is a guess (spec §7) and is the number here most worth revisiting.
EXPIRY_SEC = 30 * 24 * 60 * 60

# How often the background sweep runs. Expiry is measured in days; an hourly
# pass is far more resolution than that needs and costs one lock acquisition.
SWEEP_INTERVAL_SEC = 60 * 60

# routes/input.py:upload_file() writes every upload as /tmp/assist_<uuid>_<name>.
# The sweep refuses to unlink anything that does not match, so a hand-edited
# drafts.json cannot turn expiry into an arbitrary-delete primitive.
_UPLOAD_DIR = Path("/tmp")
_UPLOAD_PREFIX = "assist_"

_lock = threading.Lock()
_drafts = {}  # target -> {text, attachments, enter_armed, updated_at}


def _default_entry():
    return {"text": "", "attachments": [], "enter_armed": True, "updated_at": 0.0}


def _normalize_attachment(raw):
    """Coerce one tray entry, or None if it carries no usable upload path."""
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        return None
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "size": max(0, size),
        "path": path,
    }


def _normalize_entry(raw):
    """Coerce one stored draft into the canonical shape."""
    if not isinstance(raw, dict):
        raw = {}
    text = raw.get("text")
    attachments = []
    for item in raw.get("attachments") or []:
        entry = _normalize_attachment(item)
        if entry is not None:
            attachments.append(entry)
    try:
        updated_at = float(raw.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    return {
        "text": text if isinstance(text, str) else "",
        "attachments": attachments,
        # Armed by default — today's one-liner reflex is untouched until the
        # lock is deliberately disarmed (spec §4).
        "enter_armed": bool(raw.get("enter_armed", True)),
        "updated_at": updated_at,
    }


def _normalize(data):
    if not isinstance(data, dict):
        return {}
    out = {}
    for target, raw in data.items():
        if not isinstance(target, str) or not target:
            continue
        out[target] = _normalize_entry(raw)
    return out


def has_content(entry):
    """True when this draft is something the user would want a marker for."""
    return bool((entry.get("text") or "").strip() or entry.get("attachments"))


def _is_default(entry):
    """True when the entry holds nothing worth persisting.

    Note the enter_armed clause: an empty composer with a DISARMED lock is a
    deliberate setting, not an empty draft, and must survive.
    """
    return not has_content(entry) and entry.get("enter_armed", True)


def load_drafts():
    """Load from disk. Called once at import."""
    global _drafts
    try:
        data = json.loads(DRAFTS_FILE.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}
    with _lock:
        _drafts = _normalize(data)


def _save_locked():
    """Write to disk. Caller must hold _lock."""
    try:
        state.atomic_write_json(DRAFTS_FILE, _drafts)
    except OSError:
        pass


def get_draft(target):
    """Return one target's draft (deep copy). Absent targets get the default."""
    if not target:
        return _default_entry()
    with _lock:
        entry = _drafts.get(target)
        return copy.deepcopy(entry) if entry is not None else _default_entry()


def set_draft(target, text=None, attachments=None, enter_armed=None):
    """Partially update one draft and persist. Returns the resulting entry.

    Any field left None keeps its stored value, so the client can save text
    without re-sending the tray and vice versa. An update that leaves the entry
    at its default deletes it instead of storing an empty row.
    """
    global _drafts
    if not target:
        return _default_entry()
    patch = {}
    if text is not None:
        patch["text"] = text
    if attachments is not None:
        patch["attachments"] = attachments
    if enter_armed is not None:
        patch["enter_armed"] = enter_armed
    with _lock:
        current = _drafts.get(target) or _default_entry()
        entry = _normalize_entry({**current, **patch})
        entry["updated_at"] = time.time()
        if _is_default(entry):
            _drafts.pop(target, None)
            # Report "nothing stored", not the instant we stored nothing. The
            # client mirrors this into its revision map and compares it against
            # poll_block()'s `rev`, which has no key for a deleted row: a fresh
            # stamp here would leave the writer permanently ahead of the server
            # and make its own next poll look like "another device deleted this",
            # blanking a composer the user had already typed into again.
            entry["updated_at"] = 0.0
        else:
            _drafts[target] = entry
        _save_locked()
        return copy.deepcopy(entry)


def delete_draft(target):
    """Drop one draft entirely (what a successful send does)."""
    global _drafts
    if not target:
        return
    with _lock:
        if _drafts.pop(target, None) is not None:
            _save_locked()


def poll_block():
    """The /poll payload: what every browser needs to render markers and resync.

    Deliberately NOT the draft bodies. A poll runs every 5s per open browser and
    a draft can be thousands of characters; the client fetches a body from
    GET /api/draft only when it switches to that tab or sees `rev` move.

    - marks: targets whose draft has content -> the .has-draft tab markers
    - rev:   every stored target -> updated_at, so a second device notices a
             change to the tab it is sitting on (spec §9.3) and re-fetches
    """
    with _lock:
        return {
            "marks": [t for t, e in _drafts.items() if has_content(e)],
            "rev": {t: e["updated_at"] for t, e in _drafts.items()},
        }


def rename_session(old, new):
    """Re-key drafts after a tmux session rename, so a rename never orphans one.

    Mirrors shared/tab_state.py:rename_session() — server-side so the fixup
    reaches every connected device, not just the one that issued the rename.
    """
    global _drafts
    if not old or not new or old == new:
        return
    old_prefix = old + ":"
    new_prefix = new + ":"
    with _lock:
        renamed = {}
        changed = False
        for target, entry in _drafts.items():
            if target.startswith(old_prefix):
                renamed[new_prefix + target[len(old_prefix):]] = entry
                changed = True
            else:
                renamed[target] = entry
        if changed:
            _drafts = renamed
            _save_locked()


def _is_sweepable_upload(path):
    """Only ever unlink files this app wrote into /tmp (routes/input.py)."""
    try:
        p = Path(path)
    except (TypeError, ValueError):
        return False
    return (
        p.is_absolute()
        and p.parent == _UPLOAD_DIR
        and p.name.startswith(_UPLOAD_PREFIX)
    )


def sweep_expired(max_age_sec=None, now=None):
    """Drop drafts untouched for max_age_sec and unlink their orphaned uploads.

    An upload is only removed once NO surviving draft still references it — two
    drafts can legitimately carry the same path (the same file attached twice),
    and deleting one must not break the other.

    Returns (dropped_targets, removed_paths) for logging and tests.
    """
    global _drafts
    max_age = EXPIRY_SEC if max_age_sec is None else max_age_sec
    now = time.time() if now is None else now

    with _lock:
        expired = [
            t for t, e in _drafts.items() if (now - e.get("updated_at", 0.0)) > max_age
        ]
        if not expired:
            return [], []
        orphan_candidates = set()
        for target in expired:
            for att in _drafts[target]["attachments"]:
                orphan_candidates.add(att["path"])
            _drafts.pop(target, None)
        still_used = {
            att["path"] for e in _drafts.values() for att in e["attachments"]
        }
        _save_locked()

    removed = []
    for path in sorted(orphan_candidates - still_used):
        if not _is_sweepable_upload(path):
            continue
        try:
            Path(path).unlink()
            removed.append(path)
        except OSError:
            pass
    return expired, removed


load_drafts()
