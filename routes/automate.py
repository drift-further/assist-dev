"""routes/automate.py — Automate Run: launch/monitor/relaunch claude-mount in tmux."""

import copy
import hashlib
import shlex
import subprocess
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from shared import execution_park as park
from shared.agent_identity import declare_agent_command
from shared.tmux import (
    create_tmux_session,
    record_tmux_adoption,
    tmux_send_keys,
    tmux_send_text,
)
from shared.utils import load_json, save_json

automate_bp = Blueprint("automate_bp", __name__)


def _http_refusal(refusal):
    return jsonify(refusal.body()), refusal.http_status


def _record_automate_refusal(refusal):
    """Publish only the canonical terminal background refusal."""
    with state.automate_lock:
        state.automate["active"] = False
        state.automate["status"] = refusal.error
        state.automate["done_signal_at"] = None
        state.automate["trust_answered"] = None
        _automate_save()
    return refusal


def _perform_automate_background(intent, effect):
    result = park.perform(intent, effect)
    if park.is_refusal(result):
        return _record_automate_refusal(result)
    return result


def _automate_scheduled_answer(intent, target, text=None, *, enter=True):
    """Deliver one answer whose provenance is fixed by the Automate caller."""
    if intent not in (
        park.Intent.AUTOMATE_TRUST_ANSWER,
        park.Intent.AUTOMATE_AUTO_ANSWER,
    ):
        raise ValueError("not an Automate answer intent")

    def effect():
        if text:
            tmux_send_text(target, text)
        if enter:
            tmux_send_keys(target, "Enter")
        return True

    return _perform_automate_background(intent, effect)


def _build_automate_prompt(prompt, project_path):
    """Prepend @immediatenotice.md reference if the file exists in the project."""
    notice_path = Path(project_path) / state.IMMEDIATE_NOTICE_FILENAME
    if notice_path.is_file():
        return f"@{state.IMMEDIATE_NOTICE_FILENAME} {prompt}"
    return prompt


def _automate_save():
    """Persist automate state to disk (call while holding automate_lock)."""
    try:
        st = {
            k: v
            for k, v in state.automate.items()
            if k not in ("last_output_hash", "run_id", "trust_answered")
        }
        save_json(state.AUTOMATE_STATE_FILE, st)
    except Exception:
        pass


def _next_run_id():
    """Bump the monitor generation token (call while holding automate_lock).

    Every start/reconnect/recover/stop increments it; a monitor thread exits
    as soon as the stored token no longer matches the one it was spawned
    with. This replaces the old stop_event set/sleep/clear dance, which
    raced with monitors mid-iteration (soft relaunch sleeps 30s) and left
    duplicate monitors running.
    """
    run_id = state.automate.get("run_id", 0) + 1
    state.automate["run_id"] = run_id
    return run_id


def _run_id_current(run_id):
    """True if this monitor generation is still the live one."""
    with state.automate_lock:
        return state.automate.get("run_id") == run_id


def automate_recover():
    """On startup, check for orphaned -auto sessions and resume tracking."""
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        auto_sessions = [
            s for s in proc.stdout.strip().split("\n") if s.endswith("-auto")
        ]
    except Exception:
        auto_sessions = []

    if not auto_sessions:
        if state.AUTOMATE_STATE_FILE.exists():
            state.AUTOMATE_STATE_FILE.unlink(missing_ok=True)
        return

    saved = load_json(state.AUTOMATE_STATE_FILE)

    session_name = auto_sessions[0]
    base_session = session_name.removesuffix("-auto")
    project_path = state.PROJECTS_DIR / base_session
    adoption = record_tmux_adoption(
        f"{session_name}:0.0",
        surface="automate_startup_recovery",
        diagnostic_alias=f"{session_name}:0.0",
    )
    if not adoption.ok:
        return

    session_id = hashlib.md5((str(project_path) + "\n").encode()).hexdigest()[:8]
    container_name = f"claude-session-{session_id}"

    prompt = saved.get("prompt", "recovered-session") if saved else "recovered-session"
    timeout_min = saved.get("timeout_minutes", 30) if saved else 30
    continuous = saved.get("continuous", True) if saved else True
    max_iterations = saved.get("max_iterations", 0) if saved else 0
    iterations_completed = saved.get("iterations_completed", 0) if saved else 0
    stop_after = saved.get("stop_after", "") if saved else ""
    claude_cmd = saved.get("claude_cmd", "") if saved else ""

    now = time.time()
    with state.automate_lock:
        run_id = _next_run_id()
        state.automate.update(
            {
                "active": True,
                "project": base_session,
                "project_path": str(project_path),
                "session": session_name,
                "container": container_name,
                "prompt": prompt,
                "timeout_minutes": timeout_min,
                "started_at": now,
                "last_output_at": now,
                "last_output_hash": None,
                "done_signal_at": None,
                "trust_answered": None,
                "status": "running",
                "continuous": continuous,
                "max_iterations": max_iterations,
                "iterations_completed": iterations_completed,
                "stop_after": stop_after,
                "claude_cmd": claude_cmd,
            }
        )

    threading.Thread(target=_automate_monitor, args=(run_id,), daemon=True).start()


