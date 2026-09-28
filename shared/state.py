"""shared/state.py — Centralized mutable state for all Assist blueprints.

Every module-level variable that is mutated at runtime lives here.
Import with: from shared.state import <name>
"""

import copy
import json
import logging
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# OpenCode export reader: bounded, short-lived snapshots shared by viewers.
opencode_lock = threading.Lock()
opencode_slots = threading.BoundedSemaphore(2)
opencode_cache = {}

# ---------------------------------------------------------------------------
# Claude launch mode
# ---------------------------------------------------------------------------
# Valid launch modes. The mode -> command mapping itself lives in js/state.js
# (CLAUDE_COMMANDS), which is what actually builds the launch string; this dict
# is what the legacy claude_mode.txt migration in load_settings() validates
# against. Keep the two in step.
#
# This MUST stay above load_settings(), which runs at import time and reads it:
# defined below that call, a legacy claude_mode.txt made startup raise NameError.
CLAUDE_COMMANDS = {
    "npx": "npx @anthropic-ai/claude-code",
    "claude": "claude",
}

# ---------------------------------------------------------------------------
# Settings — single source of truth for all configuration
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "server": {
        "claude_mode": "claude",
        "session_init_cmd": os.environ.get("ASSIST_SESSION_INIT_CMD", ""),
        # Source a project's virtualenv in every pane Assist opens in that
        # project. "on"/"off" as a string, not a bool: the settings panel's
        # _renderToggle compares against its option strings -- same shape as
        # autoyes.all_sessions and ui.idle_tab_tucking. Default on, because
        # activation was unconditional until a restructure dropped it from the
        # launch and duplicate paths and a fresh terminal stopped picking up
        # .venv while the UI kept showing the venv badge.
        "venv_auto_activate": "on",
        "projects_dir": str(
            Path(
                os.environ.get("ASSIST_PROJECTS_DIR", Path.home() / "projects")
            )
        ),
        "restart_cmd": "assist restart",
    },
    "terminal": {
        "font_size": 13,
        "default_cols": 200,
        "default_rows": 60,
        "capture_lines": 2000,
        "tmux_history_limit": 20000,
        "idle_threshold_sec": 300,
    },
    "autoyes": {
        "default_delay": 5,
        "detection_depth": 8,
        # Master switch, "on"/"off" (a string, not a bool: the settings panel's
        # _renderToggle compares against its option strings — same shape as
        # ui.idle_tab_tucking). When on, every AGENT pane in every tmux session
        # is armed at default_delay, including sessions created later; a session
        # toggled off while this is on records autoyes.global_opt_out and stays
        # off. When off, enablement is per-session exactly as before.
        "all_sessions": "off",
    },
    "connection": {
        "poll_interval_ms": 5000,
        "ws_heartbeat_sec": 3,
        "ws_reconnect_max_ms": 30000,
        "http_fallback_poll_ms": 3000,
    },
    "ui": {
        "toast_duration_ms": 8000,
        "max_toasts": 3,
        "stale_tab_threshold_sec": 3600,
        # "off" keeps every pane in the strip and reserves the zZ sheet for
        # panes snoozed by hand. Toggled from the tab pull-out's header.
        "idle_tab_tucking": "on",
        "recent_projects_limit": 20,
        # Sudo and question popups (js/prompt-popup.js). "off" keeps each one up
        # until it is answered, closed, or its prompt goes away.
        "popup_autohide": "on",
        "popup_seconds": 5,
    },
    "limits": {
        "max_history": 2500,
        "max_upload_mb": 2048,
        "max_capture_lines": 20000,
    },
    "studio": {
        # web_base: what the BROWSER opens (the Studio SPA). Empty means
        # "derive from api_base" — Studio serves its SPA on the same origin as
        # its API, so a self-hosted or hosted install needs one URL, not two.
        # Host-neutral by default: a fresh clone must not point at anyone's Studio.
        "web_base": "",
        # api_base: server->server only. Loopback default suits a Studio running
        # beside Assist; a hosted/self-hosted Studio is an https URL.
        "api_base": "http://127.0.0.1:8090",
        # api_token: sent as `Authorization: Bearer` when non-empty. SERVER-SIDE
        # ONLY — masked in GET /api/settings, never shipped to the browser.
        "api_token": "",
    },
    "access": {
        # Networks allowed to onboard — both the open-access window and the
        # device-approval request check this. Comma-separated CIDRs; an
        # unparseable entry is dropped, and an empty list means nobody can
        # onboard by either route.
        #
        # All private ranges rather than one LAN: a VPN client is an
        # authenticated member of the network but is handed an address from
        # whatever pool its concentrator uses, which is frequently not the
        # LAN's. Public addresses stay refused, and this is a second fence
        # regardless — Flask binds loopback, nginx listens on the LAN address,
        # and neither route hands over anything without a human approving it.
        # CGNAT (100.64/10) is included because Tailscale and carrier NAT
        # both live there.
        "open_networks": (
            "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fd00::/8"
        ),
        "open_default_minutes": 5,
        "open_max_minutes": 60,
        # Device approval requests — the pull half of onboarding. A device on
        # `open_networks` asks to be let in and a logged-in session approves
        # it, so the allowlist above is shared rather than duplicated: one
        # place to widen or narrow the trust boundary.
        "request_ttl_minutes": 5,
        "request_max_pending": 3,
        "request_cooldown_sec": 30,
    },
}

