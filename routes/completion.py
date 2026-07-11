"""routes/completion.py — Inline autocomplete for @ files and / skills.

Powers the composer typeahead: as the user types `@path` or `/skill`, the
client asks here for candidates, the user picks one, and the composed string is
sent to the tmux pane verbatim — the Claude CLI running there does the actual
resolution. So these endpoints only *enumerate* candidates; they never resolve
a path or run a skill.
"""

import subprocess
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from shared.tmux import tmux_exact_target
from shared.utils import resolve_target
from routes.commands import _parse_skill_frontmatter, _scan_skills_dir

completion_bp = Blueprint("completion_bp", __name__)

_MAX_FILE_ENTRIES = 200

# Skills change rarely; re-scanning ~/.claude on every keystroke is wasteful.
_skills_cache = {"ts": 0.0, "data": None}
_SKILLS_TTL = 30.0


def _session_cwd(target):
    """Best-effort current working directory for a tmux target.

    Falls back to the configured projects dir when there is no live session
    (e.g. nothing launched yet), so `@` still has somewhere to look.
    """
    target = (target or "").strip()
    if target:
        proc = subprocess.run(
            ["tmux", "display-message", "-t", tmux_exact_target(target),
             "-p", "#{pane_current_path}"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
    return state.PROJECTS_DIR


@completion_bp.route("/complete/files")
def complete_files():
    """List immediate children of <cwd>/<dir> for @-file autocomplete.

    `dir` is the sub-path already typed after `@` (drill-in). The resolved path
    is kept inside the session cwd subtree — `..` and absolute escapes snap back
    to the cwd so the picker can't wander the whole disk from a phone.
    """
    target = resolve_target({"target": request.args.get("target", "")})
    rel = (request.args.get("dir") or "").strip().lstrip("/")

    try:
        cwd = _session_cwd(target).resolve()
    except (OSError, RuntimeError):
        cwd = state.PROJECTS_DIR

    base = (cwd / rel).resolve() if rel else cwd
    # Containment: a resolved path that escaped the cwd subtree snaps back.
    if cwd != base and cwd not in base.parents:
        base = cwd
    if not base.is_dir():
        return jsonify({"ok": True, "cwd": str(cwd), "dir": rel, "entries": []})

    try:
        entries = []
        for entry in sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."):
                continue
            entries.append({
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
            })
            if len(entries) >= _MAX_FILE_ENTRIES:
                break
    except (PermissionError, OSError) as e:
        return jsonify({"ok": False, "error": f"Permission denied: {e}"}), 403

    return jsonify({"ok": True, "cwd": str(cwd), "dir": rel, "entries": entries})


def _scan_plugin_skills():
    """Enumerate plugin skills under ~/.claude/plugins/cache as `plugin:skill`."""
    skills = []
    seen = set()
    cache = Path.home() / ".claude" / "plugins" / "cache"
    if not cache.is_dir():
        return skills
    # Layout: cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
    for skill_file in cache.glob("*/*/*/skills/*/SKILL.md"):
        try:
            plugin = skill_file.parents[3].name
            meta = _parse_skill_frontmatter(skill_file.read_text())
            name = meta.get("name")
            if not name:
                continue
            qualified = f"{plugin}:{name}"
            if qualified in seen:
                continue
            seen.add(qualified)
            skills.append({
                "name": qualified,
                "description": meta.get("description", ""),
                "source": "plugin",
            })
        except (OSError, IndexError):
            continue
    return skills


def _all_skills():
    """Merged global + plugin skills, cached for a short TTL."""
    now = time.time()
    cached = _skills_cache["data"]
    if cached is not None and (now - _skills_cache["ts"]) < _SKILLS_TTL:
        return cached
    merged = _scan_skills_dir(state.GLOBAL_SKILLS_DIR, source="global")
    names = {s["name"] for s in merged}
    for s in _scan_plugin_skills():
        if s["name"] not in names:
            names.add(s["name"])
            merged.append(s)
    merged.sort(key=lambda s: s["name"].lower())
    _skills_cache["data"] = merged
    _skills_cache["ts"] = now
    return merged


@completion_bp.route("/complete/skills")
def complete_skills():
    """Skills for /-autocomplete: global ~/.claude/skills + plugin skills.

    Optional `project` adds that project's .claude/skills. `q` prefix-filters by
    name (case-insensitive), with a substring match appended so typing the
    middle of a name still surfaces it.
    """
    q = (request.args.get("q") or "").strip().lower()
    skills = list(_all_skills())

    project = (request.args.get("project") or "").strip()
    if project:
        proj_path = state.PROJECTS_DIR / project
        if proj_path.is_dir():
            existing = {s["name"] for s in skills}
            proj_skills = [
                s for s in _scan_skills_dir(proj_path / ".claude" / "skills", source="project")
                if s["name"] not in existing
            ]
            skills = proj_skills + skills

    if q:
        prefix = [s for s in skills if s["name"].lower().startswith(q)]
        substr = [s for s in skills
                  if q in s["name"].lower() and not s["name"].lower().startswith(q)]
        skills = prefix + substr

    return jsonify({"ok": True, "skills": skills})
