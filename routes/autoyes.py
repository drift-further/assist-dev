"""routes/autoyes.py — Server-side auto-yes: scan prompts, countdown, fire."""

import logging
import re
import subprocess
import threading
import time

from flask import Blueprint, jsonify, request

import shared.state as state
from shared.tmux import tmux_send_keys, tmux_send_text
from routes.streaming import broadcast_autoyes_event

log = logging.getLogger(__name__)

autoyes_bp = Blueprint("autoyes_bp", __name__)

# Prompt patterns (server-side mirrors of JS SMART_PATTERNS)
_PERMISSION_YNA_RE = re.compile(
    r"\(y/n/a\)|\[Y/n/a\]|Allow once.*Always allow.*Deny"
    r"|Yes.*\(y\).*Always.*\(a\).*No.*\(n\)",
    re.IGNORECASE,
)
_CONFIRM_YN_RE = re.compile(r"\(y/n\)|\[Y/n\]|\[y/N\]|\(yes/no\)", re.IGNORECASE)
# Bracket/word-style prompts ([Y/n], [y/N], (yes/no)) are readline prompts
# that wait for Enter — a bare "y" just echoes and re-triggers a fresh
# countdown each tick (yyy…). TUI-style (y/n) prompts react to the bare key.
_CONFIRM_NEEDS_ENTER_RE = re.compile(r"\[Y/n\]|\[y/N\]|\(yes/no\)", re.IGNORECASE)
_NUMBERED_YES_RE = re.compile(
    r"(?:^|\n)\s*(?:[^\d\s]\s*)?1[\.\)]\s*Yes\b", re.IGNORECASE
)
# Detect highlighted/selected "Yes" option (arrow-key style, no number)
# Matches lines like: ❯ Yes, ❯ Yes   (with optional trailing comma/text)
_SELECTED_YES_RE = re.compile(r"(?:^|\n)\s*❯\s*Yes\b", re.IGNORECASE)
_NUMBERED_FOOTER_RE = re.compile(r"(?:Enter to select|Esc to cancel)\s*[·•]")


def _detect_autoyes_prompt(tail):
    """Detect prompts that auto-yes should answer. Returns (type, send_text, with_enter, summary) or None."""
    # Only check last N lines for y/n prompts — avoids false positives from
    # answered prompts still in scrollback
    lines = tail.split("\n")
    depth = state.get_setting("autoyes", "detection_depth")
    bottom = "\n".join(lines[-depth:])
    if _PERMISSION_YNA_RE.search(bottom):
        return ("permission-yna", "y", False, _extract_summary(tail, "permission"))
    # confirm-yn must sit on the LAST non-empty line — a real interactive
    # prompt waits at the bottom of the pane. A (y/n) merely *displayed*
    # mid-screen (e.g. Claude printing code containing prompts) is not one.
    last_line = ""
    for ln in reversed(lines):
        if ln.strip():
            last_line = ln
            break
    if _CONFIRM_YN_RE.search(last_line):
        if not re.search(r"\(y/n/a\)|\[Y/n/a\]", last_line, re.IGNORECASE):
            with_enter = bool(_CONFIRM_NEEDS_ENTER_RE.search(last_line))
            return ("confirm-yn", "y", with_enter, _extract_summary(tail, "confirm"))
    # Numbered prompts: Claude Code renders its TodoWrite/status panel BELOW
    # the "Esc to cancel · …" footer when tasks are active, pushing the
    # footer out of `bottom`. Anchor on the LAST footer in the tail and
    # require the Yes option just above it. Bound the footer's distance from
    # the bottom (depth*4) to ignore stale footers buried in scrollback.
    last_footer = None
    for m in _NUMBERED_FOOTER_RE.finditer(tail):
        last_footer = m
    if last_footer:
        footer_line = tail.count("\n", 0, last_footer.start())
        if (len(lines) - 1 - footer_line) <= depth * 4:
            region_start = max(0, footer_line - 6)
            region = "\n".join(lines[region_start:footer_line + 1])
            if _NUMBERED_YES_RE.search(region):
                return ("numbered-yes", "", True, _extract_summary(tail, "numbered"))
            if _SELECTED_YES_RE.search(region):
                return ("selected-yes", "", True, _extract_summary(tail, "numbered"))
    return None


# Patterns for extracting the tool/action being approved
_TOOL_LINE_RE = re.compile(
    r"[●○◉]\s*((?:Bash|Read|Edit|Write|Glob|Grep|Agent|Skill|WebFetch|WebSearch"
    r"|MultiEdit|NotebookEdit|ToolSearch|SendMessage|TaskCreate|TaskUpdate"
    r"|mcp\S+)\s*\(.*)",
    re.IGNORECASE,
)


