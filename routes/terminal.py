"""routes/terminal.py — Terminal session management, projects, capture."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.drafts as drafts
from shared import execution_park as park
import shared.state as state
import shared.tab_state as tab_state
from shared.agent_identity import resolve_process
from shared.tmux import (
    capture_pane,
    create_tmux_session,
    detect_venv,
    expected_target_identity,
    prettify_command,
    record_tmux_adoption,
    tmux_exact_target,
    tmux_send_keys,
    tmux_send_text,
    tmux_target_exists,
)

terminal_bp = Blueprint("terminal_bp", __name__)


def _http_refusal(refusal):
    return jsonify(refusal.body()), refusal.http_status


@terminal_bp.route("/terminal/projects")
def terminal_projects():
    """List project directories with venv detection."""
    if not state.PROJECTS_DIR.is_dir():
        return jsonify(
            {
                "projects": [],
                "note": f"Projects directory does not exist: {state.PROJECTS_DIR}",
            }
        )

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

    # Sanitize: tmux session names can't contain dots or colons — they break
    # the session:window.pane target grammar everywhere downstream. Same rule
    # as /terminal/rename and /terminal/duplicate.
    session_name = project.replace(".", "-").replace(":", "-")

    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={session_name}"],
        capture_output=True,
        timeout=5,
    )
    if check.returncode == 0:
        return park.perform(
            park.Intent.BARE_TERMINAL,
            lambda: _existing_terminal_effect(session_name),
        )

    cols = data.get("cols", state.get_setting("terminal", "default_cols"))
    rows = data.get("rows", state.get_setting("terminal", "default_rows"))
    # Clamp to sane range
    cols = max(40, min(int(cols), 400))
    rows = max(60, min(int(rows), 200))

    init_cmd = state.get_setting("server", "session_init_cmd")
    skip_init = data.get("skip_init", False)
    intent = (
        park.Intent.TERMINAL_INIT_LAUNCH
        if init_cmd and not skip_init
        else park.Intent.BARE_TERMINAL
    )
    result = park.perform(
        intent,
        lambda: _terminal_launch_effect(
            project_path,
            session_name,
            cols,
            rows,
            init_cmd if intent is park.Intent.TERMINAL_INIT_LAUNCH else "",
        ),
    )
    if park.is_refusal(result):
        return _http_refusal(result)
    return result


def _existing_terminal_effect(session_name):
    adoption = record_tmux_adoption(
        f"{session_name}:0.0",
        surface="existing_terminal",
        diagnostic_alias=f"{session_name}:0.0",
    )
    if not adoption.ok:
        return jsonify({"ok": False, "error": adoption.status}), 409
    state.tmux_target = f"{session_name}:0.0"
    identity = adoption.identity
    return jsonify(
        {
            "ok": True,
            "session": session_name,
            "target": state.tmux_target,
            "existed": True,
            "expected_target_identity": identity.as_dict(),
        }
    )


def _terminal_launch_effect(project_path, session_name, cols, rows, init_cmd):
    """Create one terminal unit while the park decision lock is held."""

    created = create_tmux_session(
        session_name=session_name,
        cwd=project_path,
        cols=cols,
        rows=rows,
        surface="fresh_terminal",
        diagnostic_alias=f"{session_name}:0.0",
    )
    if not created.ok:
        return (
            jsonify({"ok": False, "error": created.status}),
            500,
        )
    identity = created.identity

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

    venv = detect_venv(project_path)
    if init_cmd:
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
            "expected_target_identity": identity.as_dict(),
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

        # Carried through so enrich_panes_with_agents() can reject a config
        # that predates the pane it claims to name — see the note there.
        try:
            config_mtime = config_path.stat().st_mtime
        except OSError:
            continue

        team_name = config.get("name", "")
        lead_sid = config.get("leadSessionId", "")

        if lead_sid:
            lead_session_to_team[lead_sid] = team_name

        for member in config.get("members", []):
            pane_id = member.get("tmuxPaneId", "")
            if member.get("backendType") == "tmux" and pane_id.startswith("%"):
                # Teams accumulate here forever and pane ids repeat, so two
                # configs can claim the same one. Newest wins; without this the
                # winner was whatever order glob() happened to return.
                existing = pane_id_map.get(pane_id)
                if existing and existing["config_mtime"] >= config_mtime:
                    continue
                pane_id_map[pane_id] = {
                    "agent_name": member.get("name", ""),
                    "agent_color": member.get("color", ""),
                    "team_name": team_name,
                    "config_mtime": config_mtime,
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
        if not info:
            continue
        # tmux hands out pane ids from %0 again every time its server restarts,
        # and team configs under ~/.claude/teams are never garbage collected, so
        # a long-dead team keeps naming whatever pane later inherits its id. A
        # config written BEFORE the pane existed cannot be describing it — a
        # real registration records a concrete tmuxPaneId, so it is always
        # written after the pane it registers. Without this, a July team config
        # relabelled the live assist-dev tab "perf-orch" and the session looked
        # missing from the strip entirely.
        created = pane.get("created") or 0
        if created and created > info["config_mtime"]:
            continue
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
            "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_current_command}\t#{pane_width}\t#{pane_height}\t#{session_activity}\t#{pane_pid}\t#{pane_id}\t#{session_created}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return jsonify({"sessions": [], "active_target": state.tmux_target})

    panes = []
    seen_sessions = set()
    # Snapshot the model memo once. terminal_sessions() is the first-load
    # renderer and the poll-failure fallback, so it must never resolve a model
    # itself — the /poll request path owns that work (and may run
    # concurrently, one handler per browser; observe_model()'s
    # _MIN_CONFIRM_SECONDS gate is what makes that concurrency safe). Before
    # the first poll lands these fields are simply absent and the tab line
    # renders empty.
    with state._activity_lock:
        model_memo = {key: dict(value) for key, value in state.pane_model.items()}
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
            # Epoch seconds the tmux session was opened — the key behind
            # "sort by opened date" in the tab strip.
            created = int(parts[9]) if len(parts) >= 10 and parts[9].isdigit() else 0
            is_subpane = parts[0] in seen_sessions
            seen_sessions.add(parts[0])
            agent_kind = resolve_process(target, pane_pid, parts[3])
            memo = model_memo.get(target) or {}
            panes.append(
                {
                    "target": target,
                    "session": parts[0],
                    "created": created,
                    "window": int(parts[1]),
                    "pane": int(parts[2]),
                    "command": parts[3],
                    "width": int(parts[4]),
                    "height": int(parts[5]),
                    "idle_seconds": idle_seconds,
                    "pane_pid": pane_pid,
                    "pane_id": pane_id,
                    "is_subpane": is_subpane,
                    "command_display": prettify_command(parts[3]),
                    "agent_kind": agent_kind or "unknown",
                    "model": memo.get("model"),
                    "model_effort": memo.get("effort"),
                    "model_changed_at": memo.get("changed_at", 0.0),
                }
            )

    enrich_panes_with_agents(panes)

    # Same ordering /poll applies, so the first render before a poll lands is
    # already correct rather than briefly showing raw tmux order.
    return jsonify(
        {
            "sessions": tab_state.apply_order(panes),
            "active_target": state.tmux_target,
            "tab_state": tab_state.get_tab_state(),
        }
    )


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
    # Floor is 10, not 60: the 60-row convention for claude panes lives in the
    # frontend _calcTermSize; TUI auto-fit needs exact viewport rows (claude
    # panes are excluded from TUI fit).
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


@terminal_bp.route("/terminal/unpin", methods=["POST"])
def terminal_unpin():
    """Unpin a session's window-size so it follows whatever tmux client attaches.

    /terminal/resize uses `resize-window`, which pins window-size=manual. That
    keeps the window fixed when a real client attaches, so an oversized terminal
    shows dead padding (the dotted rows). Unsetting the window option restores
    the global `latest` behavior: the window snaps to the attached client's
    exact size. Note: the next /terminal/resize re-pins it to manual.
    """
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not session:
        return jsonify({"ok": False, "error": "No session specified"}), 400

    proc = subprocess.run(
        ["tmux", "set-option", "-t", tmux_exact_target(session), "-w", "-u", "window-size"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return (
            jsonify({"ok": False, "error": f"unpin failed: {proc.stderr}"}),
            500,
        )

    return jsonify({"ok": True, "session": session})


@terminal_bp.route("/terminal/kill", methods=["POST"])
def terminal_kill():
    """Kill a tmux session."""
    return park.perform(park.Intent.STOP, _terminal_kill_effect)


def _terminal_kill_effect():
    """Stop the exact selected terminal under the decision lock."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not session:
        return jsonify({"ok": False, "error": "No session specified"}), 400

    proc = subprocess.run(
        ["tmux", "kill-session", "-t", f"={session}"],
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


@terminal_bp.route("/terminal/clear", methods=["POST"])
def terminal_clear():
    """Clear a pane's tmux scrollback without delivering input.

    Used by the client's double-tap-active-tab gesture to wipe accumulated
    scrollback artifacts while retaining client-clear behavior. This automatic
    UI action deliberately has no input-delivery behavior.
    """
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify({"ok": False, "error": "No target specified"}), 400

    subprocess.run(
        ["tmux", "clear-history", "-t", tmux_exact_target(target)],
        capture_output=True, timeout=5,
    )
    return jsonify({"ok": True, "target": target})


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

    # Carry pin/order/snooze and any composer drafts across the rename.
    # Server-side, so every connected device sees the fix-up, not just the one
    # that issued the rename.
    tab_state.rename_session(old_name, new_name)
    drafts.rename_session(old_name, new_name)

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
        ["tmux", "display-message", "-t", tmux_exact_target(session), "-p", "#{pane_current_path}"],
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

    init_cmd = state.get_setting("server", "session_init_cmd")
    skip_init = data.get("skip_init", False)

    # Get the CWD of the source session's active pane
    cwd_proc = subprocess.run(
        ["tmux", "display-message", "-t", tmux_exact_target(session), "-p", "#{pane_current_path}"],
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
    rows = max(60, min(int(rows), 200))

    intent = (
        park.Intent.TERMINAL_INIT_DUPLICATE
        if init_cmd and not skip_init
        else park.Intent.BARE_TERMINAL
    )
    result = park.perform(
        intent,
        lambda: _terminal_duplicate_effect(
            new_name,
            cwd,
            cols,
            rows,
            init_cmd if intent is park.Intent.TERMINAL_INIT_DUPLICATE else "",
        ),
    )
    if park.is_refusal(result):
        return _http_refusal(result)
    return result


def _terminal_duplicate_effect(new_name, cwd, cols, rows, init_cmd):
    """Create one duplicate terminal unit under the decision lock."""

    created = create_tmux_session(
        session_name=new_name,
        cwd=cwd,
        cols=cols,
        rows=rows,
        surface="duplicate",
        diagnostic_alias=f"{new_name}:0.0",
    )
    if not created.ok:
        return (
            jsonify({"ok": False, "error": created.status}),
            500,
        )
    identity = created.identity

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

    # Run the configured session initialization command.
    if init_cmd:
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
            "expected_target_identity": identity.as_dict(),
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

    result = park.perform(
        park.Intent.TERMINAL_RUN_INIT,
        lambda: _terminal_run_init_effect(session, init_cmd),
    )
    if park.is_refusal(result):
        return _http_refusal(result)
    return result


def _terminal_run_init_effect(session, init_cmd):
    """Deliver configured init only while the decision lock remains held."""

    # Verify session exists
    check = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"],
        capture_output=True,
        timeout=5,
    )
    if check.returncode != 0:
        return jsonify({"ok": False, "error": f"Session '{session}' not found"}), 404

    # Exact-match the session here too — it could die between the has-session
    # check above and the sends, and a bare name would prefix-match another
    # live session.
    target = f"={session}:0.0"
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
    # Per-pane TUI override from the chip (absent = auto-detect, as always).
    raw_tui = request.args.get("tui")
    tui = None if raw_tui in (None, "") else raw_tui in ("1", "true", "True")
    content, info = capture_pane(target, lines, tui=tui)
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


@terminal_bp.route("/terminal/explore/pick", methods=["POST"])
def terminal_explore_pick():
    """Open a native OS folder picker dialog and return the selected path."""
    result = park.perform(
        park.Intent.NATIVE_FOLDER_PICKER, _terminal_explore_pick_effect
    )
    if park.is_refusal(result):
        return _http_refusal(result)
    return result


def _terminal_explore_pick_effect():
    """Resolve and spawn the platform picker only under the decision lock."""
    import shutil

    data = request.get_json(silent=True) or {}
    start = (data.get("start") or str(Path.home())).strip()

    import platform
    system = platform.system()

    if system == "Darwin":
        # macOS: osascript is built-in, no install required
        script = f'POSIX path of (choose folder with prompt "Select Project Folder" default location POSIX file "{start}")'
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            path = proc.stdout.strip().rstrip("/")
            if path:
                return jsonify({"ok": True, "path": path, "name": Path(path).name})
        return jsonify({"ok": False, "cancelled": True, "error": "No folder selected"})

    if shutil.which("zenity"):
        proc = subprocess.run(
            ["zenity", "--file-selection", "--directory",
             "--title=Select Project Folder", f"--filename={start}/"],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            path = proc.stdout.strip()
            if path:
                return jsonify({"ok": True, "path": path, "name": Path(path).name})
        return jsonify({"ok": False, "cancelled": proc.returncode == 1, "error": "No folder selected"})

    if shutil.which("kdialog"):
        proc = subprocess.run(
            ["kdialog", "--getexistingdirectory", start, "--title", "Select Project Folder"],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            path = proc.stdout.strip()
            if path:
                return jsonify({"ok": True, "path": path, "name": Path(path).name})
        return jsonify({"ok": False, "cancelled": True, "error": "No folder selected"})

    return jsonify({"ok": False, "error": "No native dialog tool found — install zenity: sudo apt install zenity"}), 501


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

    # Claude Code stores transcripts under a path-munged dir name built from
    # the project's ABSOLUTE path, as {session_id}.jsonl. Current versions
    # replace every non-alphanumeric char with "-" (e.g. /home/user/foo.bar
    # -> -home-user-foo-bar); older versions kept underscores, so check
    # both forms.
    claude_projects = Path.home() / ".claude" / "projects"
    munged_dirs = {
        re.sub(r"[^A-Za-z0-9]", "-", str(project_path)),
        re.sub(r"[/.:]", "-", str(project_path)),
    }

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
            # Check if a transcript file exists for resume
            resumable = any(
                (claude_projects / d / f"{session_id}.jsonl").exists()
                for d in munged_dirs
            )
            sessions.append(
                {
                    "session_id": str(session_id),
                    "started_at": started.isoformat() if started else None,
                    "ended_at": ended.isoformat() if ended else None,
                    "total_tokens": tokens,
                    "tool_calls": tools,
                    "resumable": resumable,
                }
            )
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"ok": True, "sessions": [], "note": str(e)})

    return jsonify({"ok": True, "sessions": sessions})
