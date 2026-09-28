"""`assist expose` — let a phone on the LAN reach this host's Assist directly.

Writes two lines into the checkout's .env: ASSIST_BIND (the LAN address, a
second listener beside loopback) and that address's origin in
ASSIST_ALLOWED_ORIGINS, so the login POST from the phone is not refused as
cross-origin. Everything else in .env is left byte-for-byte alone.

nginx or Caddy in front remains the advanced path (README); this is the one
that needs no root and no second config file.
"""

import ipaddress
import os
import re
import socket
from pathlib import Path

from cli import http, proc
from cli.config import _decode_value


_ORIGINS = "ASSIST_ALLOWED_ORIGINS"
_BIND = "ASSIST_BIND"


def detect_lan_ip() -> str | None:
    """The source address of the default route. A UDP connect sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


def check_address(value: str | None) -> str:
    """Return a normalised LAN address, or raise ValueError saying why not."""
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        raise ValueError(f"not an IP address: {value!r}") from None
    if address.is_unspecified:
        raise ValueError(
            f"{address} would listen on every interface; give the LAN address instead"
        )
    if address.is_loopback:
        raise ValueError(f"{address} is loopback; Assist already listens there")
    if address.is_global:
        raise ValueError(
            f"{address} is a public address. Assist must never face the internet "
            "(see SECURITY.md); use a LAN or VPN address"
        )
    return str(address)


def origin_for(address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}:{port}"


def _assignment_re(name: str) -> re.Pattern:
    return re.compile(rf"^\s*(?:export\s+)?{name}\s*=(.*)$")


def _read_value(lines: list[str], name: str) -> str | None:
    pattern = _assignment_re(name)
    value = None
    for line in lines:
        match = pattern.match(line)
        if match:
            try:
                value = _decode_value(match.group(1))
            except ValueError:
                value = None
    return value


def _set_value(lines: list[str], name: str, value: str | None) -> list[str]:
    """Replace the last live assignment of `name` (dropping any others), or append."""
    pattern = _assignment_re(name)
    hits = [index for index, line in enumerate(lines) if pattern.match(line)]
    kept = [line for index, line in enumerate(lines) if index not in hits[:-1]]
    if hits:
        last = hits[-1] - len(hits[:-1])
        if value is None:
            del kept[last]
        else:
            kept[last] = f"{name}={value}"
    elif value is not None:
        kept.append(f"{name}={value}")
    return kept


def _load(env_file: Path) -> list[str]:
    try:
        return env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _save(env_file: Path, lines: list[str]) -> None:
    env_file.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def write_exposure(env_file: Path, address: str, port: int) -> str:
    """Set ASSIST_BIND and add the origin. Returns the origin."""
    lines = _load(env_file)
    origin = origin_for(address, port)
    current = [
        item.strip()
        for item in (_read_value(lines, _ORIGINS) or "").split(",")
        if item.strip()
    ]
    if origin not in (item.lower() for item in current):
        current.append(origin)
    lines = _set_value(lines, _BIND, address)
    lines = _set_value(lines, _ORIGINS, ",".join(current))
    _save(env_file, lines)
    return origin


def remove_exposure(env_file: Path) -> None:
    """Drop ASSIST_BIND. Origins stay: a proxy may still use them."""
    _save(env_file, _set_value(_load(env_file), _BIND, None))


def _server_running() -> bool:
    try:
        http.get("/health")
    except http.ApiError:
        return False
    return True


def _sync_environment(env_file: Path) -> None:
    """Make this process's environment match the edited .env before a restart.

    resolve() loaded the OLD .env into os.environ, and restart hands that
    environment to assist-ctl. Sourcing an .env that no longer assigns
    ASSIST_BIND does not unset an inherited one, so without this `--off`
    restarts straight back onto the LAN address.
    """
    lines = _load(env_file)
    for name in (_BIND, _ORIGINS):
        value = _read_value(lines, name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def command(resolved, ip: str | None, off: bool = False) -> int:
    env_file = resolved.home / ".env"
    if off:
        remove_exposure(env_file)
        print(f"Removed ASSIST_BIND from {env_file}.")
        address = None
    else:
        candidate = ip if ip is not None else detect_lan_ip()
        if candidate is None:
            proc.print_error("could not work out this host's LAN address; pass --ip ADDRESS")
            return 1
        try:
            address = check_address(candidate)
        except ValueError as exc:
            proc.print_error(str(exc))
            return 1
        origin = write_exposure(env_file, address, resolved.port)
        print(f"ASSIST_BIND={address}  (in {env_file})")
        print(f"Allowed origin: {origin}")
    _sync_environment(env_file)

    if _server_running():
        print("Restarting the server to apply it...")
        code = proc.restart(resolved)
        if code != 0:
            proc.print_error("the restart failed; the change in .env applies at the next start")
            return code
        applied = "Assist now listens"
    else:
        print("The server is not running; start it with: assist start")
        applied = "Assist will listen"
    if address is None:
        print(f"{applied} on loopback only.")
    else:
        print(f"{applied} on {address} as well as loopback.")
        print()
        print(f"Phone URL: {origin_for(address, resolved.port)}/")
        print("Next: assist pair   (signs the phone in without typing the token)")
    return 0