def _extract_summary(tail, prompt_type):
    """Extract a short description of what's being approved."""
    lines = tail.split("\n")
    if prompt_type == "permission":
        # Look for tool invocation line: ● Bash(command) or ● Read(path)
        for line in reversed(lines):
            m = _TOOL_LINE_RE.search(line)
            if m:
                text = m.group(1).strip()
                return text[:80] if len(text) > 80 else text
        # Fallback: look for "Allow" line
        for line in reversed(lines):
            if "Allow" in line and ("once" in line or "always" in line):
                continue  # skip the "Allow once / Always allow" footer
            if "Allow" in line:
                text = line.strip()
                return text[:80] if len(text) > 80 else text
    elif prompt_type == "confirm":
        # Look for the question line above (y/n)
        for i, line in enumerate(lines):
            if re.search(r"\(y/n\)|\[Y/n\]|\(yes/no\)", line, re.IGNORECASE):
                text = line.strip()
                # Remove the (y/n) suffix
                text = re.sub(r"\s*\(y/n\)|\[Y/n\]|\(yes/no\)\s*", "", text).strip()
                return text[:80] if len(text) > 80 else text
    elif prompt_type == "numbered":
        # Look for tool invocation above the numbered options (prefer over generic question)
        for i in range(len(lines) - 1, -1, -1):
            if re.match(r"\s*(?:[^\d\s]\s*)?1[\.\)]\s", lines[i]):
                # Found option 1 — search upward for a tool line first
                for j in range(i - 1, max(i - 8, -1), -1):
                    m = _TOOL_LINE_RE.search(lines[j])
                    if m:
                        text = m.group(1).strip()
                        return text[:80] if len(text) > 80 else text
                # Fallback: first non-generic line above options
                for j in range(i - 1, max(i - 4, -1), -1):
                    candidate = lines[j].strip()
                    if (
                        not candidate
                        or candidate.startswith("─")
                        or len(candidate) <= 3
                    ):
                        continue
                    # Skip generic questions
                    if re.match(
                        r"Do you want to proceed\??$", candidate, re.IGNORECASE
                    ):
                        continue
                    return candidate[:80] if len(candidate) > 80 else candidate
                break
    return None


# Patterns that mark the end of an interactive prompt block. Whatever sits
# *below* one of these in the captured pane is Claude Code's status bar
# (Vibing… timer, token counts, tips) which animates every second — hashing
# it would reset the countdown each tick and the prompt would never fire.
_PROMPT_TERMINATOR_RE = re.compile(
    r"(?:Enter to select|Esc to cancel)\s*[·•]"
    r"|\(y/n(?:/a)?\)"
    r"|\[Y/n(?:/a)?\]"
    r"|\[y/N\]"
    r"|\(yes/no\)",
    re.IGNORECASE,
)


def _prompt_hash(tail):
    """Hash a stable region of the prompt to detect the same prompt across ticks.

    Hashes ~500 chars ending at the prompt's terminator line (the footer or
    y/n marker), so the animated status bar below the prompt doesn't change
    the hash every second. Falls back to the last 500 chars when no
    terminator is found.
    """
    matches = list(_PROMPT_TERMINATOR_RE.finditer(tail))
    if matches:
        last = matches[-1]
        end = tail.find("\n", last.end())
        if end < 0:
            end = len(tail)
        start = max(0, end - 500)
        return hash(tail[start:end])
    return hash(tail[-500:])


def autoyes_scanner():
    """Background thread: scans all tmux panes every 1s, manages countdowns."""
    while True:
        try:
            _autoyes_scan_tick()
        except Exception:
            pass
        time.sleep(1.0)


