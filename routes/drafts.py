"""routes/drafts.py — Per-target composer drafts: read, save, clear, expire.

The marker set and a per-target revision stamp ride along on every /poll
(shared/drafts.py:poll_block); these routes carry the draft BODIES, which are
too big to ship every 5s to every browser. The client reads one on tab switch
and on a revision bump, and writes it debounced.
"""

import threading
import time

from flask import Blueprint, jsonify, request

from shared import drafts

drafts_bp = Blueprint("drafts_bp", __name__)


def _target_of(data):
    """Drafts are always addressed explicitly.

    Deliberately NOT shared.utils.resolve_target(): that falls back to the
    server-wide active target, and a client whose active tab has drifted from
    the server's would silently read or overwrite a DIFFERENT tab's draft.
    """
    value = data.get("target")
    if not isinstance(value, str):
        return ""
    return value.strip()


@drafts_bp.route("/api/draft")
def get_draft_api():
    target = _target_of(request.args)
    if not target:
        return jsonify({"ok": False, "error": "target required"}), 400
    return jsonify({"ok": True, "target": target, "draft": drafts.get_draft(target)})


@drafts_bp.route("/api/draft", methods=["PUT"])
def put_draft_api():
    """Partial save. Omitted fields keep their stored value."""
    data = request.get_json(silent=True) or {}
    target = _target_of(data)
    if not target:
        return jsonify({"ok": False, "error": "target required"}), 400

    text = data.get("text")
    if text is not None and not isinstance(text, str):
        return jsonify({"ok": False, "error": "text must be a string"}), 400
    attachments = data.get("attachments")
    if attachments is not None and not isinstance(attachments, list):
        return jsonify({"ok": False, "error": "attachments must be a list"}), 400
    enter_armed = data.get("enter_armed")
    if enter_armed is not None and not isinstance(enter_armed, bool):
        return jsonify({"ok": False, "error": "enter_armed must be a boolean"}), 400
    if text is None and attachments is None and enter_armed is None:
        return jsonify({"ok": False, "error": "No data"}), 400

    entry = drafts.set_draft(
        target, text=text, attachments=attachments, enter_armed=enter_armed
    )
    return jsonify({"ok": True, "target": target, "draft": entry})


@drafts_bp.route("/api/draft", methods=["DELETE"])
def delete_draft_api():
    data = request.get_json(silent=True) or {}
    target = _target_of(data) or _target_of(request.args)
    if not target:
        return jsonify({"ok": False, "error": "target required"}), 400
    drafts.delete_draft(target)
    return jsonify({"ok": True, "target": target})


def drafts_sweeper():
    """Background pass: drop expired drafts and the uploads only they held.

    Started from serve.py alongside the other daemons. Sleeps FIRST so a
    restart loop cannot turn startup into a hot sweep loop.
    """
    while True:
        time.sleep(drafts.SWEEP_INTERVAL_SEC)
        try:
            dropped, removed = drafts.sweep_expired()
            if dropped:
                print(
                    f"[drafts] expired {len(dropped)} draft(s), "
                    f"removed {len(removed)} orphaned upload(s)",
                    flush=True,
                )
        except Exception as exc:  # a sweep must never kill the daemon
            print(f"[drafts] sweep failed: {exc}", flush=True)


def start_sweeper():
    threading.Thread(target=drafts_sweeper, daemon=True).start()
