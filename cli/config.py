"""Resolve Claude Assist paths and environment configuration."""

import os
import re
from dataclasses import dataclass
from pathlib import Path


_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$"
)
_PARAMETER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def resolve(script_path: Path | None = None) -> Config:
    """Resolve the active Assist configuration."""

    user_home = os.environ.get("HOME")
    if not user_home:
        raise ConfigError("assist: HOME is not set")

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME") or str(
        Path(user_home) / ".config"
    )
    config_file = Path(xdg_config_home) / "claude-assist" / "config.env"
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

    try:
        token = (home / "auth_token").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        token = None

    return Config(
        home=home,
        port=port,
        api=f"http://127.0.0.1:{port}",
        python=python,
        config_file=config_file,
        token=token,
    )
