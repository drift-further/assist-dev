"""routes/streaming.py — WebSocket terminal streaming."""

import hashlib
import json
import subprocess
import threading
import time

import shared.state as state
from shared.tmux import capture_pane, set_ws_send_timeout

# The sock route is registered via register_streaming() called from serve.py.
# We cannot use a Blueprint for @sock.route — flask-sock requires the app-level Sock instance.

_sock = None  # Set by register_streaming()

# How long to wait after the resize toggle for the TUI to flush its redraw
# before we snapshot the new content hash. 0.3s is plenty for Ink/ncurses.
_REDRAW_FLUSH_SEC = 0.3


def _force_redraw(target):
    """Force a TUI in `target` to fully repaint, clearing tmux-grid artifacts.

    Why: tmux pane cells outside a TUI's current redraw region can hold stale
    content from earlier output. capture-pane returns the grid verbatim, so the
    browser sees the staleness as overlapping/garbled characters. A real tmux
    client attach fixes this because attaching triggers ioctl(TIOCSWINSZ) on
    the pty, which delivers SIGWINCH from the kernel to the foreground process
    group of the controlling tty (the actual TUI — e.g. node/claude — not its
    bash parent), and the TUI does a full clear-and-redraw.

    Bare SIGWINCH is not enough: TUIs typically no-op the signal handler when
    the size hasn't changed. We replicate the attach behavior by briefly
    resizing the window by one row, then back. That's a real size change → real
    SIGWINCH delivery via the tty layer → full clear-and-redraw.

    Side-effect we have to undo: tmux's `resize-window` pins the window's
    `window-size` option to `manual`. We save the prior value and restore it.

    Side-effect we have to suppress: the TUI's repaint changes the captured
    content. The poll loop hashes captures and bumps `pane_last_activity` on
    any change, which would reclassify an idle session as active. After the
    redraw we snapshot the new content hash and restore the prior activity
    timestamp so the next poll sees no change.

    Skipped if a real tmux client is attached — don't tug on someone's terminal
    out from under them. Best-effort otherwise: failure is silent.
    """
    try:
        info = subprocess.run(
            ["tmux", "display-message", "-t", target, "-p",
             "#{pane_width}\t#{pane_height}\t#{session_attached}"],
            capture_output=True, text=True, timeout=2,
        )
        if info.returncode != 0:
            return
        parts = info.stdout.strip().split("\t")
        if len(parts) < 3:
            return
        try:
            w = int(parts[0])
            h = int(parts[1])
            attached = int(parts[2] or "0")
        except ValueError:
            return
        if attached > 0 or h < 2:
            return

        with state._activity_lock:
            saved_activity = state.pane_last_activity.get(target)

        opt = subprocess.run(
            ["tmux", "show-options", "-t", target, "-w", "-v", "window-size"],
            capture_output=True, text=True, timeout=2,
        )
        old_window_size = (
            opt.stdout.strip()
            if opt.returncode == 0 and opt.stdout.strip()
            else None
        )

        subprocess.run(
            ["tmux", "resize-window", "-t", target, "-x", str(w), "-y", str(h - 1)],
            capture_output=True, timeout=2,
        )
        subprocess.run(
            ["tmux", "resize-window", "-t", target, "-x", str(w), "-y", str(h)],
            capture_output=True, timeout=2,
        )

        if old_window_size:
            subprocess.run(
                ["tmux", "set-option", "-t", target, "-w",
                 "window-size", old_window_size],
                capture_output=True, timeout=2,
            )
        else:
            subprocess.run(
                ["tmux", "set-option", "-t", target, "-w", "-u", "window-size"],
                capture_output=True, timeout=2,
            )

        time.sleep(_REDRAW_FLUSH_SEC)

        cap = subprocess.run(
            ["tmux", "capture-pane", "-e", "-p", "-t", target, "-S", "-60"],
            capture_output=True, text=True, timeout=2,
        )
        if cap.returncode == 0:
            tail = cap.stdout.rstrip("\n")
            if tail:
                new_hash = hashlib.md5(tail.encode()).hexdigest()
                with state._activity_lock:
                    state.pane_content_hash[target] = new_hash
                    if saved_activity is not None:
                        state.pane_last_activity[target] = saved_activity
    except Exception:
        pass