@automate_bp.route("/api/automate/start", methods=["POST"])
def automate_start():
    """Launch claude-mount in a tmux session for the current project."""
    result = park.perform(park.Intent.AUTOMATE_START, _automate_start_request)
    if park.is_refusal(result):
        return _http_refusal(result)
    return result


def _automate_start_request():
    """Complete start unit; called only while the park decision lock is held."""
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return jsonify({"ok": False, "error": "No prompt provided"}), 400

    with state.automate_lock:
        if state.automate["active"]:
            return jsonify({"ok": False, "error": "Automation already running"}), 409

    # The park decision lock serializes this effect.  Do not publish Automate
    # state until a newly created pane has a durable origin receipt.
    try:
        return _automate_start_inner(data, prompt)
    except Exception as e:
        with state.automate_lock:
            state.automate["active"] = False
            state.automate["status"] = "start_failed"
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"start failed: {e}"}), 500


def _automate_start_inner(data, prompt):
    """Body of automate_start; runs with the active=True claim held."""
    if not state.CLAUDE_MOUNT_SCRIPT or not state.CLAUDE_MOUNT_SCRIPT.exists():
        with state.automate_lock:
            state.automate["active"] = False
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "ASSIST_MOUNT_SCRIPT not configured — set it in .env",
                }
            ),
            400,
        )

    if not state.tmux_target:
        with state.automate_lock:
            state.automate["active"] = False
        return jsonify({"ok": False, "error": "No active project session"}), 400

    base_session = state.tmux_target.split(":")[0]

    # Use per-project settings as defaults for form values
    proj = state.get_project_settings(base_session)
    timeout_min = int(data.get("timeout", proj["automate"]["timeout"]))
    timeout_min = max(1, min(120, timeout_min))
    continuous = data.get("continuous")
    if continuous is None:
        continuous = proj["automate"]["continuous"]
    continuous = bool(continuous)
    max_iterations = int(data.get("iterations", proj["automate"]["max_iterations"]))
    max_iterations = max(0, min(999, max_iterations))
    stop_after = data.get("stop_after", proj["automate"]["stop_after"]) or ""
    claude_cmd = (data.get("claude_cmd") or "").strip()
    project_path = state.PROJECTS_DIR / base_session
    if not project_path.is_dir():
        with state.automate_lock:
            state.automate["active"] = False
        return (
            jsonify({"ok": False, "error": f"Project dir not found: {base_session}"}),
            404,
        )

    session_name = f"{base_session}-auto"

    session_id = hashlib.md5((str(project_path) + "\n").encode()).hexdigest()[:8]
    container_name = f"claude-session-{session_id}"

    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True,
        timeout=5,
    )
    subprocess.run(
        ["docker", "rm", "-f", container_name], capture_output=True, timeout=10
    )

    created = create_tmux_session(
        session_name=session_name,
        cwd=project_path,
        cols=200,
        rows=50,
        surface="automate_start",
        diagnostic_alias=f"{session_name}:0.0",
    )
    if not created.ok:
        with state.automate_lock:
            state.automate["active"] = False
        return jsonify({"ok": False, "error": created.status}), 500

    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "history-limit", "20000"],
        capture_output=True,
        timeout=5,
    )

    full_prompt = _build_automate_prompt(prompt, project_path)
    full_prompt = full_prompt.replace("\n", " ").replace("\r", " ")
    escaped_prompt = full_prompt.replace("'", "'\\''")

    # Write per-project packages to temp file for container entrypoint
    project_packages_file = ""
    proj_pkgs = proj.get("packages", {}).get("pip", [])
    if proj_pkgs:
        pkg_file = Path(tempfile.gettempdir()) / f"assist-pkgs-{base_session}.txt"
        pkg_file.write_text("\n".join(proj_pkgs) + "\n")
        project_packages_file = str(pkg_file)

    env_prefix = ""
    if project_packages_file:
        env_prefix += f"PROJECT_PACKAGES_FILE={project_packages_file} "
    if claude_cmd:
        env_prefix += f"CLAUDE_CMD={shlex.quote(claude_cmd)} "
    cmd = f"{env_prefix}bash {state.CLAUDE_MOUNT_SCRIPT} -n '{escaped_prompt}'"

    target = f"{session_name}:0.0"
    tmux_send_text(target, cmd)
    tmux_send_keys(target, "Enter")
    # The mount script launches Claude behind Docker, outside the pane's local
    # descendant tree. Declare the server-owned launch explicitly.
    declare_agent_command(target, claude_cmd or "claude")

    now = time.time()
    with state.automate_lock:
        run_id = _next_run_id()
        state.automate.update(
            {
                "active": True,
                "project": base_session,
                "project_path": str(project_path),
                "session": session_name,
                "container": container_name,
                "prompt": prompt,
                "timeout_minutes": timeout_min,
                "started_at": now,
                "last_output_at": now,
                "last_output_hash": None,
                "done_signal_at": None,
                "trust_answered": None,
                "status": "running",
                "continuous": continuous,
                "max_iterations": max_iterations,
                "iterations_completed": 0,
                "stop_after": stop_after,
                "claude_cmd": claude_cmd,
            }
        )
        _automate_save()

    threading.Thread(target=_automate_monitor, args=(run_id,), daemon=True).start()

    return jsonify(
        {
            "ok": True,
            "session": session_name,
            "container": container_name,
            "target": f"{session_name}:0.0",
        }
    )


