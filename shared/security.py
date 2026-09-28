"""Origin allowlisting for HTTP and WebSocket requests.

Drift Assist is a single-owner LAN tool protected by a shared secret. Origin
checking is an independent browser-side defense against a hostile website
causing command execution with an authenticated browser session. These helpers
reject cross-origin requests while leaving same-origin and non-browser (curl,
no Origin header) traffic untouched.

The allowlist is a FIXED set of full origins (scheme + host + port).
Matching the request's own Host header was removed on purpose: DNS
rebinding lets an attacker's domain resolve to this host, making
Origin == Host true for a hostile page. Enumerating the real origins
keeps LAN access working while still rejecting an attacker's own origin —
a rebound evil.com page still sends Origin: http://evil.com, which is not
in this set.

Only loopback ships built in. The hostname and LAN address a browser
actually uses are per-install facts, not properties of this program, so
they belong in .env via ASSIST_ALLOWED_ORIGINS (comma-separated full
origins):

    ASSIST_ALLOWED_ORIGINS=http://assist.example.lan,http://192.0.2.10:8089

Every browser origin that reaches Flask needs an entry — the LAN address
when clients hit it directly, and every extra hostname that resolves here.
A missing name 403s POSTs while GETs keep working, which looks like a
broken deploy rather than a policy decision, so add all of them at once.

The same list also fences the Host header. Every hostname named there, plus
loopback, may arrive in Host; any other name gets a 421. That closes DNS
rebinding for GETs too, which the Origin check alone cannot: a rebound page's
same-origin GETs carry no Origin, but they do carry the attacker's name.
"""

import os
from urllib.parse import urlsplit

from flask import jsonify, request

# Names that only ever mean this host. A browser cannot be made to send one of
# these for an attacker's page, so they pass the Host check on any port.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Full origins as browsers send them: scheme://host[:port]. Rebuilt by
# configure(); loopback follows ASSIST_PORT, because a user who moves off 8089
# opens http://localhost:<their port> and must not be refused at login.
ALLOWED_ORIGINS = set()

# Bare hostnames a request may carry in Host: loopback plus every host named in
# ASSIST_ALLOWED_ORIGINS. Hostname only, no port — the reference nginx forwards
# `Host $host`, which drops the port, and the port is not what rebinding varies.
ALLOWED_HOSTS = set()


def _hostname(value):
    """Lower-case hostname of a Host header or origin netloc; None if malformed."""
    try:
        parts = urlsplit("//" + value.strip())
        name = parts.hostname
        parts.port  # raises ValueError on a non-numeric port
    except ValueError:
        return None
    if not name or any(ch.isspace() for ch in value.strip()):
        return None
    return name.rstrip(".").lower() or None


def configure(port=None):
    """Build the origin and Host allowlists from the environment.

    Runs at import with ASSIST_PORT; serve.py calls it again with the port it
    actually binds, since `--port` can differ from the environment.
    """
    if port is None:
        port = os.environ.get("ASSIST_PORT") or "8089"
    origins = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    for extra in os.environ.get("ASSIST_ALLOWED_ORIGINS", "").split(","):
        extra = extra.strip().lower()
        if extra:
            origins.add(extra)
    hosts = set(LOOPBACK_HOSTS)
    for origin in origins:
        name = _hostname(urlsplit(origin).netloc)
        if name:
            hosts.add(name)
    ALLOWED_ORIGINS.clear()
    ALLOWED_ORIGINS.update(origins)
    ALLOWED_HOSTS.clear()
    ALLOWED_HOSTS.update(hosts)


configure()


def origin_allowed(origin: str | None) -> bool:
    """Return True if a request bearing this Origin header may proceed.

    - No Origin header: allowed (same-origin GETs, curl, server-to-server).
    - Origin exactly in the fixed allowlist: allowed.
    - Anything else (including "null"): rejected.
    """
    if not origin:
        return True
    return origin.strip().lower() in ALLOWED_ORIGINS


def host_allowed(host: str | None) -> bool:
    """True when the Host header names this install (see ALLOWED_HOSTS)."""
    if not host:
        return False
    name = _hostname(host)
    return name is not None and name in ALLOWED_HOSTS


# Neither is browser data worth rebinding for: /health returns a constant, and
# the CLI proxy is POST-only (the Origin check still applies) and is called by
# containers under whatever address their network reaches the host by.
_HOST_EXEMPT = {"poll_bp.health", "poll_bp.cli_proxy"}


def register_request_guards(app):
    """Install the Host and Origin checks, ahead of the auth gate."""

    @app.before_request
    def _check_host():
        if request.endpoint in _HOST_EXEMPT:
            return None
        host = request.headers.get("Host", "")
        if host_allowed(host):
            return None
        name = _hostname(host) or host
        body = (
            f"421 Misdirected Request: this Drift Assist does not answer to "
            f"the host name {name!r}.\n\n"
            f"If that is the address you use for it, add its origin (for "
            f"example http://{name}) to ASSIST_ALLOWED_ORIGINS in .env, then "
            f"run `assist restart`.\n"
        )
        return body, 421, {"Content-Type": "text/plain; charset=utf-8"}

    # Origin allowlist — reject cross-origin state-changing requests.
    # GET/HEAD/OPTIONS pass through (OPTIONS must work for same-origin
    # preflights; GETs gain nothing for an attacker without a readable ACAO).
    @app.before_request
    def _check_origin():
        if request.method in ("POST", "DELETE", "PATCH", "PUT"):
            origin = request.headers.get("Origin")
            if not origin_allowed(origin):
                # A browser posting the sign-in form would otherwise show bare
                # JSON, and the user blames their token. Say what to change.
                if request.endpoint == "static_bp.login":
                    from routes.static import login_origin_refused

                    return login_origin_refused(origin)
                return jsonify({"ok": False, "error": "Origin not allowed"}), 403
