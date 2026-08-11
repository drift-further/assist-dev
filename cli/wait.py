"""Polling state machine for waiting on an Assist tmux pane."""

import hashlib
import time
from urllib.parse import urlencode

from cli import http
from routes.autoyes import _detect_autoyes_prompt
from shared.agent_identity import _ANSI_ESCAPE_RE


DEFAULT_TIMEOUT = 110.0
POLL_INTERVAL = 2.0
# Tracks /poll's approximately five-second recompute cadence.
_POLL_STALENESS_SECONDS = 5.0
QUIET_STATES = {"idle", "shell"}


class WaitError(Exception):
    """A wait failure that is safe to show without a traceback."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        self.exit_code = exit_code
        super().__init__(message)


def _capture_content(target: str) -> str:
    path = "/terminal/capture?" + urlencode({"target": target})
    response = http.get(path)
    content = response.get("content")
    if not isinstance(content, str):
        raise WaitError(f"assist: capture returned no content for {target}")
    return content


def _content_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def capture_hash(target: str) -> str:
    """Return the md5 hash of the server's default-depth pane capture."""
    return _content_hash(_capture_content(target))


def _agent_kind(poll_response: dict, target: str) -> str | None:
    for pane in poll_response.get("sessions") or []:
        if pane.get("target") == target:
            return pane.get("agent_kind")
    return None


def _prompt_detail(tail: str, agent_kind: str | None) -> str | None:
    plain_tail = _ANSI_ESCAPE_RE.sub("", tail)
    detected = _detect_autoyes_prompt(plain_tail, agent_kind)
    if detected is None:
        return None
    prompt_type, _send_text, _with_enter, summary = detected
    return f"{prompt_type}: {summary or '(no summary)'}"


def wait_for(
    target: str,
    timeout: float,
    baseline_hash: str | None = None,
) -> tuple[str, str | None]:
    """Wait for a pane to become idle, present a prompt, or reach timeout."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    content_changed = baseline_hash is None
    first_poll = True

    while True:
        if not first_poll and time.monotonic() >= deadline:
            return "working", None

        poll_response = http.get("/poll")
        states = poll_response.get("states") or {}
        target_state = states.get(target)
        if not isinstance(target_state, dict):
            raise WaitError(f"assist: target not found in /poll: {target}")

        state = target_state.get("state")
        try:
            idle_seconds = float(target_state.get("idle_seconds") or 0)
        except (TypeError, ValueError):
            idle_seconds = 0.0

        captured_tail = None
        changed_this_poll = False
        if not content_changed:
            captured_tail = _capture_content(target)
            content_changed = _content_hash(captured_tail) != baseline_hash
            changed_this_poll = content_changed

        quiet = state in QUIET_STATES
        fresh = (
            baseline_hash is None
            or content_changed
            or idle_seconds >= _POLL_STALENESS_SECONDS
        )
        if fresh and not changed_this_poll:
            prompt_quiet = quiet or idle_seconds >= POLL_INTERVAL
            if prompt_quiet:
                if captured_tail is None:
                    captured_tail = _capture_content(target)
                detail = _prompt_detail(
                    captured_tail,
                    _agent_kind(poll_response, target),
                )
                if detail is not None:
                    return "prompt", detail
            if quiet:
                return "idle", None

        first_poll = False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "working", None
        time.sleep(min(POLL_INTERVAL, remaining))


def _wait_with_autoyes(
    session: str,
    target: str,
    timeout: float,
    baseline_hash: str | None,
) -> tuple[str, str | None]:
    status = http.get("/autoyes/status")
    was_enabled = bool((status.get("sessions") or {}).get(session))
    try:
        if not was_enabled:
            http.post("/autoyes/toggle", {"session": session})
        return wait_for(target, timeout, baseline_hash)
    finally:
        if not was_enabled:
            restored_status = http.get("/autoyes/status")
            enabled = bool(
                (restored_status.get("sessions") or {}).get(session)
            )
            if enabled:
                http.post("/autoyes/toggle", {"session": session})


def _timeout_label(timeout: float) -> str:
    return f"{float(timeout):g}"


def run_wait(
    session: str,
    target: str,
    timeout: float,
    baseline_hash: str | None,
    autoyes: bool = False,
) -> int:
    if autoyes:
        state, detail = _wait_with_autoyes(
            session,
            target,
            timeout,
            baseline_hash,
        )
    else:
        state, detail = wait_for(target, timeout, baseline_hash)

    if state == "idle":
        return 0
    if state == "prompt":
        print(detail)
        return 10
    print(
        f"still working after {_timeout_label(timeout)}s — "
        f"re-run: assist wait {session}"
    )
    return 75


def command(
    session: str | None,
    timeout: float = DEFAULT_TIMEOUT,
    pane: str = "0.0",
    autoyes: bool = False,
) -> int:
    from cli.session import resolve_target

    target = resolve_target(session, pane)
    baseline_hash = capture_hash(target)
    exit_code = run_wait(
        session,
        target,
        timeout,
        baseline_hash,
        autoyes=autoyes,
    )
    if exit_code == 0:
        from cli.session import print_next_hint

        print_next_hint("wait", session)
    return exit_code
