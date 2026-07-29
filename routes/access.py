"""routes/access.py — the temporary open-access window.

Two routes, no state: `shared.auth` owns the window. Both are POST and behind
the normal auth gate, so the existing Origin allowlist gives them CSRF
protection and only an already-logged-in client can open a window.
"""

import math

from flask import Blueprint, jsonify, request

import shared.auth as auth
from shared import state

access_bp = Blueprint("access_bp", __name__)


@access_bp.route("/access/open", methods=["POST"])
def access_open():
    """Open (or restart) the window."""
    if not auth.open_networks():
        # Fail loudly rather than opening a window that can admit no one.
        return jsonify({"ok": False, "error": "no networks configured"}), 400

    cfg = state.get_settings().get("access") or {}
    data = request.get_json(silent=True) or {}
    try:
        minutes = float(data.get("minutes", cfg.get("open_default_minutes", 5)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "minutes must be a number"}), 400
    if not math.isfinite(minutes):
        return jsonify({"ok": False, "error": "minutes must be a number"}), 400

    minutes = max(1.0, min(minutes, float(cfg.get("open_max_minutes", 60))))
    return jsonify({"ok": True, "access": auth.open_window(minutes * 60)})


@access_bp.route("/access/close", methods=["POST"])
def access_close():
    """Shut the window early. Idempotent."""
    return jsonify({"ok": True, "access": auth.close_window()})