_settings = {}
_settings_lock = threading.Lock()
SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

# ---------------------------------------------------------------------------
# Per-project settings — overrides global defaults on a per-project basis
# ---------------------------------------------------------------------------
PROJECT_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "project_settings.json"
_project_settings = {}
_project_settings_lock = threading.Lock()

DEFAULT_PROJECT_SETTINGS = {
    "autoyes": {
        "delay": 5,
        "enabled_default": False,
        # Opt-out from the all_sessions switch. Deliberately independent of
        # enabled_default — each is authoritative in exactly one regime (switch
        # off -> enabled_default; switch on -> not global_opt_out), so neither
        # has to encode the other's history.
        "global_opt_out": False,
        # A session armed by hand answers prompts in its agent panes only, as
        # the all_sessions switch always has. True also lets it answer plain
        # shell panes: apt, ssh host keys and any (y/n) that shell prints.
        "shell_ok": False,
    },
    "automate": {
        "default_prompt": "",
        "timeout": 10,
        "continuous": True,
        "max_iterations": 0,
        "stop_after": "",
    },
    "triggers": {
        "done_signals": [],
        "done_idle_sec": 60,
        "trust_auto_approve": True,
        "relaunch_wait_sec": 30,
    },
    "packages": {
        "pip": [],
    },
}


# Bounds the settings panel already enforces (the min/max on each field in
# js/settings.js), mirrored so the API and CLI meet the same limits. Enforced on
# PATCH only: a hand edit outside them is deliberate, and load checks type.
SETTING_RANGES = {
    "terminal.font_size": (8, 24),
    "terminal.default_cols": (40, 400),
    "terminal.default_rows": (10, 200),
    "terminal.capture_lines": (100, 50000),
    "terminal.tmux_history_limit": (1000, 100000),
    "terminal.idle_threshold_sec": (30, 3600),
    "autoyes.default_delay": (0.1, 30),
    "autoyes.detection_depth": (2, 30),
    "connection.poll_interval_ms": (1000, 30000),
    "connection.ws_heartbeat_sec": (1, 30),
    "connection.ws_reconnect_max_ms": (5000, 120000),
    "connection.http_fallback_poll_ms": (1000, 30000),
    "ui.toast_duration_ms": (2000, 30000),
    "ui.max_toasts": (1, 10),
    "ui.stale_tab_threshold_sec": (300, 86400),
    "ui.recent_projects_limit": (5, 100),
    "ui.popup_seconds": (1, 60),
    "limits.max_history": (100, 50000),
    "limits.max_upload_mb": (1, 4096),
    "limits.max_capture_lines": (1000, 100000),
    "access.open_default_minutes": (1, 1440),
    "access.open_max_minutes": (1, 1440),
    # Per-project (DEFAULT_PROJECT_SETTINGS); same clamp as the runtime delay.
    "autoyes.delay": (0.1, 30),
}

