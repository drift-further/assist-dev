"""routes/static.py — Static file serving and the login page."""

from flask import Blueprint, make_response, redirect, request, send_from_directory

from shared.auth import set_auth_cookie, token_matches
from shared.state import DATA_DIR

static_bp = Blueprint("static_bp", __name__)

_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Assist // Sign in</title>
<style>
  body {{ background:#080c10; color:#00ff41; font-family:'JetBrains Mono',ui-monospace,monospace;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  form {{ width:min(92vw,340px); }}
  h1 {{ font-size:15px; color:#00d4ff; letter-spacing:1px; margin:0 0 4px; }}
  p {{ font-size:12px; color:#5a6b7a; margin:0 0 18px; line-height:1.5; }}
  input {{ width:100%; box-sizing:border-box; background:#0d141a; color:#00ff41;
           border:1px solid #1d2b36; border-radius:4px; padding:13px; font:inherit;
           font-size:16px; margin-bottom:10px; }}
  button {{ width:100%; background:transparent; color:#ff9500; border:1px solid #ff9500;
            border-radius:4px; padding:13px; font:inherit; font-size:14px; }}
  .err {{ color:#ff0040; font-size:12px; min-height:16px; margin-top:10px; }}
</style></head>
<body><form method="POST" action="/login">
  <h1>&#9671; ASSIST</h1>
  <p>Paste the token from <code>auth_token</code> in the install directory.</p>
  <input type="password" name="token" placeholder="token" autocomplete="current-password"
         autocapitalize="off" spellcheck="false" autofocus>
  <button type="submit">Sign in</button>
  <div class="err">{error}</div>
</form></body></html>"""


@static_bp.route("/login", methods=["GET", "POST"])
def login():
    """Exchange the shared secret for a long-lived cookie."""
    if request.method == "GET":
        return _LOGIN_PAGE.format(error="")
    if not token_matches(request.form.get("token")):
        return _LOGIN_PAGE.format(error="Invalid token."), 401
    return set_auth_cookie(make_response(redirect("/")))


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