@automate_bp.route("/api/automate/status")
def automate_status():
    """Return current automate state."""
    with state.automate_lock:
        if not state.automate["active"] and not state.automate["status"]:
            return jsonify({"active": False, **park.activation_status()})

        now = time.time()
        elapsed = (
            now - state.automate["started_at"] if state.automate["started_at"] else 0
        )
        idle = (
            now - state.automate["last_output_at"]
            if state.automate["last_output_at"]
            else 0
        )

        return jsonify(
            {
                **park.activation_status(),
                "active": state.automate["active"],
                "status": state.automate["status"],
                "project": state.automate["project"],
                "session": state.automate["session"],
                "container": state.automate["container"],
                "prompt": state.automate["prompt"],
                "timeout_minutes": state.automate["timeout_minutes"],
                "elapsed_seconds": round(elapsed, 1),
                "idle_seconds": round(idle, 1),
                "continuous": state.automate["continuous"],
                "max_iterations": state.automate["max_iterations"],
                "iterations_completed": state.automate["iterations_completed"],
                "stop_after": state.automate.get("stop_after", ""),
            }
        )


@automate_bp.route("/api/automate/reconnect", methods=["POST"])
def automate_reconnect():
    """Reconnect to an orphaned -auto tmux session."""
    with state.automate_lock:
        if state.automate["active"]:
            return jsonify({"ok": False, "error": "Automation already active"}), 409

    try:
        proc = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        auto_sessions = [
            s for s in proc.stdout.strip().split("\n") if s.endswith("-auto")
        ]
    except Exception:
        auto_sessions = []

    if not auto_sessions:
        return jsonify({"ok": False, "error": "No -auto sessions found"}), 404

    session_name = auto_sessions[0]
    base_session = session_name.removesuffix("-auto")
    project_path = state.PROJECTS_DIR / base_session
    adoption = record_tmux_adoption(
        f"{session_name}:0.0",
        surface="automate_reconnect",
        diagnostic_alias=f"{session_name}:0.0",
    )
    if not adoption.ok:
        return jsonify({"ok": False, "error": adoption.status}), 409

    session_id = hashlib.md5((str(project_path) + "\n").encode()).hexdigest()[:8]
    container_name = f"claude-session-{session_id}"

    container_running = False
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        container_running = proc.stdout.strip().lower() == "true"
    except Exception:
        pass

    data = request.get_json(silent=True) or {}
    timeout_min = int(data.get("timeout", 60))

    saved = (
        load_json(state.AUTOMATE_STATE_FILE)
        if state.AUTOMATE_STATE_FILE.exists()
        else {}
    )

    now = time.time()
    with state.automate_lock:
        run_id = _next_run_id()
        state.automate.update(
            {
                "active": True,
                "project": base_session,
                "project_path": str(project_path),
                "session": session_name,
                "container": container_name,
                "prompt": saved.get("prompt", "(reconnected)"),
                "timeout_minutes": timeout_min,
                "started_at": now,
                "last_output_at": now,
                "last_output_hash": None,
                "done_signal_at": None,
                "trust_answered": None,
                "status": "running",
                "continuous": saved.get("continuous", True),
                "max_iterations": saved.get("max_iterations", 0),
                "iterations_completed": saved.get("iterations_completed", 0),
                "stop_after": saved.get("stop_after", ""),
                "claude_cmd": saved.get("claude_cmd", ""),
            }
        )
        _automate_save()

    threading.Thread(target=_automate_monitor, args=(run_id,), daemon=True).start()

    return jsonify(
        {
            "ok": True,
            "session": session_name,
            "container": container_name,
            "container_running": container_running,
            "target": f"{session_name}:0.0",
        }
    )


