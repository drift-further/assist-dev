"""Fixtures for the auto-yes prompt detector.

Auto-yes types keystrokes into a live agent pane with no human in the loop, so
this detector has to be wrong in neither direction:

  * Too loose and ordinary output that merely *looks* like a prompt gets
    answered — and pane text is attacker-influenced the moment an agent prints
    a diff, cats a README or renders something it fetched.
  * Too strict and it fails SILENTLY: panes sit on dialogs looking idle, and
    nobody finds out for hours.

So every fixture here is labelled with the direction it protects. Positive
fixtures are prompts that MUST keep firing; negative fixtures are text that
MUST NOT fire; and `KnownLooseTests` pins what is still loose on purpose, so
the residual risk is a visible, reviewable fact rather than a surprise.

Pane shapes are modelled on real captures taken from this host (Claude Code
2.1.241) plus the live transcriptions recorded in the comments of
routes/autoyes.py for opencode 1.18.4, cursor-agent 2026.07.23 and codex 0.147.

Run: .venv/bin/python3 -m unittest tests.test_autoyes_detection
"""

import unittest
from unittest.mock import patch

from routes.autoyes import _detect_autoyes_prompt

_DEPTH = 8

# A Claude Code pane's bottom rows, measured from live panes on this host. The
# composer box and the three status rows occupy six of the eight lines the
# detector looks at, which is why "anchor on the last non-empty line" — the
# rule confirm-yn uses — would disable a TUI detector outright.
_RULE = "─" * 60
_COMPOSER = [_RULE, "❯ ", _RULE]
_STATUS = [
    "  ▓▓▓▓▒▒▒▒▒▒ 43.2% (345k/800k) │ 1h24m │  studio/main",
    "  studio ⌀ │ ✎ 1 │ Opus 5",
    "  -- INSERT -- ⏵⏵ auto mode on · 1 shell · ↰ 1 agent",
]


def _idle_pane(*content):
    """A pane sitting at the composer: content, input box, status rows."""
    return "\n".join([*content, "", *_COMPOSER, *_STATUS])


def _dialog_pane(*content):
    """A pane blocked on a dialog: the box replaces the composer."""
    return "\n".join([*content, *_STATUS])


