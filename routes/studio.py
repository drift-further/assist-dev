"""Studio integration — connection state, attention inbox, deep links.

All Studio I/O happens on the background refresher thread started in serve.py,
which writes an in-process snapshot. Request handlers (including /poll) only
ever READ that snapshot, so a slow or dead Studio can never stall the UI.
"""
import copy
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from routes.poll import _find_project_dir
from shared import state
from shared.studio_client import client

studio_bp = Blueprint("studio", __name__)

# Refresher cadence. Inbox is the badge's freshness budget; health is cheap but
# pointless to hammer; a disconnected Studio backs off so a dead host isn't
# probed six times a minute forever.
_INBOX_INTERVAL = 10.0
_HEALTH_INTERVAL = 30.0
_BACKOFF_INTERVAL = 60.0

# Sessions launched by `sto session start` are named studio_e<effort>_s<session>.
_SESSION_EFFORT_RE = re.compile(r"^studio_e(\d+)_s\d+$")

_snapshot = {
    "state": "unconfigured",
    "inbox": [],
    "inbox_count": 0,
    "checked_at": 0.0,
    "http_code": None,
}
_snapshot_lock = threading.Lock()
# Only one refresh may run at a time: the refresher thread and a concurrent
# /studio/test would otherwise publish their snapshots out of order.
_refresh_lock = threading.Lock()
# question_id -> time answered. An inbox fetch that began BEFORE an answer
# committed is still in flight; without this it republishes the answered
# question and resurrects the badge for a whole cycle.
_answered_recently = {}
_ANSWERED_TTL = 120.0


def _prune_answered():
    cutoff = time.time() - _ANSWERED_TTL
    for qid in [q for q, at in _answered_recently.items() if at < cutoff]:
        _answered_recently.pop(qid, None)


def _publish_inbox_locked(rows, now):
    """Store an inbox fetch into the snapshot. Caller must hold _snapshot_lock.

    Every path that publishes an inbox goes through here so the
    _answered_recently filter cannot be skipped: a fetch that began BEFORE an
    answer committed is still in flight, and publishing it raw republishes the
    answered question and resurrects its badge for a whole cycle.
    """
    _prune_answered()
    _snapshot["inbox"] = [q for q in rows if q.get("id") not in _answered_recently]
    _snapshot["inbox_count"] = len(_snapshot["inbox"])
    _snapshot["checked_at"] = now


def studio_snapshot():
    """A deep copy of the last refresher result. No I/O."""
    with _snapshot_lock:
        return copy.deepcopy(_snapshot)


def studio_poll_block(target, meta):
    """The `studio` key for /poll. Snapshot + cache reads only — no network.

    project comes from the active pane's cwd (same resolver as the ◇ deep
    link); effort comes only from a `studio_e<N>_s<M>` session name, because
    a cwd cannot tell us WHICH effort of a project a pane is working on.
    """
    snap = studio_snapshot()
    block = {
        "state": snap["state"],
        "inbox_count": snap["inbox_count"],
        "project_id": None,
        "project_name": None,
        "effort": None,
        "web_base": client.web_base(),
    }
    if snap["state"] != "connected":
        return block
    cwd = (meta or {}).get("project_dir")
    proj = _match_project(cwd, client.cached_projects()) if cwd else None
    if proj:
        block["project_id"] = proj["id"]
        block["project_name"] = proj.get("name")
    m = _SESSION_EFFORT_RE.match((target or "").split(":")[0])
    if m:
        block["effort"] = int(m.group(1))
    return block


def _explicitly_configured():
    """True when the user has actually pointed Assist at a Studio.

    The default loopback api_base is a convenience, not a configuration: a
    failed probe against it means "no Studio here" (unconfigured), not an
    outage (unreachable). A token, a web_base, or a non-default api_base are
    each an explicit act of configuration.
    """
    default_api = state.DEFAULT_SETTINGS["studio"]["api_base"].rstrip("/")
    try:
        web = str(state.get_setting("studio", "web_base") or "").strip()
    except (KeyError, TypeError):
        web = ""
    return bool(client.token()) or bool(web) or client.api_base() != default_api


def _state_for(code):
    if code == 200:
        return "connected"
    if code in (401, 403):
        return "unauthorized"
    if not client.configured() or not _explicitly_configured():
        return "unconfigured"
    return "unreachable"


def refresh_once():
    """One refresh cycle: probe, fetch the inbox, warm the projects cache.

    Returns the new connection state. Keeps the previous inbox on failure —
    stale data beats an empty panel during an outage.
    """
    with _refresh_lock:
        code = client.health()
        st = _state_for(code)
        inbox = None
        if st == "connected":
            icode, rows = client.inbox()
            if icode == 200:
                inbox = rows
            elif icode in (401, 403):
                st, code = "unauthorized", icode
            else:
                # ANY other inbox outcome (404/429/5xx/0) means the feature is
                # unusable even though /api/health answered. Falling through to
                # "connected" here would light a green state over a dead inbox.
                st, code = "unreachable", icode
            # Warm the projects cache so /poll's resolver never makes a call.
            client.projects()
        with _snapshot_lock:
            _snapshot["state"] = st
            _snapshot["http_code"] = code
            _snapshot["checked_at"] = time.time()
            if inbox is not None:
                _publish_inbox_locked(inbox, _snapshot["checked_at"])
        return st


