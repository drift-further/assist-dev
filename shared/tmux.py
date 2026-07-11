"""shared/tmux.py — tmux and X11/macOS interaction helpers."""

import os
import platform
import re
import subprocess
import time
import uuid

from shared.state import WS_SEND_TIMEOUT

_IS_MAC = platform.system() == "Darwin"

# Wrapper-TUI detection: when a pane runs a script that exec's docker/podman
# to host the real TUI (e.g. claude inside docker via claude-mount.sh), the
# in-container TUI writes to its own pty. The host tmux pane receives the
# output but never sees clean erase-codes between animation frames, so stale
# cells accumulate above the live viewport. Capturing only the visible
# screen (`-S 0`) hides the accumulated mess.
_WRAPPER_COMMS = ("docker", "podman", "lxc-attach", "kubectl")

# Native-TUI detection: claude running directly in the host pane renders on
# the MAIN screen (alternate_on=0) with a diff-based renderer that skips
# cells it believes unchanged (e.g. runs of spaces). After a reflow or a
# frame taller than the pane, its model diverges from the tmux grid and the
# skipped cells keep stale characters. SIGWINCH-via-resize triggers exactly
# those diff redraws, so the heal for these panes is Ctrl+L (full
# clear-and-repaint, re-renders in-flight typed input). Note: npx-launched
# claude shows comm "node" — too generic to include safely.
_NATIVE_TUI_COMMS = ("claude",)
_WRAPPER_CACHE: dict[str, tuple[float, bool]] = {}
_WRAPPER_CACHE_TTL = 5.0  # seconds
# Entries for dead targets are never evicted individually; just reset the
# whole cache when it grows past this (it repopulates within one TTL).
_WRAPPER_CACHE_MAX = 256


def _has_wrapper_descendant(target, pane_pid):
    """True if any descendant of pane_pid is a known TUI-wrapper.

    Cached per target for 5s to avoid scanning /proc on every poll. Linux-
    only path via /proc; falls back to False on macOS or any read error.
    """
    if pane_pid is None or _IS_MAC:
        return False
    now = time.time()
    cached = _WRAPPER_CACHE.get(target)
    if cached and now - cached[0] < _WRAPPER_CACHE_TTL:
        return cached[1]
    if len(_WRAPPER_CACHE) > _WRAPPER_CACHE_MAX:
        _WRAPPER_CACHE.clear()
    try:
        children: dict[int, list[tuple[int, str]]] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                # comm can contain spaces and parens — find the LAST `)`
                # then parse the fixed fields that follow.
                with open(f"/proc/{entry}/stat") as f:
                    raw = f.read()
                close = raw.rfind(")")
                if close < 0:
                    continue
                ppid = int(raw[close + 2:].split()[1])
                with open(f"/proc/{entry}/comm") as f:
                    comm = f.read().strip()
                children.setdefault(ppid, []).append((int(entry), comm))
            except (OSError, ValueError, IndexError):
                continue
        stack = [int(pane_pid)]
        seen = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            for cpid, ccomm in children.get(pid, []):
                if ccomm in _WRAPPER_COMMS:
                    _WRAPPER_CACHE[target] = (now, True)
                    return True
                stack.append(cpid)
        _WRAPPER_CACHE[target] = (now, False)
        return False
    except Exception:
        return False

# tmux `send-keys -l` has an internal command buffer limit around 16 KB
# (fails with "command too long"). Above this threshold we fall back to
# `load-buffer` (stdin) + `paste-buffer -p`, which has no practical limit
# and, via bracketed paste, is also the semantically correct way to deliver
# large paste operations to bash readline / Claude Code.
_SEND_KEYS_BYTE_LIMIT = 8192

# xdotool key name -> tmux send-keys name
TMUX_KEY_MAP = {
    "ctrl+c": "C-c",
    "ctrl+d": "C-d",
    "ctrl+l": "C-l",
    "ctrl+r": "C-r",
    "ctrl+o": "C-o",
    "ctrl+t": "C-t",
    "ctrl+a": "C-a",
    "ctrl+e": "C-e",
    "ctrl+u": "C-u",
    "ctrl+k": "C-k",
    "ctrl+w": "C-w",
    "ctrl+g": "C-g",
    "ctrl+z": "C-z",
    "ctrl+shift+v": None,  # handled as literal paste
    "shift+Tab": "BTab",
    "Escape": "Escape",
    "Return": "Enter",
    "Up": "Up",
    "Down": "Down",
    "Left": "Left",
    "Right": "Right",
    "Tab": "Tab",
    "Page_Up": "PPage",
    "Page_Down": "NPage",
    "End": "End",
    "Home": "Home",
}