# Numeric settings whose integer default still admits a fraction.
_FRACTIONAL_SETTINGS = frozenset({"autoyes.default_delay", "autoyes.delay"})
# Opened by the browser (window.open / href), so a scheme other than http(s)
# would be script in this origin. api_base too: an empty web_base derives from
# it (shared/studio_client.py:web_base).
_URL_SETTINGS = frozenset({"studio.web_base", "studio.api_base"})


def _is_http_url(value):
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def check_setting(path, value, default, enforce_range=True):
    """Return (ok, value) for one settings leaf judged against its default.

    The value must have the default's type: bool for bool, a finite
    non-negative number for a number (whole unless the key is fractional; a
    whole float is normalised to int), a string for a string, a list of strings
    for a list. "on"/"off" toggles, claude_mode and web_base take only their
    legal values. enforce_range adds SETTING_RANGES.
    """
    if isinstance(default, bool):
        return isinstance(value, bool), value
    if isinstance(default, (int, float)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return False, value
        if isinstance(default, int) and path not in _FRACTIONAL_SETTINGS:
            if value != int(value):
                return False, value
            value = int(value)
        if enforce_range and path in SETTING_RANGES:
            low, high = SETTING_RANGES[path]
            if not low <= value <= high:
                return False, value
        return True, value
    if isinstance(default, str):
        if not isinstance(value, str):
            return False, value
        if default in ("on", "off"):
            return value in ("on", "off"), value
        if path == "server.claude_mode":
            return value in CLAUDE_COMMANDS, value
        if path in _URL_SETTINGS:
            return (value == "" or _is_http_url(value)), value
        return True, value
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value), value
    return True, value


def _sanitize(values, defaults, prefix="", drop=False):
    """Reset (or, with drop, remove) each saved leaf check_setting refuses.

    Works in place, key by key, so one bad value costs only itself. Returns the
    dotted paths that were replaced.
    """
    bad = []
    for key, default in defaults.items():
        if key not in values:
            continue
        path = prefix + key
        value = values[key]
        if isinstance(default, dict):
            if isinstance(value, dict):
                bad.extend(_sanitize(value, default, path + ".", drop))
                continue
            ok = False
        else:
            ok, value = check_setting(path, value, default, enforce_range=False)
        if ok:
            values[key] = value
            continue
        bad.append(path)
        if drop:
            del values[key]
        else:
            values[key] = copy.deepcopy(default)
    return bad


def read_json_state(path, default, expect=None):
    """Load a JSON state file; a file that does not parse is moved aside.

    Missing: `default`. Unparseable, or not of type `expect`: renamed to
    `<name>.corrupt-<timestamp>`, logged at ERROR, and `default` returned — so
    the next save writes a fresh file instead of destroying the only copy of
    what was there (a truncated settings.json used to lose the Studio token on
    the first PATCH).
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text())
        if expect is not None and not isinstance(data, expect):
            raise ValueError(
                f"top level is {type(data).__name__}, expected {expect.__name__}"
            )
    except FileNotFoundError:
        return default
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError included
        _quarantine(path, exc)
        return default
    except OSError as exc:
        log.error("state: cannot read %s (%s); using defaults", path, exc)
        return default
    return data


def _quarantine(path, reason):
    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
    try:
        path.rename(dest)
    except OSError as exc:
        log.error(
            "state: %s is unreadable (%s) and could NOT be moved aside (%s); "
            "the next save will overwrite it — copy it out by hand now",
            path, reason, exc,
        )
        return
    log.error(
        "state: %s is unreadable (%s); moved it to %s and started from "
        "defaults. Restore or repair it by hand.",
        path, reason, dest.name,
    )


def _deep_merge(base, override):
    """Merge override into base recursively. Returns new dict."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def atomic_write_json(path, data, indent=2):
    """Write JSON to *path* atomically: tmp file + flush + fsync + os.replace.

    A crash mid-write leaves the old file intact instead of torn JSON
    (which loaders silently swallow, falling back to defaults).
    """
    path = Path(path)
    # settings.json now holds the Studio API token; every file written through
    # this helper is single-user state, so 0600 across the board. The mode is set
    # before content is written, not after it.
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(data, f, indent=indent)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def load_settings():
    """Load settings from disk, merge with defaults. Call once at startup."""
    global _settings
    saved = read_json_state(SETTINGS_FILE, {}, expect=dict)
    merged = _deep_merge(copy.deepcopy(DEFAULT_SETTINGS), saved)
    bad = _sanitize(merged, DEFAULT_SETTINGS)
    if bad:
        log.warning(
            "state: %s: invalid values reset to defaults: %s",
            SETTINGS_FILE, ", ".join(bad),
        )
    with _settings_lock:
        _settings = merged

    # One-time migration: claude_mode.txt -> settings.json
    _mode_file = DATA_DIR / "claude_mode.txt"
    if _mode_file.exists():
        try:
            mode = _mode_file.read_text().strip()
            if mode in CLAUDE_COMMANDS:
                _settings["server"]["claude_mode"] = mode
            _mode_file.unlink()
            save_settings()
        except OSError:
            pass

    _apply_settings()


