"""routes/streaming.py — WebSocket terminal streaming."""

import json
import threading
import time

import shared.state as state
from shared.security import origin_allowed
from shared.tmux import (
    capture_pane,
    set_ws_send_timeout,
)

# The sock route is registered via register_streaming() called from serve.py.
# We cannot use a Blueprint for @sock.route — flask-sock requires the app-level Sock instance.

_sock = None  # Set by register_streaming()

# Default scrollback lines for a stream when the client doesn't specify.
# Used both before the first message arrives and as the parse fallback.
_DEFAULT_LINES = 2000

def _tui_flag(msg):
    """Normalize a subscribe message's per-pane TUI override.

    True/False pin the capture range to match the mode the chip is showing;
    absent (None) leaves detection to capture_pane, as before the toggle.
    """
    v = msg.get("tui")
    return None if v is None else bool(v)


def _full_frame(target, lines, tui=None):
    """Capture `target` and return the JSON 'full' frame, or an 'error' frame
    when the capture fails (dead/unknown target) so the client doesn't sit on
    a healthy-looking but frozen terminal. Returns (frame_json, info)."""
    content, info = capture_pane(target, lines, tui=tui)
    if content is None:
        return (
            json.dumps({"type": "error", "error": "Target not found", "target": target}),
            None,
        )
    return (
        json.dumps(
            {
                "type": "full",
                "content": content,
                "target": target,
                "info": info,
                "ts": time.time(),
            }
        ),
        info,
    )


def register_streaming(sock_instance):
    """Register the WebSocket route on the given Sock instance.

    Called from serve.py after app and sock are created.
    """
    global _sock
    _sock = sock_instance

    @_sock.route("/terminal/stream")
    def terminal_stream(ws):
        """WebSocket endpoint for real-time terminal streaming."""
        # WS handshakes bypass CORS entirely — enforce the same Origin
        # allowlist as the HTTP before_request hook (shared/security.py).
        if not origin_allowed(ws.environ.get("HTTP_ORIGIN")):
            try:
                ws.close()
            except Exception:
                pass
            return

        target = None
        lines = _DEFAULT_LINES
        tui = None

        try:
            raw = ws.receive(timeout=5)
            if raw:
                msg = json.loads(raw)
                target = msg.get("target", state.tmux_target)
                lines = min(int(msg.get("lines", _DEFAULT_LINES)), 20000)
                tui = _tui_flag(msg)
        except Exception:
            target = state.tmux_target

        # One entry per connection, mutated in place on resubscribe. The lock
        # serializes ws.send() across the three threads that write to this
        # socket (handler, streamer, autoyes broadcaster) — simple_websocket's
        # send() has no internal lock and can interleave partial writes.
        client = {
            "ws": ws,
            "lock": threading.Lock(),
            "target": target,
            "lines": lines,
            "tui": tui,
            "last_send": time.time(),
        }

        if not target:
            try:
                _send_to(client, json.dumps({"type": "error", "error": "No target specified"}))
            except Exception:
                pass
            return

        # First frame goes out immediately.
        try:
            frame, _info = _full_frame(target, lines, tui)
            _send_to(client, frame)
        except Exception:
            return

        set_ws_send_timeout(ws)

        with state.ws_lock:
            cache_key = f"{target}:{lines}:{tui}"
            state.ws_last_content.pop(cache_key, None)
            state.ws_clients.append(client)
        _ensure_streamer()

        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "ping":
                        _send_to(client, json.dumps({"type": "pong"}))
                        continue
                    if msg.get("type") == "subscribe":
                        new_target = msg.get("target", target)
                        new_lines = min(int(msg.get("lines", lines)), 20000)
                        new_tui = _tui_flag(msg)
                        with state.ws_lock:
                            cache_key = f"{new_target}:{new_lines}:{new_tui}"
                            state.ws_last_content.pop(cache_key, None)
                            client["target"] = new_target
                            client["lines"] = new_lines
                            client["tui"] = new_tui
                        target = new_target
                        lines = new_lines
                        tui = new_tui
                        _ensure_streamer()
                        frame, _info = _full_frame(target, lines, tui)
                        _send_to(client, frame)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            _remove_ws_client(ws)