class _DetectorCase(unittest.TestCase):
    def setUp(self):
        # Pin detection_depth so a hand-edited settings.json cannot change what
        # these fixtures mean.
        patcher = patch(
            "shared.state.get_setting",
            side_effect=lambda *keys: _DEPTH,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertFires(self, tail, agent_kind, expected_type, msg=""):
        got = _detect_autoyes_prompt(tail, agent_kind)
        self.assertIsNotNone(got, f"expected {expected_type}, got nothing. {msg}")
        self.assertEqual(got[0], expected_type, msg)
        return got

    def assertSilent(self, tail, agent_kind, msg=""):
        got = _detect_autoyes_prompt(tail, agent_kind)
        self.assertIsNone(got, f"expected no detection, got {got!r}. {msg}")


class PermissionYnaPositiveTests(_DetectorCase):
    """MUST FIRE. Breaking any of these is a silent-stall regression."""

    def test_marker_above_a_status_bar_still_fires(self):
        """The shape a TUI dialog has: marker line, then the status rows.

        This is the fixture that rules out anchoring on the last non-empty
        line — in a Claude pane that line is always the status bar.
        """
        tail = _dialog_pane(
            "● Bash(rm -rf /tmp/scratch)",
            "  Delete the scratch directory",
            "Allow this? (y/n/a)",
        )
        self.assertFires(tail, "claude", "permission-yna")

    def test_marker_as_the_last_line_fires(self):
        """The readline shape: nothing below the prompt at all."""
        tail = "\n".join(
            [
                "Writing config.toml",
                "Overwrite the existing file? (y/n/a)",
            ]
        )
        self.assertFires(tail, "claude", "permission-yna")

    def test_bracket_marker_fires(self):
        tail = _dialog_pane("Apply this patch? [Y/n/a]")
        self.assertFires(tail, "claude", "permission-yna")

    def test_marker_with_trailing_whitespace_fires(self):
        """A pane capture right-pads rows; a prompt is still a prompt."""
        tail = _dialog_pane("Allow this? (y/n/a)     ")
        self.assertFires(tail, "claude", "permission-yna")

    def test_allow_once_button_row_fires_anywhere_in_the_window(self):
        """A full option row is self-corroborating — it keeps the wide window."""
        tail = _idle_pane("  Allow once    Always allow    Deny")
        self.assertFires(tail, "claude", "permission-yna")

    def test_yes_always_no_button_row_fires_anywhere_in_the_window(self):
        tail = _idle_pane("  Yes (y)   Always (a)   No (n)")
        self.assertFires(tail, "claude", "permission-yna")


class PermissionYnaNegativeTests(_DetectorCase):
    """MUST NOT FIRE. Each of these is text an agent routinely prints."""

    def test_this_repos_own_source_line_does_not_fire(self):
        """An agent reading routes/autoyes.py used to auto-answer itself.

        Verbatim from the pattern definition as it stood before this was
        anchored: it holds both the bare marker and the `.*`-joined option row,
        so displaying this one line was enough to fire.
        """
        tail = _idle_pane(
            '    r"\\(y/n/a\\)|\\[Y/n/a\\]|Allow once.*Always allow.*Deny"',
        )
        self.assertSilent(tail, "claude")

    def test_the_current_pattern_source_does_not_fire_either(self):
        """The same guarantee for the lines that replaced it."""
        for line in (
            '    r"(?:\\(y/n/a\\)|\\[Y/n/a\\])\\s*$",',
            '    r"Allow once\\s+Always allow\\s+Deny"',
            '    r"|Yes.*\\(y\\).*Always.*\\(a\\).*No.*\\(n\\)",',
        ):
            with self.subTest(line=line):
                self.assertSilent(_idle_pane(line), "claude")

    def test_prose_mentioning_the_marker_does_not_fire(self):
        tail = _idle_pane(
            "The installer answers (y/n/a) prompts for you when --yes is set.",
        )
        self.assertSilent(tail, "claude")

    def test_marker_inside_a_shell_transcript_does_not_fire(self):
        tail = _idle_pane(
            "  ⎿  usage: deploy [-f] [--confirm (y/n/a)] TARGET",
        )
        self.assertSilent(tail, "claude")

    def test_scrollback_marker_outside_the_window_does_not_fire(self):
        """Already bounded today — pinned so the window cannot be widened."""
        tail = _idle_pane(
            "Allow this? (y/n/a)",
            "y",
            "Done.",
            "",
            "Next task starting.",
        )
        self.assertSilent(tail, "claude")

    def test_non_claude_panes_never_use_this_branch(self):
        tail = _dialog_pane("Allow this? (y/n/a)")
        for kind in ("codex", "opencode", "cursor", "gemini", "shell"):
            with self.subTest(agent_kind=kind):
                self.assertSilent(tail, kind)


class KnownLooseTests(_DetectorCase):
    """Documented residual risk, pinned so it is visible rather than assumed.

    The end-of-line anchor removes the case that actually bites — a marker
    embedded in displayed code or prose — but a line whose last characters are
    the marker still fires. Tightening further (requiring a tool line above,
    or the last non-empty line) could not be shown safe in the firing
    direction, so it was not done. See SECURITY.md.
    """

    def test_a_doc_line_ending_in_the_marker_still_fires(self):
        tail = _idle_pane("  Answer the prompt with (y/n/a)")
        self.assertFires(tail, "claude", "permission-yna")


class NumberedPromptRegressionTests(_DetectorCase):
    """The branch real Claude Code permission prompts actually take today."""

    def test_codex_approval_block_fires(self):
        tail = _dialog_pane(
            _RULE,
            "  › 1. Yes, proceed (y)",
            "    2. No, and tell me what to do instead",
            "Press enter to confirm or esc to cancel",
        )
        self.assertFires(tail, "codex", "numbered-yes")

    def test_codex_directory_trust_fires(self):
        tail = _dialog_pane(
            "Do you trust the contents of this directory?",
            _RULE,
            "  › 1. Yes, continue",
            "    2. No, quit",
            "Press enter to continue",
        )
        self.assertFires(tail, "codex", "numbered-yes")

    def test_claude_permission_menu_fires(self):
        tail = _dialog_pane(
            "Do you want to proceed?",
            _RULE,
            "  1. Yes",
            "  2. Yes, and don't ask again for rm commands",
            "  3. No, and tell Claude what to do differently (esc)",
            "Enter to select · Esc to cancel",
        )
        self.assertFires(tail, "claude", "numbered-yes")

    def test_ask_user_question_is_never_answered(self):
        """An internal divider means a design question put to the human."""
        tail = _dialog_pane(
            "Which approach should we take?",
            _RULE,
            "  1. Yes: rewrite the scanner",
            "  2. No: document it instead",
            _RULE,
            "  3. Chat about this",
            "Enter to select · Esc to cancel",
        )
        self.assertSilent(tail, "claude")

    def test_selected_yes_without_a_number_fires(self):
        tail = _dialog_pane(
            _RULE,
            "  ❯ Yes",
            "    2. No",
            "Enter to select · Esc to cancel",
        )
        self.assertFires(tail, "claude", "selected-yes")


class ShellPromptRegressionTests(_DetectorCase):
    """Last-line-anchored matchers — unchanged, pinned."""

    def test_package_confirm_fires_with_enter(self):
        tail = "Need to get 12.3 MB.\nDo you want to continue? [Y/n] "
        _type, send, enter, _summary = self.assertFires(
            tail, "shell", "package-confirm"
        )
        self.assertEqual((send, enter), ("y", True))

    def test_ssh_host_key_fires_with_yes(self):
        tail = (
            "ED25519 key fingerprint is SHA256:abc.\n"
            "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
        )
        _type, send, enter, _summary = self.assertFires(tail, "shell", "ssh-host-key")
        self.assertEqual((send, enter), ("yes", True))

    def test_confirm_yn_needs_the_last_line(self):
        self.assertFires(tail="Overwrite? (y/n)", agent_kind="shell",
                         expected_type="confirm-yn")
        self.assertSilent("Overwrite? (y/n)\nnever mind, moving on\n", "shell")

    def test_opencode_permission_needs_its_header(self):
        row = "  Allow once  Allow always  Reject"
        self.assertSilent("\n".join(["△ nothing here", row]), "opencode")
        self.assertFires(
            "\n".join(["△ Permission required", "Access external directory", row]),
            "opencode",
            "opencode-permission",
        )

    def test_cursor_permission_needs_header_and_option_row(self):
        tail = "\n".join(
            [
                " $  cat /etc/os-release | head -3 in .",
                "",
                " Run this command?",
                " Not in allowlist: cat, head",
                "  → Run (once) (y)",
                "    Run Everything (shift+tab)",
            ]
        )
        self.assertFires(tail, "cursor", "cursor-permission")
        self.assertSilent(" Run this command?\n  (no option row)", "cursor")

    def test_cursor_trust_needs_its_live_footer(self):
        answered = "\n".join(
            ["│  ⚠ Workspace Trust Required", "│  [a] Trust this workspace",
             "│  ⏳ Trusting workspace..."]
        )
        self.assertSilent(answered, "cursor")
        live = answered.replace(
            "⏳ Trusting workspace...", "Use arrow keys to navigate, Enter to select"
        )
        self.assertFires(live, "cursor", "cursor-trust")


if __name__ == "__main__":
    unittest.main()
