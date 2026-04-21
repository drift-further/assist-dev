"""routes/commands.py — Skills, saved commands, split pane management."""

import json
import subprocess
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from shared.tmux import detect_venv, tmux_send_keys, tmux_send_text

commands_bp = Blueprint("commands_bp", __name__)


def _parse_skill_frontmatter(text):
    """Extract YAML frontmatter (name, description) from a SKILL.md file."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    meta = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def _scan_skills_dir(skills_dir, source="project"):
    """Scan a .claude/skills/ directory and return list of skill dicts."""
    skills = []
    if not skills_dir.is_dir():
        return skills
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            meta = _parse_skill_frontmatter(skill_file.read_text())
            if meta.get("name"):
                skills.append(
                    {
                        "name": meta["name"],
                        "description": meta.get("description", ""),
                        "source": source,
                    }
                )
        except Exception:
            continue
    return skills


@commands_bp.route("/api/skills/<project>")
def get_skills(project):
    """List skills from project .claude/skills/ + global ~/.claude/skills/."""
    project_path = state.PROJECTS_DIR / project
    if not project_path.is_dir():
        return jsonify({"ok": False, "error": "Project not found"}), 404

    project_skills = _scan_skills_dir(
        project_path / ".claude" / "skills", source="project"
    )
    project_names = {s["name"] for s in project_skills}

    global_skills = [
        s
        for s in _scan_skills_dir(state.GLOBAL_SKILLS_DIR, source="global")
        if s["name"] not in project_names
    ]

    return jsonify({"ok": True, "skills": project_skills + global_skills})


@commands_bp.route("/api/commands/<project>", methods=["GET"])
def get_commands(project):
    """Read .assist-commands.json from a project root."""
    project_path = state.PROJECTS_DIR / project
    if not project_path.is_dir():
        return jsonify({"ok": False, "error": "Project not found"}), 404
    cmd_file = project_path / ".assist-commands.json"
    if not cmd_file.exists():
        return jsonify({"ok": True, "commands": []})
    try:
        data = json.loads(cmd_file.read_text())
        return jsonify({"ok": True, "commands": data.get("commands", [])})
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@commands_bp.route("/api/commands/<project>", methods=["POST"])
def save_commands(project):
    """Save commands array to .assist-commands.json in project root."""
    project_path = state.PROJECTS_DIR / project
    if not project_path.is_dir():
        return jsonify({"ok": False, "error": "Project not found"}), 404
    data = request.get_json(silent=True) or {}
    commands = data.get("commands", [])
    cmd_file = project_path / ".assist-commands.json"
    try:
        cmd_file.write_text(json.dumps({"commands": commands}, indent=2))
        return jsonify({"ok": True})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@commands_bp.route("/api/commands/run", methods=["POST"])
def run_command():
    """Run a command in a tmux split pane alongside the main session."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    cmd = (data.get("cmd") or "").strip()
    project = (data.get("project") or "").strip()

    if not session or not cmd:
        return jsonify({"ok": False, "error": "session and cmd required"}), 400

    target_pane = f"{session}:0.1"

    subprocess.run(
        ["tmux", "kill-pane", "-t", target_pane],
        capture_output=True,
        timeout=5,
    )
    time.sleep(0.1)

    proc = subprocess.run(
        ["tmux", "split-window", "-v", "-l", "30%", "-d", "-t", f"{session}:0.0"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return (
            jsonify({"ok": False, "error": f"split-window failed: {proc.stderr}"}),
            500,
        )

    if project:
        project_path = state.PROJECTS_DIR / project
        venv = detect_venv(project_path) if project_path.is_dir() else None
        if venv:
            tmux_send_text(target_pane, f"source {project_path}/{venv}/bin/activate")
            tmux_send_keys(target_pane, "Enter")
            time.sleep(0.2)

    tmux_send_text(target_pane, cmd)
    tmux_send_keys(target_pane, "Enter")

    return jsonify({"ok": True, "target": target_pane})


@commands_bp.route("/api/commands/stop", methods=["POST"])
def stop_command():
    """Kill the split pane (pane 1) of a session."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not session:
        return jsonify({"ok": False, "error": "session required"}), 400

    target_pane = f"{session}:0.1"
    proc = subprocess.run(
        ["tmux", "kill-pane", "-t", target_pane],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return (
            jsonify({"ok": False, "error": f"kill-pane failed: {proc.stderr}"}),
            500,
        )
    return jsonify({"ok": True})


@commands_bp.route("/api/commands/pane/<session>")
def check_split_pane(session):
    """Check if split pane (pane 1) exists for a session."""
    target_pane = f"{session}:0.1"
    proc = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            target_pane,
            "-p",
            "#{pane_id}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    exists = proc.returncode == 0 and bool(proc.stdout.strip())
    return jsonify({"ok": True, "exists": exists, "target": target_pane})
