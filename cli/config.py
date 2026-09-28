"""Resolve Drift Assist paths and environment configuration."""

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$"
)
_PARAMETER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CONFIG_DIR_NAME = "drift-assist"
# Read for one release when the new directory is absent, so an install made
# before the rename keeps working until install.sh is re-run.
LEGACY_CONFIG_DIR_NAME = "claude-assist"
_legacy_notice_shown = False
# Pre-XDG defaults, read only to find a server started before the move.
# ASSIST_LEGACY_PID_FILE exists for tests; nothing else should set it.
LEGACY_PID_FILE = Path(os.environ.get("ASSIST_LEGACY_PID_FILE", "/tmp/assist-server.pid"))
LEGACY_LOG_FILE = Path("/tmp/assist-server.log")


class ConfigError(Exception):
    """A configuration error that is safe to show to the user."""


@dataclass(frozen=True)
class Config:
    home: Path
    port: int
    api: str
    python: str
    config_file: Path
    token: str | None
    pid_file: Path
    log_file: Path
    control_dir: Path
    auth_token_path: Path


def _strip_comment(value: str) -> str:
    quote = None
    escaped = False
    comment_boundary = False

    for index, character in enumerate(value):
        if escaped:
            escaped = False
            comment_boundary = False
            continue

        if quote == "'":
            if character == "'":
                quote = None
            continue

        if quote == '"':
            if character == "\\":
                escaped = True
            elif character == '"':
                quote = None
            continue

        if character == "\\":
            escaped = True
            comment_boundary = False
        elif character in ("'", '"'):
            quote = character
            comment_boundary = False
        elif character == "#" and comment_boundary:
            return value[:index].strip()
        elif character.isspace():
            comment_boundary = True
        else:
            comment_boundary = False

    return value.strip()


def _expand_parameter(value: str, index: int) -> tuple[str, int]:
    if index + 1 >= len(value):
        return "$", index + 1

    next_character = value[index + 1]
    if next_character == "{":
        closing = value.find("}", index + 2)
        if closing == -1:
            return "$", index + 1
        name = value[index + 2 : closing]
        if _PARAMETER_RE.fullmatch(name):
            return os.environ.get(name, ""), closing + 1
        return value[index : closing + 1], closing + 1

    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", value[index + 1 :])
    if match is None:
        return "$", index + 1

    name = match.group(0)
    return os.environ.get(name, ""), index + 1 + len(name)


def _decode_value(raw_value: str) -> str:
    value = _strip_comment(raw_value)
    expand_tilde = value.startswith("~")
    result = []
    quote = None
    index = 0

    while index < len(value):
        character = value[index]

        if quote == "'":
            if character == "'":
                quote = None
            else:
                result.append(character)
            index += 1
            continue

        if quote == '"':
            if character == '"':
                quote = None
                index += 1
            elif character == "\\" and index + 1 < len(value):
                result.append(value[index + 1])
                index += 2
            elif character == "$":
                expanded, index = _expand_parameter(value, index)
                result.append(expanded)
            else:
                result.append(character)
                index += 1
            continue

        if character in ("'", '"'):
            quote = character
            index += 1
        elif character == "\\" and index + 1 < len(value):
            result.append(value[index + 1])
            index += 2
        elif character == "$":
            expanded, index = _expand_parameter(value, index)
            result.append(expanded)
        else:
            result.append(character)
            index += 1

    if quote is not None:
        raise ValueError("unterminated quote")

    decoded = "".join(result)
    if expand_tilde:
        decoded = os.path.expanduser(decoded)
    return decoded


