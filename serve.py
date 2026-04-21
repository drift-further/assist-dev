"""Claude Assist — Phone voice input bridge for Claude Code terminal sessions.

App factory: imports all blueprints, registers them, starts background threads.
"""

import os
import threading

from flask import Flask
from flask_sock import Sock

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

    # Global CORS — applies to all routes across all blueprints
    @app.after_request
    def _cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
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

    app.register_blueprint(static_bp)
    app.register_blueprint(input_bp)
    app.register_blueprint(terminal_bp)
    app.register_blueprint(git_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(autoyes_bp)
    app.register_blueprint(automate_bp)
    app.register_blueprint(container_bp)
    app.register_blueprint(poll_bp)

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
    args = parser.parse_args()

    app.run(host="0.0.0.0", port=args.port)