@automate_bp.route("/api/automate/patch", methods=["POST"])
def automate_patch():
    """Update settings on a live automation."""
    data = request.get_json(silent=True) or {}
    with state.automate_lock:
        if not state.automate["active"]:
            return jsonify({"ok": False, "error": "No automation running"}), 404
        if "timeout" in data:
            state.automate["timeout_minutes"] = max(1, min(120, int(data["timeout"])))
        if "continuous" in data:
            state.automate["continuous"] = bool(data["continuous"])
        if "iterations" in data:
            state.automate["max_iterations"] = max(0, min(999, int(data["iterations"])))
        if "stop_after" in data:
            state.automate["stop_after"] = data["stop_after"] or ""
        _automate_save()
        return jsonify(
            {
                "ok": True,
                "timeout_minutes": state.automate["timeout_minutes"],
                "continuous": state.automate["continuous"],
                "max_iterations": state.automate["max_iterations"],
                "stop_after": state.automate.get("stop_after", ""),
            }
        )


@automate_bp.route("/api/automate/stop", methods=["POST"])
def automate_stop():
    """Stop the running automation."""
    return park.perform(park.Intent.STOP, _automate_stop_effect)


def _automate_stop_effect():
    """Stop the exact selected Automate unit under the decision lock."""
    with state.automate_lock:
        if not state.automate["active"]:
            return jsonify({"ok": False, "error": "No automation running"}), 404
        container = state.automate["container"]
        session = state.automate["session"]
        _next_run_id()  # invalidate any running monitor generation
        state.automate["active"] = False
        state.automate["status"] = "stopped"
        state.automate["done_signal_at"] = None
        state.automate["trust_answered"] = None
        _automate_save()

    _automate_cleanup(container, session)

    return jsonify({"ok": True, "status": "stopped"})


def _automate_cleanup(container, session):
    """Kill container and tmux session. Best-effort — never raises."""
    if container:
        try:
            subprocess.run(
                ["docker", "kill", container], capture_output=True, timeout=10
            )
        except Exception as e:
            print(f"[automate] docker kill {container} failed: {e}")
        try:
            subprocess.run(
                ["docker", "rm", "-f", container], capture_output=True, timeout=10
            )
        except Exception as e:
            print(f"[automate] docker rm {container} failed: {e}")
    if session:
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", session], capture_output=True, timeout=5
            )
        except Exception as e:
            print(f"[automate] tmux kill-session {session} failed: {e}")


