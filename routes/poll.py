"""routes/poll.py — Consolidated polling, health check, CLI proxy."""

import hashlib
import json as json_mod
import os
import re
import subprocess
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from routes.terminal import enrich_panes_with_agents
from shared.tmux import prettify_command

poll_bp = Blueprint("poll_bp", __name__)

# Claude Code native installs run as version-named binaries (e.g. `2.1.206`),
# so pane_current_command for a spawned subagent pane is a bare version string.
_VERSION_CMD_RE = re.compile(r"\d+(?:\.\d+){1,3}")


def _find_project_dir(cwd):
    """Find the nearest project root for a tmux pane cwd."""
    check_dir = cwd
    for _ in range(6):
        if (check_dir / ".git").is_dir() or (check_dir / ".claude").is_dir() or (check_dir / ".opencode").is_dir():
            return check_dir
        parent = check_dir.parent
        if parent == check_dir:
            break
        check_dir = parent
    return cwd


def _run_git(project_dir, args, timeout=2):
    """Run a read-only git command without taking optional index locks.

    Claude/OpenCode status polling runs frequently from Assist. Plain `git status`
    may refresh the index and race an interactive `git commit`, producing the
    lock error shown in terminal panes. `GIT_OPTIONAL_LOCKS=0` asks git to avoid
    optional index refresh locks for status-ish reads.
    """
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(project_dir),
        env=env,
    )


def _read_json(path):
    try:
        if path.exists():
            return json_mod.loads(path.read_text())
    except Exception:
        pass
    return None


def _merge_opencode_meta(result, project_dir):
    """Merge optional OpenCode plugin cache into the Assist metadata payload."""
    candidates = [
        project_dir / ".claude" / "state" / "opencode-status.json",
        project_dir / ".opencode" / "status.json",
    ]
    for path in candidates:
        oc = _read_json(path)
        if not oc:
            continue
        result.setdefault("agent", "opencode")
        if oc.get("model"):
            result["model"] = oc.get("model")
        if oc.get("status"):
            result["session_status"] = oc.get("status")
        if oc.get("session_id"):
            result["session_id"] = oc.get("session_id")
        if oc.get("todos") is not None:
            result["open_tasks"] = oc.get("todos")
        if oc.get("updated"):
            result["agent_updated"] = oc.get("updated")
        return


