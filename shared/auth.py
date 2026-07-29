"""Shared-secret auth for Assist.

Every endpoint here can start a process in a live tmux pane — /api/commands/run
takes an arbitrary command string by design, because the commands are ones the
user authored themselves. That makes the trust boundary, not input validation,
the thing that has to hold: anyone who can reach the port can run anything.

A single shared secret gates the whole app. It is generated on first start and
written to `auth_token` beside the code (0600, gitignored), so a fresh install
needs no configuration step — the operator reads it out of the file or the
startup log once and logs in from the phone.

The browser never stores the raw token. It gets a cookie holding an HMAC of it,
which is what the server compares against; a token presented directly (header or
query string, for curl and the container CLI proxy) is compared to the real
value. Both comparisons are constant-time.

A second way in exists for onboarding: a temporary open-access window. It is
not an auth bypass — it admits one client from a configured LAN range to `GET
/` and hands it the same cookie a token login would, then closes. Four limits
apply at once: endpoint, network, single use, and a monotonic deadline.
"""

import hmac
import ipaddress
import math
import os
import secrets
import threading
import time
from hashlib import sha256
from pathlib import Path

COOKIE_NAME = "assist_auth"
HEADER_NAME = "X-Assist-Token"

# Ten years: this is a single-user tool on a phone that must not be asked to
# re-authenticate mid-session. Revocation is deleting `auth_token` and
# restarting — every issued cookie stops matching, because the HMAC key changed.
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600

_TOKEN_PATH = Path(__file__).resolve().parent.parent / "auth_token"
_token_cache = None

# Open-access window. `_window_deadline` is a time.monotonic() value so a clock
# correction cannot extend it; None means closed. `_last_onboard` holds one
# pending report for the UI and is cleared by the first /poll that carries it.
_window_lock = threading.Lock()
_window_deadline = None
_last_onboard = None
_last_onboard_at = 0.0  # monotonic, for the TTL below
_onboard_seq = 0

# The onboard report is the only warning the operator gets that someone walked
# in, so it is published to EVERY polling client for this long and de-duplicated
# client-side by its id — never handed to whichever poll happened to arrive
# first and then destroyed. A poll that dies in flight must not be able to eat a
# security notice. It still expires, so it cannot surface an hour later out of
# context.
_ONBOARD_TTL_SEC = 300


