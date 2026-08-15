"""Session discovery and pane capture commands for the Assist CLI."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

from cli import http, wait as wait_cli


NEXT_COMMANDS = {
    "launch": (
        'assist send {session} "continue" --enter',
        "assist send {session} --enter",
        "assist wait {session}",
        "assist view {session}",
    ),
    "send": (
        "assist wait {session}",
        'assist send {session} "continue" --enter',
        "assist send {session} --enter",
        "assist view {session}",
    ),
    "view": (
        "assist view {session}",
        "assist wait {session}",
        'assist send {session} "continue" --enter',
        "assist kill {session}",
    ),
    "wait": (
        "assist view {session}",
        "assist wait {session}",
        'assist send {session} "continue" --enter',
        "assist kill {session}",
    ),
    "ls": (
        "assist launch --session {session} --cwd . --wait",
        "assist view {session}",
        "assist ls",
    ),
}


CALLBACK_HINT = (
    "callback: to be told instead of polling, send this too —\n"
    "  when you are done, or if you hit an issue or question, run: "
    'assist send {reply_to} "<status>" --enter'
)


def print_next_hint(verb: str, session: str | None) -> None:
    """Print the measured next commands for a successful session verb."""
    if os.environ.get("ASSIST_NO_HINTS") == "1" or not session:
        return
    commands = NEXT_COMMANDS.get(verb)
    if commands is None:
        return
    rendered = (command.format(session=session) for command in commands)
    print("next: " + "  ·  ".join(rendered), file=sys.stderr)


def reply_address() -> str | None:
    """Return the caller's own tmux session — the address a delegate replies to."""
    override = os.environ.get("ASSIST_REPLY_TO")
    if override:
        return override.strip() or None
    if not os.environ.get("TMUX"):
        return None
    command = ["tmux", "display-message", "-p"]
    pane = os.environ.get("TMUX_PANE")
    if pane:
        command += ["-t", pane]
    command.append("#S")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def print_callback_hint(target_session: str | None) -> None:
    """Offer the sender a callback, so it can stop polling the pane it just wrote to."""
    if os.environ.get("ASSIST_NO_HINTS") == "1":
        return
    reply_to = reply_address()
    if not reply_to or reply_to == target_session:
        return
    print(CALLBACK_HINT.format(reply_to=shlex.quote(reply_to)), file=sys.stderr)


def resolve_target(session: str | None, pane: str = "0.0") -> str:
    """Return an explicit tmux target, refusing the server-side fallback."""
    if not session:
        print("assist: no session given", file=sys.stderr)
        raise SystemExit(2)
    return f"{session}:{pane}"


def _merged_sessions(include_cwd: bool = False) -> dict:
    sessions_response = http.get("/terminal/sessions")
    poll_response = http.get("/poll")
    states = poll_response.get("states") or {}

    panes = []
    for pane in sessions_response.get("sessions") or []:
        merged = dict(pane)
        pane_state = states.get(pane.get("target")) or {}
        merged.update(pane_state)
        if include_cwd:
            cwd_target = merged.get("target") or merged.get("session")
            if cwd_target:
                path = "/terminal/cwd?" + urlencode({"session": cwd_target})
                merged["cwd"] = http.get(path).get("cwd")
            else:
                merged["cwd"] = None
        panes.append(merged)

    merged_response = dict(sessions_response)
    merged_response["sessions"] = panes
    return merged_response


def _text(value: object) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _idle_text(value: object) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{int(float(value))}s"
    except (TypeError, ValueError):
        return "-"


def _display_rows(panes: list[dict], include_cwd: bool) -> list[list[str]]:
    rows = []
    for pane in panes:
        name = pane.get("target") if pane.get("is_subpane") else pane.get("session")
        row = [
            _text(name),
            _text(pane.get("state")),
            _text(pane.get("agent_kind")),
            _idle_text(pane.get("idle_seconds")),
            _text(pane.get("command_display")),
        ]
        if include_cwd:
            row.append(_text(pane.get("cwd")))
        rows.append(row)
    return rows


def _print_aligned(rows: list[list[str]]) -> None:
    if not rows:
        return
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]) - 1)]
    for row in rows:
        columns = [
            value.ljust(widths[index]) if index < len(widths) else value
            for index, value in enumerate(row)
        ]
        print("  ".join(columns))


def list_sessions(json_output: bool = False, include_cwd: bool = False) -> int:
    response = _merged_sessions(include_cwd=include_cwd)
    if json_output:
        print(json.dumps(response, indent=4))
        return 0

    rows = _display_rows(response.get("sessions") or [], include_cwd)
    _print_aligned(rows)
    if rows:
        print_next_hint("ls", rows[0][0])
    return 0


def view(session: str | None, pane: str = "0.0", lines: int | None = None) -> int:
    target = resolve_target(session, pane)
    query = {"target": target}
    if lines is not None:
        query["lines"] = lines
    response = http.get("/terminal/capture?" + urlencode(query))
    content = response.get("content")
    if content is not None:
        sys.stdout.write(str(content))
    print_next_hint("view", session)
    return 0


