"""routes/input.py — Paste, copy, key, type, upload, history, favorites.

There used to be a stored sudo password here: `/sudo-password` wrote it to
`sudo_pw.dat` beside the code and `/sudo-send` typed it into whichever pane the
request named. It is gone, and should not come back. The auth cookie has a
ten-year lifetime and is never revalidated, so anything that turns "holds a
cookie" into "is root, without ever seeing the password" widens the blast
radius of one borrowed phone further than a single-owner tool can carry.

Nothing was lost by removing it. `pane_awaits_secret()` below already detects a
live password prompt in the pane, and /type sends what you typed byte-for-byte
with no trimming, no expansion and no history entry — the same keystrokes, with
nothing at rest.
"""

import re
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.segments as segments
import shared.state as state
import shared.utils as utils
from shared import execution_park as park
from shared.agent_identity import declare_agent_command
from shared.tmux import (
    TMUX_KEY_MAP,
    ExpectedTargetIdentity,
    expected_target_identity,
    generation_bound_delivery,
    get_clipboard,
    pane_awaits_secret,
)
from shared.utils import (
    add_to_history,
    fix_first_word_case,
    load_json,
    resolve_target,
    save_json,
)

input_bp = Blueprint("input_bp", __name__)
_favorites_lock = threading.RLock()


def _load_favorites():
    """Favorites with stable ids guaranteed, persisting the upgrade if it minted any."""
    with _favorites_lock:
        favs = load_json(state.FAVORITES_FILE, default=[])
        favs, changed = segments.ensure_ids(favs)
        if changed:
            save_json(state.FAVORITES_FILE, favs)
        return favs


@input_bp.route("/history")
def history():
    return jsonify(
        {
            "history": load_json(state.HISTORY_FILE, default=[]),
            "favorites": _load_favorites(),
        }
    )