def get_token():
    """Return the shared secret, generating and persisting it on first call."""
    global _token_cache
    if _token_cache:
        return _token_cache

    if _TOKEN_PATH.exists():
        existing = _TOKEN_PATH.read_text(encoding="utf-8").strip()
        if existing:
            _token_cache = existing
            return _token_cache

    token = secrets.token_urlsafe(32)
    # Mode at CREATE time: a plain open() would leave the secret world-readable
    # for the length of the write (same reasoning as state.atomic_write_json).
    fd = os.open(_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    _token_cache = token
    return _token_cache


def cookie_value():
    """The value a logged-in browser holds — a derivative, not the secret."""
    return hmac.new(get_token().encode(), b"assist-auth-v1", sha256).hexdigest()


def request_authenticated(request):
    """True if this request carries a valid cookie or the token itself."""
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and hmac.compare_digest(cookie, cookie_value()):
        return True
    presented = request.headers.get(HEADER_NAME) or request.args.get("token")
    if presented and hmac.compare_digest(presented, get_token()):
        return True
    return False


def token_matches(candidate):
    """Constant-time check of a token typed into the login form."""
    return bool(candidate) and hmac.compare_digest(candidate.strip(), get_token())


def set_auth_cookie(response):
    """Give this response the logged-in cookie. One definition, two callers."""
    response.set_cookie(
        COOKIE_NAME,
        cookie_value(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
    )
    return response


# ---------------------------------------------------------------------------
# Temporary open-access window
# ---------------------------------------------------------------------------


def client_ip(request):
    """The real peer address.

    nginx sets X-Real-IP with $remote_addr — an unconditional overwrite, so it
    is the actual TCP peer. X-Forwarded-For is $proxy_add_x_forwarded_for here,
    which APPENDS to whatever the client sent; its leftmost entry is
    attacker-controlled and must never be read. With no header the request came
    straight to loopback, and remote_addr (127.0.0.1) is correct and out of
    scope — a host-local process can read `auth_token` anyway.
    """
    return (request.headers.get("X-Real-IP") or request.remote_addr or "").strip()


def _networks_raw():
    from shared import state

    configured = (state.get_settings().get("access") or {}).get("open_networks")
    if configured is None:
        return state.DEFAULT_SETTINGS["access"]["open_networks"]
    return str(configured)


def open_networks():
    """Configured CIDRs, unparseable entries dropped. May be empty."""
    nets = []
    for chunk in _networks_raw().split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            continue
    return nets


def ip_in_scope(ip):
    """True if `ip` falls in a configured network. Empty config -> always False."""
    nets = open_networks()
    if not nets:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def is_onboarding_navigation(request):
    """True only for a real top-level page load of the app shell.

    Two ways in that `request.endpoint` alone does not close:

    Flask maps HEAD and OPTIONS onto the same endpoint as GET, and OPTIONS is
    additionally skipped by the Origin guard — either would spend the window
    and collect the cookie without ever rendering the app.

    Sec-Fetch-Dest is stamped by the browser and a page cannot forge it. An
    `<img src="http://assist.drift/">` on any LAN page sends `image`, a fetch
    sends `empty`, and sw.js precaching `/` sends `empty`; only an address-bar
    navigation sends `document`. Without this check a hostile or merely
    unlucky background request burns the operator's window and walks away with
    a permanent cookie for its own browser. Absent means a non-browser client
    (curl, the verification matrix), which was never the exposure.
    """
    if request.method != "GET":
        return False
    dest = request.headers.get("Sec-Fetch-Dest")
    return dest is None or dest == "document"


def open_window(seconds):
    """Start (or restart) the window. Returns the new state."""
    global _window_deadline
    with _window_lock:
        _window_deadline = time.monotonic() + seconds
    print(
        f"[assist] access window open for {int(seconds)}s "
        f"(networks: {_networks_raw()})",
        flush=True,
    )
    return window_state()


def close_window():
    """Shut the window early. Idempotent. Returns the new state."""
    global _window_deadline
    with _window_lock:
        was_open = _window_deadline is not None
        _window_deadline = None
    if was_open:
        print("[assist] access window closed", flush=True)
    return window_state()


def consume_window(ip, user_agent=""):
    """Spend the window on one in-scope client. True if this caller won it.

    The check-and-clear is a single critical section, so two devices racing
    `GET /` cannot both be admitted. The scope test sits outside the lock
    because it reads settings, not window state.
    """
    global _window_deadline, _last_onboard, _last_onboard_at, _onboard_seq
    if not ip_in_scope(ip):
        return False
    with _window_lock:
        if _window_deadline is None or time.monotonic() >= _window_deadline:
            return False
        _window_deadline = None
        _onboard_seq += 1
        _last_onboard = {
            "id": _onboard_seq,
            "ip": ip,
            "ua": user_agent,
            "at": time.time(),
        }
        _last_onboard_at = time.monotonic()
    print(f"[assist] access window consumed by {ip} ({user_agent})", flush=True)
    return True


def window_state():
    """Snapshot for /poll and the access endpoints.

    The onboard report is published to every caller until it expires, carrying
    a stable `id` so each browser toasts it exactly once. Reads never mutate
    it: at-least-once delivery plus client-side de-duplication, because the
    alternative — hand it to the first poll and clear it — loses the notice
    entirely if that poll's connection dies, or shows it on whichever tab
    happened to ask first rather than the operator's.
    """
    global _last_onboard
    with _window_lock:
        deadline = _window_deadline
        onboard = _last_onboard
        if onboard is not None and time.monotonic() - _last_onboard_at > _ONBOARD_TTL_SEC:
            _last_onboard = onboard = None
    is_open = deadline is not None and time.monotonic() < deadline
    return {
        "open": is_open,
        "remaining_sec": math.ceil(deadline - time.monotonic()) if is_open else 0,
        "networks": _networks_raw(),
        "last_onboard": onboard,
    }
