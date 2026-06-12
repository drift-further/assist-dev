"""routes/streaming.py — WebSocket terminal streaming."""

import hashlib
import json
import subprocess
import threading
import time

import shared.state as state
from shared.security import origin_allowed
from shared.tmux import (
    _NATIVE_TUI_COMMS,
    _has_wrapper_descendant,
    capture_pane,
    set_ws_send_timeout,
    tmux_exact_target,
)

# The sock route is registered via register_streaming() called from serve.py.
# We cannot use a Blueprint for @sock.route — flask-sock requires the app-level Sock instance.

_sock = None  # Set by register_streaming()

# Default scrollback lines for a stream when the client doesn't specify.
# Used both before the first message arrives and as the parse fallback.
_DEFAULT_LINES = 2000

# How long to wait after the resize toggle for the TUI to flush its redraw
# before we snapshot the new content hash. 0.3s is plenty for Ink/ncurses.
_REDRAW_FLUSH_SEC = 0.3

# Floor sizes for the redraw toggle. If a previous toggle left the pane stuck
# tiny (e.g. second resize-window call failed), heal it back up to a usable
# size instead of preserving the broken state. Matches the floors used in
# /terminal/resize and js/terminal.js:_calcTermSize.
_MIN_REDRAW_COLS = 40
_MIN_REDRAW_ROWS = 60

# Periodic self-heal: when a TUI session has been streaming-stable (no content
# change) for this long AND we haven't redrawn in the throttle window, fire a
# background _force_redraw to clear any accumulated tmux-grid artifacts.
_REDRAW_STABLE_SEC = 3.0
_REDRAW_THROTTLE_SEC = 10.0