@input_bp.route("/favorite", methods=["POST"])
def favorite():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No text provided"}), 400

    with _favorites_lock:
        favs = _load_favorites()
        existing = next((f for f in favs if f.get("text") == text), None)
        if existing:
            # A favorite carrying a handle is a named segment other prompts may reference,
            # so a stray star tap must not silently delete it. The caller re-sends with
            # force to confirm.
            if segments.normalize_handle(existing.get("handle")) and not data.get("force"):
                return jsonify({
                    "ok": True,
                    "action": "kept",
                    "reason": "segment",
                    "id": existing.get("id"),
                    "handle": existing.get("handle"),
                })
            favs = [f for f in favs if f.get("text") != text]
            action = "removed"
        else:
            favs.insert(0, {
                "id": "f_" + uuid.uuid4().hex[:8],
                "text": text,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            action = "added"

        save_json(state.FAVORITES_FILE, favs)
        return jsonify({"ok": True, "action": action})


@input_bp.route("/favorite/<fid>", methods=["PATCH"])
def update_favorite(fid):
    """Assign or clear a handle, edit the body, retitle. Promotes a favorite to a segment."""
    data = request.get_json(silent=True) or {}
    with _favorites_lock:
        favs = _load_favorites()
        fav = next((f for f in favs if f.get("id") == fid), None)
        if fav is None:
            return jsonify({"ok": False, "error": "No such favorite"}), 404

        if "handle" in data:
            handle = segments.normalize_handle(data.get("handle"))
            if handle:
                if not segments.valid_handle(handle):
                    return jsonify({
                        "ok": False,
                        "error": "Handle must be 2-32 chars: lowercase letters, digits, . _ -",
                    }), 400
                owner = segments.handle_owner(favs, handle, ignore_id=fid)
                if owner is not None:
                    return jsonify({"ok": False, "error": f"[{handle}] is already taken"}), 409
                fav["handle"] = handle
            else:
                fav.pop("handle", None)

        if "text" in data:
            text = (data.get("text") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "Body cannot be empty"}), 400
            fav["text"] = text

        if "label" in data:
            label = (data.get("label") or "").strip()
            if label:
                fav["label"] = label
            else:
                fav.pop("label", None)

        save_json(state.FAVORITES_FILE, favs)
        return jsonify({"ok": True, "favorite": fav})


@input_bp.route("/favorite/<fid>", methods=["DELETE"])
def delete_favorite(fid):
    with _favorites_lock:
        favs = _load_favorites()
        remaining = [f for f in favs if f.get("id") != fid]
        if len(remaining) == len(favs):
            return jsonify({"ok": False, "error": "No such favorite"}), 404
        save_json(state.FAVORITES_FILE, remaining)
        return jsonify({"ok": True})


@input_bp.route("/segments/expand", methods=["POST"])
def expand_segments():
    """Authoritative preview: exactly what /type would send for this composer text."""
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    seg_map = segments.segment_map(_load_favorites())
    return jsonify({
        "ok": True,
        "expanded": segments.expand(text, seg_map),
        "tokens": segments.find_tokens(text, seg_map),
    })


@input_bp.route("/history", methods=["DELETE"])
def clear_history():
    with utils._history_lock:
        save_json(state.HISTORY_FILE, [])
    return jsonify({"ok": True})


@input_bp.route("/key", methods=["POST"])
def send_key():
    """Send an explicit operator keyboard shortcut via tmux."""
    return park.perform(park.Intent.OPERATOR_INTERACTIVE, _send_key_effect)


def _send_key_effect():
    """Complete one explicit operator key action under the decision lock."""
    data = request.get_json(silent=True) or {}
    keys = (data.get("keys") or "").strip()
    if not keys:
        return jsonify({"ok": False, "error": "No keys provided"}), 400

    allowed = set(TMUX_KEY_MAP.keys()) | {
        "Escape Escape",
        "ctrl+c ctrl+c",
        "ctrl+b ctrl+b",
    }
    if keys not in allowed:
        return jsonify({"ok": False, "error": "Key combo not allowed"}), 403

    target = resolve_target(data)

    expected = expected_target_identity(target) if target else None
    if expected is None:
        return jsonify({"ok": False, "error": "target_absent"}), 409
    if keys == "ctrl+shift+v":
        try:
            content = get_clipboard()
        except Exception:
            content = None
        if not content:
            return jsonify({"ok": False, "error": "clipboard read failed"}), 500
        result = generation_bound_delivery(expected, text=content)
    else:
        first, _, second = keys.partition(" ")
        if second:
            if first != second or not TMUX_KEY_MAP.get(first):
                return jsonify({"ok": False, "error": "Key combo not allowed"}), 403
            tmux_keys = (TMUX_KEY_MAP[first], TMUX_KEY_MAP[first])
        else:
            tmux_key = TMUX_KEY_MAP.get(keys)
            if not tmux_key:
                return jsonify({"ok": False, "error": "Key combo not allowed"}), 403
            tmux_keys = (tmux_key,)
        result = generation_bound_delivery(expected, keys=tmux_keys)
    if not result.ok:
        status = 409 if result.status == "target_absent" else 502
        return jsonify({"ok": False, "error": result.status}), status
    state.touch_activity(target)
    return jsonify({"ok": True, "via": "tmux"})


@input_bp.route("/type", methods=["POST"])
def type_text():
    """Type text into the terminal and optionally press Enter."""
    return park.perform(
        park.Intent.OPERATOR_INTERACTIVE,
        lambda: _type_text_effect(require_carried_identity=False),
    )


@input_bp.route("/type/client-resume", methods=["POST"])
def type_client_resume():
    return park.perform(
        park.Intent.CLIENT_SESSION_RESUME,
        lambda: _type_text_effect(require_carried_identity=True),
    )


@input_bp.route("/type/client-restart", methods=["POST"])
def type_client_restart():
    return park.perform(
        park.Intent.CLIENT_SESSION_RESTART,
        lambda: _type_text_effect(require_carried_identity=True),
    )


def _type_text_effect(require_carried_identity=False):
    """Complete one explicit operator composer/CLI action under the lock."""
    data = request.get_json(silent=True) or {}
    enter = data.get("enter", True)
    target = resolve_target(data)
    if require_carried_identity:
        try:
            expected = ExpectedTargetIdentity.from_value(
                data.get("expected_target_identity")
            )
        except (KeyError, TypeError, ValueError):
            expected = None
    else:
        expected = expected_target_identity(target) if target else None
    if expected is None:
        return jsonify({"ok": False, "error": "target_absent"}), 409

    # A secret is text typed at a password prompt. Every convenience applied below
    # rewrites a password into the wrong string — the surrounding whitespace is
    # stripped, a first word that happens to be a command name is lowercased
    # ("Git" -> "git"), and expansion eats the backslash in `\[`. History would
    # also keep it in the clear. One flag turns all four off.
    #
    # The caller's claim is only ever a hint: the browser decides from the pane
    # copy it last rendered, which is frozen while streaming is paused, and a
    # script POSTing here has no view at all. So when nobody claimed it, ask tmux
    # what the pane is actually showing rather than trusting the omission.
    secret = bool(data.get("secret"))
    raw_text = data.get("text") or ""
    if not secret and raw_text:
        secret = pane_awaits_secret(expected.pane_id)
    text = raw_text if secret else raw_text.strip()
    no_history = bool(data.get("no_history")) or secret
    if not text and not enter:
        return jsonify({"ok": False, "error": "No text provided"}), 400
    if text and enter and not secret and not data.get("raw"):
        text = fix_first_word_case(text)

    # Only the composer opts in. The quick-action command buttons also POST here and
    # must keep sending shell text byte-for-byte, brackets and all.
    send_text = text
    if text and data.get("expand") and not secret:
        send_text = segments.expand(text, segments.segment_map(_load_favorites()))

    result = generation_bound_delivery(expected, text=send_text, enter=enter)
    if result.ok:
        # Declarations belong to explicit agent launches Assist typed, never to
        # the generic session launcher whose init command may leave a bare shell,
        # and never to a password answering a prompt.
        if text and enter and not secret:
            declare_agent_command(expected.pane_id, text)
        state.touch_activity(target)
        # History stores what was typed, not what was sent — so reloading a prompt
        # built from segments brings back the compact token form.
        if text and not no_history:
            add_to_history(text)
        return jsonify({"ok": True, "via": "tmux", "sent_chars": len(send_text)})
    status = 409 if result.status == "target_absent" else 502
    return jsonify({"ok": False, "error": result.status}), status


_UPLOAD_CHUNK = 1024 * 1024  # stream to disk 1MB at a time — never buffer whole file


@input_bp.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "No filename"}), 400
    limit = state.MAX_UPLOAD_SIZE
    too_large = f"File too large ({limit // (1024 * 1024)}MB max)"
    content_length = request.content_length
    if content_length and content_length > limit:
        return jsonify({"ok": False, "error": too_large}), 413
    raw_name = f.filename
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name)
    sanitized = re.sub(r"_+", "_", sanitized).lstrip(".")
    if not sanitized:
        sanitized = "file"
    short_uuid = uuid.uuid4().hex[:8]
    dest = Path(f"/tmp/assist_{short_uuid}_{sanitized}")

    written = 0
    oversized = False
    try:
        with dest.open("wb") as out:
            while True:
                chunk = f.stream.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    oversized = True
                    break
                out.write(chunk)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": f"Write failed: {exc.strerror}"}), 500
    if oversized:
        dest.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": too_large}), 413
    return jsonify({"ok": True, "path": str(dest), "name": raw_name, "size": written})
