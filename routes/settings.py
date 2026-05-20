"""routes/settings.py — Settings API: read, update, and server restart."""

import os
import subprocess

from flask import Blueprint, jsonify, request

from shared import state

settings_bp = Blueprint("settings_bp", __name__)

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
    updated = state.patch_settings(data)
    return jsonify({"ok": True, "settings": updated})


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
    return jsonify({"ok": True, "project": project, "settings": updated})


@settings_bp.route("/api/restart", methods=["POST"])
def restart_server():
    """Execute the configured restart command."""
    cmd = state.get_setting("server", "restart_cmd")
    if not cmd:
        return jsonify({"ok": False, "error": "No restart command configured"}), 400
    try:
        # Spawn detached so the response can be sent before we die
        subprocess.Popen(
            cmd,
            shell=True,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "cmd": cmd})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