def _automate_soft_clear():
    """Attempt Automate's `/clear` delivery under its fixed provenance."""
    with state.automate_lock:
        session = state.automate["session"]
        prompt = state.automate["prompt"]
        project_path = state.automate["project_path"]
        project_name = state.automate["project"]

    target = f"{session}:0.0"

    def effect():
        print(f"[automate] Soft relaunch: sending /clear to {target}")
        tmux_send_text(target, "/clear")
        tmux_send_keys(target, "Enter")
        return target, prompt, project_path, project_name

    return _perform_automate_background(park.Intent.AUTOMATE_SOFT_CLEAR, effect)


def _automate_soft_resend(run_id, target, prompt, project_path):
    """Attempt the saved-prompt resend under Automate provenance."""

    def effect():
        if not _run_id_current(run_id):
            print(f"[automate] Soft relaunch aborted — monitor superseded ({target})")
            return False

        full_prompt = _build_automate_prompt(prompt, project_path)
        full_prompt = full_prompt.replace("\n", " ").replace("\r", " ")
        tmux_send_text(target, full_prompt)
        tmux_send_keys(target, "Enter")

        now = time.time()
        with state.automate_lock:
            state.automate["last_output_at"] = now
            state.automate["last_output_hash"] = None
            state.automate["done_signal_at"] = None
            state.automate["trust_answered"] = None
            state.automate["status"] = "running"
            _automate_save()

        print(f"[automate] Soft relaunch complete — prompt re-sent to {target}")
        return True

    return _perform_automate_background(park.Intent.AUTOMATE_SOFT_RESEND, effect)


def _automate_soft_relaunch(run_id):
    """Attempt `/clear`, then the separately classified saved-prompt resend."""
    clear_result = _automate_soft_clear()
    if park.is_refusal(clear_result):
        return clear_result

    target, prompt, project_path, project_name = clear_result
    wait = (
        state.get_project_setting(project_name, "triggers", "relaunch_wait_sec")
        if project_name
        else 30
    )
    time.sleep(wait)
    return _automate_soft_resend(run_id, target, prompt, project_path)


def _automate_relaunch(run_id):
    """Clean up old container/session and launch a fresh one with the same prompt."""
    return _perform_automate_background(
        park.Intent.AUTOMATE_HARD_RELAUNCH,
        lambda: _automate_relaunch_effect(run_id),
    )


def _automate_relaunch_effect(run_id):
    """Complete hard-relaunch unit, called only inside ``park.perform``."""
    with state.automate_lock:
        container = state.automate["container"]
        session = state.automate["session"]
        prompt = state.automate["prompt"]
        project = state.automate["project"]
        project_path = state.automate["project_path"]
        claude_cmd = state.automate.get("claude_cmd", "")

    if not state.CLAUDE_MOUNT_SCRIPT or not state.CLAUDE_MOUNT_SCRIPT.exists():
        with state.automate_lock:
            state.automate["active"] = False
            state.automate["status"] = "relaunch_failed"
        print("[automate] Relaunch failed — ASSIST_MOUNT_SCRIPT not configured")
        return False

    _automate_cleanup(container, session)
    time.sleep(2)

    if not _run_id_current(run_id):
        print("[automate] Hard relaunch aborted — monitor superseded")
        return False

    session_name = f"{project}-auto"
    session_id = hashlib.md5((str(project_path) + "\n").encode()).hexdigest()[:8]
    container_name = f"claude-session-{session_id}"

    subprocess.run(
        ["docker", "rm", "-f", container_name], capture_output=True, timeout=10
    )

    created = create_tmux_session(
        session_name=session_name,
        cwd=project_path,
        cols=200,
        rows=50,
        surface="automate_hard_relaunch",
        diagnostic_alias=f"{session_name}:0.0",
    )
    if not created.ok:
        with state.automate_lock:
            state.automate["active"] = False
            state.automate["status"] = "relaunch_failed"
        return False

    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "history-limit", "20000"],
        capture_output=True,
        timeout=5,
    )

    full_prompt = _build_automate_prompt(prompt, project_path)
    full_prompt = full_prompt.replace("\n", " ").replace("\r", " ")
    escaped_prompt = full_prompt.replace("'", "'\\''")

    # Write per-project packages to temp file for container entrypoint
    proj = state.get_project_settings(project)
    project_packages_file = ""
    proj_pkgs = proj.get("packages", {}).get("pip", [])
    if proj_pkgs:
        pkg_file = Path(tempfile.gettempdir()) / f"assist-pkgs-{project}.txt"
        pkg_file.write_text("\n".join(proj_pkgs) + "\n")
        project_packages_file = str(pkg_file)

    env_prefix = ""
    if project_packages_file:
        env_prefix += f"PROJECT_PACKAGES_FILE={project_packages_file} "
    if claude_cmd:
        env_prefix += f"CLAUDE_CMD={shlex.quote(claude_cmd)} "
    cmd = f"{env_prefix}bash {state.CLAUDE_MOUNT_SCRIPT} -n '{escaped_prompt}'"
    target = f"{session_name}:0.0"
    tmux_send_text(target, cmd)
    tmux_send_keys(target, "Enter")
    declare_agent_command(target, claude_cmd or "claude")

    now = time.time()
    with state.automate_lock:
        state.automate.update(
            {
                "active": True,
                "session": session_name,
                "container": container_name,
                "started_at": now,
                "last_output_at": now,
                "last_output_hash": None,
                "done_signal_at": None,
                "trust_answered": None,
                "status": "running",
            }
        )
        _automate_save()
    return True


