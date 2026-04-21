"""routes/git.py — Git operations and venv creation in isolated tmux sessions."""

import concurrent.futures
import subprocess
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from shared.tmux import detect_venv, tmux_send_keys, tmux_send_text
from shared.utils import resolve_target

git_bp = Blueprint("git_bp", __name__)


@git_bp.route("/api/git/run", methods=["POST"])
def git_run():
    """Run a git command in a temporary tmux session, isolated from Claude Code."""
    data = request.get_json(silent=True) or {}
    command = (data.get("command") or "").strip()
    target = resolve_target(data)

    if not command:
        return jsonify({"ok": False, "error": "No command provided"}), 400

    first_cmd = command.split("&&")[0].strip()
    if not first_cmd.startswith("git "):
        return jsonify({"ok": False, "error": "Only git commands allowed"}), 403

    if not target:
        return jsonify({"ok": False, "error": "No active session"}), 400

    try:
        proc = subprocess.run(
            [
                "tmux",
                "display-message",
                "-t",
                target,
                "-p",
                "#{pane_current_path}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return (
                jsonify({"ok": False, "error": "Cannot determine project directory"}),
                500,
            )
        project_dir = proc.stdout.strip()
    except Exception as e:
        return jsonify({"ok": False, "error": f"tmux error: {e}"}), 500

    session_id = f"_git_{uuid.uuid4().hex[:8]}"

    def _run_git():
        try:
            subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session_id,
                    "-c",
                    project_dir,
                    "-x",
                    "200",
                    "-y",
                    "50",
                ],
                capture_output=True,
                timeout=10,
            )

            full_cmd = f"{command} ; tmux wait-for -S {session_id}"
            tmux_send_text(f"{session_id}:0.0", full_cmd)
            tmux_send_keys(f"{session_id}:0.0", "Enter")

            subprocess.run(
                ["tmux", "wait-for", session_id],
                capture_output=True,
                timeout=60,
            )
            time.sleep(0.2)

            cap = subprocess.run(
                [
                    "tmux",
                    "capture-pane",
                    "-p",
                    "-t",
                    f"{session_id}:0.0",
                    "-S",
                    "-100",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = cap.stdout.rstrip("\n") if cap.returncode == 0 else ""

            subprocess.run(
                ["tmux", "kill-session", "-t", session_id],
                capture_output=True,
                timeout=5,
            )

            return {"ok": True, "output": output}
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_id],
                capture_output=True,
                timeout=5,
            )
            return {"ok": False, "error": "Command timed out (60s)"}
        except Exception as e:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_id],
                capture_output=True,
                timeout=5,
            )
            return {"ok": False, "error": str(e)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_git)
        try:
            result = future.result(timeout=65)
        except concurrent.futures.TimeoutError:
            return jsonify({"ok": False, "error": "Execution timeout"}), 504

    status_code = 200 if result.get("ok") else 500
    return jsonify(result), status_code


@git_bp.route("/api/venv/create", methods=["POST"])
def venv_create():
    """Create a .venv in the active tmux pane's project directory."""
    data = request.get_json(silent=True) or {}
    target = resolve_target(data)
    if not target:
        return jsonify({"ok": False, "error": "No active session"}), 400

    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-t", target, "-p", "#{pane_current_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return (
                jsonify({"ok": False, "error": "Cannot determine project directory"}),
                500,
            )
        project_dir = proc.stdout.strip()
    except Exception as e:
        return jsonify({"ok": False, "error": f"tmux error: {e}"}), 500

    project_path = Path(project_dir)
    if detect_venv(project_path):
        return jsonify({"ok": False, "error": "venv already exists"}), 409

    try:
        proc = subprocess.run(
            ["python3", "-m", "venv", str(project_path / ".venv")],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": proc.stderr.strip() or "venv creation failed",
                    }
                ),
                500,
            )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "venv creation timed out"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    tmux_send_text(target, f"source {project_path}/.venv/bin/activate")
    tmux_send_keys(target, "Enter")

    return jsonify({"ok": True, "path": str(project_path / ".venv")})
