"""Origin allowlisting for HTTP and WebSocket requests.

Claude Assist is a single-user LAN tool with no auth; the browser's
same-origin policy is the only thing standing between "any website the
user visits" and command execution in a live tmux pane. These helpers
reject cross-origin requests while leaving same-origin and non-browser
(curl, no Origin header) traffic untouched.
"""

from urllib.parse import urlparse

# Origins always trusted regardless of the Host header.
ALLOWED_ORIGIN_HOSTS = {
    "assist.drift",
    "localhost",
    "127.0.0.1",
}


def origin_allowed(origin: str | None, request_host: str | None = None) -> bool:
    """Return True if a request bearing this Origin header may proceed.

    - No Origin header: allowed (same-origin GETs, curl, server-to-server).
    - Origin host in the allowlist: allowed.
    - Origin host matching the request's own Host header: allowed
      (covers direct LAN-IP access without enumerating IPs).
    """
    if not origin:
        return True
    try:
        origin_host = urlparse(origin).hostname or ""
    except ValueError:
        return False
    if origin_host in ALLOWED_ORIGIN_HOSTS:
        return True
    if request_host:
        request_hostname = request_host.rsplit(":", 1)[0] if ":" in request_host and not request_host.startswith("[") else request_host
        if origin_host == request_hostname:
            return True
    return False