def _past_stop_time(stop_after, started_at=None):
    """Check if current local time is past the next HH:MM cutoff after started_at.
    Supports overnight runs: start at 14:00 with stop_after=05:00 → cutoff is
    tomorrow at 05:00, not today."""
    if not stop_after:
        return False
    try:
        hour, minute = map(int, stop_after.split(":"))
        now = datetime.now()
        anchor = datetime.fromtimestamp(started_at) if started_at else now
        cutoff = anchor.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cutoff <= anchor:
            cutoff += timedelta(days=1)
        return now >= cutoff
    except (ValueError, TypeError):
        return False


def _automate_monitor(run_id):
    """Background thread: watches container state + output staleness. Auto-relaunches.

    `run_id` is the generation token captured at spawn; the thread exits as
    soon as state.automate["run_id"] no longer matches (a newer start/
    reconnect/recover/stop superseded this monitor).
    """
    while True:
        time.sleep(5)

        if not _run_id_current(run_id):
            return

        try:
            result = _automate_monitor_iteration(run_id)
            if result == "stop":
                return
        except SystemExit:
            raise
        except Exception as e:
            print(f"[automate] monitor iteration error: {e}")
            traceback.print_exc()


def _done_signal_present(output, done_signals):
    """Line-anchored done-signal detection.

    A plain substring match also hits the launch command line and Claude's
    transcript echo of the prompt (both contain the signal text), causing
    soft relaunches mid-work. Evaluate line by line: skip the launch wrapper
    line and transcript-echo lines (> / ❯ prefixed), and require the signal
    at/near the start of the stripped line — a genuinely printed
    DONE_SIGNAL line still matches, a signal buried mid-sentence does not.
    """
    if not done_signals:
        return False
    wrapper = (
        state.CLAUDE_MOUNT_SCRIPT.name
        if state.CLAUDE_MOUNT_SCRIPT
        else "claude-mount.sh"
    )
    for raw in output.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if wrapper in line or "claude-mount.sh" in line:
            continue
        if line.startswith(">") or line.startswith("❯"):
            continue
        for sig in done_signals:
            idx = line.find(sig)
            if 0 <= idx <= 8:
                return True
    return False


