"""routes/terminal.py — Terminal session management, projects, capture."""

import json
import os
import subprocess
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from shared.tmux import (
    capture_pane,
    detect_venv,
    tmux_send_keys,
    tmux_send_text,
    tmux_target_exists,
)

terminal_bp = Blueprint("terminal_bp", __name__)


@terminal_bp.route("/terminal/projects")
def terminal_projects():
    """List project directories with venv detection."""
    if not state.PROJECTS_DIR.is_dir():
        return jsonify({"projects": [], "error": "Projects dir not found"}), 404

    projects = []
    for entry in sorted(state.PROJECTS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        venv = detect_venv(entry)
        projects.append(
            {
                "name": entry.name,
                "path": str(entry),
                "venv": venv,
                "has_git": (entry / ".git").exists(),
            }
        )
    return jsonify({"projects": projects})


@terminal_bp.route("/terminal/launch", methods=["POST"])
def terminal_launch():
    """Launch a tmux session for a project with optional venv activation."""
    data = request.get_json(silent=True) or {}
    project = (data.get("project") or "").strip()
    if not project:
        return jsonify({"ok": False, "error": "No project specified"}), 400

    # Accept optional cwd override (used by "New session" on renamed/duplicated tabs)
    cwd_override = (data.get("cwd") or "").strip()

    project_path = state.PROJECTS_DIR / project
    if not project_path.is_dir():
        if cwd_override and Path(cwd_override).is_dir():
            project_path = Path(cwd_override)
        else:
            return jsonify({"ok": False, "error": "Project not found"}), 404

    session_name = project

    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={session_name}"],
        capture_output=True,
        timeout=5,
    )
    if check.returncode == 0:
        state.tmux_target = f"{session_name}:0.0"
        return jsonify(
            {
                "ok": True,
                "session": session_name,
                "target": state.tmux_target,
                "existed": True,
            }
        )

    cols = data.get("cols", state.get_setting("terminal", "default_cols"))
    rows = data.get("rows", state.get_setting("terminal", "default_rows"))
    # Clamp to sane range
    cols = max(40, min(int(cols), 400))
    rows = max(10, min(int(rows), 200))

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
            str(cols),
            "-y",
            str(rows),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        return (
            jsonify({"ok": False, "error": f"tmux new-session failed: {proc.stderr}"}),
            500,
        )

    subprocess.run(
        [
            "tmux",
            "set-option",
            "-t",
            session_name,
            "history-limit",
            str(state.get_setting("terminal", "tmux_history_limit")),
        ],
        capture_output=True,
        timeout=5,
    )

    for var in state.CLAUDE_ENV_VARS:
        subprocess.run(
            ["tmux", "set-environment", "-t", session_name, "-r", var],
            capture_output=True,
            timeout=5,
        )
        tmux_send_text(f"{session_name}:0.0", f"unset {var}")
        tmux_send_keys(f"{session_name}:0.0", "Enter")
    time.sleep(0.1)

    venv = detect_venv(project_path)
    if venv:
        tmux_send_text(f"{session_name}:0.0", f"source {venv}/bin/activate")
        tmux_send_keys(f"{session_name}:0.0", "Enter")
        time.sleep(0.3)

    init_cmd = state.get_setting("server", "session_init_cmd")
    skip_init = data.get("skip_init", False)
    if init_cmd and not skip_init:
        tmux_send_text(f"{session_name}:0.0", init_cmd)
        tmux_send_keys(f"{session_name}:0.0", "Enter")
        time.sleep(0.3)

    state.tmux_target = f"{session_name}:0.0"
    return jsonify(
        {
            "ok": True,
            "session": session_name,
            "target": state.tmux_target,
            "venv": venv,
            "existed": False,
            "init_cmd": init_cmd or "",
        }
    )


