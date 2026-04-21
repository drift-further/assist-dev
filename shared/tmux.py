"""shared/tmux.py — tmux and X11 interaction helpers."""

import subprocess
import uuid

from shared.state import WS_SEND_TIMEOUT

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
}


def set_clipboard(text):
    """Set the X11 clipboard via xclip."""
    proc = subprocess.run(
        ["xclip", "-selection", "clipboard"],
        input=text,
        text=True,
        timeout=5,
    )
    return proc.returncode == 0


def paste_to_terminal():
    """Paste clipboard into the focused terminal via xdotool Ctrl+Shift+V."""
    proc = subprocess.run(
        ["xdotool", "key", "--clearmodifiers", "ctrl+shift+v"],
        timeout=5,
    )
    return proc.returncode == 0


def send_keys(keys):
    """Send one or more key combos via xdotool. keys is a space-separated string."""
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
    """Capture tmux pane content and info. Returns (content, info) or (None, None)."""
    proc = subprocess.run(
        ["tmux", "capture-pane", "-e", "-p", "-t", target, "-S", f"-{lines}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return None, None

    info_proc = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            target,
            "-p",
            "#{pane_current_command}\t#{pane_width}\t#{pane_height}\t#{cursor_y}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    info = {}
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

    content = proc.stdout
    lines_list = content.split("\n")
    while lines_list and not lines_list[-1]:
        lines_list.pop()
    content = "\n".join(lines_list)
    return content, info


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
