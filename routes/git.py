"""routes/git.py — Git operations and venv creation in isolated tmux sessions."""

import concurrent.futures
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

from shared import execution_park as park
from shared.tmux import create_tmux_session, detect_venv, tmux_send_keys, tmux_send_text
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


def _strip_userinfo(url):
    """A remote URL without any user:token@ it may carry."""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", r"\1", url, flags=re.I)


@git_bp.route("/api/git/preview")
def git_preview():
    """What Commit & push would act on, for its confirm step. Read-only.

    Branch, the upstream the push goes to (and its remote URL), and how many
    paths `git add -A` would stage, untracked files included.
    """
    from routes.poll import _run_git
    from routes.studio import _pane_cwd

    target = resolve_target(request.args)
    project_dir = _pane_cwd(target)
    if not project_dir:
        return jsonify({"ok": False, "error": "Cannot determine project directory"}), 400
    try:
        head = _run_git(project_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
        if head.returncode != 0:
            return jsonify({"ok": False, "error": "Not a git repository"}), 400
        branch = head.stdout.strip()
        upstream = _run_git(
            project_dir,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            timeout=5,
        )
        upstream = upstream.stdout.strip() if upstream.returncode == 0 else ""
        remote = _run_git(project_dir, ["config", "--get", f"branch.{branch}.remote"], timeout=5)
        remote = remote.stdout.strip() if remote.returncode == 0 else ""
        remote_url = ""
        if remote:
            got = _run_git(project_dir, ["remote", "get-url", remote], timeout=5)
            if got.returncode == 0:
                remote_url = _strip_userinfo(got.stdout.strip())
        status = _run_git(
            project_dir, ["status", "--porcelain", "--untracked-files=all"], timeout=10
        )
        if status.returncode != 0:
            return jsonify({"ok": False, "error": "git status failed"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "git timed out"}), 504
    changed = sum(1 for line in status.stdout.splitlines() if line.strip())
    return jsonify(
        {
            "ok": True,
            "branch": branch,
            "upstream": upstream,
            "remote": remote,
            "remote_url": remote_url,
            "changed": changed,
        }
    )


@git_bp.route("/api/git/run", methods=["POST"])
def git_run():
    """Run a fixed git op in a temporary tmux session, isolated from Claude Code."""
    result = park.perform(park.Intent.FIXED_GIT, _git_run_effect)
    if park.is_refusal(result):
        return jsonify(result.body()), result.http_status
    return result


def _git_run_effect():
    """Complete the fixed Git unit while the park lock remains held."""
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
        created_session_id = None
        try:
            created = create_tmux_session(
                session_name=session_id,
                cwd=project_dir,
                cols=200,
                rows=50,
                surface="temporary_git",
                diagnostic_alias=f"{session_id}:0.0",
            )
            if not created.ok:
                return {"ok": False, "error": created.status}
            pane_id = created.identity.pane_id
            created_session_id = created.identity.session_id

            full_cmd = f"{command} ; tmux wait-for -S {session_id}"
            tmux_send_text(pane_id, full_cmd)
            tmux_send_keys(pane_id, "Enter")

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
                    pane_id,
                    "-S",
                    "-100",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = cap.stdout.rstrip("\n") if cap.returncode == 0 else ""

            subprocess.run(
                ["tmux", "kill-session", "-t", created_session_id],
                capture_output=True,
                timeout=5,
            )

            return {"ok": True, "output": output}
        except subprocess.TimeoutExpired:
            if created_session_id is not None:
                subprocess.run(
                    ["tmux", "kill-session", "-t", created_session_id],
                    capture_output=True,
                    timeout=5,
                )
            return {"ok": False, "error": "Command timed out (60s)"}
        except Exception as e:
            if created_session_id is not None:
                subprocess.run(
                    ["tmux", "kill-session", "-t", created_session_id],
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
    result = park.perform(park.Intent.PROJECT_VENV, _venv_create_effect)
    if park.is_refusal(result):
        return jsonify(result.body()), result.http_status
    return result


def _venv_create_effect():
    """Complete venv creation/activation while the park lock remains held."""
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
