"""Read OpenCode's public session exports without changing the interactive TUI.

The project status plugin is deliberately not an identity source: several panes
can share its one file. Every read names a conversation and a live pane generation.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import shared.state as state
from shared.agent_identity import _scan_process_tree
from shared.tmux import tmux_exact_target

SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]{1,120}\Z")
TARGET = re.compile(r"[^:\x00-\x20]{1,200}:\d+\.\d+\Z")
CLI_TIMEOUT = 12
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_LIST_SESSIONS = 200
MAX_MESSAGES = 500
MAX_PART_CHARS = 24000
MAX_VIEW_CHARS = 300000
CACHE_TTL = 3
CACHE_ENTRIES = 4


class OpenCodeError(Exception):
    def __init__(self, code, message, status=503):
        super().__init__(message)
        self.code = code
        self.status = status


def pane_context(target):
    """Exact local process identity, including an agent restarted in the same shell."""
    if not isinstance(target, str) or not TARGET.fullmatch(target):
        raise OpenCodeError("invalid_target", "Choose an OpenCode pane.", 400)
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_exact_target(target),
             "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3,
        )
        fields = proc.stdout.strip().split("\t")
        if proc.returncode or len(fields) != 4 or not fields[0].startswith("%"):
            raise OpenCodeError("pane_gone", "This pane is no longer available.", 409)
        pane_id, pid, _command, directory = fields
        scan = _scan_process_tree(target, int(pid))
        if not scan or scan.get("agent_kind") != "opencode" or not scan.get("agent_start_time"):
            raise OpenCodeError("not_opencode", "Output is available for local OpenCode panes.", 409)
        agent_pid = scan["agent_pid"]
        # A remote attach can have the same cwd and session title as a local run.
        # Do not silently show a local database in place of that remote server.
        argv = Path(f"/proc/{agent_pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        if "attach" in argv:
            url = next((arg for arg in argv if arg.startswith(("http://", "https://"))), "")
            if urlsplit(url).hostname not in ("localhost", "127.0.0.1", "::1"):
                raise OpenCodeError("remote_session", "Remote OpenCode output is available in Terminal.", 409)
        # The export CLI must read the same store as the pane. Refuse rather than
        # guess when a pane runs with a different home/data store from Assist.
        environ = dict(
            item.split(b"=", 1) for item in Path(f"/proc/{agent_pid}/environ").read_bytes().split(b"\0")
            if b"=" in item
        )
        for key in ("HOME", "XDG_DATA_HOME"):
            pane_value = environ.get(key.encode(), b"").decode("utf-8", "replace")
            if pane_value != os.environ.get(key, ""):
                raise OpenCodeError("different_store", "This OpenCode pane uses a different data store. Use Terminal.", 409)
        directory = os.path.realpath(directory)
        if not Path(directory).is_dir():
            raise OpenCodeError("directory_gone", "The pane's folder is no longer available.", 409)
        identity = [pane_id, pid, scan.get("root_start_time"), agent_pid,
                    scan["agent_start_time"], directory]
        generation = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
        return {"target": target, "generation": generation, "directory": directory}
    except OpenCodeError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise OpenCodeError("pane_unavailable", "Could not verify this OpenCode pane. Use Terminal.") from None


def check_generation(context, expected):
    if not expected or context["generation"] != expected:
        raise OpenCodeError("pane_changed", "The pane changed. Choose its conversation again.", 409)


def resolve_binary():
    """Find OpenCode even when the server's PATH omits user or Homebrew installs."""
    candidates = (
        os.environ.get("ASSIST_OPENCODE_BIN"),
        shutil.which("opencode"),
        "~/.local/bin/opencode",
        "~/.opencode/bin/opencode",
        "/opt/homebrew/bin/opencode",
        "/usr/local/bin/opencode",
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def run_cli(arguments, directory):
    """Bound time, disk output and concurrency; never return CLI stderr to the phone."""
    executable = resolve_binary()
    if not executable:
        raise OpenCodeError("cli_missing", "OpenCode is not installed on the Assist host.")
    if not state.opencode_slots.acquire(blocking=False):
        raise OpenCodeError("busy", "Output is refreshing in another viewer. Retry shortly.", 429)
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(
                [executable, *arguments, "--pure"], cwd=directory,
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
            )
            try:
                deadline = time.monotonic() + CLI_TIMEOUT
                while True:
                    if os.fstat(output.fileno()).st_size > MAX_EXPORT_BYTES or os.fstat(errors.fileno()).st_size > MAX_EXPORT_BYTES:
                        raise OpenCodeError("output_too_large", "This export is too large for Output. Use Terminal.", 413)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OpenCodeError("timeout", "OpenCode took too long to export. Retry or use Terminal.", 504)
                    try:
                        proc.wait(timeout=min(remaining, 0.1))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                output.seek(0)
                raw = output.read(MAX_EXPORT_BYTES + 1)
                if len(raw) > MAX_EXPORT_BYTES:
                    raise OpenCodeError("output_too_large", "This export is too large for Output. Use Terminal.", 413)
                if proc.returncode:
                    raise OpenCodeError("export_failed", "OpenCode could not read this conversation. Retry or use Terminal.")
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise OpenCodeError("invalid_export", "OpenCode returned an unreadable export. Use Terminal.") from None
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    except OSError:
        raise OpenCodeError("cli_unavailable", "Could not run OpenCode. Use Terminal.") from None
    finally:
        state.opencode_slots.release()


def cached_cli(arguments, context):
    key = (context["generation"], tuple(arguments))
    now = time.monotonic()
    with state.opencode_lock:
        for stale, entry in list(state.opencode_cache.items()):
            if now - entry[0] >= CACHE_TTL:
                state.opencode_cache.pop(stale, None)
        entry = state.opencode_cache.get(key)
        if entry:
            return entry[1], entry[2]
    data = run_cli(arguments, context["directory"])
    captured = time.time()
    with state.opencode_lock:
        while len(state.opencode_cache) >= CACHE_ENTRIES:
            state.opencode_cache.pop(next(iter(state.opencode_cache)))
        state.opencode_cache[key] = (time.monotonic(), data, captured)
    return data, captured


def _same_directory(value, directory):
    return isinstance(value, str) and os.path.isabs(value) and os.path.realpath(value) == directory


def list_sessions(context):
    data, _captured = cached_cli(
        ["session", "list", "--format", "json", "--max-count", str(MAX_LIST_SESSIONS)], context,
    )
    if not isinstance(data, list):
        raise OpenCodeError("invalid_export", "OpenCode returned an unreadable session list.")
    sessions = []
    for item in data[:MAX_LIST_SESSIONS]:
        if not isinstance(item, dict) or not SESSION_ID.fullmatch(str(item.get("id", ""))):
            continue
        if not _same_directory(item.get("directory"), context["directory"]):
            continue
        sessions.append({"id": item["id"], "title": str(item.get("title") or "Untitled")[:240]})
    return sessions


def normalize_export(data, session_id, directory, limit):
    """Project the public export onto bounded, inert display text."""
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict) or not isinstance(data.get("messages"), list):
        raise OpenCodeError("invalid_export", "OpenCode returned an unreadable conversation.")
    info = data["info"]
    if info.get("id") != session_id or not _same_directory(info.get("directory"), directory):
        raise OpenCodeError("session_mismatch", "This conversation does not belong to the pane's folder.", 409)
    # Exports include messages beyond the current revert point. Match the active
    # conversation by excluding the reverted suffix instead of presenting it as live.
    reverted = (info.get("revert") or {}).get("messageID") if isinstance(info.get("revert"), dict) else None
    if reverted is not None and not isinstance(reverted, str):
        raise OpenCodeError("invalid_export", "OpenCode returned an unreadable revert point.")
    source = [m for m in data["messages"] if isinstance(m, dict) and isinstance(m.get("info"), dict)]
    source = [m for m in source if not reverted or str(m["info"].get("id", "")) < reverted]
    source = [m for m in source if m["info"].get("role") in ("user", "assistant")]
    total = len(source)
    remaining = MAX_VIEW_CHARS
    clipped = False

    def text(value, cap=MAX_PART_CHARS):
        nonlocal remaining, clipped
        value = value if isinstance(value, str) else ""
        size = min(cap, max(remaining, 0))
        result = value[:size]
        remaining -= len(result)
        if len(value) > size:
            clipped = True
            result += "\n[Output clipped; view the full detail in Terminal.]"
        return result

    messages = []
    # Spend the payload budget on recent output first, then restore chronology.
    for message in reversed(source[-limit:]):
        mi = message["info"]
        parts = []
        raw_parts = message.get("parts")
        if not isinstance(raw_parts, list):
            raw_parts = []
        for part in raw_parts[:200]:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in ("text", "reasoning") and not part.get("ignored"):
                value = text(part.get("text"))
                if value:
                    parts.append({"type": kind, "text": value})
            elif kind == "tool":
                tool = part.get("state") if isinstance(part.get("state"), dict) else {}
                details = json.dumps(tool.get("input", {}), ensure_ascii=False, indent=2)
                output = tool.get("output") or tool.get("error") or ""
                parts.append({"type": "tool", "title": text(str(part.get("tool") or "Tool"), 120),
                              "status": text(tool.get("status"), 40),
                              "text": text(details + ("\n\n" + output if isinstance(output, str) else ""))})
            elif kind == "file":
                parts.append({"type": "file", "text": text(part.get("filename") or "Attachment", 240)})
        if len(raw_parts) > 200:
            clipped = True
        error = mi.get("error")
        if isinstance(error, dict):
            # Raw API errors can carry response headers and cookies. Only the
            # public error name is needed to explain an incomplete answer.
            parts.append({"type": "error", "text": text(str(error.get("name") or "Response failed"), 120)})
        messages.append({"id": str(mi.get("id", ""))[:160], "role": mi["role"], "parts": parts})
        if remaining <= 0:
            clipped = True
            break
    messages.reverse()
    model = info.get("model") if isinstance(info.get("model"), dict) else {}
    return {
        "session": {"id": session_id, "title": str(info.get("title") or "Untitled")[:240],
                    "agent": str(info.get("agent") or "")[:80],
                    "model": str(model.get("id") or model.get("modelID") or "")[:120],
                    "provider": str(model.get("providerID") or "")[:80],
                    "variant": str(model.get("variant") or "")[:40]},
        "messages": messages, "total": total, "shown": len(messages),
        "has_older": total > len(messages) and limit < MAX_MESSAGES and remaining > 0,
        "clipped": clipped or total > MAX_MESSAGES,
        "reverted": bool(reverted),
    }


def transcript(context, session_id, limit):
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise OpenCodeError("invalid_session", "Choose a valid OpenCode conversation.", 400)
    data, captured = cached_cli(["export", session_id], context)
    result = normalize_export(data, session_id, context["directory"], limit)
    result["captured_at"] = captured
    return result