# Per-target timestamps for the self-heal heuristic. Written by the streamer
# thread and (via _maybe_redraw_async) WS handler threads; plain dict ops are
# fine here — a lost-update race only skews the throttle by one window.
_stable_since: dict[str, float] = {}
_last_redraw_time: dict[str, float] = {}


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
        # `=name:` (colon required!) forces exact session-name matching — a
        # dead target must fail, not prefix-match into resizing some other
        # live session's pane. Bare `=name` is broken on tmux 3.4: pane-target
        # commands reject it and display-message expands formats empty.
        exact = tmux_exact_target(target)
        info = subprocess.run(
            ["tmux", "display-message", "-t", exact, "-p",
             "#{pane_width}\t#{pane_height}\t#{session_attached}\t#{pane_pid}\t#{pane_current_command}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=2,
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
        if attached > 0:
            return
        pane_pid = None
        if len(parts) >= 4:
            try:
                pane_pid = int(parts[3])
            except ValueError:
                pass
        cmd = parts[4] if len(parts) >= 5 else ""

        target_w = max(w, _MIN_REDRAW_COLS)
        target_h = max(h, _MIN_REDRAW_ROWS)

        with state._activity_lock:
            saved_activity = state.pane_last_activity.get(target)

        is_wrapper = _has_wrapper_descendant(target, pane_pid)

        if is_wrapper or cmd in _NATIVE_TUI_COMMS:
            # Wrapper TUI (e.g. claude inside docker via claude-mount.sh):
            # the in-container TUI's foreground pty is several hops from the
            # host pane, so SIGWINCH-via-resize can't reach it. Ctrl+L does,
            # because tmux send-keys goes through the host pty -> docker
            # stdin -> container pty -> claude, and claude implements C-l as
            # full clear-and-redraw. This wipes the dirty cells without
            # losing any in-flight typed input (claude re-renders it).
            #
            # Native host claude gets the same treatment for the opposite
            # reason: SIGWINCH *does* reach it, but its diff renderer skips
            # cells it believes unchanged, so a resize-driven redraw is what
            # LEAVES stale cells. Only C-l invalidates its screen model.
            subprocess.run(
                ["tmux", "send-keys", "-t", exact, "C-l"],
                capture_output=True, timeout=2,
            )
        else:
            opt = subprocess.run(
                ["tmux", "show-options", "-t", exact, "-w", "-v", "window-size"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=2,
            )
            old_window_size = (
                opt.stdout.strip()
                if opt.returncode == 0 and opt.stdout.strip()
                else None
            )

            subprocess.run(
                ["tmux", "resize-window", "-t", exact, "-x", str(target_w), "-y", str(target_h - 1)],
                capture_output=True, timeout=2,
            )
            subprocess.run(
                ["tmux", "resize-window", "-t", exact, "-x", str(target_w), "-y", str(target_h)],
                capture_output=True, timeout=2,
            )

            if old_window_size:
                subprocess.run(
                    ["tmux", "set-option", "-t", exact, "-w",
                     "window-size", old_window_size],
                    capture_output=True, timeout=2,
                )
            else:
                subprocess.run(
                    ["tmux", "set-option", "-t", exact, "-w", "-u", "window-size"],
                    capture_output=True, timeout=2,
                )

        time.sleep(_REDRAW_FLUSH_SEC)

        cap = subprocess.run(
            ["tmux", "capture-pane", "-e", "-p", "-t", exact, "-S", "-60"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=2,
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


def _maybe_redraw_async(target, info):
    """Kick a background repaint on connect/subscribe for TUI/wrapper panes.

    Plain shell panes don't accumulate stale alt-screen cells — skip them so
    a connect doesn't pay the redraw cost (or tug at the pane) for nothing.
    Throttled via _last_redraw_time so reconnect churn can't fire redraws
    back-to-back; the streamer's periodic self-heal shares the same clock.
    """
    if not info or not (
        info.get("alternate_on") or info.get("is_wrapper") or info.get("is_native_tui")
    ):
        return
    now = time.time()
    if now - _last_redraw_time.get(target, 0.0) < _REDRAW_THROTTLE_SEC:
        return
    _last_redraw_time[target] = now
    threading.Thread(target=_force_redraw, args=(target,), daemon=True).start()


def _full_frame(target, lines):
    """Capture `target` and return the JSON 'full' frame, or an 'error' frame
    when the capture fails (dead/unknown target) so the client doesn't sit on
    a healthy-looking but frozen terminal. Returns (frame_json, info)."""
    content, info = capture_pane(target, lines)
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
        if not origin_allowed(
            ws.environ.get("HTTP_ORIGIN"), ws.environ.get("HTTP_HOST")
        ):
            try:
                ws.close()
            except Exception:
                pass
            return

        target = None
        lines = _DEFAULT_LINES

        try:
            raw = ws.receive(timeout=5)
            if raw:
                msg = json.loads(raw)
                target = msg.get("target", state.tmux_target)
                lines = min(int(msg.get("lines", _DEFAULT_LINES)), 20000)
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
            "last_send": time.time(),
        }

        if not target:
            try:
                _send_to(client, json.dumps({"type": "error", "error": "No target specified"}))
            except Exception:
                pass
            return

        # First frame goes out immediately — even if the pane has stale
        # grid artifacts. Any repaint runs in the background afterwards and
        # the streamer pushes the cleaned frame when the content changes.
        try:
            frame, info = _full_frame(target, lines)
            _send_to(client, frame)
            _maybe_redraw_async(target, info)
        except Exception:
            return

        set_ws_send_timeout(ws)

        with state.ws_lock:
            cache_key = f"{target}:{lines}"
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
                        with state.ws_lock:
                            cache_key = f"{new_target}:{new_lines}"
                            state.ws_last_content.pop(cache_key, None)
                            client["target"] = new_target
                            client["lines"] = new_lines
                        target = new_target
                        lines = new_lines
                        _ensure_streamer()
                        frame, info = _full_frame(target, lines)
                        _send_to(client, frame)
                        _maybe_redraw_async(target, info)
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
                        # Snapshot (entry, target, lines) under the lock —
                        # subscribe mutates entries in place.
                        clients = [
                            (c, c["target"], c["lines"]) for c in state.ws_clients
                        ]

                if not clients:
                    # Sleep OUTSIDE ws_lock — holding it here starves
                    # connecting handlers and the autoyes broadcaster.
                    time.sleep(0.1)
                    continue

                now = time.time()

                targets = {}
                for client, target, lines in clients:
                    key = (target, lines)
                    if key not in targets:
                        targets[key] = []
                    targets[key].append(client)

                for (target, lines), group in targets.items():
                    try:
                        content, info = capture_pane(target, lines)
                    except Exception:
                        continue
                    if content is None:
                        continue

                    cache_key = f"{target}:{lines}"
                    with state.ws_lock:
                        prev_content = state.ws_last_content.get(cache_key)

                    if prev_content == content:
                        # Self-heal: a TUI (alt-screen on host, native
                        # claude on the main screen, or wrapped in
                        # docker/podman) that's been quiet for a few
                        # seconds may have left stale grid cells from
                        # cursor-position writes that never triggered a
                        # full repaint. _force_redraw picks the right
                        # strategy per kind (resize toggle vs Ctrl+L) and
                        # runs in a daemon thread so the 0.3s flush doesn't
                        # block the poll. Throttled so it can't churn.
                        if info and (
                            info.get("alternate_on")
                            or info.get("is_wrapper")
                            or info.get("is_native_tui")
                        ):
                            stable_for = now - _stable_since.get(target, now)
                            since_redraw = now - _last_redraw_time.get(target, 0.0)
                            if stable_for >= _REDRAW_STABLE_SEC and since_redraw >= _REDRAW_THROTTLE_SEC:
                                _last_redraw_time[target] = now
                                threading.Thread(
                                    target=_force_redraw, args=(target,), daemon=True
                                ).start()
                        continue

                    _stable_since[target] = now

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
                for client, _t, _l in clients:
                    if now - client["last_send"] >= state.WS_HEARTBEAT_INTERVAL:
                        try:
                            _send_to(client, json.dumps({"type": "heartbeat", "ts": now}))
                        except Exception:
                            _remove_ws_client(client["ws"])

                active_keys = {f"{t}:{l}" for t, l in targets}
                with state.ws_lock:
                    stale = [k for k in state.ws_last_content if k not in active_keys]
                    for k in stale:
                        del state.ws_last_content[k]

                active_targets = {t for t, _ in targets}
                for d in (_stable_since, _last_redraw_time):
                    for t in [t for t in list(d) if t not in active_targets]:
                        del d[t]

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