def _save_settings_locked():
    """Write settings to disk. Caller must hold _settings_lock."""
    try:
        atomic_write_json(SETTINGS_FILE, _settings)
    except OSError:
        pass


def save_settings():
    """Persist current settings to disk."""
    with _settings_lock:
        _save_settings_locked()


def get_settings():
    """Return full settings dict (deep copy)."""
    with _settings_lock:
        return copy.deepcopy(_settings)


def patch_settings(patch):
    """Deep-merge patch into settings, save, and re-apply. Returns updated settings.

    The file write happens while still holding the lock so two overlapping
    patches cannot persist an older snapshot last.
    """
    global _settings
    with _settings_lock:
        _settings = _deep_merge(_settings, patch)
        _save_settings_locked()
        result = copy.deepcopy(_settings)
    _apply_settings()
    return result


def get_setting(*keys):
    """Get a nested setting value. e.g. get_setting('terminal', 'font_size')"""
    with _settings_lock:
        val = _settings
        for k in keys:
            val = val[k]
        return val


# ---------------------------------------------------------------------------
# Per-project settings functions
# ---------------------------------------------------------------------------
def load_project_settings():
    """Load per-project settings from disk. Call once at startup."""
    global _project_settings
    data = read_json_state(PROJECT_SETTINGS_FILE, {}, expect=dict)
    bad = []
    for project in list(data):
        if not isinstance(data[project], dict):
            del data[project]
            bad.append(project)
            continue
        bad.extend(
            f"{project}.{path}"
            for path in _sanitize(data[project], DEFAULT_PROJECT_SETTINGS, drop=True)
        )
    if bad:
        log.warning(
            "state: %s: invalid values dropped (defaults apply): %s",
            PROJECT_SETTINGS_FILE, ", ".join(bad),
        )
    _project_settings = data


def _save_project_settings_locked():
    """Write project settings to disk. Caller must hold _project_settings_lock."""
    try:
        atomic_write_json(PROJECT_SETTINGS_FILE, _project_settings)
    except OSError:
        pass


def rename_project_settings(old, new):
    """Move a session's overrides to its new name (routes/terminal.py:on_session_renamed).

    Keyed by session name, so a rename used to strand them: an Auto-Yes opt-out
    stayed behind under the old name while the global switch armed the new one.
    Moved, not copied, so a later session reusing the old name starts clean.
    """
    if not old or not new or old == new:
        return
    with _project_settings_lock:
        if old not in _project_settings:
            return
        _project_settings[new] = _project_settings.pop(old)
        _save_project_settings_locked()


def get_project_settings(project):
    """Return merged defaults + project overrides (deep copy)."""
    with _project_settings_lock:
        overrides = copy.deepcopy(_project_settings.get(project, {}))
    return _deep_merge(copy.deepcopy(DEFAULT_PROJECT_SETTINGS), overrides)


