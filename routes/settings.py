"""routes/settings.py — Settings API: read, update, and server restart."""

import os
import shlex
import subprocess

from flask import Blueprint, jsonify, request

from shared import state

settings_bp = Blueprint("settings_bp", __name__)

# Keys that may only be changed by editing settings.json on disk —
# never via the HTTP API (they feed shell execution).
_API_BLOCKED_KEYS = {"server.restart_cmd", "server.session_init_cmd"}

# Shell operators that must never appear in the configured restart command
# now that it is executed list-form (no shell to interpret them anyway).
_SHELL_OPERATORS = set(";|&$`<>")


def _filter_patch(patch, defaults, blocked=frozenset(), _prefix=""):
    """Keep only keys that exist in the defaults structure (recursively).

    Returns (filtered_patch, rejected_key_paths). Keys in `blocked`
    (dotted paths) are stripped even when they exist in defaults.
    """
    filtered = {}
    rejected = []
    for key, value in patch.items():
        path = f"{_prefix}{key}"
        if key not in defaults or path in blocked:
            rejected.append(path)
            continue
        default_value = defaults[key]
        if isinstance(default_value, dict) and isinstance(value, dict):
            sub_filtered, sub_rejected = _filter_patch(
                value, default_value, blocked, _prefix=path + "."
            )
            rejected.extend(sub_rejected)
            if sub_filtered:
                filtered[key] = sub_filtered
        elif isinstance(default_value, dict) != isinstance(value, dict):
            # Type-shape mismatch (dict vs scalar) — refuse to clobber.
            rejected.append(path)
        else:
            filtered[key] = value
    return filtered, rejected

# Track server start time for uptime display
_start_time = None


def init_start_time():
    """Called once at startup to record PID and start time."""
    global _start_time
    import time

    _start_time = time.time()


@settings_bp.route("/api/settings")
def get_settings():
    """Return full settings merged with defaults."""
    import time

    pid = os.getpid()
    uptime = int(time.time() - _start_time) if _start_time else 0
    return jsonify(
        {
            "ok": True,
            "settings": state.get_settings(),
            "defaults": state.DEFAULT_SETTINGS,
            "pid": pid,
            "uptime": uptime,
        }
    )


@settings_bp.route("/api/settings", methods=["PATCH"])
def patch_settings():
    """Deep-merge patch into settings, save, re-apply runtime values."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400
    filtered, rejected = _filter_patch(
        data, state.DEFAULT_SETTINGS, blocked=_API_BLOCKED_KEYS
    )
    if not filtered:
        return jsonify({"ok": False, "error": "No valid keys", "rejected": rejected}), 400
    updated = state.patch_settings(filtered)
    return jsonify({"ok": True, "settings": updated, "rejected": rejected})


@settings_bp.route("/api/project-settings/<project>")
def get_project_settings_api(project):
    """Return per-project settings merged with defaults."""
    return jsonify(
        {
            "ok": True,
            "project": project,
            "settings": state.get_project_settings(project),
            "defaults": state.DEFAULT_PROJECT_SETTINGS,
        }
    )


@settings_bp.route("/api/project-settings/<project>", methods=["PATCH"])
def patch_project_settings_api(project):
    """Deep-merge patch into project settings, auto-save."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400
    data, rejected = _filter_patch(data, state.DEFAULT_PROJECT_SETTINGS)
    if not data:
        return jsonify({"ok": False, "error": "No valid keys", "rejected": rejected}), 400
    updated = state.patch_project_settings(project, data)
    # Sync auto-yes runtime state when the patch touches it. Otherwise toggling
    # "Auto-enable: ON" in the per-project panel only persists to disk; the
    # scanner doesn't pick it up until the user next switches tabs or the
    # server restarts.
    autoyes_patch = data.get("autoyes")
    if isinstance(autoyes_patch, dict):
        with state.autoyes_lock:
            if "enabled_default" in autoyes_patch:
                enabled = bool(autoyes_patch["enabled_default"])
                state.autoyes_sessions[project] = enabled
                if not enabled:
                    stale = [
                        t
                        for t in state.autoyes_countdowns
                        if t.startswith(project + ":")
                    ]
                    for t in stale:
                        state.autoyes_countdowns.pop(t, None)
                        state.autoyes_answered.pop(t, None)
                    state.autoyes_delays.pop(project, None)
            if "delay" in autoyes_patch:
                try:
                    state.autoyes_delays[project] = max(
                        1, min(30, int(autoyes_patch["delay"]))
                    )
                except (ValueError, TypeError):
                    pass
    return jsonify(
        {"ok": True, "project": project, "settings": updated, "rejected": rejected}
    )


@settings_bp.route("/api/restart", methods=["POST"])
def restart_server():
    """Execute the configured restart command (list-form, no shell)."""
    cmd = state.get_setting("server", "restart_cmd")
    if not cmd:
        return jsonify({"ok": False, "error": "No restart command configured"}), 400
    if any(ch in cmd for ch in _SHELL_OPERATORS):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "restart_cmd contains shell operators; "
                    "edit settings.json to a plain argv command",
                }
            ),
            500,
        )
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"Unparseable restart_cmd: {e}"}), 500
    if not argv:
        return jsonify({"ok": False, "error": "Empty restart command"}), 500
    try:
        # Spawn detached so the response can be sent before we die
        subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "cmd": cmd})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