def _load_assignments(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"assist: unable to read {path}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        match = _ASSIGNMENT_RE.match(line)
        if match is None:
            continue

        name, raw_value = match.groups()
        try:
            os.environ[name] = _decode_value(raw_value)
        except ValueError as exc:
            raise ConfigError(
                f"assist: invalid assignment in {path}:{line_number}: {exc}"
            ) from exc


def user_config_file(xdg_config_home: Path) -> Path:
    """Return the user config file, falling back to the pre-rename directory.

    The new path wins whenever its directory exists. The legacy path is used
    only when the new directory is absent and the legacy file is present, and
    that is reported once per process on stderr.
    """

    global _legacy_notice_shown
    current = xdg_config_home / CONFIG_DIR_NAME / "config.env"
    legacy = xdg_config_home / LEGACY_CONFIG_DIR_NAME / "config.env"
    if current.parent.exists() or not legacy.is_file():
        return current
    if not _legacy_notice_shown:
        _legacy_notice_shown = True
        print(
            f"assist: reading legacy config {legacy}; "
            f"move it to {current.parent}/ (re-run ./install.sh)",
            file=sys.stderr,
        )
    return legacy


def state_dir_for(user_home: str) -> Path:
    """Per-user runtime state: PID file and log. assist-ctl uses the same path."""
    xdg_state_home = os.environ.get("XDG_STATE_HOME") or str(
        Path(user_home) / ".local" / "state"
    )
    return Path(xdg_state_home) / CONFIG_DIR_NAME


# ASSIST_PROC_ROOT exists for tests that exercise the non-/proc (macOS) branch.
PROC_ROOT = Path(os.environ.get("ASSIST_PROC_ROOT", "/proc"))
_PYTHON_NAME = re.compile(r"[Pp]ython(3(\.[0-9]+)?)?")


def process_argv(pid: int) -> list[str] | None:
    """argv with its boundaries intact from /proc, or None where there is no /proc."""
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.decode(errors="replace").rstrip("\0").split("\0")


def _process_command(pid: int) -> str:
    """`ps` text: argv joined with spaces. Matched whole, never split."""
    try:
        return subprocess.run(
            ["ps", "-ww", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_python(path: str) -> bool:
    return bool(_PYTHON_NAME.fullmatch(os.path.basename(path)))


def _is_script(candidate: str, home: Path) -> bool:
    scripts = {str(home / "serve.py"), str(home.resolve() / "serve.py")}
    return candidate in scripts or os.path.realpath(candidate) in scripts


def server_owner(pid: int, home: Path) -> str:
    """"ours", "foreign" or "stale" -- the same rule as assist-ctl's pid_owner.

    Ours only when the process IS this checkout's server: a Python interpreter
    as the executable and this serve.py in the script position after it. A
    reader or editor carrying the path (`tail -f <home>/serve.py`) is stale.
    """
    argv = process_argv(pid)
    if argv is not None:
        if len(argv) >= 2 and _is_python(argv[0]):
            if _is_script(argv[1], home):
                return "ours"
            if argv[1].endswith("/serve.py"):
                return "foreign"
        return "stale"
    # No /proc: every space in the joined text is a candidate boundary, since
    # either path may contain spaces.
    command = _process_command(pid)
    scripts = (str(home / "serve.py"), str(home.resolve() / "serve.py"))
    for index, character in enumerate(command):
        if character != " " or not _is_python(command[:index]):
            continue
        tail = command[index + 1 :]
        if any(tail == script or tail.startswith(script + " ") for script in scripts):
            return "ours"
        if re.match(r"(/.*?/serve\.py)( |$)", tail):  # absolute path in the script slot
            return "foreign"
    return "stale"


def runs_checkout(pid: int, home: Path) -> bool:
    return server_owner(pid, home) == "ours"


def _legacy_server_pid(home: Path) -> int | None:
    """PID in the pre-XDG /tmp PID file, only if it runs THIS checkout's serve.py."""
    try:
        pid = int(LEGACY_PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if runs_checkout(pid, home) else None


def resolve(
    script_path: Path | None = None,
    activation_expected_home: Path | None = None,
) -> Config:
    """Resolve the active Assist configuration."""

    user_home = os.environ.get("HOME")
    if not user_home:
        raise ConfigError("assist: HOME is not set")

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME") or str(
        Path(user_home) / ".config"
    )
    config_file = user_config_file(Path(xdg_config_home))
    if config_file.is_file():
        _load_assignments(config_file)

    assist_home = os.environ.get("ASSIST_HOME")
    if not assist_home:
        if script_path is None:
            home = Path(__file__).resolve().parent.parent
        else:
            home = Path(script_path).resolve().parent.parent
        assist_home = str(home)
        os.environ["ASSIST_HOME"] = assist_home

    home = Path(assist_home)
    if not home.is_dir():
        raise ConfigError(f"assist: ASSIST_HOME not found: {assist_home}")

    # Activation validates the marker-selected checkout before reading that
    # checkout's .env, token, or any controller path.  Thus a synthetic/default
    # XDG marker cannot redirect an isolated CLI into canonical configuration.
    if activation_expected_home is not None:
        try:
            actual = home.resolve(strict=True)
            expected = activation_expected_home.resolve(strict=True)
        except OSError as exc:
            raise ConfigError(f"assist: activation_home_unresolvable: {exc}") from exc
        if actual != expected:
            raise ConfigError(
                "assist: activation_home_mismatch: "
                f"invoked={expected} resolved={actual}"
            )

    env_file = home / ".env"
    if env_file.is_file():
        _load_assignments(env_file)

    port_value = os.environ.get("ASSIST_PORT") or "8089"
    try:
        port = int(port_value)
    except ValueError as exc:
        raise ConfigError(f"assist: invalid ASSIST_PORT: {port_value}") from exc

    venv_python = home / ".venv" / "bin" / "python"
    python = str(venv_python) if os.access(venv_python, os.X_OK) else "python3"

    state_dir = state_dir_for(user_home)
    pid_file = Path(os.environ.get("ASSIST_PID_FILE") or state_dir / "assist.pid")
    log_file = Path(os.environ.get("ASSIST_LOG_FILE") or state_dir / "assist.log")
    if "ASSIST_PID_FILE" not in os.environ and not pid_file.exists():
        legacy_pid = _legacy_server_pid(home)
        if legacy_pid is not None:
            # A server started before the XDG move is still running from this
            # checkout: report on it where it actually is. assist-ctl migrates
            # the PID file on its next start/stop/status.
            pid_file = LEGACY_PID_FILE
            if "ASSIST_LOG_FILE" not in os.environ:
                log_file = LEGACY_LOG_FILE
    control_dir = Path(os.environ.get("ASSIST_CONTROL_DIR", "/tmp/assist-park-v16"))
    auth_token_path = Path(
        os.environ.get("ASSIST_AUTH_TOKEN_PATH", str(home / "auth_token"))
    )

    try:
        token = auth_token_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        token = None

    return Config(
        home=home,
        port=port,
        api=f"http://127.0.0.1:{port}",
        python=python,
        config_file=config_file,
        token=token,
        pid_file=pid_file,
        log_file=log_file,
        control_dir=control_dir,
        auth_token_path=auth_token_path,
    )