def get_agent_info_map():
    """Build agent metadata maps from Claude Code team configs and session files.

    Claude Code >=2.1 no longer passes --agent-name/--team-name flags on
    spawned processes.  Instead, team membership lives in filesystem state:
      ~/.claude/teams/{team}/config.json  — members with tmuxPaneId, color, name
      ~/.claude/sessions/{pid}.json       — maps PID → sessionId

    Returns:
        tuple: (pane_id_map, lead_pids)
            pane_id_map: {tmux_pane_id -> {agent_name, agent_color, team_name}}
            lead_pids:   {str(claude_pid) -> team_name}
    """
    teams_dir = Path.home() / ".claude" / "teams"
    sessions_dir = Path.home() / ".claude" / "sessions"

    pane_id_map = {}
    lead_pids = {}

    if not teams_dir.is_dir():
        return pane_id_map, lead_pids

    lead_session_to_team = {}

    for config_path in teams_dir.glob("*/config.json"):
        try:
            config = json.loads(config_path.read_text())
        except Exception:
            continue

        team_name = config.get("name", "")
        lead_sid = config.get("leadSessionId", "")

        if lead_sid:
            lead_session_to_team[lead_sid] = team_name

        for member in config.get("members", []):
            pane_id = member.get("tmuxPaneId", "")
            if member.get("backendType") == "tmux" and pane_id.startswith("%"):
                pane_id_map[pane_id] = {
                    "agent_name": member.get("name", ""),
                    "agent_color": member.get("color", ""),
                    "team_name": team_name,
                }

    # Map lead session IDs to PIDs via session files
    if lead_session_to_team and sessions_dir.is_dir():
        for session_file in sessions_dir.glob("*.json"):
            try:
                sess = json.loads(session_file.read_text())
                sid = sess.get("sessionId", "")
                if sid in lead_session_to_team:
                    lead_pids[session_file.stem] = lead_session_to_team[sid]
            except Exception:
                continue

    return pane_id_map, lead_pids


def enrich_panes_with_agents(panes):
    """Enrich pane dicts with agent/team metadata from Claude Code team configs.

    Matches team members by tmux pane_id, and identifies team leads by
    tracing process ancestry from the claude PID to the tmux pane shell PID.
    """
    pane_id_map, lead_pids = get_agent_info_map()

    if not pane_id_map and not lead_pids:
        return

    # Match team members by tmux pane_id
    for pane in panes:
        info = pane_id_map.get(pane.get("pane_id", ""))
        if info:
            pane["agent_name"] = info["agent_name"]
            pane["agent_color"] = info["agent_color"]
            pane["team_name"] = info["team_name"]

    # Match team leads by process ancestry
    if lead_pids:
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,ppid="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                parent_map = {}
                for line in proc.stdout.splitlines():
                    parts = line.split()
                    if len(parts) == 2:
                        parent_map[parts[0]] = parts[1]

                pane_pid_set = {p["pane_pid"] for p in panes if p.get("pane_pid")}

                for lead_pid, team_name in lead_pids.items():
                    pid = lead_pid
                    for _ in range(10):
                        ppid = parent_map.get(pid)
                        if not ppid or ppid in ("0", "1"):
                            break
                        if ppid in pane_pid_set:
                            for pane in panes:
                                if (
                                    pane.get("pane_pid") == ppid
                                    and "team_name" not in pane
                                ):
                                    pane["team_name"] = team_name
                            break
                        pid = ppid
        except Exception:
            pass


@terminal_bp.route("/terminal/sessions")
def terminal_sessions():
    """List active tmux sessions and panes."""
    proc = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_current_command}\t#{pane_width}\t#{pane_height}\t#{session_activity}\t#{pane_pid}\t#{pane_id}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return jsonify({"sessions": [], "active_target": state.tmux_target})

    panes = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 6:
            target = f"{parts[0]}:{parts[1]}.{parts[2]}"
            activity = int(parts[6]) if len(parts) >= 7 and parts[6].isdigit() else 0
            idle_seconds = int(time.time()) - activity if activity else 0
            pane_pid = parts[7] if len(parts) >= 8 else ""
            pane_id = parts[8] if len(parts) >= 9 else ""
            panes.append(
                {
                    "target": target,
                    "session": parts[0],
                    "window": int(parts[1]),
                    "pane": int(parts[2]),
                    "command": parts[3],
                    "width": int(parts[4]),
                    "height": int(parts[5]),
                    "idle_seconds": idle_seconds,
                    "pane_pid": pane_pid,
                    "pane_id": pane_id,
                }
            )

    enrich_panes_with_agents(panes)

    return jsonify({"sessions": panes, "active_target": state.tmux_target})