# Claude Code native installs run as version-named binaries (e.g. `2.1.206`).
_VERSION_CMD_RE = re.compile(r"\d+(?:\.\d+){1,3}")


def prettify_command(cmd):
    """Human-readable pane command: version-named binaries render as `claude`."""
    if cmd and _VERSION_CMD_RE.fullmatch(cmd):
        return "claude"
    return cmd


def set_clipboard(text):
    """Set the system clipboard (pbcopy on macOS, xclip on Linux)."""
    if _IS_MAC:
        cmd = ["pbcopy"]
    else:
        cmd = ["xclip", "-selection", "clipboard"]
    proc = subprocess.run(cmd, input=text, text=True, timeout=5)
    return proc.returncode == 0


def get_clipboard():
    """Read the system clipboard (pbpaste on macOS, xclip -o on Linux). Returns text or None."""
    try:
        if _IS_MAC:
            cmd = ["pbpaste"]
        else:
            cmd = ["xclip", "-selection", "clipboard", "-o"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return proc.stdout if proc.returncode == 0 else None
    except Exception:
        return None


def paste_to_terminal():
    """Paste clipboard into the focused terminal.

    macOS: osascript Command+V (requires Accessibility permissions).
    Linux: xdotool Ctrl+Shift+V.
    Fallback-only — prefer tmux path when a target is available.
    """
    if _IS_MAC:
        proc = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using command down'],
            timeout=5,
        )
    else:
        proc = subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "ctrl+shift+v"],
            timeout=5,
        )
    return proc.returncode == 0


def send_keys(keys):
    """Send key combos via xdotool (Linux). Fallback-only — always prefer tmux path.

    On macOS this is a no-op (returns False) because xdotool is not available
    and the tmux path handles every key combo in TMUX_KEY_MAP already.
    """
    if _IS_MAC:
        return False
    proc = subprocess.run(
        ["xdotool", "key", "--clearmodifiers"] + keys.split(),
        timeout=5,
    )
    return proc.returncode == 0


