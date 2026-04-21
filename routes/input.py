"""routes/input.py — Paste, copy, key, type, upload, history, favorites, sudo pw."""

import base64
import re
import subprocess
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from shared.tmux import (
    TMUX_KEY_MAP,
    set_clipboard,
    paste_to_terminal,
    send_keys,
    tmux_send_keys,
    tmux_send_text,
    tmux_target_exists,
)
from shared.utils import (
    add_to_history,
    fix_first_word_case,
    load_json,
    resolve_target,
    save_json,
)

input_bp = Blueprint("input_bp", __name__)


@input_bp.route("/paste", methods=["POST"])
def paste():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No text provided"}), 400
    send_enter = data.get("enter", True)
    text = text.replace("\n", " ").replace("\r", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No text provided"}), 400
    text = fix_first_word_case(text)

    target = resolve_target(data)

    if target and tmux_target_exists(target):
        if not tmux_send_text(target, text):
            return jsonify({"ok": False, "error": "tmux send-keys failed"}), 500
        if send_enter:
            time.sleep(0.05)
            tmux_send_keys(target, "Enter")
        state.touch_activity(target)
        add_to_history(text)
        return jsonify({"ok": True, "via": "tmux"})

    if not set_clipboard(text):
        return jsonify({"ok": False, "error": "xclip failed"}), 500
    if not paste_to_terminal():
        return jsonify({"ok": False, "error": "xdotool failed"}), 500
    if send_enter:
        time.sleep(0.05)
        subprocess.run(["xdotool", "key", "Return"], timeout=5)
    add_to_history(text)
    return jsonify({"ok": True, "via": "xdotool"})


@input_bp.route("/copy", methods=["POST"])
def copy():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No text provided"}), 400
    if not set_clipboard(text):
        return jsonify({"ok": False, "error": "xclip failed"}), 500
    add_to_history(text)
    return jsonify({"ok": True})


@input_bp.route("/history")
def history():
    return jsonify(
        {
            "history": load_json(state.HISTORY_FILE, default=[]),
            "favorites": load_json(state.FAVORITES_FILE, default=[]),
        }
    )


@input_bp.route("/favorite", methods=["POST"])
def favorite():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No text provided"}), 400

    favs = load_json(state.FAVORITES_FILE, default=[])
    existing = [f for f in favs if f["text"] == text]
    if existing:
        favs = [f for f in favs if f["text"] != text]
        action = "removed"
    else:
        favs.insert(0, {"text": text, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
        action = "added"

    save_json(state.FAVORITES_FILE, favs)
    return jsonify({"ok": True, "action": action})


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
                clip = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if clip.returncode == 0 and clip.stdout:
                    tmux_send_text(target, clip.stdout)
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
    if not text and not enter:
        return jsonify({"ok": False, "error": "No text provided"}), 400
    if text and enter:
        text = fix_first_word_case(text)

    target = resolve_target(data)

    if target and tmux_target_exists(target):
        if text and not tmux_send_text(target, text):
            return jsonify({"ok": False, "error": "tmux send-keys failed"}), 500
        if enter:
            time.sleep(0.05)
            tmux_send_keys(target, "Enter")
        state.touch_activity(target)
        if text:
            add_to_history(text)
        return jsonify({"ok": True, "via": "tmux"})

    proc = subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "12", text],
        timeout=10,
    )
    if proc.returncode != 0:
        return jsonify({"ok": False, "error": "xdotool type failed"}), 500

    if data.get("enter", True):
        time.sleep(0.05)
        subprocess.run(["xdotool", "key", "Return"], timeout=5)

    add_to_history(text)
    return jsonify({"ok": True, "via": "xdotool"})


@input_bp.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "No filename"}), 400
    content_length = request.content_length
    if content_length and content_length > state.MAX_UPLOAD_SIZE:
        return jsonify({"ok": False, "error": "File too large (50MB max)"}), 413
    raw_name = f.filename
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name)
    sanitized = re.sub(r"_+", "_", sanitized).lstrip(".")
    if not sanitized:
        sanitized = "file"
    short_uuid = uuid.uuid4().hex[:8]
    dest = Path(f"/tmp/assist_{short_uuid}_{sanitized}")
    data = f.read(state.MAX_UPLOAD_SIZE + 1)
    if len(data) > state.MAX_UPLOAD_SIZE:
        return jsonify({"ok": False, "error": "File too large (50MB max)"}), 413
    dest.write_bytes(data)
    return jsonify({"ok": True, "path": str(dest), "name": raw_name, "size": len(data)})


# ---------------------------------------------------------------------------
# Sudo password — server-side persistence (survives browser localStorage eviction)
# ---------------------------------------------------------------------------
_SUDO_PW_FILE = state.DATA_DIR / "sudo_pw.dat"


@input_bp.route("/sudo-password", methods=["GET"])
def sudo_password_get():
    """Return the stored sudo password (base64-encoded on disk)."""
    try:
        encoded = _SUDO_PW_FILE.read_text().strip()
        pw = base64.b64decode(encoded).decode()
        return jsonify({"ok": True, "has_password": True, "password": pw})
    except (OSError, ValueError):
        return jsonify({"ok": True, "has_password": False})


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