@terminal_bp.route("/terminal/states")
def terminal_states():
    """Lightweight state detection for all panes (no content capture)."""
    AGENT_COMMANDS = {
        "claude",
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

    proc = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_index}\t#{pane_index}\t"
            "#{pane_current_command}\t#{pane_pid}\t#{session_activity}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return jsonify({"states": {}})

    states = {}
    now = int(time.time())
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        target = f"{parts[0]}:{parts[1]}.{parts[2]}"
        command = parts[3]
        pane_pid = parts[4]
        activity = int(parts[5]) if parts[5].isdigit() else 0
        idle_seconds = now - activity if activity else 0

        idle_thresh = state.get_setting("terminal", "idle_threshold_sec")
        if command in AGENT_COMMANDS:
            st = "idle" if idle_seconds > idle_thresh else "running"
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

    return jsonify({"states": states})


@terminal_bp.route("/terminal/scan")
def terminal_scan():
    """Capture the last 60 lines of every tmux pane for prompt detection."""
    proc = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_current_command}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return jsonify({"panes": []})

    results = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        target = f"{parts[0]}:{parts[1]}.{parts[2]}"
        session_name = parts[0]
        command = parts[3]

        cap = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target, "-S", "-60"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if cap.returncode != 0:
            continue
        tail = cap.stdout.rstrip("\n")
        if tail:
            results.append(
                {
                    "target": target,
                    "session": session_name,
                    "command": command,
                    "tail": tail,
                }
            )

    return jsonify({"panes": results})


@terminal_bp.route("/terminal/target", methods=["POST"])
def terminal_set_target():
    """Set the active tmux target for input routing."""
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()

    if not target:
        state.tmux_target = None
        return jsonify({"ok": True, "target": None})

    if not tmux_target_exists(target):
        return jsonify({"ok": False, "error": "Target session not found"}), 404

    state.tmux_target = target
    return jsonify({"ok": True, "target": state.tmux_target})


@terminal_bp.route("/terminal/resize", methods=["POST"])
def terminal_resize():
    """Resize a tmux session window to given cols x rows."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    cols = data.get("cols")
    rows = data.get("rows")

    if not session:
        return jsonify({"ok": False, "error": "No session specified"}), 400
    if not cols or not rows:
        return jsonify({"ok": False, "error": "cols and rows required"}), 400

    cols = max(40, min(int(cols), 400))
    rows = max(10, min(int(rows), 200))

    proc = subprocess.run(
        [
            "tmux",
            "resize-window",
            "-t",
            f"={session}",
            "-x",
            str(cols),
            "-y",
            str(rows),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return (
            jsonify({"ok": False, "error": f"resize failed: {proc.stderr}"}),
            500,
        )

    return jsonify({"ok": True, "session": session, "cols": cols, "rows": rows})


@terminal_bp.route("/terminal/kill", methods=["POST"])
def terminal_kill():
    """Kill a tmux session."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not session:
        return jsonify({"ok": False, "error": "No session specified"}), 400

    proc = subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return (
            jsonify({"ok": False, "error": f"kill-session failed: {proc.stderr}"}),
            500,
        )

    if state.tmux_target and state.tmux_target.startswith(f"{session}:"):
        state.tmux_target = None

    return jsonify({"ok": True, "session": session})