def _terminal_streamer():
    """Background thread: polls tmux, pushes changes to WebSocket clients."""
    _empty_count = 0
    try:
        while True:
            try:
                with state.ws_lock:
                    if not state.ws_clients:
                        _empty_count += 1
                        if _empty_count > 50:
                            # Exit decision and flag flip must be atomic:
                            # a client connecting in between would see a
                            # live-but-doomed streamer and get no frames.
                            # Inside ws_lock, ws_clients is still empty here
                            # and _ensure_streamer will start a fresh thread
                            # for the next connect.
                            state.ws_streamer_running = False
                            return
                        clients = []
                    else:
                        _empty_count = 0
                        # Snapshot (entry, target, lines, tui) under the lock —
                        # subscribe mutates entries in place.
                        clients = [
                            (c, c["target"], c["lines"], c.get("tui"))
                            for c in state.ws_clients
                        ]

                if not clients:
                    # Sleep OUTSIDE ws_lock — holding it here starves
                    # connecting handlers and the autoyes broadcaster.
                    time.sleep(0.1)
                    continue

                now = time.time()

                # Grouped by TUI override too: two clients on the same pane with
                # different overrides need different capture ranges, so they
                # can't share a capture or a dedup entry.
                targets = {}
                for client, target, lines, tui in clients:
                    key = (target, lines, tui)
                    if key not in targets:
                        targets[key] = []
                    targets[key].append(client)

                for (target, lines, tui), group in targets.items():
                    try:
                        content, info = capture_pane(target, lines, tui=tui)
                    except Exception:
                        continue
                    if content is None:
                        continue

                    cache_key = f"{target}:{lines}:{tui}"
                    with state.ws_lock:
                        prev_content = state.ws_last_content.get(cache_key)

                    if prev_content == content:
                        continue

                    msg_data = {
                        "type": "full",
                        "content": content,
                        "target": target,
                        "info": info,
                        "ts": now,
                    }

                    with state.ws_lock:
                        state.ws_last_content[cache_key] = content

                    try:
                        msg = json.dumps(msg_data)
                    except Exception:
                        continue

                    for client in group:
                        try:
                            _send_to(client, msg)
                        except Exception:
                            _remove_ws_client(client["ws"])

                # Per-client heartbeats: a client watching a quiet target
                # still needs traffic inside the frontend's 8s inactivity
                # window even while OTHER targets are busy — a global
                # sent_any flag starves it into a reconnect loop.
                for client, _t, _l, _tui in clients:
                    if now - client["last_send"] >= state.WS_HEARTBEAT_INTERVAL:
                        try:
                            _send_to(client, json.dumps({"type": "heartbeat", "ts": now}))
                        except Exception:
                            _remove_ws_client(client["ws"])

                # Keys are (target, lines, tui) and the dedup cache is written as
                # f"{target}:{lines}:{tui}" (see cache_key above) — both the arity
                # and the format have to match, or this silently raises and the
                # whole cleanup below is skipped for the life of the process.
                active_keys = {f"{t}:{l}:{tui}" for t, l, tui in targets}
                with state.ws_lock:
                    stale = [k for k in state.ws_last_content if k not in active_keys]
                    for k in stale:
                        del state.ws_last_content[k]

            except Exception:
                time.sleep(0.5)

            time.sleep(0.1)
    except Exception:
        pass
    finally:
        with state.ws_lock:
            # Only clear the flag if no replacement streamer has started
            # since (the normal-exit path above already cleared it, which
            # lets _ensure_streamer spawn a successor before this runs).
            if state.ws_streamer_thread is threading.current_thread():
                state.ws_streamer_running = False


def _send_to(client, msg):
    """Send on a client's socket, serialized by its per-client lock.

    simple_websocket's send() is not thread-safe; the streamer, the WS
    handler thread, and the autoyes broadcaster can all write to the same
    socket. Also stamps last_send for per-client heartbeat bookkeeping.
    """
    with client["lock"]:
        client["ws"].send(msg)
        client["last_send"] = time.time()


def _remove_ws_client(ws):
    with state.ws_lock:
        state.ws_clients[:] = [c for c in state.ws_clients if c["ws"] is not ws]


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
    for client in clients:
        try:
            _send_to(client, msg)
        except Exception:
            _remove_ws_client(client["ws"])
