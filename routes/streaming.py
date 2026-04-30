"""routes/streaming.py — WebSocket terminal streaming."""

import json
import os
import signal
import subprocess
import threading
import time

import shared.state as state
from shared.tmux import capture_pane, set_ws_send_timeout

# The sock route is registered via register_streaming() called from serve.py.
# We cannot use a Blueprint for @sock.route — flask-sock requires the app-level Sock instance.

_sock = None  # Set by register_streaming()


def _force_repaint(target):
    """Send SIGWINCH to target's pane process to force a TUI repaint.

    Why: tmux pane cells outside a TUI's current redraw region can hold stale
    content from earlier output. capture-pane returns the full grid verbatim,
    so the browser sees the staleness as artifacts. A real tmux client attach
    fixes this only because it triggers a SIGWINCH (size-changed) repaint as
    a side-effect. We replicate just that signal — no stdin contamination,
    no behavior change for non-TUI processes (which ignore SIGWINCH).

    Best-effort: failure is silent.
    """
    try:
        proc = subprocess.run(
            ["tmux", "list-panes", "-t", target, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        pid = proc.stdout.strip().split("\n")[0]
        if pid.isdigit():
            os.kill(int(pid), signal.SIGWINCH)
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
            is_first = not any(t == target for _w, t, _l in state.ws_clients)
            cache_key = f"{target}:{lines}"
            state.ws_last_content.pop(cache_key, None)
            state.ws_clients.append((ws, target, lines))
        if is_first:
            _force_repaint(target)
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
                        with state.ws_lock:
                            state.ws_clients[:] = [
                                (w, t, l) for w, t, l in state.ws_clients if w is not ws
                            ]
                            is_first = not any(
                                t == new_target for _w, t, _l in state.ws_clients
                            )
                            cache_key = f"{new_target}:{new_lines}"
                            state.ws_last_content.pop(cache_key, None)
                            state.ws_clients.append((ws, new_target, new_lines))
                        if is_first:
                            _force_repaint(new_target)
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
