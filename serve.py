"""Claude Assist — Phone voice input bridge for Claude Code terminal sessions.

App factory: imports all blueprints, registers them, starts background threads.
"""

import os
import threading

from flask import Flask, jsonify, request
from flask_sock import Sock

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
    from routes.input import input_bp
    from routes.terminal import terminal_bp
    from routes.git import git_bp
    from routes.commands import commands_bp
    from routes.autoyes import autoyes_bp
    from routes.automate import automate_bp
    from routes.container import container_bp
    from routes.poll import poll_bp
    from routes.completion import completion_bp

    app.register_blueprint(static_bp)
    app.register_blueprint(input_bp)
    app.register_blueprint(terminal_bp)
    app.register_blueprint(git_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(autoyes_bp)
    app.register_blueprint(automate_bp)
    app.register_blueprint(container_bp)
    app.register_blueprint(poll_bp)
    app.register_blueprint(completion_bp)

    from routes.settings import settings_bp, init_start_time

    app.register_blueprint(settings_bp)
    init_start_time()

    # Register WebSocket route (flask-sock requires app-level Sock, not Blueprint)
    from routes.streaming import register_streaming

    register_streaming(sock)

    return app


# Create app at module level (needed for `flask run` and direct execution)
app = create_app()

# Start background threads at module level so they run under any deployment
# mode (direct execution, WSGI, flask run) — not just __main__.
from routes.autoyes import autoyes_scanner, restore_autoyes_from_settings  # noqa: E402
from routes.automate import automate_recover  # noqa: E402

threading.Thread(target=autoyes_scanner, daemon=True).start()
restore_autoyes_from_settings()
automate_recover()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Claude Assist server")
    parser.add_argument("--port", type=int, default=8089, help="Port to listen on")
    # Bind all interfaces: LAN clients reach Flask DIRECTLY at
    # http://<lan-ip>:8089 (nginx only serves the assist.drift hostname, not
    # a bare IP). Pass --host 127.0.0.1 to lock down to loopback — but only
    # once LAN clients are moved onto an nginx vhost that serves the IP.
    parser.add_argument("--host", default="0.0.0.0", help="Interface to bind")
    args = parser.parse_args()

    app.run(host=args.host, port=args.port)
