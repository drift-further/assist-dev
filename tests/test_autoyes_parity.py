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
    # Judge #6: a long wrapped option 2 and a task panel below the footer. The
    # server saw option 1 in its -60 capture plus the screen; the browser, cut to
    # 60 rows, did not, so Auto-Yes answered what the action bar did not offer.
    ("wrapped option 2 over a task panel", "claude",
     "\n".join([_RULE, "  1. Yes", "  2. Yes, and don't ask again for commands like `deploy",
                 *[f"     --flag-{i:02d} /srv/common/path/{i:02d}" for i in range(30)],
                 "  3. No", "Enter to select · Esc to cancel",
                 *[f"  ☐ task {i}" for i in range(30)]]),
     "numbered-yes"),
]

DEPTHS = (8, 12)
# Settings allows 2..30. The generated boundary fixtures run across all of it.
RANGE_DEPTHS = (2, 8, 15, 16, 30)

_FOOTER = "Enter to select · Esc to cancel"


def _boundary_corpus(depth):
    """Fixtures on the edges of the window, for one depth.

    The window is the option lookback above the footer plus the rows the footer
    may sit above the bottom (depth*4). Each fixture fills it exactly, or misses
    by one row, so an input window cut any shorter or any longer shows up.
    """
    below = [f"  ☐ task {i}" for i in range(depth * 4)]
    wrap = lambda n: [f"     --flag-{i:03d} /srv/common/{i:03d}" for i in range(n)]
    lookback = max(depth * 4, 60)
    return [
        # No rule (codex style): option 1 exactly 60 rows above the footer.
        (f"d{depth} unanchored, option 1 at the lookback edge", "claude",
         "\n".join(["  1. Yes", "  2. Yes, and don't ask again for `deploy", *wrap(57),
                    "  3. No", _FOOTER, *below]), "numbered-yes"),
        (f"d{depth} unanchored, option 1 one row past it", "claude",
         "\n".join(["  1. Yes", "  2. Yes, and don't ask again for `deploy", *wrap(58),
                    "  3. No", _FOOTER, *below]), None),
        # A top rule at the anchor search floor, max(depth*4, 60) above the footer.
        (f"d{depth} anchored at the search floor", "claude",
         "\n".join([_RULE, "  1. Yes", "  2. Yes, and don't ask again for `deploy",
                    *wrap(lookback - 4), "  3. No", _FOOTER, *below]),
         "numbered-yes"),
        (f"d{depth} footer one row past its bound", "claude",
         "\n".join([_RULE, "  1. Yes", "  2. No", _FOOTER, *below, "  ☐ one more"]), None),
    ]


def _expected(expected, depth):
    return expected.get(depth) if isinstance(expected, dict) else expected


def _cases():
    """(depth, name, kind, tail, want) over the static and generated corpora."""
    for depth in DEPTHS:
        for name, kind, tail, expected in CORPUS:
            yield depth, name, kind, tail, _expected(expected, depth)
    for depth in RANGE_DEPTHS:
        for name, kind, tail, want in _boundary_corpus(depth):
            yield depth, name, kind, tail, want


# What a pane capture looks like around a fixture: scrollback the window must
# drop, colour escapes, and blank rows under the last line of output.
_SCROLLBACK = [f"  earlier output {i}" for i in range(300)]


def _capture(tail, ansi):
    lines = tail.split("\n")
    if ansi:
        lines = [f"\x1b[38;5;{i % 200}m{ln}\x1b[0m" if ln else ln for i, ln in enumerate(lines)]
    return "\n".join([*_SCROLLBACK, *lines, "", "", "\x1b[0m" if ansi else "", ""])


def _depth_patch(depth):
    return patch("shared.state.get_setting", side_effect=lambda *keys: depth)


def _server(tail, agent_kind, depth):
    with _depth_patch(depth):
        got = _detect_autoyes_prompt(tail, agent_kind)
    return got[0] if got else None


def _wire_tail(raw, depth):
    """The tail /poll serializes for this capture (routes/poll.py)."""
    from routes import poll
    target = "parity-wire:0.0"
    with _depth_patch(depth):
        poll._store_scan_capture(target, raw, (None, 0.0))
    with poll._SCAN_LOCK:
        return poll._SCAN_CACHE.pop(target)["tail"]