def register_streaming(sock_instance):
    """Register the WebSocket route on the given Sock instance.

    Called from serve.py after app and sock are created.
    """
    global _sock
    _sock = sock_instance

    @_sock.route("/terminal/stream")
    def terminal_stream(ws):
        """WebSocket endpoint for real-time terminal streaming."""
        target = None
        lines = 200

        try:
            raw = ws.receive(timeout=5)
            if raw:
                msg = json.loads(raw)
                target = msg.get("target", state.tmux_target)
                lines = min(int(msg.get("lines", 2000)), 20000)
        except Exception:
            target = state.tmux_target

        if not target:
            try:
                ws.send(json.dumps({"type": "error", "error": "No target specified"}))
            except Exception:
                pass
            return

        # Force a clean repaint BEFORE the initial capture so the first frame
        # the browser renders is already free of stale tmux-grid artifacts.
        _force_redraw(target)

        try:
            content, info = capture_pane(target, lines)
            if content is not None:
                ws.send(
                    json.dumps(
                        {
                            "type": "full",
                            "content": content,
                            "target": target,
                            "info": info,
                            "ts": time.time(),
                        }
                    )
                )
        except Exception:
            return

        set_ws_send_timeout(ws)

        with state.ws_lock:
            cache_key = f"{target}:{lines}"
            state.ws_last_content.pop(cache_key, None)
            state.ws_clients.append((ws, target, lines))
        _ensure_streamer()

        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "ping":
                        ws.send(json.dumps({"type": "pong"}))
                        continue
                    if msg.get("type") == "subscribe":
                        new_target = msg.get("target", target)
                        new_lines = min(int(msg.get("lines", lines)), 20000)
                        # Repaint BEFORE the resubscribe capture so the user
                        # sees a clean first frame on the new tab.
                        _force_redraw(new_target)
                        with state.ws_lock:
                            state.ws_clients[:] = [
                                (w, t, l) for w, t, l in state.ws_clients if w is not ws
                            ]
                            cache_key = f"{new_target}:{new_lines}"
                            state.ws_last_content.pop(cache_key, None)
                            state.ws_clients.append((ws, new_target, new_lines))
                        target = new_target
                        lines = new_lines
                        _ensure_streamer()
                        content, info = capture_pane(target, lines)
                        if content is not None:
                            ws.send(
                                json.dumps(
                                    {
                                        "type": "full",
                                        "content": content,
                                        "target": target,
                                        "info": info,
                                        "ts": time.time(),
                                    }
                                )
                            )
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            _remove_ws_client(ws)


