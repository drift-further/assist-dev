"""routes/static.py — Static file serving and the login page."""

import html

from flask import Blueprint, make_response, redirect, request, send_from_directory

from shared.auth import client_ip, ip_in_scope, set_auth_cookie, token_matches
from shared.state import DATA_DIR

static_bp = Blueprint("static_bp", __name__)

# Injected into _LOGIN_PAGE through its {request_ui} field. Kept as its own
# plain string because _LOGIN_PAGE goes through str.format, which would
# otherwise demand every brace below be doubled — str.format does not rescan
# what it substitutes in, so these braces are safe exactly here.
#
# Inline rather than a module under /js/: that directory sits behind the auth
# gate, so a logged-out browser cannot load one.
_REQUEST_UI = """
  <div class="req-sep">or</div>
  <button type="button" id="req-btn" class="req-btn">Request approval from a logged-in session</button>
  <div id="req-state" class="req-state"></div>
<script>
(function () {
  var btn = document.getElementById('req-btn');
  var out = document.getElementById('req-state');
  var timer = null, poller = null, remaining = 0;

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    if (poller) { clearInterval(poller); poller = null; }
  }
  function clock(s) {
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  function idle(msg) {
    stop();
    out.className = 'req-state';
    out.textContent = msg || '';
    btn.disabled = false;
    btn.textContent = 'Request approval from a logged-in session';
  }
  function waiting(code) {
    // The request has landed; leaving the button on "Requesting…" would read
    // as a stuck spinner for the whole five minutes.
    btn.textContent = 'Request sent';
    out.className = 'req-state waiting';
    out.textContent = '';
    var big = document.createElement('div');
    big.className = 'req-code';
    big.textContent = code;
    var msg = document.createElement('div');
    msg.textContent = 'Waiting for approval\\u2026 ';
    var cl = document.createElement('span');
    cl.id = 'req-clock';
    cl.textContent = clock(remaining);
    msg.appendChild(cl);
    out.appendChild(big);
    out.appendChild(msg);
    timer = setInterval(function () {
      remaining = Math.max(0, remaining - 1);
      var el = document.getElementById('req-clock');
      if (el) el.textContent = clock(remaining);
    }, 1000);
  }
  function poll(claim) {
    poller = setInterval(function () {
      fetch('/access/request/status?claim=' + encodeURIComponent(claim))
        .then(function (r) { return r.json(); })
        .then(function (st) {
          if (st.status === 'approved') {
            stop();
            out.className = 'req-state';
            out.textContent = 'Approved \\u2014 loading\\u2026';
            location.href = '/';
          } else if (st.status === 'denied') {
            idle('Request denied.');
          } else if (st.status === 'expired') {
            idle('No one answered. Paste the token instead.');
          } else {
            remaining = st.remaining_sec;
          }
        })
        .catch(function () { /* transient — the next tick re-syncs */ });
    }, 2000);
  }

  btn.addEventListener('click', function () {
    btn.disabled = true;
    btn.textContent = 'Requesting\\u2026';
    fetch('/access/request', { method: 'POST' })
      .then(function (r) {
        return r.json().then(function (d) { return { code: r.status, body: d }; });
      })
      .then(function (res) {
        if (res.code === 429) { idle('Too many requests. Try again in a moment.'); return; }
        if (!res.body || !res.body.ok) { idle('Could not request approval.'); return; }
        remaining = res.body.request.expires_in;
        waiting(res.body.request.code);
        poll(res.body.claim);
      })
      .catch(function () { idle('Could not reach the server.'); });
  });
})();
</script>
"""

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
  .req-sep {{ color:#5a6b7a; font-size:11px; text-align:center; margin:14px 0 10px; }}
  .req-btn {{ width:100%; background:transparent; color:#00d4ff; border:1px solid #1d2b36;
              border-radius:4px; padding:13px; font:inherit; font-size:13px; }}
  .req-btn:disabled {{ color:#5a6b7a; }}
  .req-state {{ color:#5a6b7a; font-size:12px; text-align:center; min-height:18px;
                margin-top:12px; line-height:1.6; }}
  .req-state.waiting {{ color:#00ff41; }}
  .req-code {{ font-size:34px; letter-spacing:10px; color:#00d4ff; margin-bottom:6px;
               text-indent:10px; }}
</style></head>
<body><form method="POST" action="/login">
  <h1>&#9671; ASSIST</h1>
  <p>Paste the token from <code>auth_token</code> in the install directory.</p>
  <input type="password" name="token" placeholder="token" autocomplete="current-password"
         autocapitalize="off" spellcheck="false" autofocus>
  <button type="submit">Sign in</button>
  <div class="err">{error}</div>
  {request_ui}
</form></body></html>"""


@static_bp.route("/login", methods=["GET", "POST"])
def login():
    """Exchange the shared secret for a long-lived cookie."""
    # The request button is offered only where a request could actually
    # succeed: shared.auth refuses out-of-scope callers, so showing it to an
    # off-LAN visitor would be a control that can only ever fail.
    #
    # Out of scope it says so, naming the address the server saw. Rendering
    # nothing was worse: a missing button is indistinguishable from a broken
    # deploy, and the one fact needed to tell them apart — which IP arrived —
    # is only visible here. The address is the visitor's own, so naming it
    # discloses nothing they do not already know.
    ip = client_ip(request)
    if ip_in_scope(ip):
        request_ui = _REQUEST_UI
    else:
        request_ui = (
            '<div class="req-sep">or</div>'
            '<div class="req-state">Approval requests aren\'t available from '
            "your network (" + html.escape(ip or "unknown") + ").</div>"
        )
    if request.method == "GET":
        return _LOGIN_PAGE.format(error="", request_ui=request_ui)
    if not token_matches(request.form.get("token")):
        return _LOGIN_PAGE.format(error="Invalid token.", request_ui=request_ui), 401
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


@static_bp.route("/icons/<path:filename>")
def serve_icons(filename):
    return send_from_directory(DATA_DIR / "icons", filename, max_age=31536000)
