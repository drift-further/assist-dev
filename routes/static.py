"""routes/static.py — Static file serving."""

from flask import Blueprint, send_from_directory

from shared.state import DATA_DIR

static_bp = Blueprint("static_bp", __name__)


@static_bp.route("/")
def index():
    return send_from_directory(DATA_DIR, "index.html")


@static_bp.route("/sw.js")
def serve_sw():
    response = send_from_directory(DATA_DIR, "sw.js")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@static_bp.route("/css/<path:filename>")
def serve_css(filename):
    return send_from_directory(DATA_DIR / "css", filename)


@static_bp.route("/js/<path:filename>")
def serve_js(filename):
    return send_from_directory(DATA_DIR / "js", filename)


@static_bp.route("/fonts/<path:filename>")
def serve_fonts(filename):
    return send_from_directory(
        DATA_DIR / "fonts", filename, max_age=31536000
    )  # 1 year cache — font files are immutable
