"""routes/input.py — Paste, copy, key, type, upload, history, favorites, sudo pw."""

import base64
import re
import subprocess
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.segments as segments
import shared.state as state
from shared.agent_identity import declare_agent_command
from shared.tmux import (
    TMUX_KEY_MAP,
    get_clipboard,
    send_keys,
    tmux_send_keys,
    tmux_send_text,
    tmux_target_exists,
    _IS_MAC,
)
from shared.utils import (
    add_to_history,
    fix_first_word_case,
    load_json,
    resolve_target,
    save_json,
)

input_bp = Blueprint("input_bp", __name__)


def _load_favorites():
    """Favorites with stable ids guaranteed, persisting the upgrade if it minted any."""
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
    save_json(state.HISTORY_FILE, [])
    return jsonify({"ok": True})


@input_bp.route("/key", methods=["POST"])
def send_key():
    """Send a keyboard shortcut via tmux or xdotool."""
    data = request.get_json(silent=True) or {}
    keys = (data.get("keys") or "").strip()
    if not keys:
        return jsonify({"ok": False, "error": "No keys provided"}), 400

    allowed = set(TMUX_KEY_MAP.keys()) | {"Escape Escape", "ctrl+c ctrl+c"}
    if keys not in allowed:
        return jsonify({"ok": False, "error": "Key combo not allowed"}), 403

    target = resolve_target(data)

    if target and tmux_target_exists(target):
        state.touch_activity(target)
        if keys == "ctrl+shift+v":
            try:
                content = get_clipboard()
                if content:
                    tmux_send_text(target, content)
                    return jsonify({"ok": True, "via": "tmux"})
            except Exception:
                pass
            return jsonify({"ok": False, "error": "clipboard read failed"}), 500

        if keys == "Escape Escape":
            tmux_send_keys(target, "Escape")
            tmux_send_keys(target, "Escape")
            return jsonify({"ok": True, "via": "tmux"})

        if keys == "ctrl+c ctrl+c":
            tmux_send_keys(target, "C-c")
            tmux_send_keys(target, "C-c")
            return jsonify({"ok": True, "via": "tmux"})

        tmux_key = TMUX_KEY_MAP.get(keys)
        if tmux_key:
            if not tmux_send_keys(target, tmux_key):
                return jsonify({"ok": False, "error": "tmux send-keys failed"}), 500
            return jsonify({"ok": True, "via": "tmux"})

    if not send_keys(keys):
        return jsonify({"ok": False, "error": "xdotool failed"}), 500
    return jsonify({"ok": True, "via": "xdotool"})


@input_bp.route("/type", methods=["POST"])
def type_text():
    """Type text into the terminal and optionally press Enter."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    enter = data.get("enter", True)
    no_history = bool(data.get("no_history"))
    if not text and not enter:
        return jsonify({"ok": False, "error": "No text provided"}), 400
    if text and enter:
        text = fix_first_word_case(text)

    # Only the composer opts in. The quick-action command buttons also POST here and
    # must keep sending shell text byte-for-byte, brackets and all.
    send_text = text
    if text and data.get("expand"):
        send_text = segments.expand(text, segments.segment_map(_load_favorites()))

    target = resolve_target(data)

    if target and tmux_target_exists(target):
        if send_text and not tmux_send_text(target, send_text):
            return jsonify({"ok": False, "error": "tmux send-keys failed"}), 500
        if enter:
            time.sleep(0.05)
            tmux_send_keys(target, "Enter")
        # Declarations belong to explicit agent launches Assist typed, never to
        # the generic session launcher whose init command may leave a bare shell.
        if text and enter:
            declare_agent_command(target, text)
        state.touch_activity(target)
        # History stores what was typed, not what was sent — so reloading a prompt
        # built from segments brings back the compact token form.
        if text and not no_history:
            add_to_history(text)
        return jsonify({"ok": True, "via": "tmux", "sent_chars": len(send_text)})

    if _IS_MAC:
        return jsonify({"ok": False, "error": "No active tmux target — open a session first"}), 500

    proc = subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "12", send_text],
        timeout=10,
    )
    if proc.returncode != 0:
        return jsonify({"ok": False, "error": "xdotool type failed"}), 500

    if data.get("enter", True):
        time.sleep(0.05)
        subprocess.run(["xdotool", "key", "Return"], timeout=5)

    if not no_history:
        add_to_history(text)
    return jsonify({"ok": True, "via": "xdotool", "sent_chars": len(send_text)})


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


# ---------------------------------------------------------------------------
# Sudo password — server-side persistence (survives browser localStorage eviction)
# ---------------------------------------------------------------------------
_SUDO_PW_FILE = state.DATA_DIR / "sudo_pw.dat"


def _read_sudo_password():
    """Return the stored sudo password, or None when absent/unreadable."""
    try:
        encoded = _SUDO_PW_FILE.read_text().strip()
        return base64.b64decode(encoded).decode()
    except (OSError, ValueError):
        return None


@input_bp.route("/sudo-password", methods=["GET"])
def sudo_password_get():
    """Report whether a sudo password is stored. Never returns the password."""
    return jsonify({"ok": True, "has_password": _read_sudo_password() is not None})


@input_bp.route("/sudo-send", methods=["POST"])
def sudo_send():
    """Send the stored sudo password + Enter to the target pane, server-side.

    The password never leaves the server and is never added to history.
    """
    pw = _read_sudo_password()
    if not pw:
        return jsonify({"ok": False, "error": "No password stored"}), 404

    data = request.get_json(silent=True) or {}
    target = resolve_target(data)

    if not target or not tmux_target_exists(target):
        return (
            jsonify({"ok": False, "error": "No active tmux target — open a session first"}),
            400,
        )

    if not tmux_send_text(target, pw):
        return jsonify({"ok": False, "error": "tmux send-keys failed"}), 500
    time.sleep(0.05)
    tmux_send_keys(target, "Enter")
    state.touch_activity(target)
    return jsonify({"ok": True, "via": "tmux"})


@input_bp.route("/sudo-password", methods=["POST"])
def sudo_password_set():
    """Store or clear the sudo password."""
    data = request.get_json(silent=True) or {}
    if data.get("clear"):
        try:
            _SUDO_PW_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify({"ok": True, "cleared": True})
    pw = data.get("password", "")
    if not pw:
        return jsonify({"ok": False, "error": "No password provided"}), 400
    try:
        encoded = base64.b64encode(pw.encode()).decode()
        _SUDO_PW_FILE.write_text(encoded + "\n")
        _SUDO_PW_FILE.chmod(0o600)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "stored": True})
