"""One prompt corpus, run through BOTH Auto-Yes detectors.

The server (routes/autoyes.py:_detect_autoyes_prompt) decides what Auto-Yes
answers. The browser (js/actions.js:detectSmartActions) decides what the action
bar offers and whether the tab shows the Auto-Yes toggle. They are two
implementations of one rule, and they drifted: with the question's top rule
scrolled out of view, the server answered "1. Yes" on an AskUserQuestion that
the browser correctly showed as a question (review rel #8). Nothing failed,
because no test ran the pair.

Every fixture below states what BOTH must decide, in both directions — answer
(with which type) or leave alone — and runs at two detection depths, because the
browser used to hard-code the depth the server reads from Settings.

Run: .venv/bin/python3 -m unittest tests.test_autoyes_parity
"""

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from routes.autoyes import _detect_autoyes_prompt

ROOT = Path(__file__).resolve().parents[1]

_RULE = "─" * 60
_STATUS = [
    "  ▓▓▓▓▒▒▒▒▒▒ 43.2% (345k/800k) │ 1h24m │  studio/main",
    "  studio ⌀ │ ✎ 1 │ Opus 5",
    "  -- INSERT -- ⏵⏵ auto mode on · 1 shell · ↰ 1 agent",
]


def _dialog(*content):
    return "\n".join([*content, *_STATUS])


# The browser's ids for the same decisions. numbered-yes is the server's name for
# a numbered menu whose option 1 is Yes; the browser calls it numbered-options.
_CLIENT_TYPE = {"numbered-yes": "numbered-options"}

# (name, agent_kind, tail, expected) — expected is the server's type, None for
# "must not answer", or {depth: type-or-None} where the depth decides it.
CORPUS = [
    # --- must answer ---------------------------------------------------------
    ("claude y/n/a marker", "claude",
     _dialog("● Bash(rm -rf /tmp/scratch)", "Allow this? (y/n/a)"), "permission-yna"),
    ("claude allow-once row", "claude",
     _dialog("  Allow once    Always allow    Deny"), "permission-yna"),
    ("claude numbered permission menu", "claude",
     _dialog("Do you want to proceed?", _RULE, "  1. Yes",
             "  2. Yes, and don't ask again for rm commands",
             "  3. No, and tell Claude what to do differently (esc)",
             "Enter to select · Esc to cancel"), "numbered-yes"),
    ("codex approval, no separator at all", "codex",
     "\n".join(["Would you like to run the following command?", "", "  $ git status", "",
                "› 1. Yes, proceed (y)",
                "  2. Yes, and don't ask again for this command (p)",
                "  3. No, and tell Codex what to do differently (esc)", "",
                "  Press enter to confirm or esc to cancel"]), "numbered-yes"),
    ("codex directory trust", "codex",
     _dialog("Do you trust the contents of this directory?", _RULE,
             "  › 1. Yes, continue", "    2. No, quit", "Press enter to continue"),
     "numbered-yes"),
    ("selected yes", "claude",
     _dialog(_RULE, "  ❯ Yes", "    2. No", "Enter to select · Esc to cancel"),
     "selected-yes"),
    ("opencode permission", "opencode",
     "\n".join(["△ Permission required", "Access external directory",
                "  Allow once  Allow always  Reject"]), "opencode-permission"),
    ("cursor permission", "cursor",
     "\n".join([" $  cat /etc/os-release | head -3 in .", "", " Run this command?",
                " Not in allowlist: cat, head", "  → Run (once) (y)",
                "    Run Everything (shift+tab)"]), "cursor-permission"),
    ("cursor trust, live footer", "cursor",
     "\n".join(["│  ⚠ Workspace Trust Required", "│  [a] Trust this workspace",
                "│  Use arrow keys to navigate, Enter to select"]), "cursor-trust"),
    ("apt confirm", "shell",
     "Need to get 12.3 MB.\nDo you want to continue? [Y/n] ", "package-confirm"),
    ("ssh host key", "shell",
     "ED25519 key fingerprint is SHA256:abc.\n"
     "Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
     "ssh-host-key"),
    ("bare (y/n) on the last line", "shell", "Overwrite? (y/n)", "confirm-yn"),

    # --- must not answer -----------------------------------------------------
    ("AskUserQuestion with its top rule", "claude",
     _dialog("Which approach should we take?", _RULE, "  1. Yes: rewrite the scanner",
             "  2. No: document it instead", _RULE, "  3. Chat about this",
             "Enter to select · Esc to cancel"), None),
    # rel #8: the question's top rule has scrolled out of the lookback. Only the
    # divider before "Chat about this" is left, so the block has no anchor.
    ("AskUserQuestion, top rule scrolled away", "claude",
     _dialog(*[f"  the question wraps, line {i}" for i in range(4)],
             "  1. Yes: rewrite the scanner", "  2. No: document it instead",
             _RULE, "  3. Chat about this", "Enter to select · Esc to cancel"), None),
    ("selected yes above a divider, no top rule", "claude",
     _dialog("  ❯ Yes: ship it", "    2. No: wait", _RULE, "    3. Chat about this",
             "Enter to select · Esc to cancel"), None),
    ("codex downgrade menu is not a Yes", "codex",
     "\n".join(["› 1. Retry with a faster model", "  2. Dismiss and keep waiting",
                "  Press enter to confirm or esc to cancel"]), None),
    ("the pattern's own source line", "claude",
     _dialog('    r"(?:\\(y/n/a\\)|\\[Y/n/a\\])\\s*$",'), None),
    ("(y/n) that is no longer the last line", "shell",
     "Overwrite? (y/n)\nnever mind, moving on\n", None),
    ("cursor trust, already answered", "cursor",
     "\n".join(["│  ⚠ Workspace Trust Required", "│  [a] Trust this workspace",
                "│  ⏳ Trusting workspace..."]), None),
    ("opencode row without its header", "opencode",
     "△ nothing here\n  Allow once  Allow always  Reject", None),
    ("y/n/a on a non-claude pane", "codex", _dialog("Allow this? (y/n/a)"), None),

    # --- decided by the detection depth -------------------------------------
    # The marker sits ten lines up: outside a depth-8 window, inside depth 12.
    ("y/n/a marker ten lines up", "claude",
     "\n".join(["Allow this? (y/n/a)", *[f"  status row {i}" for i in range(9)]]),
     {8: None, 12: "permission-yna"}),
    # The footer bound is depth*4 on both sides now: 31 rows below the footer is
    # inside depth 8's 32, and 40 is outside it but inside depth 12's 48.
    ("footer 31 rows up", "claude",
     _dialog(_RULE, "  1. Yes", "  2. No", "Enter to select · Esc to cancel",
             *[f"  todo {i}" for i in range(31 - len(_STATUS))]),
     "numbered-yes"),
    ("footer 40 rows up", "claude",
     _dialog(_RULE, "  1. Yes", "  2. No", "Enter to select · Esc to cancel",
             *[f"  todo {i}" for i in range(40 - len(_STATUS))]),
     {8: None, 12: "numbered-yes"}),
]

