"""routes/automate.py — Automate Run: launch/monitor/relaunch claude-mount in tmux."""

import copy
import hashlib
import shlex
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from shared.tmux import tmux_send_keys, tmux_send_text
from shared.utils import load_json, save_json

automate_bp = Blueprint("automate_bp", __name__)


def _build_automate_prompt(prompt, project_path):
    """Prepend @immediatenotice.md reference if the file exists in the project."""
    notice_path = Path(project_path) / state.IMMEDIATE_NOTICE_FILENAME
    if notice_path.is_file():
        return f"@{state.IMMEDIATE_NOTICE_FILENAME} {prompt}"
    return prompt


def _automate_save():
    """Persist automate state to disk (call while holding automate_lock)."""
    try:
        st = {k: v for k, v in state.automate.items() if k != "last_output_hash"}
        save_json(state.AUTOMATE_STATE_FILE, st)
    except Exception:
        pass


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
                "status": "running",
                "continuous": continuous,
                "max_iterations": max_iterations,
                "iterations_completed": iterations_completed,
                "stop_after": stop_after,
                "claude_cmd": claude_cmd,
            }
        )

    state.automate_stop_event.set()
    time.sleep(0.1)
    state.automate_stop_event.clear()
    threading.Thread(target=_automate_monitor, daemon=True).start()


@automate_bp.route("/api/automate/start", methods=["POST"])
def automate_start():
    """Launch claude-mount in a tmux session for the current project."""
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return jsonify({"ok": False, "error": "No prompt provided"}), 400

    with state.automate_lock:
        if state.automate["active"]:
            return jsonify({"ok": False, "error": "Automation already running"}), 409
        state.automate["active"] = True

    if not state.CLAUDE_MOUNT_SCRIPT or not state.CLAUDE_MOUNT_SCRIPT.exists():
        with state.automate_lock:
            state.automate["active"] = False
        return jsonify({"ok": False, "error": "ASSIST_MOUNT_SCRIPT not configured — set it in .env"}), 400

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

    proc = subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(project_path),
            "-x",
            "200",
            "-y",
            "50",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        with state.automate_lock:
            state.automate["active"] = False
        return jsonify({"ok": False, "error": f"tmux failed: {proc.stderr}"}), 500

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

    tmux_send_text(f"{session_name}:0.0", cmd)
    tmux_send_keys(f"{session_name}:0.0", "Enter")

    now = time.time()
    with state.automate_lock:
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
                "status": "running",
                "continuous": continuous,
                "max_iterations": max_iterations,
                "iterations_completed": 0,
                "stop_after": stop_after,
                "claude_cmd": claude_cmd,
            }
        )
        _automate_save()

    state.automate_stop_event.set()
    time.sleep(0.1)
    state.automate_stop_event.clear()
    threading.Thread(target=_automate_monitor, daemon=True).start()

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
            return jsonify({"active": False})

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
                "status": "running",
                "continuous": saved.get("continuous", True),
                "max_iterations": saved.get("max_iterations", 0),
                "iterations_completed": saved.get("iterations_completed", 0),
                "stop_after": saved.get("stop_after", ""),
                "claude_cmd": saved.get("claude_cmd", ""),
            }
        )
        _automate_save()

    state.automate_stop_event.set()
    time.sleep(0.1)
    state.automate_stop_event.clear()
    threading.Thread(target=_automate_monitor, daemon=True).start()

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
    with state.automate_lock:
        if not state.automate["active"]:
            return jsonify({"ok": False, "error": "No automation running"}), 404
        container = state.automate["container"]
        session = state.automate["session"]
        state.automate["active"] = False
        state.automate["status"] = "stopped"
        _automate_save()

    state.automate_stop_event.set()
    _automate_cleanup(container, session)

    return jsonify({"ok": True, "status": "stopped"})


def _automate_cleanup(container, session):
    """Kill container and tmux session."""
    if container:
        subprocess.run(["docker", "kill", container], capture_output=True, timeout=10)
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, timeout=10
        )
    if session:
        subprocess.run(
            ["tmux", "kill-session", "-t", session], capture_output=True, timeout=5
        )


