"""shared/segments.py — [handle] prompt segments: validation, migration, expansion.

A *segment* is a favorite that has been given a short handle. Typing `[sol-dist]` in
the composer sends that favorite's whole body instead of the literal token, so a
600-word standing instruction costs ten characters of phone screen. Expansion happens
here, server-side, right before the tmux send — the composer keeps the token form and
so does the history entry.

Pure module: no Flask, no filesystem. The caller loads favorites.json and passes the
list in.
"""

import re
import uuid

# Handle grammar: lowercase, opens alphanumeric, 2-32 chars. Deliberately narrow —
# a shell glob (`ls [ab]*`), a char class (`[0-9]`) or an index (`arr[0]`) must never
# be mistakable for a token. Combined with "only substitute handles that exist", a
# collision needs a segment named after the exact glob body.
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

# A token is [handle] not preceded by a backslash; \[handle] is the literal escape.
TOKEN_RE = re.compile(r"(?<!\\)\[([a-z0-9][a-z0-9._-]{1,31})\]")

_ESCAPE_RE = re.compile(r"\\\[")

# Segments may reference segments. Three levels is enough to share a common block
# between two bigger ones without letting a mistake run away.
MAX_DEPTH = 3


def valid_handle(handle):
    """True if *handle* is a well-formed segment handle."""
    return bool(handle) and bool(HANDLE_RE.match(handle))


def normalize_handle(handle):
    """Trim and lowercase a user-supplied handle. Returns '' for empty input."""
    return (handle or "").strip().lower()


def ensure_ids(favs):
    """Give every favorite a stable id. Returns (favs, changed).

    favorites.json predates segments and keyed entries by their exact text, which
    stops working the moment a body is editable. Ids are minted lazily on first read
    so an existing file upgrades itself without a migration step.
    """
    changed = False
    for fav in favs:
        if not fav.get("id"):
            fav["id"] = "f_" + uuid.uuid4().hex[:8]
            changed = True
    return favs, changed


def segment_map(favs):
    """{handle: body} for every favorite carrying a usable handle.

    First occurrence wins — the save path rejects duplicates, so a collision here can
    only come from a hand-edited file.
    """
    segments = {}
    for fav in favs:
        handle = normalize_handle(fav.get("handle"))
        if valid_handle(handle) and handle not in segments:
            segments[handle] = fav.get("text") or ""
    return segments


def handle_owner(favs, handle, ignore_id=None):
    """The favorite currently holding *handle*, ignoring one id (the row being saved)."""
    handle = normalize_handle(handle)
    for fav in favs:
        if normalize_handle(fav.get("handle")) == handle and fav.get("id") != ignore_id:
            return fav
    return None


def find_tokens(text, segments):
    """Every [handle] in *text*, in order, flagged known/unknown.

    Powers the composer's chip strip and the expansion preview: an unknown handle is
    reported rather than rejected, because it still sends fine (verbatim).
    """
    tokens = []
    for match in TOKEN_RE.finditer(text or ""):
        handle = match.group(1)
        tokens.append({
            "handle": handle,
            "known": handle in segments,
            "chars": len(segments.get(handle, "")),
        })
    return tokens


def _expand(text, segments, depth, stack):
    if depth >= MAX_DEPTH:
        return text

    def substitute(match):
        handle = match.group(1)
        # Unknown handle, or one already being expanded further up the stack: leave
        # it exactly as typed. That is what keeps `ls [abc]*` intact and what stops
        # a segment that references itself from recursing.
        if handle not in segments or handle in stack:
            return match.group(0)
        return _expand(segments[handle], segments, depth + 1, stack + (handle,))

    return TOKEN_RE.sub(substitute, text or "")


def expand(text, segments):
    """Substitute every known [handle] in *text* with its segment body.

    Unknown handles pass through verbatim. `\\[x]` sends a literal `[x]`. Nesting is
    resolved up to MAX_DEPTH with a cycle guard.
    """
    expanded = _expand(text, segments, 0, ())
    # Unescape once, at the very end, so an escape inside a segment body survives
    # its own expansion pass before being resolved.
    return _ESCAPE_RE.sub("[", expanded)