def launch(
    session: str | None,
    cwd: str | None = None,
    cols: int | None = None,
    rows: int | None = None,
    wait_for_completion: bool = False,
    timeout: float = wait_cli.DEFAULT_TIMEOUT,
) -> int:
    if not session:
        resolve_target(session)

    payload: dict[str, object] = {
        "project": session,
        "skip_init": True,
    }
    if cwd is not None:
        payload["cwd"] = cwd
    if cols is not None:
        payload["cols"] = cols
    if rows is not None:
        payload["rows"] = rows

    response = http.post("/terminal/launch", payload)
    for key in ("session", "target", "venv", "existed"):
        print(f"{key}: {_text(response.get(key))}")

    if wait_for_completion:
        exit_code = wait_cli.run_wait(
            response["session"],
            response["target"],
            timeout,
            baseline_hash=None,
        )
        if exit_code != 0:
            return exit_code
    print_next_hint("launch", response.get("session"))
    return 0


def send(
    session: str | None,
    text: str | None = None,
    file_path: str | None = None,
    enter: bool = False,
    pane: str = "0.0",
    wait_for_completion: bool = False,
    timeout: float = wait_cli.DEFAULT_TIMEOUT,
    autoyes: bool = False,
) -> int:
    target = resolve_target(session, pane)
    if autoyes and not wait_for_completion:
        print("assist: --autoyes requires --wait", file=sys.stderr)
        raise SystemExit(2)
    if (text is None) == (file_path is None):
        print(
            "assist: exactly one of text or --file is required",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if file_path == "-":
        send_text = sys.stdin.read()
    elif file_path is not None:
        try:
            send_text = Path(file_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print(f"assist: unable to read {file_path}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
    else:
        send_text = text

    baseline_hash = None
    if wait_for_completion:
        baseline_hash = wait_cli.capture_hash(target)

    response = http.post(
        "/type",
        {
            "target": target,
            "text": send_text,
            "enter": enter,
            "no_history": True,
            "raw": True,
        },
    )
    sent_chars = response.get("sent_chars")
    if sent_chars is None:
        sent_chars = 0
    print(f"sent {sent_chars} chars to {target}", flush=True)
    print_callback_hint(session)
    if wait_for_completion:
        exit_code = wait_cli.run_wait(
            session,
            target,
            timeout,
            baseline_hash,
            autoyes=autoyes,
        )
        if exit_code != 0:
            return exit_code
    print_next_hint("send", session)
    return 0


def _http_error_detail(error: http.ApiError) -> str:
    try:
        response = json.loads(error.body)
    except (json.JSONDecodeError, TypeError):
        response = {}
    detail = response.get("error") if isinstance(response, dict) else None
    if not detail:
        detail = str(error)
    return " ".join(str(detail).split())


def kill(session: str | None, pane: str = "0.0") -> int:
    """Kill a tmux session through Assist's existing terminal endpoint."""
    resolve_target(session, pane)
    try:
        response = http.post("/terminal/kill", {"session": session})
    except http.ApiError as exc:
        if exc.kind != "http":
            raise
        print(f"assist: {_http_error_detail(exc)}", file=sys.stderr)
        return exc.exit_code
    print(f"killed {response.get('session') or session}")
    return 0


def _autoyes_state(response: dict, session: str) -> tuple[bool, object | None]:
    enabled = bool((response.get("sessions") or {}).get(session))
    delay = (response.get("delays") or {}).get(session)
    return enabled, delay


def _delay_text(delay: object | None) -> str:
    if delay is None:
        return "server default"
    try:
        return f"{float(delay):g}s"
    except (TypeError, ValueError):
        return str(delay)


def _print_autoyes_state(
    session: str,
    enabled: bool,
    delay: object | None,
) -> None:
    if enabled:
        print(f"autoyes {session}: on (delay: {_delay_text(delay)})")
    else:
        print(f"autoyes {session}: off")


def autoyes(
    session: str | None,
    mode: str,
    delay: float | None = None,
) -> int:
    """Persistently set or inspect auto-yes without accidentally re-toggling it."""
    if not session:
        resolve_target(session)
    if delay is not None and mode != "on":
        print("assist: --delay requires --on", file=sys.stderr)
        raise SystemExit(2)

    status = http.get("/autoyes/status")
    enabled, current_delay = _autoyes_state(status, session)
    if mode != "status":
        requested = mode == "on"
        if enabled != requested:
            payload: dict[str, object] = {"session": session}
            if requested and delay is not None:
                payload["delay"] = delay
            http.post("/autoyes/toggle", payload)
            status = http.get("/autoyes/status")
            enabled, current_delay = _autoyes_state(status, session)

    _print_autoyes_state(session, enabled, current_delay)
    return 0