def _autoyes_scan_tick():
    """One scan cycle for auto-yes."""
    with state.autoyes_lock:
        if not state.autoyes_sessions:
            return
        active_sessions = {k for k, v in state.autoyes_sessions.items() if v}

    if not active_sessions:
        return

    proc = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_index}\t#{pane_index}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        # tmux server gone — every session is dead; drop all auto-yes state
        # so ghost countdowns/toggles aren't served for the process lifetime.
        if "no server" in (proc.stderr or "").lower():
            with state.autoyes_lock:
                state.autoyes_countdowns.clear()
                state.autoyes_answered.clear()
                state.autoyes_sessions.clear()
                state.autoyes_delays.clear()
        return

    now = time.time()

    # Build live session/pane sets, then prune state for dead sessions and
    # dead panes BEFORE scanning (killed sessions otherwise leak countdown/
    # answered/sessions/delays entries forever).
    live_sessions = set()
    live_targets = set()
    pane_rows = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        live_sessions.add(parts[0])
        live_targets.add(f"{parts[0]}:{parts[1]}.{parts[2]}")
        pane_rows.append(parts)

    with state.autoyes_lock:
        for sess in list(state.autoyes_sessions):
            if sess not in live_sessions:
                state.autoyes_sessions.pop(sess, None)
                state.autoyes_delays.pop(sess, None)
        for t in list(state.autoyes_countdowns):
            if t not in live_targets:
                state.autoyes_countdowns.pop(t, None)
        for t in list(state.autoyes_answered):
            if t not in live_targets:
                state.autoyes_answered.pop(t, None)

    for parts in pane_rows:
        session_name = parts[0]
        if session_name not in active_sessions:
            continue

        target = f"{parts[0]}:{parts[1]}.{parts[2]}"

        cap = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target, "-S", "-60"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if cap.returncode != 0:
            continue
        tail = cap.stdout.rstrip("\n")
        if not tail:
            continue

        phash = _prompt_hash(tail)
        detected = _detect_autoyes_prompt(tail)

        # Collect broadcast event to fire AFTER releasing the lock
        # (broadcast_autoyes_event also acquires autoyes_lock — avoid deadlock).
        # Likewise collect the keystrokes to send: tmux subprocesses + sleep
        # must not run under the lock.
        broadcast_event = None
        fire_action = None  # (send_text, with_enter, prompt_type)

        with state.autoyes_lock:
            if not detected:
                state.autoyes_countdowns.pop(target, None)
                # Clear answered cache when content changes (no prompt visible).
                # This ensures a NEW prompt with the same hash as a previous one
                # (e.g., consecutive edits to the same file) is not skipped.
                if target in state.autoyes_answered:
                    if state.autoyes_answered[target][0] != phash:
                        del state.autoyes_answered[target]
                continue

            log.info("autoyes: detected %s on %s", detected[0], target)

            answered = state.autoyes_answered.get(target)
            if answered and answered[0] == phash:
                if now - answered[1] < 3.0:
                    # Recently fired — y is still being processed
                    state.autoyes_countdowns.pop(target, None)
                    continue
                else:
                    # Same hash but too old — new prompt or send failed
                    log.info(
                        "autoyes: answered-cache expired for %s (%.1fs old)",
                        target,
                        now - answered[1],
                    )
                    del state.autoyes_answered[target]

            existing = state.autoyes_countdowns.get(target)

            if existing and existing["cancelled"]:
                if existing["prompt_hash"] == phash:
                    continue
                del state.autoyes_countdowns[target]
                existing = None

            if existing and existing["prompt_hash"] == phash:
                if now >= existing["deadline"]:
                    prompt_type, send_text, with_enter, _summary = detected
                    state.autoyes_answered[target] = (phash, now)
                    state.autoyes_countdowns.pop(target, None)
                    fire_action = (send_text, with_enter, prompt_type)
                    broadcast_event = (target, "fired", detected[0])
            else:
                # Re-check enablement: a toggle-off mid-tick must not
                # recreate a phantom countdown that's never cleaned up.
                if not state.autoyes_sessions.get(session_name):
                    state.autoyes_countdowns.pop(target, None)
                    continue
                proj_delay = state.get_project_setting(session_name, "autoyes", "delay")
                delay = state.autoyes_delays.get(session_name, proj_delay)
                summary = detected[3] if len(detected) > 3 else None
                state.autoyes_countdowns[target] = {
                    "prompt_hash": phash,
                    "deadline": now + delay,
                    "delay": delay,
                    "cancelled": False,
                    "prompt_type": detected[0],
                    "summary": summary,
                }
                broadcast_event = (target, "countdown", detected[0])

        # Send keystrokes outside the lock (subprocess + sleep)
        if fire_action:
            send_text, with_enter, prompt_type = fire_action
            if send_text:
                tmux_send_text(target, send_text)
            if with_enter:
                time.sleep(0.05)
                tmux_send_keys(target, "Enter")
            log.info(
                "autoyes: FIRED %s on %s (send=%r enter=%r)",
                prompt_type,
                target,
                send_text,
                with_enter,
            )

        # Broadcast outside the lock
        if broadcast_event:
            broadcast_autoyes_event(*broadcast_event)


@autoyes_bp.route("/autoyes/status")
def autoyes_status():
    """Return auto-yes state for all sessions."""
    with state.autoyes_lock:
        countdowns = {}
        now = time.time()
        for target, cd in state.autoyes_countdowns.items():
            if not cd["cancelled"]:
                countdowns[target] = {
                    "remaining": max(0, round(cd["deadline"] - now, 1)),
                    "prompt_type": cd["prompt_type"],
                    "delay": cd.get("delay", state.AUTOYES_DELAY),
                    "summary": cd.get("summary"),
                }
        return jsonify(
            {
                "sessions": dict(state.autoyes_sessions),
                "countdowns": countdowns,
                "delays": dict(state.autoyes_delays),
            }
        )


