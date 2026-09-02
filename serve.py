"""Claude Assist — phone-first web terminal for Claude Code sessions.

App factory: imports all blueprints, registers them, starts background threads.
"""

import os
import threading

from flask import Flask, g, jsonify, redirect, request
from flask_sock import Sock

import shared.auth as auth
from shared.security import origin_allowed

os.environ.setdefault("DISPLAY", ":0")

# Strip Claude Code session markers so tmux sessions launched from here
# can run `claude` without the "nested session" error.
_CLAUDE_ENV_VARS = ("CLAUDECODE",)
for _v in _CLAUDE_ENV_VARS:
    os.environ.pop(_v, None)


def create_app():
    """Create and configure the Flask application."""
    app = Flask(__name__)
    sock = Sock(app)

    # Origin allowlist — reject cross-origin state-changing requests.
    # GET/HEAD/OPTIONS pass through (OPTIONS must work for same-origin
    # preflights; GETs gain nothing for an attacker without a readable ACAO).
    @app.before_request
    def _check_origin():
        if request.method in ("POST", "DELETE", "PATCH", "PUT"):
            if not origin_allowed(request.headers.get("Origin")):
                return jsonify({"ok": False, "error": "Origin not allowed"}), 403

    # Shared-secret gate. The Origin allowlist above only stops a hostile *page*
    # in the user's browser; it does nothing about a direct request from any
    # device that can reach the port, and /api/commands/run runs arbitrary
    # commands by design. This is what makes that unreachable without the token.
    #
    # /api/cli-proxy is exempt: containers call it from an isolated network
    # (docker/claude-mount.sh iptables-pins them to the host's :8089) and have no
    # way to hold the token. It is fail-closed on its own ASSIST_CLI_ALLOWED
    # allowlist, and nginx restricts it to the container subnet.
    # /health is a liveness probe (assist-ctl polls it to decide whether a
    # restart succeeded) and carries no data worth gating.
    # The device-approval pair is exempt for the same reason /login is: the
    # caller has no token yet. Both are fenced inside shared.auth by the LAN
    # allowlist, a pending cap, a per-IP cooldown and a single-use claim, and
    # neither can reach a command-exec surface.
    _AUTH_EXEMPT = {
        "static_bp.login",
        "poll_bp.cli_proxy",
        "poll_bp.health",
        "access_bp.access_request",
        "access_bp.access_request_status",
    }

    @app.before_request
    def _require_auth():
        if request.endpoint in _AUTH_EXEMPT:
            return None
        if auth.request_authenticated(request):
            return None
        # Temporary open access. A live window admits ONE client from a
        # configured LAN range to this one endpoint; loading the app shell is
        # what mints its cookie, and the window is spent doing it. Every API
        # route stays behind the cookie, so an open window is never itself a
        # command-exec surface.
        if (
            request.endpoint == "static_bp.index"
            and auth.is_onboarding_navigation(request)
            and auth.consume_window(
                auth.client_ip(request), request.headers.get("User-Agent", "")
            )
        ):
            g.assist_onboard = True
            return None
        # A browser navigating to a page gets the login form; anything else
        # (fetch, WebSocket handshake, curl) gets a machine-readable 401.
        if "text/html" in (request.headers.get("Accept") or ""):
            return redirect("/login")
        return jsonify({"ok": False, "error": "Authentication required"}), 401

    @app.after_request
    def _issue_onboard_cookie(response):
        # Unconditional, including on a non-200: the window is already spent,
        # so handing the cookie over anyway costs a page reload rather than a
        # second window.
        if g.get("assist_onboard"):
            auth.set_auth_cookie(response)
        return response

    # CORS — echo only fixed-allowlist Origins, never emit a wildcard.
    @app.after_request
    def _cors(response):
        origin = request.headers.get("Origin")
        if origin and origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, PATCH, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Vary"] = "Origin"
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    # Register blueprints
    from routes.static import static_bp
    from routes.access import access_bp
    from routes.input import input_bp
    from routes.terminal import terminal_bp
    from routes.git import git_bp
    from routes.commands import commands_bp
    from routes.autoyes import autoyes_bp
    from routes.automate import automate_bp
    from routes.container import container_bp
    from routes.poll import poll_bp
    from routes.completion import completion_bp
    from routes.studio import studio_bp
    from routes.tabstate import tabstate_bp
    from routes.drafts import drafts_bp

    app.register_blueprint(static_bp)
    app.register_blueprint(access_bp)
    app.register_blueprint(input_bp)
    app.register_blueprint(terminal_bp)
    app.register_blueprint(git_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(autoyes_bp)
    app.register_blueprint(automate_bp)
    app.register_blueprint(container_bp)
    app.register_blueprint(poll_bp)
    app.register_blueprint(completion_bp)
    app.register_blueprint(studio_bp)
    app.register_blueprint(tabstate_bp)
    app.register_blueprint(drafts_bp)

    from routes.settings import settings_bp, init_start_time

    app.register_blueprint(settings_bp)
    init_start_time()

    # Register WebSocket route (flask-sock requires app-level Sock, not Blueprint)
    from routes.streaming import register_streaming

    register_streaming(sock)

    return app


def start_application_backgrounds():
    """Start application work only after the process owns its listener.

    The activation-only candidate must establish its observation watermark and
    finish the old-generation snapshot without importing these modules or
    starting any Flask, AutoYes, Automate, drafts, or Studio work.
    """
    from routes.autoyes import autoyes_scanner, restore_autoyes_from_settings
    from routes.automate import automate_recover
    from routes.drafts import start_sweeper as start_drafts_sweeper
    from routes.studio import studio_refresher

    threading.Thread(target=autoyes_scanner, daemon=True).start()
    threading.Thread(target=studio_refresher, daemon=True).start()
    start_drafts_sweeper()
    restore_autoyes_from_settings()
    automate_recover()


# WSGI/flask-run compatibility retains the established import-time app.  The
# direct executable path below is separate so activation can remain observer-
# only until the controller authorises the bind.
if __name__ != "__main__":
    app = create_app()
    start_application_backgrounds()


if __name__ == "__main__":
    import argparse
    from werkzeug.serving import make_server

    parser = argparse.ArgumentParser(description="Claude Assist server")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ASSIST_PORT", "8089")),
        help="Port to listen on",
    )
    # Loopback only. nginx listens on the LAN address:port clients already use
    # and forwards here, so no client URL changes while Flask itself is
    # unreachable from the network — the blast radius of an unauthenticated
    # endpoint slipping through is the host, not the LAN. Reverting this to
    # 0.0.0.0 re-exposes every endpoint directly. The vhost lives in a separate
    # infrastructure repo; README has the equivalent server block.
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind")
    parser.add_argument("--park-handoff-fd", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    handoff = None
    if args.park_handoff_fd is not None:
        # This import is deliberately the sole pre-bind subsystem.  candidate_prebind
        # does read-only observation and blocks until old-generation termination and
        # explicit bind authorisation.
        from shared.park_activation import candidate_prebind

        handoff = candidate_prebind(args.park_handoff_fd)

    if handoff is not None:
        from shared.park_activation import ExecutionObserver

        if not ExecutionObserver.final_quiescent(handoff.snapshot):
            raise RuntimeError("park_drain_pending")

    if args.park_handoff_fd is None:
        from shared.launch_provenance import initialize_for_startup

        initialize_for_startup()

    app = create_app()
    server = make_server(args.host, args.port, app, threaded=True)
    # Binding is the activation milestone.  Publish it before recovery/scanner
    # startup, whose initial read-only sweeps may legitimately take longer than
    # the controller handshake timeout.
    if handoff is not None:
        from shared import execution_park
        from shared.park_activation import ACTIVATION_PATH

        execution_park.configure_activation(
            ACTIVATION_PATH, handoff.snapshot.get("units") or ()
        )
        execution_park.publish_parked()
        handoff.notify_bound()
    start_application_backgrounds()

    auth.print_startup_token_notice()
    server.serve_forever()