def patch_project_settings(project, patch):
    """Deep-merge patch into project settings, save under lock, return updated."""
    global _project_settings
    with _project_settings_lock:
        current = _project_settings.get(project, {})
        _project_settings[project] = _deep_merge(current, patch)
        _save_project_settings_locked()
        result = _deep_merge(
            copy.deepcopy(DEFAULT_PROJECT_SETTINGS),
            copy.deepcopy(_project_settings[project]),
        )
    return result


def get_project_setting(project, *keys):
    """Get a nested project setting. Falls through to defaults."""
    settings = get_project_settings(project)
    val = settings
    for k in keys:
        val = val[k]
    return val


# ---------------------------------------------------------------------------
# Paths (non-configurable)
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent  # assist/ directory
HISTORY_FILE = DATA_DIR / "history.json"
FAVORITES_FILE = DATA_DIR / "favorites.json"
GLOBAL_SKILLS_DIR = Path(
    os.environ.get("ASSIST_SKILLS_DIR", Path.home() / ".claude" / "skills")
)


# ---------------------------------------------------------------------------
# Container config — global container build/runtime settings
# ---------------------------------------------------------------------------
CONTAINER_CONFIG_FILE = DATA_DIR / "container_config.json"
_container_config = {}
_container_config_lock = threading.Lock()

CONTAINER_IMAGE_NAME = "drift-assist-container"
# The pre-rename default. Status reports it for one release while it is the
# only image built; a build always writes CONTAINER_IMAGE_NAME.
LEGACY_CONTAINER_IMAGE_NAME = "claude-assist-container"

DEFAULT_CONTAINER_CONFIG = {
    "base": {
        "node_version": "20",
        "python_version": "3",
        "claude_version": "latest",
    },
    "resources": {
        "memory": "16g",
        "cpus": "4",
        "pids_limit": 512,
    },
    "network": {
        "bind_address": "127.0.0.1",
        "allow_lan": False,
        "allow_ports": [5432, 8089],
        "gateway_host": "",
    },
    "packages": {
        "pip": [],
        "system": [],
    },
    "image": {
        "name": CONTAINER_IMAGE_NAME,
        "built_at": None,
        "build_hash": None,
    },
    "cli_proxy": {
        "enabled": False,
        "container_command": "",
    },
}


def load_container_config():
    global _container_config
    _container_config = read_json_state(CONTAINER_CONFIG_FILE, {}, expect=dict)


def _save_container_config_locked():
    """Write container config to disk. Caller must hold _container_config_lock."""
    try:
        atomic_write_json(CONTAINER_CONFIG_FILE, _container_config)
    except OSError:
        pass


def get_container_config():
    """Return merged defaults + saved config (deep copy)."""
    with _container_config_lock:
        saved = copy.deepcopy(_container_config)
    return _deep_merge(copy.deepcopy(DEFAULT_CONTAINER_CONFIG), saved)


def patch_container_config(patch):
    global _container_config
    with _container_config_lock:
        _container_config = _deep_merge(_container_config, patch)
        _save_container_config_locked()
        result = _deep_merge(
            copy.deepcopy(DEFAULT_CONTAINER_CONFIG), copy.deepcopy(_container_config)
        )
    return result


# ---------------------------------------------------------------------------
# Extensions — registered SDK/tool bundles for the container
# ---------------------------------------------------------------------------
EXTENSIONS_FILE = DATA_DIR / "extensions.json"
BUILTIN_EXTENSIONS_DIR = (
    Path(__file__).resolve().parent.parent / "docker" / "extensions"
)
_extensions = []
_extensions_lock = threading.Lock()


def load_extensions():
    global _extensions
    _extensions = read_json_state(EXTENSIONS_FILE, [], expect=list)
    if not _extensions and BUILTIN_EXTENSIONS_DIR.is_dir():
        for f in sorted(BUILTIN_EXTENSIONS_DIR.glob("*.json")):
            try:
                ext = json.loads(f.read_text())
                _extensions.append(ext)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if _extensions:
            save_extensions()


def _save_extensions_locked():
    """Write extensions to disk. Caller must hold _extensions_lock."""
    try:
        atomic_write_json(EXTENSIONS_FILE, _extensions)
    except OSError:
        pass