@terminal_bp.route("/terminal/rename", methods=["POST"])
def terminal_rename():
    """Rename a tmux session."""
    data = request.get_json(silent=True) or {}
    old_name = (data.get("session") or "").strip()
    new_name = (data.get("name") or "").strip()

    if not old_name or not new_name:
        return jsonify({"ok": False, "error": "session and name required"}), 400

    # Sanitize: tmux session names can't contain dots or colons
    new_name = new_name.replace(".", "-").replace(":", "-")

    proc = subprocess.run(
        ["tmux", "rename-session", "-t", f"={old_name}", new_name],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return (
            jsonify({"ok": False, "error": f"rename failed: {proc.stderr}"}),
            500,
        )

    # Update active target if it pointed to the old session
    if state.tmux_target and state.tmux_target.startswith(f"{old_name}:"):
        suffix = state.tmux_target[len(old_name) :]
        state.tmux_target = new_name + suffix

    return jsonify(
        {"ok": True, "old": old_name, "new": new_name, "target": state.tmux_target}
    )


@terminal_bp.route("/terminal/cwd")
def terminal_cwd():
    """Return the current working directory of a tmux session."""
    session = request.args.get("session", "").strip()
    if not session:
        return jsonify({"ok": False, "error": "No session specified"}), 400
    proc = subprocess.run(
        ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return jsonify({"ok": False, "error": "Could not read session directory"}), 500
    return jsonify({"ok": True, "cwd": proc.stdout.strip()})


@terminal_bp.route("/terminal/duplicate", methods=["POST"])
def terminal_duplicate():
    """Create a new tmux session in the same directory as an existing one."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    new_name = (data.get("name") or "").strip()

    if not session:
        return jsonify({"ok": False, "error": "No session specified"}), 400

    # Get the CWD of the source session's active pane
    cwd_proc = subprocess.run(
        ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if cwd_proc.returncode != 0 or not cwd_proc.stdout.strip():
        return jsonify({"ok": False, "error": "Could not read session directory"}), 500

    cwd = cwd_proc.stdout.strip()

    # Generate a unique session name if not provided
    if not new_name:
        base = session
        for i in range(2, 20):
            candidate = f"{base}-{i}"
            check = subprocess.run(
                ["tmux", "has-session", "-t", f"={candidate}"],
                capture_output=True,
                timeout=5,
            )
            if check.returncode != 0:
                new_name = candidate
                break
        if not new_name:
            return (
                jsonify({"ok": False, "error": "Could not generate unique name"}),
                500,
            )

    new_name = new_name.replace(".", "-").replace(":", "-")

    # Check name isn't taken
    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={new_name}"],
        capture_output=True,
        timeout=5,
    )
    if check.returncode == 0:
        return (
            jsonify({"ok": False, "error": f"Session '{new_name}' already exists"}),
            409,
        )

    cols = data.get("cols", state.get_setting("terminal", "default_cols"))
    rows = data.get("rows", state.get_setting("terminal", "default_rows"))
    cols = max(40, min(int(cols), 400))
    rows = max(10, min(int(rows), 200))

    proc = subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            new_name,
            "-c",
            cwd,
            "-x",
            str(cols),
            "-y",
            str(rows),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        return (
            jsonify({"ok": False, "error": f"new-session failed: {proc.stderr}"}),
            500,
        )

    subprocess.run(
        [
            "tmux",
            "set-option",
            "-t",
            new_name,
            "history-limit",
            str(state.get_setting("terminal", "tmux_history_limit")),
        ],
        capture_output=True,
        timeout=5,
    )

    # Strip Claude environment variables (same as terminal_launch)
    for var in state.CLAUDE_ENV_VARS:
        subprocess.run(
            ["tmux", "set-environment", "-t", new_name, "-r", var],
            capture_output=True,
            timeout=5,
        )
        tmux_send_text(f"{new_name}:0.0", f"unset {var}")
        tmux_send_keys(f"{new_name}:0.0", "Enter")
    time.sleep(0.1)

    # Detect and activate venv if present
    project_path = Path(cwd)
    venv = detect_venv(project_path)
    if venv:
        tmux_send_text(f"{new_name}:0.0", f"source {venv}/bin/activate")
        tmux_send_keys(f"{new_name}:0.0", "Enter")
        time.sleep(0.3)

    # Run session init command (daic install, claude start, etc.)
    init_cmd = state.get_setting("server", "session_init_cmd")
    skip_init = data.get("skip_init", False)
    if init_cmd and not skip_init:
        tmux_send_text(f"{new_name}:0.0", init_cmd)
        tmux_send_keys(f"{new_name}:0.0", "Enter")
        time.sleep(0.3)

    target = f"{new_name}:0.0"
    state.tmux_target = target

    return jsonify(
        {
            "ok": True,
            "session": new_name,
            "target": target,
            "cwd": cwd,
            "init_cmd": init_cmd or "",
        }
    )


@terminal_bp.route("/terminal/run-init", methods=["POST"])
def terminal_run_init():
    """Run the session init command in an existing tmux session."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not session:
        return jsonify({"ok": False, "error": "No session specified"}), 400

    init_cmd = state.get_setting("server", "session_init_cmd")
    if not init_cmd:
        return jsonify(
            {"ok": True, "skipped": True, "reason": "No init command configured"}
        )

    # Verify session exists
    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"],
        capture_output=True,
        timeout=5,
    )
    if check.returncode != 0:
        return jsonify({"ok": False, "error": f"Session '{session}' not found"}), 404

    target = f"{session}:0.0"
    tmux_send_text(target, init_cmd)
    tmux_send_keys(target, "Enter")

    return jsonify({"ok": True, "init_cmd": init_cmd})


@terminal_bp.route("/terminal/capture")
def terminal_capture():
    """Capture text content from a tmux pane (HTTP fallback for WebSocket)."""
    target = request.args.get("target", state.tmux_target or "")
    if not target:
        return jsonify({"ok": False, "error": "No target specified"}), 400

    lines = min(
        int(request.args.get("lines", state.get_setting("terminal", "capture_lines"))),
        state.get_setting("limits", "max_capture_lines"),
    )
    content, info = capture_pane(target, lines)
    if content is None:
        return jsonify({"ok": False, "error": "capture-pane failed"}), 500

    return jsonify(
        {
            "ok": True,
            "content": content,
            "target": target,
            "info": info,
            "ts": time.time(),
        }
    )


@terminal_bp.route("/terminal/sessions/history/<project>")
def session_history(project):
    """Return prior Claude Code sessions for a project from claude_archives DB."""
    try:
        import psycopg2
    except ImportError:
        return jsonify({"ok": False, "error": "psycopg2 not installed — pip install psycopg2-binary"}), 501

    project_path = state.PROJECTS_DIR / project
    if not project_path.is_dir():
        return jsonify({"ok": False, "error": "Project not found"}), 404

    sessions = []
    try:
        conn = psycopg2.connect(
            dbname=os.environ.get("ASSIST_DB_NAME", "claude_archives"),
            host=os.environ.get("ASSIST_DB_HOST", "localhost"),
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT session_id, started_at, ended_at, total_tokens, tool_calls
            FROM sessions
            WHERE project_path = %s
            ORDER BY started_at DESC
            LIMIT 5
        """,
            (str(project_path),),
        )
        for row in cur.fetchall():
            session_id, started, ended, tokens, tools = row
            # Check if session file exists for resume
            session_file = (
                Path.home() / ".claude" / "projects" / project / f"{session_id}.json"
            )
            sessions.append(
                {
                    "session_id": str(session_id),
                    "started_at": started.isoformat() if started else None,
                    "ended_at": ended.isoformat() if ended else None,
                    "total_tokens": tokens,
                    "tool_calls": tools,
                    "resumable": session_file.exists(),
                }
            )
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"ok": True, "sessions": [], "note": str(e)})

    return jsonify({"ok": True, "sessions": sessions})
