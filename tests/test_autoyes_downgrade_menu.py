"""The codex model-downgrade veto: auto-yes must not press Enter near one.

Codex has two menus that change the model instead of approving an action:

  * the slow-request menu — "Our systems are thinking a bit more about this
    request…", option 1 "Retry with a faster model", option 2 "Dismiss and keep
    waiting";
  * the quota menu — "Approaching rate limits — Switch to gpt-5.6-luna for lower
    credit usage?", option 1 the switch.

Neither is answerable by the numbered branch (option 1 is not "Yes"), so the
detector was already silent ON them. What bit on 2026-09-05 is the other
direction: both are Enter-activated with the downgrade preselected, so an Enter
sent for something else — a permission prompt that was live one tick earlier —
lands on the menu and silently retiers the pane. Two of the three switches that
day are timestamp-identical with an Enter Assist delivered.

So these fixtures pin a VETO, not a matcher. Direction of each test is in its
name: `_is_vetoed` cases must stop firing, `_still_fires` cases must not be
caught by the veto's collateral. The veto errs wide on purpose — a pane that
merely displays the text is suppressed too — and KnownWideTests pins that as a
visible fact rather than a surprise.

Pure functions only: no tmux server, no live pane, no HTTP.

Run: .venv/bin/python3 -m unittest tests.test_autoyes_downgrade_menu
"""

import unittest
from unittest.mock import patch

from routes.autoyes import _CODEX_DOWNGRADE_RE, detect_answerable_prompt

_DEPTH = 8

# codex 0.153.4's approval block, transcribed in routes/autoyes.py's comments
# and already covered by test_autoyes_detection. It is the "would otherwise
# fire" half of every composition test below: without it, a veto test proves
# nothing, because the detector would have returned None anyway.
_CODEX_APPROVAL = [
    "  $ rg -n 'grant' sql/010_grants.sql",
    "",
    "  Would you like to run the following command?",
    "› 1. Yes, proceed (y)",
    "  2. Yes, and don't ask again for commands that start with `rg` (p)",
    "  3. No, and tell Codex what to do differently (esc)",
    "  Press enter to confirm or esc to cancel",
]

# The slow-request menu as codex draws it. Strings are verbatim from the
# 0.153.4 binary; the layout is codex's standard selection popup.
_SLOW_REQUEST_MENU = [
    "  Our systems are thinking a bit more about this request before responding.",
    "  Hang tight or retry with a faster model for a quicker response, though it",
    "  may be less capable of handling complex requests.",
    "",
    "› 1. Retry with a faster model",
    "  2. Dismiss and keep waiting",
    "  Press enter to confirm or esc to cancel",
]

# The quota menu, as recorded live on 2026-08-10.
_QUOTA_MENU = [
    "  Approaching rate limits — Switch to gpt-5.6-luna for lower credit usage?",
    "› 1. Switch to gpt-5.6-luna",
    "  2. Keep the current model",
    "  Press enter to confirm or esc to cancel",
]

_STATUS = ["", "  gpt-6-astra max · Context 62% left · ~/src/x"]


def _pane(*blocks):
    return "\n".join([ln for block in blocks for ln in block] + _STATUS)


class _VetoCase(unittest.TestCase):
    def setUp(self):
        # Pin detection_depth, same reason as test_autoyes_detection: a
        # hand-edited settings.json must not change what these fixtures mean.
        patcher = patch(
            "shared.state.get_setting",
            side_effect=lambda *keys: _DEPTH,
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class BaselineTests(_VetoCase):
    """Without the menu on screen, the codex approval must still be answered."""

    def test_codex_approval_alone_still_fires(self):
        result = detect_answerable_prompt(_pane(_CODEX_APPROVAL), "codex")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "numbered-yes")
        # Bare Enter — which is exactly why the menus below are dangerous.
        self.assertEqual((result[1], result[2]), ("", True))