def _automate_monitor_iteration(run_id):
    """Single pass of the monitor loop. Wrapped by _automate_monitor for fault tolerance."""
    with state.automate_lock:
        if not state.automate["active"]:
            return "stop"
        session = state.automate["session"]
        container = state.automate["container"]
        timeout_sec = state.automate["timeout_minutes"] * 60
        last_hash = state.automate["last_output_hash"]
        project_name = state.automate["project"]

    proj = (
        state.get_project_settings(project_name)
        if project_name
        else copy.deepcopy(state.DEFAULT_PROJECT_SETTINGS)
    )

    proc = None
    try:
        proc = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-p",
                "-t",
                f"{session}:0.0",
                "-S",
                "-50",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            content_hash = hashlib.md5(proc.stdout.encode()).hexdigest()
            if content_hash != last_hash:
                with state.automate_lock:
                    state.automate["last_output_at"] = time.time()
                    state.automate["last_output_hash"] = content_hash
    except Exception:
        pass

    try:
        if (
            proc is not None
            and proc.returncode == 0
            and proj["triggers"]["trust_auto_approve"]
        ):
            out = proc.stdout
            trust_send = None  # (text_or_None, with_enter)
            if "Yes, I trust this folder" in out:
                trust_send = ("1", True)
            elif "trust the files" in out or "Yes, proceed" in out:
                trust_send = (None, True)
            if trust_send:
                # Fired-once guard: wrapper panes keep the trust-prompt text
                # in stale cells, so a stateless handler re-sends 1+Enter
                # every cycle. Key on a hash of the matched lines (mirrors
                # autoyes_answered); cleared on relaunch/start/stop.
                trust_lines = [
                    l
                    for l in out.split("\n")
                    if "trust" in l.lower() or "Yes, proceed" in l
                ]
                trust_hash = hashlib.md5(
                    "\n".join(trust_lines).encode()
                ).hexdigest()
                with state.automate_lock:
                    already_fired = state.automate.get("trust_answered") == trust_hash
                if not already_fired:
                    answer_result = _automate_scheduled_answer(
                        park.Intent.AUTOMATE_TRUST_ANSWER,
                        f"{session}:0.0",
                        trust_send[0],
                    )
                    if park.is_refusal(answer_result):
                        return "stop"
                    with state.automate_lock:
                        state.automate["trust_answered"] = trust_hash
    except Exception:
        pass

    try:
        if proc is not None and proc.returncode == 0:
            done_signals = proj["triggers"]["done_signals"]
            signal_present = _done_signal_present(proc.stdout, done_signals)
            with state.automate_lock:
                if signal_present:
                    if state.automate["done_signal_at"] is None:
                        state.automate["done_signal_at"] = time.time()
                        print(f"[automate] Done signal detected in {session}")
                else:
                    state.automate["done_signal_at"] = None
    except Exception:
        pass

    container_running = False
    container_status_known = False
    try:
        proc = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                container,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            container_running = proc.stdout.strip().lower() == "true"
            container_status_known = True
        elif (
            "No such" in (proc.stderr or "") or "no such" in (proc.stdout or "").lower()
        ):
            container_status_known = True  # confirmed gone
    except Exception:
        pass  # docker daemon unresponsive — leave status unknown, retry next cycle

    now = time.time()
    relaunch_type = None
    should_stop = False

    with state.automate_lock:
        if not state.automate["active"]:
            return "stop"

        if (
            container_status_known
            and not container_running
            and state.automate["status"] == "running"
        ):
            if now - state.automate["started_at"] > 15:
                relaunch_type = "hard"
            else:
                return None
        elif (
            state.automate["done_signal_at"] is not None
            and now - state.automate["done_signal_at"]
            > proj["triggers"]["done_idle_sec"]
            and state.automate["status"] == "running"
        ):
            print(
                f"[automate] Done signal + {proj['triggers']['done_idle_sec']}s idle — soft relaunch"
            )
            state.automate["done_signal_at"] = None
            relaunch_type = "soft"
        elif (
            now - state.automate["last_output_at"] > timeout_sec
            and state.automate["status"] == "running"
        ):
            relaunch_type = "soft"
        else:
            return None

    if relaunch_type is None:
        return None

    if relaunch_type == "soft":
        try:
            result = _automate_soft_relaunch(run_id)
        except Exception as e:
            print(f"[automate] soft relaunch failed: {e}")
            traceback.print_exc()
            return None
    else:
        try:
            result = _automate_relaunch(run_id)
        except Exception as e:
            print(f"[automate] hard relaunch failed: {e}")
            traceback.print_exc()
            return None
    return "stop" if park.is_refusal(result) else None
