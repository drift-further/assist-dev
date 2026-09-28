"""shared/key_combo.py — one structured key combo to one tmux key name.

`/key` accepts `{"combo": {"ctrl": bool, "alt": bool, "shift": bool, "key": str}}`
beside its fixed `keys` allowlist. The key is one printable ASCII character or
one of NAMED_KEYS; anything else is refused. The result is always a single
tmux key name, sent with send-keys and never as free text.

What tmux 3.7c actually does with each spelling was measured against a raw
pane (see tests/test_key_combo.py), and the rules below follow from it:

- Modifiers are prefixed C-, M-, S- in that order (`C-M-S-Left`).
- Shift + letter is the uppercase letter. `S-a` arrives as a plain `a`.
- Shift + any other printable character is refused: which character it makes
  depends on the keyboard layout, so the caller sends that character itself.
- Shift + Tab is `BTab`. `S-Tab` arrives as a plain Tab.
- Names tmux cannot parse are refused, because send-keys then types the name
  itself as text: `C-BSpace` arrives as the nine characters "C-BSpace". The same
  happens to Ctrl with `# $ % & *` and to Ctrl + Escape.

Other combos go through as tmux spells them. Whether a pane can tell, say,
Ctrl+Enter from Enter is up to tmux's extended-keys handshake with the program
in that pane, not to this module.
"""

NAMED_KEYS = {
    **{f"F{n}": f"F{n}" for n in range(1, 13)},
    "Up": "Up",
    "Down": "Down",
    "Left": "Left",
    "Right": "Right",
    "Home": "Home",
    "End": "End",
    "PgUp": "PPage",
    "PgDn": "NPage",
    "Insert": "IC",
    "Delete": "DC",
    "Tab": "Tab",
    "Escape": "Escape",
    "Enter": "Enter",
    "BSpace": "BSpace",
    "Space": "Space",
}

MODIFIERS = ("ctrl", "alt", "shift")

# Ctrl + these makes tmux type the key's name as literal text.
_CTRL_UNNAMED = frozenset("#$%&*") | {"BSpace", "Escape"}


class ComboError(ValueError):
    """The combo is outside the grammar; the message says why."""


def combo_to_tmux(combo):
    """Translate one combo dict to one tmux key name, or raise ComboError."""
    if not isinstance(combo, dict):
        raise ComboError("combo must be an object")
    unknown = set(combo) - {"key", *MODIFIERS}
    if unknown:
        raise ComboError(f"unknown combo field: {sorted(unknown)[0]}")
    for name in MODIFIERS:
        if not isinstance(combo.get(name, False), bool):
            raise ComboError(f"{name} must be true or false")
    ctrl = combo.get("ctrl", False)
    alt = combo.get("alt", False)
    shift = combo.get("shift", False)
    key = combo.get("key")
    if not isinstance(key, str) or not key:
        raise ComboError("combo needs a key")

    if key == " ":
        key = "Space"
    if key in NAMED_KEYS:
        base = NAMED_KEYS[key]
        if key == "Tab" and shift:
            base, shift = "BTab", False
    elif len(key) == 1 and "!" <= key <= "~":
        base = key
        if shift:
            if not key.isalpha():
                raise ComboError(f"Shift+{key}: send the shifted character itself")
            base, shift = key.upper(), False
    else:
        raise ComboError("key must be one printable character or a named key")

    if ctrl and key in _CTRL_UNNAMED:
        raise ComboError(f"tmux has no name for Ctrl+{key}")
    prefix = ("C-" if ctrl else "") + ("M-" if alt else "") + ("S-" if shift else "")
    return prefix + base