class DowngradeMenuVetoTests(_VetoCase):
    """An Enter that would fire for something else must be withheld."""

    def test_slow_request_menu_below_a_live_approval_is_vetoed(self):
        # The shape that cost the tier on 2026-09-05: the approval was live one
        # tick ago, the menu opened over it, the Enter would land on the menu.
        pane = _pane(_CODEX_APPROVAL, _SLOW_REQUEST_MENU)
        self.assertIsNotNone(_detect_ignoring_veto(pane))
        self.assertIsNone(detect_answerable_prompt(pane, "codex"))

    def test_slow_request_menu_above_a_live_approval_is_vetoed(self):
        pane = _pane(_SLOW_REQUEST_MENU, _CODEX_APPROVAL)
        self.assertIsNotNone(_detect_ignoring_veto(pane))
        self.assertIsNone(detect_answerable_prompt(pane, "codex"))

    def test_quota_menu_is_vetoed(self):
        pane = _pane(_CODEX_APPROVAL, _QUOTA_MENU)
        self.assertIsNotNone(_detect_ignoring_veto(pane))
        self.assertIsNone(detect_answerable_prompt(pane, "codex"))

    def test_quota_menu_alone_was_never_answerable_and_still_is_not(self):
        # Option 1 is "Switch to…", not "Yes" — the numbered branch never
        # matched it. Pinned so a future widening of _NUMBERED_YES_RE cannot
        # quietly make this menu answerable again.
        self.assertIsNone(detect_answerable_prompt(_pane(_QUOTA_MENU), "codex"))

    def test_slow_request_menu_alone_was_never_answerable_and_still_is_not(self):
        self.assertIsNone(
            detect_answerable_prompt(_pane(_SLOW_REQUEST_MENU), "codex")
        )

    def test_each_menu_phrase_vetoes_on_its_own(self):
        # A narrow pane wraps the menu, so no single line is guaranteed intact.
        # Each phrase has to carry the veto by itself.
        for phrase in (
            "Retry with a faster model",
            "Dismiss and keep waiting",
            "thinking a bit more about this request",
            "Approaching rate limits",
            "for lower credit usage",
        ):
            with self.subTest(phrase=phrase):
                pane = _pane(_CODEX_APPROVAL, ["  " + phrase])
                self.assertIsNone(detect_answerable_prompt(pane, "codex"))


class VetoScopeTests(_VetoCase):
    """The veto is codex-only and must not cost any other pane its auto-yes."""

    def test_claude_permission_prompt_is_untouched(self):
        # Claude Code has no such menu; the same words in a Claude pane are an
        # agent quoting this file or the audit, and must not disarm the pane.
        pane = "\n".join(
            [
                "● Bash(rg -n grant sql/010_grants.sql)",
                "  Do you want to proceed? (y/n/a)",
                "  Retry with a faster model",
            ]
        )
        result = detect_answerable_prompt(pane, "claude")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "permission-yna")

    def test_luna_veto_still_applies(self):
        # The pre-existing kill-switch must survive the refactor that made both
        # vetoes share one function.
        pane = _pane(_CODEX_APPROVAL, ["  model: gpt-5.6-luna low"])
        self.assertIsNone(detect_answerable_prompt(pane, "codex"))

    def test_ordinary_codex_output_is_not_vetoed(self):
        # The words the veto keys on must not be reachable by ordinary prose
        # about models, or every codex pane loses auto-yes.
        pane = _pane(
            _CODEX_APPROVAL,
            [
                "  Switched the benchmark to a faster codec and re-ran it.",
                "  The model kept waiting for the lock to clear.",
                "  Rate limiting is handled by the proxy.",
            ],
        )
        result = detect_answerable_prompt(pane, "codex")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "numbered-yes")


class KnownWideTests(_VetoCase):
    """What the veto suppresses on purpose, recorded so it is not a surprise."""

    def test_a_pane_merely_displaying_the_menu_text_is_also_suppressed(self):
        # An agent reading routes/autoyes.py or the 2026-09-05 audit prints
        # these phrases, and that pane stops being auto-answered until the text
        # scrolls out of the 60-line capture. This is the same trade _LUNA_RE
        # makes: the human can always answer by hand, and the alternative is a
        # silent tier downgrade nobody notices.
        pane = _pane(
            _CODEX_APPROVAL,
            ['  _CODEX_DOWNGRADE_RE = re.compile(r"Retry with a faster model"'],
        )
        self.assertIsNone(detect_answerable_prompt(pane, "codex"))

    def test_the_veto_pattern_matches_its_own_source_line(self):
        # States the above as a property of the pattern rather than of one
        # fixture, so it stays true if the fixture changes.
        self.assertTrue(_CODEX_DOWNGRADE_RE.search(_CODEX_DOWNGRADE_RE.pattern))


def _detect_ignoring_veto(pane):
    """The raw detection, to prove a veto test is actually vetoing something."""
    from routes.autoyes import _detect_autoyes_prompt

    return _detect_autoyes_prompt(pane, "codex")


if __name__ == "__main__":
    unittest.main()
