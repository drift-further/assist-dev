"""Resolve the coding-agent identity of a tmux pane."""

import logging
import os
import platform
import re
import shlex
import subprocess
import threading
import time

from shared import tab_state

log = logging.getLogger(__name__)

AGENT_KINDS = ("claude", "codex", "cursor", "opencode", "gemini")
SHELL_COMMANDS = ("bash", "zsh", "sh", "fish")
_VERSION_CMD_RE = re.compile(r"\d+(?:\.\d+){1,3}")

_WRAPPER_COMMS = ("docker", "podman", "lxc-attach", "kubectl", "ssh")
_PROCESS_CACHE = {}
_PROCESS_CACHE_LOCK = threading.Lock()
_PROCESS_CACHE_TTL = 0.25
_DECLARATION_STARTUP_GRACE = 10.0
_IS_MAC = platform.system() == "Darwin"
_LOGGED_FAILURES = set()

_ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_])"
)
_CODEX_FINGERPRINT_RE = re.compile(r"·\s*Context \d+% (?:left|used)")
_CLAUDE_FINGERPRINT_RE = re.compile(
    r"⏵⏵\s+accept edits on(?:\s+\(shift\+tab to cycle\))?|\? for shortcuts"
)
_CLAUDE_DIALOG_FINGERPRINT_RE = re.compile(
    r"Allow once[\s\S]*Always allow[\s\S]*Deny"
    r"|\d+[\.)]\s+Chat about this[\s\S]*Enter to select\s*[·•]\s*Esc to cancel"
    r"|^\s*❯\s+Yes\s*$[\s\S]*^\s+No\s*$[\s\S]*"
    r"Enter to select\s*[·•]\s*Esc to cancel",
    re.MULTILINE,
)

_DIRECT_EXECUTABLE_KINDS = {
    "claude": "claude",
    "codex": "codex",
    "cursor-agent": "cursor",
    "opencode": "opencode",
    "gemini": "gemini",
    "antigravity": "gemini",
}
_AGENT_PACKAGE_KINDS = {
    "@anthropic-ai/claude-code": "claude",
    "@openai/codex": "codex",
    "@google/gemini-cli": "gemini",
    "opencode-ai": "opencode",
}
_NODE_ENTRYPOINT_COMPONENTS = {
    ("@anthropic-ai", "claude-code"): "claude",
    ("@openai", "codex"): "codex",
    ("@google", "gemini-cli"): "gemini",
    ("opencode-ai",): "opencode",
}


def _log_failure_once(target, message):
    if target in _LOGGED_FAILURES:
        return
    _LOGGED_FAILURES.add(target)
    log.exception("agent identity: %s for %s", message, target)


