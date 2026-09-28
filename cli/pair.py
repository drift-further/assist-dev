"""`assist pair` and `assist token` — get the first phone signed in.

`pair` opens the server's existing time-boxed open-access window with the
header token, then prints the phone URL and a QR code of it. The first device
from a configured private network to load that page gets the sign-in cookie,
and the window closes behind it -- the same mechanism as More -> Access -> Open,
reachable before any browser is signed in.
"""

import ipaddress
import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from cli import http, proc, qr
from cli.expose import origin_for


def _is_loopback_origin(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def phone_url(port: int, environ=os.environ) -> str | None:
    """Where a phone reaches this install, or None if it is loopback-only.

    ASSIST_BIND (from `assist expose`) wins; otherwise the first non-loopback
    entry in ASSIST_ALLOWED_ORIGINS, which is what a proxy install sets.
    """
    bind = (environ.get("ASSIST_BIND") or "").strip()
    if bind:
        return origin_for(bind, port) + "/"
    for origin in (environ.get("ASSIST_ALLOWED_ORIGINS") or "").split(","):
        origin = origin.strip().rstrip("/")
        if origin and not _is_loopback_origin(origin):
            return origin + "/"
    return None


def qr_text(url: str) -> str:
    """`qrencode` when installed, else the built-in encoder."""
    qrencode = shutil.which("qrencode")
    if qrencode:
        try:
            completed = subprocess.run(
                [qrencode, "-t", "ANSIUTF8", "-m", "2", url],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return completed.stdout.rstrip("\n")
        except (OSError, subprocess.SubprocessError):
            pass
    return qr.render(qr.encode(url))


def command(resolved, minutes: float, url: str | None = None) -> int:
    url = url or phone_url(resolved.port)
    if url is None:
        proc.print_error(
            "Assist listens on loopback only, so a phone cannot reach it yet.\n"
            "  Run: assist expose     (or pass --url if a proxy fronts it)"
        )
        return 1
    try:
        response = http.post("/access/open", {"minutes": minutes})
    except http.ApiError as exc:
        if exc.status == 400:
            proc.print_error(
                "the server has no networks to admit from "
                "(Settings -> Access -> Allowed Networks is empty)"
            )
            return 1
        raise
    remaining = int((response.get("access") or {}).get("remaining_sec") or minutes * 60)

    print(qr_text(url))
    print()
    print(f"Open on your phone:  {url}")
    print(
        f"Sign-in window open for {max(1, remaining // 60)} min. The first device on "
        "your network to load that page is signed in, then the window closes."
    )
    print("Close it early from the web UI: More -> Access -> Close.")
    print(f"Or sign in with the token instead: assist token  ({resolved.auth_token_path})")
    return 0


def token(resolved, stream=None) -> int:
    """Print the token's path always, and its value only to a terminal."""
    stream = stream if stream is not None else sys.stdout
    path = resolved.auth_token_path
    if resolved.token is None:
        proc.print_error(f"no token yet at {path}; it is created on first start (assist start)")
        return 1
    print(f"Token file: {path}", file=stream)
    if bool(getattr(stream, "isatty", lambda: False)()):
        print(resolved.token, file=stream)
    else:
        print("(value not printed: output is not a terminal; read the file)", file=stream)
    return 0