def get_claude_meta(target):
    """Read Claude/OpenCode session metadata near the pane's cwd.

    Returns a dict with context usage, cost, task, branch, edit counts, etc.
    Claude Code populates `.claude/state/context-usage.json` via DAIC's
    statusline script. OpenCode can populate `.claude/state/opencode-status.json`
    via a lightweight plugin. Git metadata is gathered independently for both.
    """
    try:
        if not target:
            return {}

        # Get pane's current working directory
        proc = subprocess.run(
            ["tmux", "display-message", "-t", target, "-p", "#{pane_current_path}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}

        cwd = Path(proc.stdout.strip())
        project_dir = _find_project_dir(cwd)
        state_dir = project_dir / ".claude" / "state"

        result = {}
        _merge_opencode_meta(result, project_dir)

        # Read context-usage.json
        try:
            ctx_file = state_dir / "context-usage.json"
            if ctx_file.exists():
                ctx = json_mod.loads(ctx_file.read_text())
                result["used_percentage"] = ctx.get("used_percentage")
                result["tokens"] = ctx.get("tokens")
                result["limit"] = ctx.get("limit")
                result["duration_ms"] = ctx.get("duration_ms")
                result["cost_usd"] = ctx.get("cost_usd")
                result["model"] = ctx.get("model")
        except Exception:
            pass

        # Read current_task.json
        try:
            task_file = state_dir / "current_task.json"
            if task_file.exists():
                task_data = json_mod.loads(task_file.read_text())
                result["task"] = task_data.get("task")
        except Exception:
            pass

        # Read block-info.json
        try:
            block_file = state_dir / "block-info.json"
            if block_file.exists():
                block = json_mod.loads(block_file.read_text())
                result["block_active"] = block.get("isActive", False)
                result["block_elapsed"] = block.get("elapsedSeconds")
                result["block_remaining"] = block.get("remainingSeconds")
        except Exception:
            pass

        # Git branch
        try:
            git_proc = _run_git(project_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
            if git_proc.returncode == 0:
                result["branch"] = git_proc.stdout.strip()
        except Exception:
            pass

        # Edited files. Use lock-free status polling so Assist does not race the
        # interactive agent for .git/index.lock.
        try:
            gs_proc = _run_git(project_dir, ["status", "--porcelain=v1", "--untracked-files=all"])
            if gs_proc.returncode == 0:
                edited = staged = untracked = deleted = 0
                for line in gs_proc.stdout.strip().split("\n"):
                    if not line:
                        continue
                    x = line[0] if len(line) > 0 else " "
                    y = line[1] if len(line) > 1 else " "
                    if x == "?" and y == "?":
                        untracked += 1
                        edited += 1
                        continue
                    if x != " ":
                        staged += 1
                    if x == "D" or y == "D":
                        deleted += 1
                    if x in "MADRCU" or y in "MADRCU":
                        edited += 1
                result["edited_files"] = edited
                result["staged_files"] = staged
                result["untracked_files"] = untracked
                result["deleted_files"] = deleted
        except Exception:
            pass

        # Open tasks (sessions/tasks/*.md not containing "status: done/completed")
        try:
            tasks_dir = project_dir / "sessions" / "tasks"
            if tasks_dir.is_dir():
                open_count = 0
                for md_file in tasks_dir.glob("*.md"):
                    try:
                        head = (
                            md_file.read_bytes()[:500]
                            .decode("utf-8", errors="ignore")
                            .lower()
                        )
                        if (
                            "status: done" not in head
                            and "status: completed" not in head
                        ):
                            open_count += 1
                    except Exception:
                        pass
                result["open_tasks"] = open_count
        except Exception:
            pass

        return result
    except Exception:
        return {}


@poll_bp.route("/poll")
def consolidated_poll():
    """Combined health + sessions + states + scan in one request.

    Replaces 4 separate polling endpoints to reduce HTTP overhead.
    Called every 5s from the frontend.
    """
    ws_count = len(state.ws_clients)
    result = {
        "status": "ok",
        "tmux_target": state.tmux_target,
        "ws_clients": ws_count,
    }

    # --- Sessions ---
    proc = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_index}\t#{pane_index}\t"
            "#{pane_current_command}\t#{pane_width}\t#{pane_height}\t"
            "#{session_activity}\t#{pane_pid}\t#{pane_id}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    panes = []
    seen_sessions = set()
    now = time.time()
    if proc.returncode == 0:
        for line in proc.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 6:
                target = f"{parts[0]}:{parts[1]}.{parts[2]}"
                pane_pid = parts[7] if len(parts) >= 8 else ""
                pane_id = parts[8] if len(parts) >= 9 else ""
                is_subpane = parts[0] in seen_sessions
                seen_sessions.add(parts[0])
                panes.append(
                    {
                        "target": target,
                        "session": parts[0],
                        "window": int(parts[1]),
                        "pane": int(parts[2]),
                        "command": parts[3],
                        "width": int(parts[4]),
                        "height": int(parts[5]),
                        "pane_pid": pane_pid,
                        "pane_id": pane_id,
                        "is_subpane": is_subpane,
                        "command_display": prettify_command(parts[3]),
                    }
                )

        enrich_panes_with_agents(panes)

    result["sessions"] = panes
    result["active_target"] = state.tmux_target

    # --- Scan ---
    # Must run BEFORE states so content-based idle_seconds is available.
    scan_results = []
    live_targets = set()
    for pane in panes:
        target = pane["target"]
        live_targets.add(target)
        cap = subprocess.run(
            ["tmux", "capture-pane", "-e", "-p", "-t", target, "-S", "-60"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if cap.returncode == 0:
            tail = cap.stdout.rstrip("\n")
            if tail:
                content_hash = hashlib.md5(tail.encode()).hexdigest()
                with state._activity_lock:
                    prev_hash = state.pane_content_hash.get(target)
                    if prev_hash is None:
                        # First time seeing this pane — treat as just active
                        state.pane_last_activity.setdefault(target, now)
                    elif content_hash != prev_hash:
                        state.pane_last_activity[target] = now
                    state.pane_content_hash[target] = content_hash
                scan_results.append(
                    {
                        "target": target,
                        "session": pane["session"],
                        "command": pane["command"],
                        "tail": tail,
                    }
                )
    result["scan"] = scan_results

    # Clean up stale targets no longer in tmux
    with state._activity_lock:
        for stale in set(state.pane_content_hash) - live_targets:
            state.pane_content_hash.pop(stale, None)
            state.pane_last_activity.pop(stale, None)

    # Persist idle state to disk (every poll cycle is ~5s, lightweight write)
    state.save_idle_state()

    # --- States ---
    AGENT_COMMANDS = {
        "claude",
        "opencode",
        "node",
        "python",
        "python3",
        "npm",
        "pip",
        "pytest",
        "make",
        "cargo",
        "go",
        "docker",
        "git",
    }
    SHELL_COMMANDS = {"bash", "zsh", "sh", "fish"}
    states = {}
    for pane in panes:
        target = pane["target"]
        command = pane["command"]
        pane_pid = pane.get("pane_pid", "")

        with state._activity_lock:
            last_active = state.pane_last_activity.get(target, now)
        idle_seconds = now - last_active

        idle_thresh = state.get_setting("terminal", "idle_threshold_sec")
        if command in AGENT_COMMANDS or _VERSION_CMD_RE.fullmatch(command):
            st = "idle" if idle_seconds > 30 else "running"
        elif command in SHELL_COMMANDS:
            if pane_pid:
                try:
                    child_check = subprocess.run(
                        ["pgrep", "-P", pane_pid],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    if child_check.stdout.strip():
                        st = "running"
                    elif idle_seconds > idle_thresh:
                        st = "idle"
                    else:
                        st = "shell"
                except Exception:
                    st = "idle" if idle_seconds > idle_thresh else "shell"
            else:
                st = "idle" if idle_seconds > idle_thresh else "shell"
        else:
            st = "running"

        states[target] = {
            "state": st,
            "command": command,
            "idle_seconds": idle_seconds,
        }
    result["states"] = states

    # Backfill idle_seconds onto pane objects for frontend use
    for pane in panes:
        pane["idle_seconds"] = states.get(pane["target"], {}).get("idle_seconds", 0)

    # --- Automate status ---
    with state.automate_lock:
        if state.automate["active"] or state.automate["status"]:
            now_ts = time.time()
            result["automate"] = {
                "active": state.automate["active"],
                "status": state.automate["status"],
                "session": state.automate["session"],
                "project": state.automate["project"],
                "elapsed_seconds": (
                    round(now_ts - state.automate["started_at"], 1)
                    if state.automate["started_at"]
                    else 0
                ),
                "idle_seconds": (
                    round(now_ts - state.automate["last_output_at"], 1)
                    if state.automate["last_output_at"]
                    else 0
                ),
                "timeout_minutes": state.automate["timeout_minutes"],
                "continuous": state.automate["continuous"],
                "max_iterations": state.automate["max_iterations"],
                "iterations_completed": state.automate["iterations_completed"],
                "stop_after": state.automate.get("stop_after", ""),
            }

    # --- Claude session metadata ---
    if state.tmux_target:
        result["claude_meta"] = get_claude_meta(state.tmux_target)

    return jsonify(result)


@poll_bp.route("/health")
def health():
    ws_count = len(state.ws_clients)
    return jsonify(
        {"status": "ok", "tmux_target": state.tmux_target, "ws_clients": ws_count}
    )


@poll_bp.route("/api/cli-proxy", methods=["POST"])
def cli_proxy():
    """Proxy CLI commands from Docker containers to a host-side CLI tool.

    Expects JSON: {"args": ["backup", "status"], "files": [{"name": "f.md", "data": "<b64>"}]}
    Returns JSON: {"stdout": "...", "stderr": "...", "returncode": 0}

    Configure via ASSIST_CLI_BIN (binary path), ASSIST_CLI_DIR (working dir),
    and ASSIST_CLI_ALLOWED (comma-separated allowed subcommands).

    Files sent from containers are saved to a temp dir on the host, and
    __PROXY_FILE_N__ placeholders in args are replaced with the real paths.
    """
    data = request.get_json(force=True, silent=True) or {}
    args = data.get("args", [])
    if not args:
        return jsonify({"error": "missing args"}), 400

    allowed_env = os.environ.get("ASSIST_CLI_ALLOWED", "")
    ALLOWED = set(allowed_env.split(",")) if allowed_env else set()
    if ALLOWED and args[0] not in ALLOWED:
        return jsonify({"error": f"subcommand '{args[0]}' not allowed"}), 403

    tmp_files = []
    proxy_files = data.get("files", [])
    if proxy_files:
        import base64
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="assist-proxy-")
        for i, fobj in enumerate(proxy_files):
            fname = fobj.get("name", f"file_{i}")
            fdata = base64.b64decode(fobj.get("data", ""))
            fpath = os.path.join(tmp_dir, fname)
            with open(fpath, "wb") as f:
                f.write(fdata)
            tmp_files.append(fpath)

        resolved_args = []
        for arg in args:
            if arg.startswith("__PROXY_FILE_") and arg.endswith("__"):
                try:
                    idx = int(arg[len("__PROXY_FILE_") : -2])
                    resolved_args.append(tmp_files[idx])
                except (ValueError, IndexError):
                    resolved_args.append(arg)
            else:
                resolved_args.append(arg)
        args = resolved_args

    cli_bin = os.environ.get("ASSIST_CLI_BIN")
    if not cli_bin:
        return (
            jsonify({"error": "ASSIST_CLI_BIN not configured", "returncode": -1}),
            500,
        )

    cmd_timeout = 30
    if "--wait-reply" in args or "-w" in args:
        cmd_timeout = 330
        for i, a in enumerate(args):
            if a == "--timeout" and i + 1 < len(args):
                try:
                    cmd_timeout = int(args[i + 1]) + 30
                except ValueError:
                    pass

    try:
        result = subprocess.run(
            [cli_bin] + args,
            capture_output=True,
            text=True,
            timeout=cmd_timeout,
            cwd=os.environ.get("ASSIST_CLI_DIR", os.path.expanduser("~")),
        )
        return jsonify(
            {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        )
    except subprocess.TimeoutExpired:
        return (
            jsonify({"error": f"timeout ({cmd_timeout}s)", "returncode": -1}),
            504,
        )
    except FileNotFoundError:
        return jsonify({"error": "CLI binary not found", "returncode": -1}), 500
    finally:
        for f in tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        if tmp_files:
            try:
                os.rmdir(os.path.dirname(tmp_files[0]))
            except OSError:
                pass
