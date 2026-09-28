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
  <button type="button" id="req-btn" class="req-btn">Request device approval</button>
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
    btn.textContent = 'Request device approval';
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
<title>Sign in \u00b7 Drift Assist</title>
<style>
  body {{ background:#0e1113; color:#e7e9ea; margin:0; min-height:100vh;
         font-family:'Helvetica Neue',Helvetica,Arial,system-ui,sans-serif;
         display:grid; place-items:center; padding:20px; box-sizing:border-box;
         background-image:radial-gradient(circle at 50% 42%,#171c202b,transparent 38%); }}
  form {{ width:min(390px,100%); box-sizing:border-box; padding:28px;
          background:#111518; border:1px solid #242b32; border-radius:8px;
          box-shadow:0 24px 60px #0008; }}
  .brand {{ display:flex; align-items:center; gap:10px; margin-bottom:25px; }}
  .brand svg {{ width:32px; height:32px; color:#eb4328; flex:none; }}
  .word {{ font:500 22px 'JetBrains Mono',ui-monospace,monospace; letter-spacing:-.055em; }}
  .word span {{ color:#8d959b; font-weight:400; }}
  h1 {{ margin:0 0 8px; font-size:24px; line-height:1.25; }}
  p {{ margin:0 0 23px; color:#a8b0b6; font-size:13px; line-height:1.55; }}
  code {{ font-family:'JetBrains Mono',ui-monospace,monospace; font-size:12px; color:#c8cfd4; }}
  label {{ display:block; margin-bottom:7px; color:#c8cfd4; font-size:12px; font-weight:600; }}
  .token {{ display:flex; gap:8px; }}
  input {{ min-width:0; flex:1; height:42px; box-sizing:border-box; padding:0 11px;
           background:#0c0f11; color:#e7e9ea; border:1px solid #242b32; border-radius:6px;
           outline:0; font:16px 'JetBrains Mono',ui-monospace,monospace; }}
  input:focus {{ border-color:#3a4650; }}
  button {{ height:42px; border-radius:6px; padding:0 16px; font-size:13px; font-weight:700;
            font-family:inherit; cursor:pointer; }}
  button[type=submit] {{ background:#eb4328; border:1px solid #eb4328; color:#fff; }}
  button[type=submit]:active {{ background:#d23a21; }}
  .err {{ color:#f25767; font-size:12px; min-height:16px; margin-top:10px; }}
  .req-sep {{ display:flex; align-items:center; gap:10px; margin:14px 0 20px; color:#5f686e;
              font:10px 'JetBrains Mono',ui-monospace,monospace; text-transform:uppercase;
              letter-spacing:.1em; }}
  .req-sep:before, .req-sep:after {{ content:""; height:1px; flex:1; background:#1c2126; }}
  .req-btn {{ width:100%; background:#141819; color:#c8cfd4; border:1px solid #242b32; }}
  .req-btn:disabled {{ color:#5f686e; }}
  .req-state {{ color:#8d959b; font-size:12px; text-align:center; min-height:18px;
                margin-top:12px; line-height:1.6; }}
  .req-state.waiting {{ color:#83d7a2; }}
  .req-code {{ font:500 34px 'JetBrains Mono',ui-monospace,monospace; letter-spacing:10px;
               color:#e7e9ea; margin-bottom:6px; text-indent:10px; }}
  .first {{ margin:18px 0 0; text-align:center; font-size:12px; }}
  .foot {{ margin-top:18px; padding-top:17px; border-top:1px solid #1c2126; text-align:center;
           color:#5f686e; font:9px 'JetBrains Mono',ui-monospace,monospace; letter-spacing:.06em; }}
  @media (max-width:480px) {{
    body {{ place-items:start center; padding:36px 12px; }}
    form {{ padding:24px 20px; }}
    .token {{ flex-direction:column; }}
    .token input, .token button {{ flex:none; width:100%; }}
  }}
</style></head>
<body><form method="POST" action="/login">
  <div class="brand"><svg viewBox="0 0 64 64" aria-hidden="true"><path d="M24 8H48Q56 8 56 16V48Q56 56 48 56H24M8 18H39M8 32H39M8 46H39" fill="none" stroke="currentColor" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="39" cy="18" r="5" fill="currentColor"/><circle cx="39" cy="32" r="5" fill="currentColor"/><circle cx="39" cy="46" r="5" fill="currentColor"/></svg><span class="word">driftassist<span>.dev</span></span></div>
  <h1>Connect to Assist</h1>
  <p>Paste the access token from <code>auth_token</code> in the install directory, or request
     approval from a device that is already signed in.</p>
  <label for="token">Access token</label>
  <div class="token">
    <input type="password" id="token" name="token" placeholder="token" autocomplete="current-password"
           autocapitalize="off" spellcheck="false" autofocus>
    <button type="submit">Continue</button>
  </div>
  <div class="err">{error}</div>
  {request_ui}
  <p class="first">No device signed in yet? Run <code>assist pair</code> on the host.</p>
  <div class="foot">PRIVATE TERMINAL ACCESS</div>
</form></body></html>"""


def _login_request_ui():
    ip = client_ip(request)
    if ip_in_scope(ip):
        return _REQUEST_UI
    return (
        '<div class="req-sep">or</div>'
        '<div class="req-state">Approval requests aren\'t available from '
        "your network (" + html.escape(ip or "unknown") + ").</div>"
    )


def login_origin_refused(origin):
    """The sign-in page, saying why a POST from this origin was refused.

    Called by the Origin guard (shared/security.py) in place of its JSON 403,
    which a phone shows as a bare error page that looks like a bad token.
    """
    error = (
        "This address (<code>" + html.escape(origin or "") + "</code>) isn't in "
        "<code>ASSIST_ALLOWED_ORIGINS</code>. Add it to <code>.env</code> on the "
        "host, then run <code>assist restart</code>."
    )
    return _LOGIN_PAGE.format(error=error, request_ui=_login_request_ui()), 403


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
    request_ui = _login_request_ui()
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