def save_extensions():
    with _extensions_lock:
        _save_extensions_locked()


def get_extensions():
    with _extensions_lock:
        return copy.deepcopy(_extensions)


def add_extension(ext):
    with _extensions_lock:
        _extensions.append(ext)
        _save_extensions_locked()
    return get_extensions()


def update_extension(ext_id, patch):
    with _extensions_lock:
        for ext in _extensions:
            if ext.get("id") == ext_id:
                ext.update(patch)
                break
        _save_extensions_locked()
    return get_extensions()


def delete_extension(ext_id):
    with _extensions_lock:
        _extensions[:] = [e for e in _extensions if e.get("id") != ext_id]
        _save_extensions_locked()
    return get_extensions()


def _apply_settings():
    """Update module-level convenience vars from current settings. Called after load/patch."""
    global MAX_HISTORY, MAX_UPLOAD_SIZE, PROJECTS_DIR, AUTOYES_DELAY
    global WS_HEARTBEAT_INTERVAL
    s = _settings
    MAX_HISTORY = s["limits"]["max_history"]
    MAX_UPLOAD_SIZE = s["limits"]["max_upload_mb"] * 1024 * 1024
    PROJECTS_DIR = Path(s["server"]["projects_dir"])
    AUTOYES_DELAY = s["autoyes"]["default_delay"]
    WS_HEARTBEAT_INTERVAL = s["connection"]["ws_heartbeat_sec"]


# Initialize with defaults until load_settings() is called
MAX_HISTORY = DEFAULT_SETTINGS["limits"]["max_history"]
MAX_UPLOAD_SIZE = DEFAULT_SETTINGS["limits"]["max_upload_mb"] * 1024 * 1024
PROJECTS_DIR = Path(DEFAULT_SETTINGS["server"]["projects_dir"])

# ---------------------------------------------------------------------------
# tmux target — convenience default for single-client use.
# Multi-client safety: input endpoints accept a "target" field in the
# request body which takes precedence over this global.
# ---------------------------------------------------------------------------
tmux_target = None

# ---------------------------------------------------------------------------
# Auto-yes state
# ---------------------------------------------------------------------------
autoyes_sessions = {}  # session_name -> True/False
autoyes_lock = threading.Lock()
autoyes_countdowns = (
    {}
)  # target -> { "prompt_hash", "deadline", "delay", "prompt_type", ... }
# target -> {(prompt_hash, prompt_type), ...}: prompts the human cancelled. Kept
# apart from the countdown, which a tick can drop, and cleared only when the
# scanner sees no prompt on that pane.
autoyes_cancelled = {}
autoyes_answered = {}  # target -> (prompt_hash, answered_at_timestamp)
autoyes_delays = {}  # session_name -> seconds (per-session override)
AUTOYES_DELAY = DEFAULT_SETTINGS["autoyes"]["default_delay"]
# Rebuilt by the auto-yes scanner each tick: session_name -> effective enabled.
# /autoyes/status serves it, because only the scan holds the live session list.
autoyes_effective = {}
# Alongside it: session_name -> "explicit" | "global", so a caller can tell a
# hand-armed session (survives the switch being turned off) from one armed only
# by the switch. Same publication point as autoyes_effective.
autoyes_sources = {}


def autoyes_enabled_for(session):
    """Resolve a session's auto-yes state and where the answer came from.

    Returns (enabled, source). `source` is "explicit" when the session decided
    for itself — a runtime toggle this process, or a persisted enabled_default /
    global_opt_out — and "global" when it is enabled only because the
    all_sessions switch is on. The scanner uses the source to decide whether the
    agent-pane filter applies: a session armed by hand keeps auto-yes on shell
    prompts, a globally-armed one does not.

    MUST be called without holding autoyes_lock — it takes that lock itself and
    then _project_settings_lock.
    """
    with autoyes_lock:
        runtime = autoyes_sessions.get(session)
    if runtime is not None:
        return bool(runtime), "explicit"
    proj = get_project_settings(session)["autoyes"]
    if get_setting("autoyes", "all_sessions") != "on":
        return bool(proj["enabled_default"]), "explicit"
    if proj["global_opt_out"]:
        return False, "explicit"
    if proj["enabled_default"]:
        return True, "explicit"
    return True, "global"

