"""Per-pane model + reasoning-effort parsing, keyed off agent_kind.

`resolve()` is pure — (agent_kind, tail) -> (model, effort) — so it can be
checked against captured footers without touching state. `observe()` wraps it
with the memo and the wall-clock-gated confirmation (see
`_MIN_CONFIRM_SECONDS` below).

The text exists only because a CLI prints it in its status line: Claude Code's
comes from a custom statusline script, Codex's from `status_line` in
~/.codex/config.toml. A stock install may legitimately show nothing. See the
"Known limitation" section of Studio spec 1981.
"""

import time
import re

# Deliberate cross-module use of two private patterns rather than a second copy
# of each: routes/poll.py already imports _VERSION_CMD_RE the same way.
from shared.agent_identity import _ANSI_ESCAPE_RE, _CODEX_FINGERPRINT_RE
import shared.state as state

# Codex's six real reasoning levels.
_CODEX_RE = re.compile(
    r"(?P<model>gpt-\d+(?:\.\d+)*-[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"(?:\s+(?P<effort>minimal|low|medium|high|xhigh|ultra))?"
)
_CODEX_VERSION_PREFIX_RE = re.compile(r"^gpt-\d+(?:\.\d+)*-")
_CODEX_FAMILY = {"sol", "luna", "terra"}

_CLAUDE_RE = re.compile(r"\b(Opus|Sonnet|Haiku|Fable)\s+(\d+(?:\.\d+)?)\b")
_CLAUDE_ID_RE = re.compile(r"\bclaude-(opus|sonnet|haiku|fable)-(\d+(?:[.-]\d+)?)\b")


def _footer_lines(tail, count):
    """Last `count` non-empty lines of a capture, ANSI stripped.

    Same region and same ANSI regex agent_identity.refine_with_content() uses.
    """
    normalized = _ANSI_ESCAPE_RE.sub("", tail or "")
    nonempty = [line for line in normalized.splitlines() if line.strip()]
    return nonempty[-count:]


def _short_codex_model(model):
    """gpt-5.6-sol -> sol. gpt-5.3-codex -> 5.3-codex (unfamiliar, still readable)."""
    rest = _CODEX_VERSION_PREFIX_RE.sub("", model)
    if rest in _CODEX_FAMILY:
        return rest
    return model[len("gpt-") :] if model.startswith("gpt-") else model


def _parse_codex(tail):
    region = _footer_lines(tail, 5)
    # Anchor to the status line — the one carrying "· Context NN% left/used".
    # Without this, a prompt that merely NAMES a model ("switch to
    # gpt-5.3-codex") outranks the real status line below it, and the
    # time-based confirmation (_MIN_CONFIRM_SECONDS) cannot catch it because
    # prose persists across polls just as long as the real status line does.
    anchored = [line for line in region if _CODEX_FINGERPRINT_RE.search(line)]
    for line in reversed(anchored or region):
        match = _CODEX_RE.search(line)
        if match:
            return _short_codex_model(match.group("model")), match.group("effort")
    return None, None


def _parse_claude(tail):
    # Bottom-up: the status line is the last thing drawn, so a lower match
    # beats a higher one.
    for line in reversed(_footer_lines(tail, 3)):
        match = _CLAUDE_RE.search(line)
        if match:
            return f"{match.group(1).lower()} {match.group(2)}", None
        match = _CLAUDE_ID_RE.search(line)
        if match:
            return f"{match.group(1)} {match.group(2)}", None
    return None, None


# One entry per agent_kind that prints a model. Adding cursor/opencode later is
# one function and one line here.
_PARSERS = {
    "codex": _parse_codex,
    "claude": _parse_claude,
}


def resolve(agent_kind, tail):
    """(model, effort) for a pane, or (None, None) when not recognised."""
    parser = _PARSERS.get(agent_kind)
    if parser is None:
        return None, None
    return parser(tail)


_MIN_CONFIRM_SECONDS = 4.0  # candidate must hold this long, not just "two calls in a row"


def observe(target, agent_kind, pane_id, tail):
    """Confirm and memoise a pane's model. Returns (model, effort, changed_at).

    A parse is published only once two agreeing polls also span at least
    `_MIN_CONFIRM_SECONDS` of wall-clock time. Call-adjacency alone is not
    enough: every open browser polls independently (phone + desktop is the
    normal case here), so two different clients can confirm the same
    one-frame flap milliseconds apart and defeat the damping this exists for.
    A miss never blanks an established value: a full-screen dialog or a
    scrolled-back pane hides the footer, and the last confirmed value stands.
    """
    parsed = resolve(agent_kind, tail)
    now = time.time()

    with state._activity_lock:
        entry = state.pane_model.get(target)

        # The pane's identity changed under us: Claude exited to a shell (or
        # a different CLI took over — `kind` differs), or tmux recycled this
        # target onto a different pane before a poll observed the gap
        # (`pane_id` differs even though `kind` is unchanged). Either way the
        # old model is not "temporarily hidden", it is gone — the never-blank
        # rule below must not display it forever or hand it to a new pane.
        if entry is not None and (
            entry.get("kind") != agent_kind or entry.get("pane_id") != pane_id
        ):
            entry = None

        if entry is None:
            entry = {
                "model": None,
                "effort": None,
                "changed_at": 0.0,
                "candidate": None,
                "candidate_since": 0.0,
                "kind": agent_kind,
                "pane_id": pane_id,
            }
            state.pane_model[target] = entry

        if parsed == (None, None):
            # Drop the pending candidate only; the published value stands.
            entry["candidate"] = None
        elif entry["candidate"] == parsed:
            # Same candidate as last time — but agreement alone no longer
            # publishes. candidate_since was stamped below the FIRST time
            # this candidate appeared and is deliberately left untouched
            # while it keeps agreeing; only elapsed time against that
            # original stamp can satisfy the gate.
            if now - entry["candidate_since"] >= _MIN_CONFIRM_SECONDS:
                published = (entry["model"], entry["effort"])
                if published != parsed:
                    # A pane's first value is not a change from anything.
                    # Stamping it would caret every tab after an `assist
                    # restart`.
                    entry["changed_at"] = 0.0 if published == (None, None) else now
                    entry["model"], entry["effort"] = parsed
        else:
            entry["candidate"] = parsed
            entry["candidate_since"] = now

        return entry["model"], entry["effort"], entry["changed_at"]
