"""Authenticated, read-only OpenCode output for an explicitly selected pane."""

from flask import Blueprint, jsonify, request

from shared import opencode

opencode_bp = Blueprint("opencode_bp", __name__)


@opencode_bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@opencode_bp.errorhandler(opencode.OpenCodeError)
def output_error(error):
    return jsonify(ok=False, error=error.code, message=str(error)), error.status


@opencode_bp.route("/terminal/opencode/sessions")
def sessions():
    context = opencode.pane_context(request.args.get("target"))
    items = opencode.list_sessions(context)
    opencode.check_generation(opencode.pane_context(context["target"]), context["generation"])
    return jsonify(ok=True, **context, sessions=items, recent_limit=opencode.MAX_LIST_SESSIONS)


@opencode_bp.route("/terminal/opencode/transcript")
def transcript():
    context = opencode.pane_context(request.args.get("target"))
    opencode.check_generation(context, request.args.get("generation"))
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        raise opencode.OpenCodeError("invalid_limit", "Invalid message limit.", 400) from None
    if not 1 <= limit <= opencode.MAX_MESSAGES:
        raise opencode.OpenCodeError("invalid_limit", "Invalid message limit.", 400)
    result = opencode.transcript(context, request.args.get("session_id"), limit)
    opencode.check_generation(opencode.pane_context(context["target"]), context["generation"])
    return jsonify(ok=True, **context, **result)
