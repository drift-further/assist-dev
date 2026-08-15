"""Origin allowlisting for HTTP and WebSocket requests.

Claude Assist is a single-user LAN tool with no auth; the browser's
same-origin policy is the only thing standing between "any website the
user visits" and command execution in a live tmux pane. These helpers
reject cross-origin requests while leaving same-origin and non-browser
(curl, no Origin header) traffic untouched.

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
"""

import os

# Full origins as browsers send them: scheme://host[:port].
ALLOWED_ORIGINS = {
    "http://localhost:8089",
    "http://127.0.0.1:8089",
}

for _extra in os.environ.get("ASSIST_ALLOWED_ORIGINS", "").split(","):
    _extra = _extra.strip().lower()
    if _extra:
        ALLOWED_ORIGINS.add(_extra)


def origin_allowed(origin: str | None) -> bool:
    """Return True if a request bearing this Origin header may proceed.

    - No Origin header: allowed (same-origin GETs, curl, server-to-server).
    - Origin exactly in the fixed allowlist: allowed.
    - Anything else (including "null"): rejected.
    """
    if not origin:
        return True
    return origin.strip().lower() in ALLOWED_ORIGINS