# ---------------------------------------------------------------------------
# Automate state
# ---------------------------------------------------------------------------
IMMEDIATE_NOTICE_FILENAME = "immediatenotice.md"
AUTOMATE_STATE_FILE = DATA_DIR / "automate_state.json"

automate = {
    "active": False,
    "project": None,
    "project_path": None,
    "session": None,
    "container": None,
    "prompt": None,
    "timeout_minutes": 10,
    "started_at": None,
    "last_output_at": None,
    "last_output_hash": None,
    "done_signal_at": None,
    "status": None,
    "continuous": True,
    "max_iterations": 0,
    "iterations_completed": 0,
    "stop_after": "",
}
automate_lock = threading.Lock()

# ---------------------------------------------------------------------------
# WebSocket state
# ---------------------------------------------------------------------------
ws_clients = []  # list of {"ws", "lock", "target", "lines", "last_send"} dicts
ws_lock = threading.Lock()
ws_streamer_running = False
ws_last_content = {}  # "target:lines" -> last content string
ws_streamer_thread = None
WS_SEND_TIMEOUT = 5  # seconds
WS_HEARTBEAT_INTERVAL = DEFAULT_SETTINGS["connection"]["ws_heartbeat_sec"]

# ---------------------------------------------------------------------------
# Content-based activity tracking — replaces tmux session_activity which is
# unreliable for Claude Code sessions (status bar updates keep it fresh).
# ---------------------------------------------------------------------------
pane_content_hash = {}  # target -> hash of last captured content
pane_last_activity = {}  # target -> time.time() of last content change
# target -> {model, effort, changed_at, candidate, candidate_since, kind}
# Written on the /poll request path under _activity_lock — that path may run
# concurrently, one handler per open browser, which is why the lock is held
# and why shared/agent_model.py's _MIN_CONFIRM_SECONDS gate is time-based
# rather than counting consecutive calls.
# Deliberately NOT persisted by save_idle_state(): it re-derives within two
# polls (~10s) after a restart, and a changed_at carried across a restart
# would caret tabs whose model never moved.
pane_model = {}
_activity_lock = threading.Lock()
_IDLE_STATE_FILE = DATA_DIR / "idle_state.json"
_idle_save_pending = False  # coalesce saves


def touch_activity(target: str):
    """Mark a pane as active right now (called on user input)."""
    import time

    with _activity_lock:
        pane_last_activity[target] = time.time()


def save_idle_state():
    """Persist idle tracking dicts to disk."""
    with _activity_lock:
        data = {
            "content_hash": dict(pane_content_hash),
            "last_activity": {k: round(v, 2) for k, v in pane_last_activity.items()},
        }
    try:
        atomic_write_json(_IDLE_STATE_FILE, data, indent=None)
    except OSError:
        pass


def load_idle_state():
    """Restore idle tracking dicts from disk on startup."""
    global pane_content_hash, pane_last_activity
    data = read_json_state(_IDLE_STATE_FILE, {}, expect=dict)
    try:
        pane_content_hash.update(data.get("content_hash", {}))
        pane_last_activity.update(data.get("last_activity", {}))
    except (TypeError, ValueError, AttributeError):
        pass


# Load on import
load_idle_state()
load_settings()
load_project_settings()
load_container_config()
load_extensions()


# ---------------------------------------------------------------------------
# Claude-mount script path
# ---------------------------------------------------------------------------
_mount_env = os.environ.get("ASSIST_MOUNT_SCRIPT", "")
if _mount_env:
    CLAUDE_MOUNT_SCRIPT = Path(_mount_env)
else:
    # Auto-detect from repo docker/ directory
    _default_mount = DATA_DIR / "docker" / "claude-mount.sh"
    CLAUDE_MOUNT_SCRIPT = _default_mount if _default_mount.exists() else None

# Claude env vars to strip from tmux sessions
CLAUDE_ENV_VARS = ("CLAUDECODE",)