def tmux_send_keys(target, *keys):
    """Send key(s) to a tmux pane."""
    proc = subprocess.run(
        ["tmux", "send-keys", "-t", target] + list(keys),
        timeout=5,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def tmux_send_text(target, text):
    """Send literal text to a tmux pane (no key interpretation).

    For small text, uses `send-keys -l` (fast path, unchanged behavior).
    For text over ~8 KB, uses `load-buffer -` + `paste-buffer -p` to bypass
    tmux's internal `send-keys` command buffer limit (~16 KB). The `-p` flag
    enables bracketed paste when the receiving app has requested it, so
    Claude Code / readline treat large pastes as a single paste block
    instead of N individual keystrokes.
    """
    byte_len = len(text.encode("utf-8", errors="replace"))

    if byte_len <= _SEND_KEYS_BYTE_LIMIT:
        proc = subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", text],
            timeout=5,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    # Large text: stage into a unique paste buffer via stdin, then paste it
    # into the target pane using bracketed paste where supported.
    buf_name = f"assist-{uuid.uuid4().hex[:12]}"
    # Generous timeout scaled by size: ~1s per 100 KB on top of a 10s floor.
    timeout = max(10, byte_len // 100_000)

    load = subprocess.run(
        ["tmux", "load-buffer", "-b", buf_name, "-"],
        input=text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if load.returncode != 0:
        return False

    paste = subprocess.run(
        ["tmux", "paste-buffer", "-b", buf_name, "-t", target, "-p", "-d"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if paste.returncode != 0:
        # `paste-buffer -d` only deletes on success; clean up orphan buffer.
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buf_name],
            capture_output=True,
            timeout=2,
        )
        return False
    return True


def tmux_target_exists(target):
    """Check if a tmux target (session:window.pane) exists."""
    proc = subprocess.run(
        ["tmux", "has-session", "-t", target.split(":")[0]],
        timeout=5,
        capture_output=True,
    )
    return proc.returncode == 0


def detect_venv(project_path):
    """Detect virtualenv directory in a project. Returns relative venv path or None."""
    for venv_dir in ("venv", ".venv", "env"):
        if (project_path / venv_dir / "bin" / "activate").exists():
            return venv_dir
    return None


def capture_pane(target, lines=2000):
    """Capture tmux pane content and info. Returns (content, info) or (None, None).

    When the pane is on the alternate screen (a TUI like Claude Code is
    running), capture only the current screen — alt-screen content does not
    flow into scrollback, and historical main-screen scrollback (e.g. prior
    Claude launch banners) would just be noise. When on the main screen,
    capture up to `lines` of scrollback so shell history is preserved.

    Wrapper sessions (claude inside docker) are captured WITH full
    scrollback like any main-screen pane — the periodic Ctrl+L self-heal in
    the streamer keeps the live region clean, so there's no reason to hide
    history. We only expose `info["is_wrapper"]` so the streamer knows to
    use Ctrl+L (not a resize toggle) when it self-heals.
    """
    info_proc = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            target,
            "-p",
            "#{pane_current_command}\t#{pane_width}\t#{pane_height}\t#{cursor_y}\t#{alternate_on}\t#{pane_pid}\t#{session_attached}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    info = {}
    alternate_on = False
    pane_pid = None
    if info_proc.returncode == 0 and info_proc.stdout.strip():
        parts = info_proc.stdout.strip().split("\t")
        if len(parts) >= 3:
            info = {
                "command": parts[0],
                "width": int(parts[1]),
                "height": int(parts[2]),
            }
            if len(parts) >= 4:
                info["cursor_y"] = int(parts[3])
            if len(parts) >= 5:
                alternate_on = parts[4] == "1"
            if len(parts) >= 6:
                try:
                    pane_pid = int(parts[5])
                except ValueError:
                    pass
            if len(parts) >= 7:
                try:
                    info["session_attached"] = int(parts[6])
                except ValueError:
                    info["session_attached"] = 0
            info["command_display"] = prettify_command(info.get("command", ""))
            info["alternate_on"] = alternate_on

    # Wrapper detection exposed to the streamer so periodic self-heal can
    # send Ctrl+L (which reaches the in-container TUI) instead of a
    # resize-window toggle (which doesn't). Does NOT affect the capture
    # range — wrapper sessions keep full scrollback.
    info["is_wrapper"] = (not alternate_on) and _has_wrapper_descendant(target, pane_pid)
    # Native claude on the main screen needs the same Ctrl+L heal — its
    # diff renderer leaves stale cells that SIGWINCH redraws can't clear.
    info["is_native_tui"] = (not alternate_on) and info.get("command") in _NATIVE_TUI_COMMS

    capture_args = ["tmux", "capture-pane", "-e", "-p", "-t", target]
    # Claude writes its transcript into tmux scrollback even while on the
    # alternate screen (unlike a true TUI such as opencode), so keep full
    # scrollback for it — mirrors the frontend's never-TUI exemption.
    claude_pane = prettify_command(info.get("command", "")) == "claude"
    if alternate_on and not claude_pane:
        capture_args += ["-S", "0"]  # true TUI: visible viewport only
    else:
        capture_args += ["-S", f"-{lines}"]

    # Pane content is arbitrary bytes; under a non-UTF8 locale a bare
    # text=True raises UnicodeDecodeError and silently freezes the stream.
    proc = subprocess.run(
        capture_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    if proc.returncode != 0:
        return None, None

    content = proc.stdout
    lines_list = content.split("\n")
    while lines_list and not lines_list[-1]:
        lines_list.pop()
    content = "\n".join(lines_list)
    return content, info


def tmux_exact_target(target):
    """Return the exact-match form of a `session[:window.pane]` target.

    Exact matching matters: a dead target must resolve to nothing, not
    prefix-match into another live session. BUT tmux 3.4 only honors the
    `=` prefix when the session part is delimited by `:` — bare `=name`
    makes pane-target commands fail ("can't find pane") and display-message
    silently expand every format variable EMPTY. `=name:` pins exact
    session matching and resolves to the session's active pane.
    """
    name, _sep, rest = target.partition(":")
    return f"={name}:{rest}"


def pane_wants_ctrl_l_heal(target):
    """True when a full repaint of `target` needs Ctrl+L rather than SIGWINCH.

    Two kinds of pane qualify: native claude (diff renderer on the main
    screen — resize-driven redraws are the ones that leave artifacts) and
    wrapper TUIs (docker/podman — SIGWINCH never reaches the in-container
    process, but send-keys does). Best-effort: False on any failure.
    """
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-t", tmux_exact_target(target), "-p",
             "#{pane_current_command}\t#{pane_pid}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=2,
        )
        if proc.returncode != 0:
            return False
        parts = proc.stdout.strip().split("\t")
        if parts and parts[0] in _NATIVE_TUI_COMMS:
            return True
        pane_pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        return _has_wrapper_descendant(target, pane_pid)
    except Exception:
        return False


def set_ws_send_timeout(ws):
    """Set a send timeout on the underlying socket so ws.send() fails fast.

    Uses SO_SNDTIMEO (send-only timeout) so the receive loop in the handler
    thread is not affected.
    """
    try:
        import socket
        import struct

        sock = ws.sock
        timeval = struct.pack("ll", WS_SEND_TIMEOUT, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, timeval)
    except Exception:
        pass
