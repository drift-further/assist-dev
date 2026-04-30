"""shared/state.py — Centralized mutable state for all Assist blueprints.

Every module-level variable that is mutated at runtime lives here.
Import with: from shared.state import <name>
"""

import copy
import json
import os
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings — single source of truth for all configuration
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "server": {
        "claude_mode": "claude",
        "session_init_cmd": os.environ.get("ASSIST_SESSION_INIT_CMD", ""),
        "projects_dir": str(
            Path(
                os.environ.get("ASSIST_PROJECTS_DIR", Path.home() / "projects")
            )
        ),
        "port": int(os.environ.get("ASSIST_PORT", 8089)),
        "restart_cmd": "assist restart",
    },
    "terminal": {
        "font_size": 13,
        "default_cols": 200,
        "default_rows": 50,
        "capture_lines": 2000,
        "tmux_history_limit": 20000,
        "idle_threshold_sec": 300,
    },
    "autoyes": {
        "default_delay": 5,
        "detection_depth": 8,
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
        "recent_projects_limit": 20,
    },
    "limits": {
        "max_history": 2500,
        "max_upload_mb": 50,
        "max_capture_lines": 20000,
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


def _deep_merge(base, override):
    """Merge override into base recursively. Returns new dict."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_settings():
    """Load settings from disk, merge with defaults. Call once at startup."""
    global _settings
    saved = {}
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    with _settings_lock:
        _settings = _deep_merge(copy.deepcopy(DEFAULT_SETTINGS), saved)

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


def save_settings():
    """Persist current settings to disk."""
    with _settings_lock:
        data = copy.deepcopy(_settings)
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def get_settings():
    """Return full settings dict (deep copy)."""
    with _settings_lock:
        return copy.deepcopy(_settings)


def patch_settings(patch):
    """Deep-merge patch into settings, save, and re-apply. Returns updated settings."""
    global _settings
    with _settings_lock:
        _settings = _deep_merge(_settings, patch)
        result = copy.deepcopy(_settings)
    save_settings()
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
    try:
        _project_settings = json.loads(PROJECT_SETTINGS_FILE.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        _project_settings = {}


def save_project_settings():
    """Persist per-project settings to disk."""
    with _project_settings_lock:
        data = copy.deepcopy(_project_settings)
    try:
        PROJECT_SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def get_project_settings(project):
    """Return merged defaults + project overrides (deep copy)."""
    with _project_settings_lock:
        overrides = _project_settings.get(project, {})
    return _deep_merge(copy.deepcopy(DEFAULT_PROJECT_SETTINGS), overrides)


def patch_project_settings(project, patch):
    """Deep-merge patch into project settings, save, return updated."""
    global _project_settings
    with _project_settings_lock:
        current = _project_settings.get(project, {})
        _project_settings[project] = _deep_merge(current, patch)
        result = _deep_merge(
            copy.deepcopy(DEFAULT_PROJECT_SETTINGS), _project_settings[project]
        )
    save_project_settings()
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
        "name": "claude-assist-container",
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
    try:
        _container_config = json.loads(CONTAINER_CONFIG_FILE.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        _container_config = {}


def save_container_config():
    with _container_config_lock:
        data = copy.deepcopy(_container_config)
    try:
        CONTAINER_CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def get_container_config():
    with _container_config_lock:
        return _deep_merge(copy.deepcopy(DEFAULT_CONTAINER_CONFIG), _container_config)


def patch_container_config(patch):
    global _container_config
    with _container_config_lock:
        _container_config = _deep_merge(_container_config, patch)
        result = _deep_merge(copy.deepcopy(DEFAULT_CONTAINER_CONFIG), _container_config)
    save_container_config()
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
    try:
        _extensions = json.loads(EXTENSIONS_FILE.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        _extensions = []
    if not _extensions and BUILTIN_EXTENSIONS_DIR.is_dir():
        for f in sorted(BUILTIN_EXTENSIONS_DIR.glob("*.json")):
            try:
                ext = json.loads(f.read_text())
                _extensions.append(ext)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if _extensions:
            save_extensions()


def save_extensions():
    with _extensions_lock:
        data = copy.deepcopy(_extensions)
    try:
        EXTENSIONS_FILE.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def get_extensions():
    with _extensions_lock:
        return copy.deepcopy(_extensions)


def add_extension(ext):
    with _extensions_lock:
        _extensions.append(ext)
    save_extensions()
    return get_extensions()


def update_extension(ext_id, patch):
    with _extensions_lock:
        for ext in _extensions:
            if ext.get("id") == ext_id:
                ext.update(patch)
                break
    save_extensions()
    return get_extensions()


def delete_extension(ext_id):
    with _extensions_lock:
        _extensions[:] = [e for e in _extensions if e.get("id") != ext_id]
    save_extensions()
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
)  # target -> { "prompt_hash", "deadline", "cancelled", "prompt_type" }
autoyes_answered = {}  # target -> (prompt_hash, answered_at_timestamp)
autoyes_delays = {}  # session_name -> seconds (per-session override)
AUTOYES_DELAY = DEFAULT_SETTINGS["autoyes"]["default_delay"]

# ---------------------------------------------------------------------------
# Automate state
# ---------------------------------------------------------------------------
AUTOMATE_DONE_SIGNAL = "~:)Investigate done(:~"
AUTOMATE_DONE_IDLE_SEC = 60
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
automate_stop_event = threading.Event()

# ---------------------------------------------------------------------------
# WebSocket state
# ---------------------------------------------------------------------------
ws_clients = []  # list of (ws, target, lines)
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
    import json

    with _activity_lock:
        data = {
            "content_hash": dict(pane_content_hash),
            "last_activity": {k: round(v, 2) for k, v in pane_last_activity.items()},
        }
    try:
        _IDLE_STATE_FILE.write_text(json.dumps(data))
    except OSError:
        pass


def load_idle_state():
    """Restore idle tracking dicts from disk on startup."""
    import json

    global pane_content_hash, pane_last_activity
    try:
        data = json.loads(_IDLE_STATE_FILE.read_text())
        pane_content_hash.update(data.get("content_hash", {}))
        pane_last_activity.update(data.get("last_activity", {}))
    except (OSError, ValueError, json.JSONDecodeError):
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

# ---------------------------------------------------------------------------
# Claude launch mode — now backed by settings.json
# ---------------------------------------------------------------------------
CLAUDE_COMMANDS = {
    "npx": "npx @anthropic-ai/claude-code",
    "claude": "claude",
}


def get_claude_mode() -> str:
    """Read current mode from settings."""
    mode = get_setting("server", "claude_mode")
    return mode if mode in CLAUDE_COMMANDS else "npx"


def set_claude_mode(mode: str):
    """Update mode in settings."""
    if mode in CLAUDE_COMMANDS:
        patch_settings({"server": {"claude_mode": mode}})


def get_claude_cmd() -> str:
    """Return the full command string for the current mode."""
    return CLAUDE_COMMANDS.get(get_claude_mode(), CLAUDE_COMMANDS["npx"])
