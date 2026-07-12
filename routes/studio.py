"""Studio doorway — read-only helpers linking Assist panes to the Studio design hub.

Resolves the active pane's cwd to a Studio project so the UI can deep-link into the
Studio SPA (hash route ``#/p/<id>``). Never writes to Studio; the only outbound call
is a cached read-only ``GET /api/projects`` on loopback.
"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from flask import Blueprint, jsonify, request

from routes.poll import _find_project_dir
from shared import state

studio_bp = Blueprint("studio", __name__)

# Cache the Studio project list briefly — it changes rarely and the resolver runs
# on every button tap.
_projects_cache = {"at": 0.0, "data": []}
_PROJECTS_TTL = 30.0


def _studio_setting(key, default):
    try:
        return state.get_setting("studio", key)
    except (KeyError, TypeError):
        return default


def _web_base():
    return (_studio_setting("web_base", "https://studio.drift") or "").rstrip("/")


def _api_base():
    return (_studio_setting("api_base", "http://127.0.0.1:8090") or "").rstrip("/")


def _fetch_projects():
    """Return Studio's project list ([{id, name, repo_path, ...}]), cached ~30s.

    Any failure (Studio down, timeout, bad JSON) yields [] so the caller falls back
    to the Studio home URL rather than erroring.
    """
    now = time.time()
    if now - _projects_cache["at"] < _PROJECTS_TTL:
        return _projects_cache["data"]
    data = []
    try:
        with urllib.request.urlopen(_api_base() + "/api/projects", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace")) or []
    except Exception:
        data = []
    _projects_cache["at"] = now
    _projects_cache["data"] = data
    return data


def _pane_cwd(target):
    """The active pane's cwd, or None if tmux can't be reached."""
    if not target:
        return None
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-t", target, "-p", "#{pane_current_path}"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def _match_project(cwd, projects):
    """Best Studio project for a cwd: the one whose repo_path is the cwd or an
    ancestor of it. Longest repo_path wins (most specific). None if no match."""
    if not cwd:
        return None
    try:
        cwd_real = os.path.realpath(str(_find_project_dir(Path(cwd))))
    except Exception:
        cwd_real = os.path.realpath(cwd)
    best = None
    best_len = -1
    for p in projects:
        rp = p.get("repo_path")
        if not rp:
            continue
        rp_real = os.path.realpath(rp)
        if cwd_real == rp_real or cwd_real.startswith(rp_real + os.sep):
            if len(rp_real) > best_len:
                best, best_len = p, len(rp_real)
    return best


@studio_bp.route("/studio/link")
def studio_link():
    """Resolve a pane target to the Studio URL to open.

    matched project -> {web_base}/#/p/<id>;  otherwise -> {web_base}/#/.
    Always 200 with a usable url (home is the safe fallback)."""
    target = request.args.get("target", "")
    web = _web_base()
    proj = _match_project(_pane_cwd(target), _fetch_projects())
    if proj:
        return jsonify({
            "url": f"{web}/#/p/{proj['id']}",
            "project_id": proj["id"],
            "project_name": proj.get("name"),
        })
    return jsonify({"url": f"{web}/#/", "project_id": None, "project_name": None})