def _stat_fields(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    close = raw.rfind(")")
    if close < 0:
        raise ValueError("malformed /proc stat")
    fields = raw[close + 2 :].split()
    if len(fields) <= 19:
        raise ValueError("malformed /proc stat")
    return fields[0], int(fields[1]), fields[19]


def _proc_start_time(pid):
    try:
        if os.path.isdir("/proc"):
            _state, _ppid, start_time = _stat_fields(int(pid))
            return start_time
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, TypeError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _kind_from_executable(executable):
    executable = os.path.basename(executable or "").lower()
    if _VERSION_CMD_RE.fullmatch(executable or ""):
        return "claude"
    return _DIRECT_EXECUTABLE_KINDS.get(executable)


def _kind_from_package_operand(token):
    token = (token or "").lower()
    for package, kind in _AGENT_PACKAGE_KINDS.items():
        version = token[len(package) + 1 :] if token.startswith(package + "@") else ""
        if token == package or version and "/" not in version:
            return kind
    return None


def _kind_from_node_entrypoint(token):
    components = tuple(
        component
        for component in (token or "").lower().replace("\\", "/").split("/")
        if component
    )
    for marker, kind in _NODE_ENTRYPOINT_COMPONENTS.items():
        width = len(marker)
        if any(
            components[index : index + width] == marker
            for index in range(len(components) - width + 1)
        ):
            return kind
    return None


def _launcher_package_operand(argv, start):
    """Return a launcher's package operand, never an option value."""
    flag_only = {"-y", "--yes", "--quiet", "--no-install", "--ignore-existing"}
    for index in range(start, len(argv)):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token in flag_only or "=" in token and token.startswith("-"):
            continue
        if token.startswith("-"):
            # Unknown options may consume the next token. Failing closed avoids
            # interpreting that option value as a package to execute.
            return None
        return token
    return None


def _kind_from_process(comm, argv):
    executable = os.path.basename(argv[0]) if argv else comm
    direct = _kind_from_executable(executable)
    if direct:
        return direct
    if not argv:
        return _kind_from_executable(comm)

    launcher = os.path.basename(argv[0]).lower()
    if launcher in ("node", "nodejs"):
        # Standard Node agent shims put the executable JS file at argv[1]. If
        # Node options are present, abstain rather than inspect their values.
        if len(argv) > 1 and argv[1] == "--" and len(argv) > 2:
            return _kind_from_node_entrypoint(argv[2])
        if len(argv) > 1 and not argv[1].startswith("-"):
            return _kind_from_node_entrypoint(argv[1])
        return None
    if launcher == "npx":
        return _kind_from_package_operand(_launcher_package_operand(argv, 1))
    if launcher == "npm" and len(argv) > 1 and argv[1] in ("exec", "x"):
        return _kind_from_package_operand(_launcher_package_operand(argv, 2))
    if (
        launcher in ("pnpm", "yarn")
        and len(argv) > 1
        and argv[1] in ("dlx", "exec")
    ):
        return _kind_from_package_operand(_launcher_package_operand(argv, 2))
    return None


def _build_process_snapshot(now):
    """Enumerate /proc once; pane roots share the resulting parent map."""
    children = {}
    start_times = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            pid = int(entry)
            _state, ppid, start_time = _stat_fields(pid)
            children.setdefault(ppid, []).append(pid)
            start_times[pid] = start_time
        except (OSError, ValueError, IndexError):
            continue
    return {
        "created_at": now,
        "children": children,
        "start_times": start_times,
        "process_info": {},
    }


def _get_process_snapshot(force=False):
    now = time.time()
    with _PROCESS_CACHE_LOCK:
        snapshot = _PROCESS_CACHE.get("snapshot")
        if (
            not force
            and snapshot
            and now - snapshot["created_at"] < _PROCESS_CACHE_TTL
        ):
            return snapshot
        snapshot = _build_process_snapshot(now)
        _PROCESS_CACHE["snapshot"] = snapshot
        return snapshot


def _snapshot_process_info(snapshot, pid):
    cached = snapshot["process_info"].get(pid)
    if cached is not None:
        return cached
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
            comm = f.read().strip()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [
                part.decode("utf-8", errors="replace")
                for part in f.read().split(b"\0")
                if part
            ]
    except OSError:
        comm = ""
        argv = []
    snapshot["process_info"][pid] = (comm, argv)
    return comm, argv


def _scan_snapshot_tree(snapshot, pane_pid):
    children = snapshot["children"]
    start_times = snapshot["start_times"]

    agent_kind = None
    agent_pid = None
    agent_start_time = None
    has_wrapper = False
    queue = [pane_pid]
    seen = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        comm, argv = _snapshot_process_info(snapshot, pid)

        executable = os.path.basename(argv[0]) if argv else comm
        if comm in _WRAPPER_COMMS or executable in _WRAPPER_COMMS:
            has_wrapper = True
        if agent_kind is None:
            agent_kind = _kind_from_process(comm, argv)
            if agent_kind is not None:
                agent_pid = pid
                agent_start_time = start_times.get(pid) or _proc_start_time(pid)
        queue.extend(children.get(pid, []))

    return {
        "agent_kind": agent_kind,
        "agent_pid": agent_pid,
        "agent_start_time": agent_start_time,
        "has_wrapper": has_wrapper,
        "root_start_time": start_times.get(pane_pid) or _proc_start_time(pane_pid),
    }


def _scan_process_tree(target, pane_pid, _force_snapshot=False):
    """Resolve one root from the shared short-lived /proc snapshot."""
    if pane_pid in (None, "") or _IS_MAC or not os.path.isdir("/proc"):
        return None
    pane_pid = int(pane_pid)
    snapshot = _get_process_snapshot(force=_force_snapshot)
    result = _scan_snapshot_tree(snapshot, pane_pid)

    root_alive = _proc_start_time(pane_pid) == result.get("root_start_time")
    agent_pid = result.get("agent_pid")
    agent_alive = agent_pid is None or (
        _proc_start_time(agent_pid) == result.get("agent_start_time")
    )
    if not _force_snapshot and (not root_alive or not agent_alive):
        # A child exit/switch or root PID reuse invalidates the snapshot now,
        # without waiting for its short TTL.
        return _scan_process_tree(target, pane_pid, _force_snapshot=True)
    return result


def _kind_from_command_name(command):
    command = os.path.basename((command or "").strip())
    return _kind_from_process(command, [command] if command else [])


def kind_from_agent_command(command):
    """Return the kind only when command is itself an agent launch."""
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        return None
    if not tokens:
        return None

    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", "./")):
        name, _sep, _value = tokens[0].partition("=")
        if not name.replace("_", "").isalnum():
            break
        tokens.pop(0)
    while tokens and tokens[0] in ("command", "exec"):
        tokens.pop(0)
    if not tokens:
        return None

    return _kind_from_process(os.path.basename(tokens[0]), tokens)


def declare_agent_command(target, command):
    """Persist identity when Assist sends a recognizable agent launch."""
    kind = kind_from_agent_command(command)
    if not kind or not target:
        return None
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-t", target, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        pane_pid = int(proc.stdout.strip()) if proc.returncode == 0 else 0
        start_time = _proc_start_time(pane_pid)
        if pane_pid <= 0 or not start_time:
            return None
        tab_state.set_agent_declaration(
            target,
            {
                "pane_pid": pane_pid,
                "proc_start_time": start_time,
                "kind": kind,
                "declared_at": time.time(),
            },
        )
        # A freshly typed launch must not be judged against the previous
        # process generation cached for this pane.
        with _PROCESS_CACHE_LOCK:
            _PROCESS_CACHE.clear()
        return kind
    except Exception:
        _log_failure_once(target, "could not persist declaration")
        return None


def resolve_process(target, pane_pid, command):
    """Resolve identity without pane content; return None when still opaque."""
    try:
        pane_pid = int(pane_pid) if pane_pid not in (None, "") else None
        scan = _scan_process_tree(target, pane_pid)
        inferred = scan.get("agent_kind") if scan else None
        if inferred is None:
            inferred = _kind_from_command_name(command)

        declaration = tab_state.get_agent_declaration(target)
        if declaration:
            current_start = (
                scan.get("root_start_time") if scan else _proc_start_time(pane_pid)
            )
            valid = (
                pane_pid is not None
                and declaration.get("pane_pid") == pane_pid
                and declaration.get("proc_start_time") == current_start
                and declaration.get("kind") in AGENT_KINDS
                and isinstance(declaration.get("declared_at"), (int, float))
            )
            contradictory = (
                inferred in AGENT_KINDS and inferred != declaration.get("kind")
            )
            bound_pid = declaration.get("agent_pid")
            bound_start = declaration.get("agent_start_time")
            bound_alive = (
                bound_pid is not None
                and bound_start
                and _proc_start_time(bound_pid) == bound_start
            )
            if bound_pid is not None and not bound_alive:
                # A cached tree may still contain the dead child. Do not let its
                # stale identity survive after invalidating the bound launch.
                if scan and scan.get("agent_pid") == bound_pid:
                    inferred = None
                tab_state.delete_agent_declaration(target)
            elif not valid or contradictory:
                tab_state.delete_agent_declaration(target)
            elif (
                inferred == declaration.get("kind")
                and scan
                and scan.get("agent_pid")
            ):
                inferred_pid = scan["agent_pid"]
                inferred_start = scan.get("agent_start_time")
                if bound_pid is not None:
                    if bound_pid == inferred_pid and bound_start == inferred_start:
                        return declaration["kind"]
                    # The declared process generation ended. A newly observed
                    # same-kind process stands on inference, not the old record.
                    tab_state.delete_agent_declaration(target)
                elif (
                    inferred_start
                    and _proc_start_time(inferred_pid) == inferred_start
                ):
                    tab_state.set_agent_declaration(
                        target,
                        {
                            **declaration,
                            "agent_pid": inferred_pid,
                            "agent_start_time": inferred_start,
                        },
                    )
                    return declaration["kind"]
                else:
                    tab_state.delete_agent_declaration(target)
            elif inferred is None and bound_pid is not None:
                tab_state.delete_agent_declaration(target)
            elif (
                inferred is None
                and 0
                <= time.time() - declaration["declared_at"]
                <= _DECLARATION_STARTUP_GRACE
            ):
                return declaration["kind"]
            else:
                tab_state.delete_agent_declaration(target)

        if inferred in AGENT_KINDS:
            return inferred
        # A transport can hide the real child process (Docker exec, SSH, etc.).
        # Abstain so callers with a capture can use the fingerprint tier.
        if scan is not None and scan.get("has_wrapper"):
            return None
        if scan is not None and (command or "") in SHELL_COMMANDS:
            return "shell"
        return None
    except Exception:
        _log_failure_once(target, "resolution failed")
        return None


def refine_with_content(kind_or_none, tail):
    """Use idle chrome only when process resolution abstained."""
    if kind_or_none in AGENT_KINDS or kind_or_none == "shell":
        return kind_or_none
    normalized = _ANSI_ESCAPE_RE.sub("", tail or "")
    nonempty = [line for line in normalized.splitlines() if line.strip()]
    fingerprint = "\n".join(nonempty[-5:])
    if _CODEX_FINGERPRINT_RE.search(fingerprint):
        return "codex"
    if _CLAUDE_FINGERPRINT_RE.search(fingerprint) or _CLAUDE_DIALOG_FINGERPRINT_RE.search(
        fingerprint
    ):
        return "claude"
    return "shell"


def resolve(target, pane_pid, command, tail=None):
    """Resolve a pane, optionally applying the content fingerprint tier."""
    process_kind = resolve_process(target, pane_pid, command)
    if tail is None:
        return process_kind or "unknown"
    return refine_with_content(process_kind, tail)


def _has_wrapper_descendant(target, pane_pid):
    """Return wrapper status from the same cached walk identity uses."""
    try:
        scan = _scan_process_tree(target, pane_pid)
        return bool(scan and scan.get("has_wrapper"))
    except Exception:
        _log_failure_once(target, "wrapper scan failed")
        return False