def studio_refresher():
    """Background thread: the ONLY place Studio I/O happens on a timer."""
    next_health = 0.0
    while True:
        delay = _BACKOFF_INTERVAL
        try:
            # health is throttled to _HEALTH_INTERVAL while connected; a
            # disconnected Studio is re-probed every cycle so recovery is quick.
            now = time.time()
            with _snapshot_lock:
                connected = _snapshot["state"] == "connected"
            if connected and now < next_health:
                icode, rows = client.inbox()
                if icode == 200:
                    with _snapshot_lock:
                        # Same suppression as refresh_once(): this is the common
                        # path (every 10s while connected), so skipping the
                        # filter here would defeat it almost every time.
                        _publish_inbox_locked(rows, time.time())
                    delay = _INBOX_INTERVAL
                else:
                    delay = _INBOX_INTERVAL if refresh_once() == "connected" else _BACKOFF_INTERVAL
            else:
                next_health = now + _HEALTH_INTERVAL
                delay = _INBOX_INTERVAL if refresh_once() == "connected" else _BACKOFF_INTERVAL
        except Exception:
            delay = _BACKOFF_INTERVAL
        time.sleep(delay)


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
    web = client.web_base()
    # cached_projects(): a tap must never block on Studio. The refresher warms
    # this cache every cycle while connected, so an empty cache means we are not
    # connected — and the Studio-home fallback below is the right answer then.
    proj = _match_project(_pane_cwd(target), client.cached_projects())
    if proj:
        return jsonify({
            "url": f"{web}/#/p/{proj['id']}",
            "project_id": proj["id"],
            "project_name": proj.get("name"),
        })
    return jsonify({"url": f"{web}/#/", "project_id": None, "project_name": None})


def _drop_from_snapshot(question_id):
    """Remove an answered question so the badge clears before the next refresh.

    Also remembers the id (see _answered_recently): an inbox fetch that started
    before this answer committed is still in flight and would otherwise
    republish the question, resurrecting it and its badge for a whole cycle.
    """
    with _snapshot_lock:
        _answered_recently[question_id] = time.time()
        _snapshot["inbox"] = [q for q in _snapshot["inbox"] if q.get("id") != question_id]
        _snapshot["inbox_count"] = len(_snapshot["inbox"])


@studio_bp.route("/studio/inbox")
def studio_inbox():
    """The attention queue, straight from the snapshot (no Studio I/O here)."""
    snap = studio_snapshot()
    return jsonify({
        "ok": True,
        "state": snap["state"],
        "checked_at": snap["checked_at"],
        "web_base": client.web_base(),
        "items": snap["inbox"],
    })


@studio_bp.route("/studio/answer", methods=["POST"])
def studio_answer():
    """Answer a blocking question, or accept a ✦ recommendation.

    Server-proxied by design: the API token stays in this process and the
    browser never learns it. Origin-gated like every other write (serve.py).

    action=answer -> submit {select, text} as the human's answer
    action=accept -> re-submit the stored recommendation as the answer
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "object body required"}), 400
    qid = data.get("question_id")
    # NOT int(): bool is an int subclass and int(1.9) truncates to 1 — both would
    # cheerfully answer a different question than the caller named.
    if isinstance(qid, bool) or not isinstance(qid, int):
        return jsonify({"ok": False, "error": "question_id must be an integer"}), 400
    action = data.get("action", "answer")
    if action not in ("answer", "accept"):
        return jsonify({"ok": False, "error": "unknown action"}), 400

    item = next((q for q in studio_snapshot()["inbox"] if q.get("id") == qid), None)
    if item is None:
        return jsonify({"ok": False, "error": "question is not in the inbox"}), 404

    if action == "accept":
        # Accept is contracted for ✦ recommendations only. A pending blocking
        # question can carry a draft answer_json, and accepting that would
        # answer a question nobody proposed an answer for.
        if item.get("status") != "recommended":
            return jsonify({"ok": False, "error": "question is not a recommendation"}), 400
        answer_json = item.get("answer_json")
        if not isinstance(answer_json, dict) or not answer_json:
            return jsonify({"ok": False, "error": "no recommendation to accept"}), 400
    else:
        answer_json = {}
        select = data.get("select")
        if select is not None and not isinstance(select, str):
            return jsonify({"ok": False, "error": "select must be a string"}), 400
        if select:
            labels = item.get("options_json")
            labels = labels if isinstance(labels, list) else []
            # Mirrors the sto CLI: a label the question never offered is a loud
            # error, not a silently stored selection the SPA can't submit.
            if select not in labels:
                return jsonify({"ok": False, "error": "option not offered"}), 400
            answer_json["selections"] = [select]
        text = data.get("text")
        if text is not None and not isinstance(text, str):
            return jsonify({"ok": False, "error": "text must be a string"}), 400
        text = (text or "").strip()
        if text:
            answer_json["text"] = text
        if not answer_json:
            return jsonify({"ok": False, "error": "empty answer"}), 400

    code, record = client.answer(qid, answer_json, status="answered", actor="user")
    if code != 200:
        return jsonify({"ok": False, "error": f"Studio returned {code}"}), 502
    _drop_from_snapshot(qid)
    return jsonify({"ok": True, "question_id": qid, "record": record})


@studio_bp.route("/studio/test", methods=["POST"])
def studio_test():
    """Force a probe now — backs the Settings and connect-sheet Test buttons."""
    st = refresh_once()
    return jsonify({"ok": True, "state": st, "inbox_count": studio_snapshot()["inbox_count"]})