def _terminal_streamer():
    """Background thread: polls tmux, pushes changes to WebSocket clients."""
    _empty_count = 0
    _last_heartbeat = time.time()
    try:
        while True:
            try:
                with state.ws_lock:
                    if not state.ws_clients:
                        _empty_count += 1
                        if _empty_count > 50:
                            return
                        time.sleep(0.1)
                        continue
                    _empty_count = 0
                    clients = list(state.ws_clients)

                now = time.time()

                targets = {}
                for ws, target, lines in clients:
                    key = (target, lines)
                    if key not in targets:
                        targets[key] = []
                    targets[key].append(ws)

                sent_any = False
                for (target, lines), sockets in targets.items():
                    try:
                        content, info = capture_pane(target, lines)
                    except Exception:
                        continue
                    if content is None:
                        continue

                    cache_key = f"{target}:{lines}"
                    prev_content = state.ws_last_content.get(cache_key)

                    if prev_content == content:
                        continue

                    # Determine if we can send a delta (append-only optimization)
                    msg_data = None
                    if prev_content is not None:
                        prev_lines = prev_content.split("\n")
                        new_lines = content.split("\n")
                        # Check for simple tail append: prev is a prefix of new
                        if (
                            len(new_lines) > len(prev_lines)
                            and new_lines[: len(prev_lines)] == prev_lines
                        ):
                            appended = new_lines[len(prev_lines) :]
                            msg_data = {
                                "type": "delta",
                                "ops": [{"op": "append", "lines": appended}],
                                "target": target,
                                "info": info,
                                "ts": now,
                            }
                        # Check for tail replacement: first N lines match, rest changed
                        elif len(new_lines) >= len(prev_lines):
                            common = 0
                            for i in range(min(len(prev_lines), len(new_lines))):
                                if prev_lines[i] != new_lines[i]:
                                    break
                                common = i + 1
                            if (
                                common > len(new_lines) * 0.5
                            ):  # >50% unchanged = worth a delta
                                msg_data = {
                                    "type": "delta",
                                    "ops": [
                                        {
                                            "op": "replace",
                                            "start": common,
                                            "end": len(prev_lines),
                                            "lines": new_lines[common:],
                                        }
                                    ],
                                    "target": target,
                                    "info": info,
                                    "ts": now,
                                }

                    # Fallback to full content
                    if msg_data is None:
                        msg_data = {
                            "type": "full",
                            "content": content,
                            "target": target,
                            "info": info,
                            "ts": now,
                        }

                    state.ws_last_content[cache_key] = content

                    try:
                        msg = json.dumps(msg_data)
                    except Exception:
                        continue

                    for ws in sockets:
                        try:
                            ws.send(msg)
                            sent_any = True
                        except Exception:
                            _remove_ws_client(ws)

                if (
                    not sent_any
                    and (now - _last_heartbeat) >= state.WS_HEARTBEAT_INTERVAL
                ):
                    hb_msg = json.dumps({"type": "heartbeat", "ts": now})
                    for ws, _t, _l in clients:
                        try:
                            ws.send(hb_msg)
                        except Exception:
                            _remove_ws_client(ws)
                    _last_heartbeat = now
                elif sent_any:
                    _last_heartbeat = now

                active_keys = {f"{t}:{l}" for t, l in targets}
                stale = [k for k in state.ws_last_content if k not in active_keys]
                for k in stale:
                    del state.ws_last_content[k]

            except Exception:
                time.sleep(0.5)

            time.sleep(0.1)
    except Exception:
        pass
    finally:
        state.ws_streamer_running = False


def _remove_ws_client(ws):
    with state.ws_lock:
        state.ws_clients[:] = [(w, t, l) for w, t, l in state.ws_clients if w is not ws]


def _ensure_streamer():
    with state.ws_lock:
        if state.ws_streamer_running and (
            state.ws_streamer_thread is None or not state.ws_streamer_thread.is_alive()
        ):
            state.ws_streamer_running = False
        if not state.ws_streamer_running:
            state.ws_streamer_running = True
            state.ws_streamer_thread = threading.Thread(
                target=_terminal_streamer, daemon=True
            )
            state.ws_streamer_thread.start()


def broadcast_autoyes_event(target, event, prompt_type):
    """Send auto-yes event to all connected WebSocket clients.

    Called by routes/autoyes.py to broadcast countdown/fired/cancelled events.
    """
    with state.autoyes_lock:
        cd = state.autoyes_countdowns.get(target)
        remaining = (
            max(0, cd["deadline"] - time.time()) if cd and event == "countdown" else 0
        )
        delay = cd.get("delay", state.AUTOYES_DELAY) if cd else state.AUTOYES_DELAY
        summary = cd.get("summary") if cd else None

    msg = json.dumps(
        {
            "type": "autoyes",
            "event": event,
            "target": target,
            "prompt_type": prompt_type,
            "remaining": round(remaining, 1),
            "delay": delay,
            "summary": summary,
        }
    )

    with state.ws_lock:
        clients = list(state.ws_clients)
    for ws, _, _ in clients:
        try:
            ws.send(msg)
        except Exception:
            _remove_ws_client(ws)
