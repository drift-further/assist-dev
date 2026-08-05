"""routes/access.py — temporary open access, and device approval requests.

Five routes over state that lives in `shared.auth`. The open/close/decide trio
is POST behind the normal auth gate, so the existing Origin allowlist gives
them CSRF protection and only an already-logged-in client can reach them.

The request/status pair is deliberately exempt from that gate — a device with
no token is exactly who calls them — and is fenced instead by the LAN
allowlist, a pending cap, a per-IP cooldown, and a secret claim that binds an
approval to the browser that asked for it.
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


@access_bp.route("/access/request", methods=["POST"])
def access_request():
    """A device on the LAN asks to be let in.

    Unauthenticated by necessity — the caller has no token, that being the
    whole point. The fence is the LAN allowlist plus the cap and cooldown in
    shared.auth, so this is no wider a surface than the open-access window
    already shipped.
    """
    result = auth.create_request(
        auth.client_ip(request), request.headers.get("User-Agent", "")
    )
    if not result["ok"]:
        status = 403 if result["error"] == "out_of_scope" else 429
        return jsonify(result), status
    return jsonify(result)


@access_bp.route("/access/request/status", methods=["GET"])
def access_request_status():
    """The waiting device polls here. Approval hands over the cookie."""
    status = auth.request_status(
        request.args.get("claim", ""), auth.client_ip(request)
    )
    response = jsonify({"ok": True, **status})
    if status["status"] == "approved":
        auth.set_auth_cookie(response)
    return response


@access_bp.route("/access/decide", methods=["POST"])
def access_decide():
    """A logged-in session's verdict on a pending request."""
    data = request.get_json(silent=True) or {}
    try:
        rid = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "id must be an integer"}), 400
    action = data.get("action")
    if action not in ("approve", "deny"):
        return jsonify({"ok": False, "error": "action must be approve or deny"}), 400
    moved = auth.decide_request(rid, action == "approve")
    return jsonify({"ok": moved, "access": auth.window_state()})