@autoyes_bp.route("/autoyes/toggle", methods=["POST"])
def autoyes_toggle():
    """Toggle auto-yes for a session."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not session:
        return jsonify({"ok": False, "error": "No session"}), 400
    delay = data.get("delay")
    with state.autoyes_lock:
        current = state.autoyes_sessions.get(session, False)
        state.autoyes_sessions[session] = not current
        if not current and delay is not None:
            # Enabling — store per-session delay
            try:
                delay = max(1, min(30, int(delay)))
            except (ValueError, TypeError):
                delay = state.AUTOYES_DELAY
            state.autoyes_delays[session] = delay
        if current:
            to_remove = [
                t for t in state.autoyes_countdowns if t.startswith(session + ":")
            ]
            for t in to_remove:
                state.autoyes_countdowns.pop(t, None)
                state.autoyes_answered.pop(t, None)
            state.autoyes_delays.pop(session, None)
    # Persist auto-yes preference for restoration after restart
    persist = {"autoyes": {"enabled_default": not current}}
    if not current and delay is not None:
        persist["autoyes"]["delay"] = delay
    state.patch_project_settings(session, persist)

    return jsonify({"ok": True, "session": session, "enabled": not current})


@autoyes_bp.route("/autoyes/cancel", methods=["POST"])
def autoyes_cancel():
    """Cancel an active auto-yes countdown for a target."""
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify({"ok": False, "error": "No target"}), 400
    # Collect the event to fire AFTER releasing the lock
    # (broadcast_autoyes_event also acquires autoyes_lock — avoid deadlock).
    broadcast_event = None
    with state.autoyes_lock:
        cd = state.autoyes_countdowns.get(target)
        if cd and not cd["cancelled"]:
            cd["cancelled"] = True
            broadcast_event = (target, "cancelled", cd["prompt_type"])
    if broadcast_event:
        broadcast_autoyes_event(*broadcast_event)
        return jsonify({"ok": True, "cancelled": True})
    return jsonify({"ok": True, "cancelled": False})


@autoyes_bp.route("/autoyes/set-delay", methods=["POST"])
def autoyes_set_delay():
    """Update the auto-yes delay for an already-enabled session.

    Applies to future prompts and recomputes any in-flight countdown so the
    new delay takes effect immediately (e.g. 5s -> 2s while a countdown runs).
    """
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not session:
        return jsonify({"ok": False, "error": "No session"}), 400
    try:
        delay = max(1, min(30, int(data.get("delay"))))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid delay"}), 400

    # Collect targets to re-broadcast AFTER releasing the lock
    # (broadcast_autoyes_event also acquires autoyes_lock — avoid deadlock).
    rebroadcast = []
    with state.autoyes_lock:
        if not state.autoyes_sessions.get(session, False):
            return jsonify({"ok": False, "error": "Auto-Yes not enabled"}), 409
        state.autoyes_delays[session] = delay
        # Recompute any active countdown for this session against the new delay,
        # anchored to the prompt's original start time.
        for target, cd in state.autoyes_countdowns.items():
            if target.startswith(session + ":") and not cd["cancelled"]:
                start = cd["deadline"] - cd["delay"]
                cd["delay"] = delay
                cd["deadline"] = start + delay
                rebroadcast.append((target, cd["prompt_type"]))

    # Persist for restoration after restart
    state.patch_project_settings(session, {"autoyes": {"delay": delay}})

    for target, prompt_type in rebroadcast:
        broadcast_autoyes_event(target, "countdown", prompt_type)

    log.info("autoyes: delay updated for %s -> %ds", session, delay)
    return jsonify({"ok": True, "session": session, "delay": delay})


def restore_autoyes_from_settings():
    """Restore auto-yes for running tmux sessions that had it enabled before restart."""
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return
    except Exception:
        return
    for session_name in proc.stdout.strip().split("\n"):
        if not session_name:
            continue
        settings = state.get_project_settings(session_name)
        if settings.get("autoyes", {}).get("enabled_default", False):
            delay = settings.get("autoyes", {}).get("delay", state.AUTOYES_DELAY)
            with state.autoyes_lock:
                state.autoyes_sessions[session_name] = True
                state.autoyes_delays[session_name] = delay
            log.info(
                "autoyes: restored for session %s (delay=%ds)", session_name, delay
            )
