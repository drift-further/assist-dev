"""routes/git.py — Git operations and venv creation in isolated tmux sessions."""

import concurrent.futures
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

from shared.tmux import detect_venv, tmux_send_keys, tmux_send_text
from shared.utils import resolve_target

git_bp = Blueprint("git_bp", __name__)

_ALLOWED_OPS = frozenset({"status", "push", "commit_push"})
_MAX_COMMIT_MESSAGE_LEN = 2000

# Control characters must never reach the message, and shlex.quote does NOT
# protect against them. The command is TYPED into an interactive pane, so
# readline sees these bytes before any shell parsing happens: a Ctrl-U (\x15)
# in a commit message erases the whole generated prefix, and the rest of the
# message becomes its own command —
#     git commit -m 'x<Ctrl-U>touch /tmp/pwn #' && git push
# leaves the shell holding `touch /tmp/pwn #' && git push`, i.e. arbitrary
# execution through a correctly-quoted argument. Ctrl-W, backspace, ESC and
# friends give the same class of edit. NUL additionally cannot survive the
# subprocess argv at all. Verified live 2026-07-28; a test that runs the
# finished string through `bash -c` cannot see this, because it never types it.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _build_git_command(op, message):
    """Map a fixed op to the exact shell line typed into the throwaway pane.

    Every branch is explicit and the tail raises: a future op added to
    _ALLOWED_OPS without a branch here must blow up, not fall through to
    whichever template happens to be last — that template commits and pushes.
    """
    if op == "status":
        return "git status"
    if op == "push":
        return "git push"
    if op == "commit_push":
        return f"git add -A && git commit -m {shlex.quote(message)} && git push"
    raise ValueError(f"No template for op: {op}")


@git_bp.route("/api/git/run", methods=["POST"])
def git_run():
    """Run a fixed git op in a temporary tmux session, isolated from Claude Code."""
    # A valid JSON body need not be an object — `["x"]` and `1` both parse, and
    # .get() on either is a 500 rather than the 400 it should be.
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    # Same absence of type guarantee one level down: op may be a list/dict/number.
    raw_op = data.get("op")
    op = raw_op.strip() if isinstance(raw_op, str) else ""
    target = resolve_target(data)

    if op not in _ALLOWED_OPS:
        return jsonify({"ok": False, "error": "Invalid or missing op"}), 400

    if op == "commit_push":
        message = data.get("message")
        # Blank-after-strip too: git aborts on an all-whitespace message, and
        # failing here says why instead of surfacing git's error in a pane.
        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "Message required"}), 400
        if _CONTROL_CHARS_RE.search(message):
            return (
                jsonify({"ok": False, "error": "Message must not contain control characters"}),
                400,
            )
        if len(message) > _MAX_COMMIT_MESSAGE_LEN:
            return jsonify({"ok": False, "error": "Message too long"}), 400
        command = _build_git_command(op, message)
    else:
        command = _build_git_command(op, "")

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