def _automate_soft_relaunch():
    """Send /clear to existing Claude session, wait, then re-send the prompt."""
    with state.automate_lock:
        session = state.automate["session"]
        prompt = state.automate["prompt"]
        project_path = state.automate["project_path"]
        project_name = state.automate["project"]

    target = f"{session}:0.0"
    print(f"[automate] Soft relaunch: sending /clear to {target}")

    tmux_send_text(target, "/clear")
    tmux_send_keys(target, "Enter")
    wait = (
        state.get_project_setting(project_name, "triggers", "relaunch_wait_sec")
        if project_name
        else 30
    )
    time.sleep(wait)

    full_prompt = _build_automate_prompt(prompt, project_path)
    full_prompt = full_prompt.replace("\n", " ").replace("\r", " ")
    tmux_send_text(target, full_prompt)
    tmux_send_keys(target, "Enter")

    now = time.time()
    with state.automate_lock:
        state.automate["last_output_at"] = now
        state.automate["last_output_hash"] = None
        state.automate["done_signal_at"] = None
        state.automate["status"] = "running"
        _automate_save()

    print(f"[automate] Soft relaunch complete — prompt re-sent to {target}")


def _automate_relaunch():
    """Clean up old container/session and launch a fresh one with the same prompt."""
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

    session_name = f"{project}-auto"
    session_id = hashlib.md5((str(project_path) + "\n").encode()).hexdigest()[:8]
    container_name = f"claude-session-{session_id}"

    subprocess.run(
        ["docker", "rm", "-f", container_name], capture_output=True, timeout=10
    )

    proc = subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(project_path),
            "-x",
            "200",
            "-y",
            "50",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
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
    tmux_send_text(f"{session_name}:0.0", cmd)
    tmux_send_keys(f"{session_name}:0.0", "Enter")

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


def _automate_monitor():
    """Background thread: watches container state + output staleness. Auto-relaunches."""
    while True:
        time.sleep(5)

        if state.automate_stop_event.is_set():
            return

        with state.automate_lock:
            if not state.automate["active"]:
                return
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
                if "Yes, I trust this folder" in out:
                    tmux_send_text(f"{session}:0.0", "1")
                    tmux_send_keys(f"{session}:0.0", "Enter")
                elif "trust the files" in out or "Yes, proceed" in out:
                    tmux_send_keys(f"{session}:0.0", "Enter")
        except Exception:
            pass

        try:
            if proc is not None and proc.returncode == 0:
                done_signals = proj["triggers"]["done_signals"]
                with state.automate_lock:
                    if any(sig in proc.stdout for sig in done_signals):
                        if state.automate["done_signal_at"] is None:
                            state.automate["done_signal_at"] = time.time()
                            print(f"[automate] Done signal detected in {session}")
                    else:
                        state.automate["done_signal_at"] = None
        except Exception:
            pass

        container_running = False
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
            container_running = proc.stdout.strip().lower() == "true"
        except Exception:
            pass

        now = time.time()
        relaunch_type = None
        should_stop = False

        with state.automate_lock:
            if not state.automate["active"]:
                return

            if not container_running and state.automate["status"] == "running":
                if now - state.automate["started_at"] > 15:
                    relaunch_type = "hard"
                else:
                    continue
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
                continue

        if relaunch_type is None:
            continue

        with state.automate_lock:
            continuous = state.automate["continuous"]
            max_iter = state.automate["max_iterations"]
            completed = state.automate["iterations_completed"]
            stop_after = state.automate.get("stop_after", "")
            state.automate["iterations_completed"] = completed + 1

            if not continuous:
                if max_iter <= 0 or (completed + 1) >= max_iter:
                    should_stop = True

            # Check time-of-day cutoff before relaunching
            if not should_stop and _past_stop_time(stop_after, state.automate["started_at"]):
                should_stop = True
                print(
                    f"[automate] Past stop_after time ({stop_after}) — stopping"
                )

            if should_stop:
                print(
                    f"[automate] Iteration {completed + 1}/{max_iter or 1} complete — stopping"
                )
                _automate_save()

        if should_stop:
            with state.automate_lock:
                container = state.automate["container"]
                session = state.automate["session"]
            _automate_cleanup(container, session)
            with state.automate_lock:
                state.automate["active"] = False
                state.automate["status"] = "completed"
                _automate_save()
            return

        with state.automate_lock:
            _automate_save()

        if relaunch_type == "soft":
            _automate_soft_relaunch()
        else:
            _automate_relaunch()