DEPTHS = (8, 12)


def _expected(expected, depth):
    return expected.get(depth) if isinstance(expected, dict) else expected


def _server(tail, agent_kind, depth):
    with patch("shared.state.get_setting", side_effect=lambda *keys: depth):
        got = _detect_autoyes_prompt(tail, agent_kind)
    return got[0] if got else None


_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const depth = Number(process.argv[1]);
globalThis.SETTINGS = {autoyes: {default_delay: 5, detection_depth: depth}};
globalThis.CLAUDE_CMD = 'claude';
globalThis._getSmartState = () => ({});
vm.runInThisContext(fs.readFileSync('js/actions.js', 'utf8'), {filename: 'js/actions.js'});
const corpus = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = corpus.map(([tail, kind]) => {
    const r = detectSmartActions(tail, 'parity:0.0', kind);
    // What Auto-Yes would answer from the browser's point of view: a detection
    // that offers the Auto-Yes toggle and is not a notify-only question.
    return r && !r.notifyOnly && _isAutoYesCandidate(r) ? r.id : null;
});
console.log(JSON.stringify(out));
"""


def _client(depth):
    payload = json.dumps([[tail, kind] for _name, kind, tail, _exp in CORPUS])
    result = subprocess.run(
        ["node", "-e", _HARNESS, str(depth)],
        cwd=ROOT, input=payload, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class DetectorParityTests(unittest.TestCase):
    def test_server_decides_every_fixture_as_labelled(self):
        for depth in DEPTHS:
            for name, kind, tail, expected in CORPUS:
                with self.subTest(depth=depth, fixture=name):
                    self.assertEqual(_server(tail, kind, depth), _expected(expected, depth))

    def test_browser_decides_every_fixture_as_labelled(self):
        for depth in DEPTHS:
            got = _client(depth)
            for (name, _kind, _tail, expected), client in zip(CORPUS, got):
                want = _expected(expected, depth)
                with self.subTest(depth=depth, fixture=name):
                    self.assertEqual(client, _CLIENT_TYPE.get(want, want))

    def test_the_corpus_covers_both_directions(self):
        outcomes = {_expected(exp, depth) is None for depth in DEPTHS for *_x, exp in CORPUS}
        self.assertEqual(outcomes, {True, False})


class DepthIsOneNumberTests(unittest.TestCase):
    """The browser reads the server's depth from /poll instead of assuming 8."""

    def test_poll_ships_the_depth_the_scanner_uses(self):
        source = (ROOT / "routes/poll.py").read_text()
        self.assertIn('result["detection_depth"] = detection_depth()', source)

    def test_the_browser_adopts_it(self):
        self.assertIn("_detectionDepth = data.detection_depth", (ROOT / "js/app.js").read_text())

    def test_no_hard_coded_detection_windows_are_left_in_the_browser(self):
        source = (ROOT / "js/actions.js").read_text()
        self.assertNotIn("FOOTER_DEPTH_MAX = 30", source)
        self.assertNotIn("> 30) return false", source)
        self.assertNotIn(".slice(-8)", source)
        self.assertNotIn(".slice(-14)", source)

    def test_junk_depths_fall_back_instead_of_killing_the_scanner(self):
        from routes import autoyes
        for raw, want in (("x", 8), (None, 8), (0, 2), (99, 30), (12, 12), ("12", 12)):
            with self.subTest(raw=raw), patch(
                "routes.autoyes.state.get_setting", return_value=raw
            ):
                self.assertEqual(autoyes.detection_depth(), want)


if __name__ == "__main__":
    unittest.main()