_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const depth = Number(process.argv[1]);
globalThis.SETTINGS = {autoyes: {default_delay: 5, detection_depth: depth}};
globalThis.CLAUDE_CMD = 'claude';
globalThis._getSmartState = () => ({});
const term = fs.readFileSync('js/terminal.js', 'utf8');
vm.runInThisContext(term.slice(term.indexOf('function _stripOsc'), term.indexOf('// Match http(s)')));
vm.runInThisContext(fs.readFileSync('js/actions.js', 'utf8'), {filename: 'js/actions.js'});
vm.runInThisContext(fs.readFileSync('js/poll-sync.js', 'utf8'), {filename: 'js/poll-sync.js'});
const answers = tail => {
    const r = detectSmartActions(tail, 'parity:0.0', kind);
    // What Auto-Yes would answer from the browser's point of view: a detection
    // that offers the Auto-Yes toggle and is not a notify-only question.
    return r && !r.notifyOnly && _isAutoYesCandidate(r) ? r.id : null;
};
let kind;
const corpus = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify({
    window: vm.runInThisContext('_detectionWindow'),
    out: corpus.map(([k, wire, stream]) => {
        kind = k;
        // Both ways a tail reaches detectSmartActions: a /poll scan entry and
        // a WebSocket frame, each through detectionTail (js/app.js, terminal.js).
        return [answers(detectionTail(wire)), answers(detectionTail(stream))];
    }),
}));
"""


def _client(depth, entries):
    result = subprocess.run(
        ["node", "-e", _HARNESS, str(depth)],
        cwd=ROOT, input=json.dumps(entries), text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class DetectorParityTests(unittest.TestCase):
    """Every fixture, as the scanner, the /poll wire and the stream each see it."""

    def test_server_decides_every_fixture_as_labelled(self):
        for depth, name, kind, tail, want in _cases():
            with self.subTest(depth=depth, fixture=name):
                # The scanner's capture: scrollback above, blank rows below.
                self.assertEqual(_server(_capture(tail, ansi=False), kind, depth), want)

    def test_browser_decides_every_fixture_as_labelled(self):
        by_depth = {}
        for depth, name, kind, tail, want in _cases():
            raw = _capture(tail, ansi=True)
            by_depth.setdefault(depth, []).append((name, want, [kind, _wire_tail(raw, depth), raw]))
        for depth, rows in by_depth.items():
            got = _client(depth, [entry for _n, _w, entry in rows])["out"]
            for (name, want, _entry), (wire, stream) in zip(rows, got):
                with self.subTest(depth=depth, fixture=name):
                    self.assertEqual(wire, _CLIENT_TYPE.get(want, want), "via /poll")
                    self.assertEqual(stream, _CLIENT_TYPE.get(want, want), "via the stream")

    def test_the_corpus_covers_both_directions(self):
        self.assertEqual({want is None for *_x, want in _cases()}, {True, False})


class InputWindowContractTests(unittest.TestCase):
    """One window: the rows both detectors read, and the rows every capture and
    the /poll wire carry. Judge #6."""

    def test_the_window_holds_the_lookback_and_the_rows_below_the_footer(self):
        from routes import autoyes
        for depth in range(2, 31):
            with self.subTest(depth=depth):
                self.assertEqual(
                    autoyes.detection_window_lines(depth),
                    max(depth * 4, autoyes._OPTION_REGION_LOOKBACK) + depth * 4 + 1,
                )

    def test_the_browser_computes_the_same_window_before_its_first_poll(self):
        from routes import autoyes
        for depth in (2, 8, 16, 30):
            with self.subTest(depth=depth):
                self.assertEqual(_client(depth, [])["window"], autoyes.detection_window_lines(depth))

    def test_poll_ships_the_window_and_the_browser_adopts_it(self):
        self.assertIn('result["detection_window"] = detection_window_lines()',
                      (ROOT / "routes/poll.py").read_text())
        self.assertIn("_detectionWindow = data.detection_window", (ROOT / "js/app.js").read_text())

    def test_every_capture_reaches_back_the_whole_window(self):
        for path in ("routes/autoyes.py", "routes/poll.py"):
            with self.subTest(path=path):
                source = (ROOT / path).read_text()
                self.assertNotIn('"-S", "-60"', source)
                self.assertIn('f"-{detection_window_lines()}"', source)


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
