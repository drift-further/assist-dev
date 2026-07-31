"""routes/tabstate.py — Tab order / pin / snooze API.

The doc also rides along on every /poll response, so these routes exist for
writes and for the first read before a poll lands.
"""

from flask import Blueprint, jsonify, request

from shared import tab_state

tabstate_bp = Blueprint("tabstate_bp", __name__)


@tabstate_bp.route("/api/tab-state")
def get_tab_state_api():
    return jsonify({"ok": True, "tab_state": tab_state.get_tab_state()})


@tabstate_bp.route("/api/tab-state", methods=["PATCH"])
def patch_tab_state_api():
    """Replace the pinned and/or order lists (session names)."""
    data = request.get_json(silent=True) or {}
    pinned = data.get("pinned")
    order = data.get("order")
    sort = data.get("sort")
    if pinned is None and order is None and sort is None:
        return jsonify({"ok": False, "error": "No data"}), 400
    for name, value in (("pinned", pinned), ("order", order)):
        if value is not None and not isinstance(value, list):
            return jsonify({"ok": False, "error": f"{name} must be a list"}), 400
    if sort is not None and sort not in tab_state.SORT_MODES:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"sort must be one of {', '.join(tab_state.SORT_MODES)}",
                }
            ),
            400,
        )
    return jsonify(
        {
            "ok": True,
            "tab_state": tab_state.set_lists(pinned=pinned, order=order, sort=sort),
        }
    )


@tabstate_bp.route("/api/tab-state/snooze", methods=["POST"])
def snooze_api():
    """Snooze or wake one target. The server stamps at/was_busy itself."""
    data = request.get_json(silent=True) or {}
    target = data.get("target")
    if not isinstance(target, str) or not target:
        return jsonify({"ok": False, "error": "target required"}), 400
    updated = tab_state.set_snooze(target, bool(data.get("on", True)))
    return jsonify({"ok": True, "tab_state": updated})


@tabstate_bp.route("/api/tab-state/import", methods=["POST"])
def import_api():
    """One-time migration of a browser's old localStorage lists.

    A no-op once the server doc holds anything, so the first device to sync
    wins and a second one cannot clobber it.
    """
    data = request.get_json(silent=True) or {}
    doc, imported = tab_state.import_legacy(
        data.get("pinned") or [],
        data.get("order") or [],
        data.get("snoozed") or [],
    )
    return jsonify({"ok": True, "imported": imported, "tab_state": doc})
