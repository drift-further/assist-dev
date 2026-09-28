"""Auto-Yes fails safe: when in doubt, the prompt is left to the human.

Auto-Yes types into a live pane with nobody watching, so every way it can lose
track of what the human decided, or of what is on screen, must end in "don't
answer". Each class pins one of those, in both directions: the unsafe answer is
gone, and the answer that should still happen still does.

  * A cancelled countdown stays cancelled (review rel #2). One failed identity
    probe used to drop the cancel with the countdown, and the next tick answered.
  * The answer is re-checked against the screen as it is sent (rel #3). A codex
    downgrade menu that replaced the prompt during the countdown took the Enter.
  * Arming is an absolute set, not a toggle (rel #4). A client with a stale view
    of the state turned Auto-Yes OFF when it meant ON.
  * A session armed by hand leaves shell panes alone unless it is marked
    shell-ok (sec F8), as the all-sessions switch always has.
  * STOP is a real touch target (ux #7).

Run: .venv/bin/python3 -m unittest tests.test_autoyes_failsafe
"""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import shared.state as state
from routes import autoyes
from shared.tmux import DeliveryResult

ROOT = Path(__file__).resolve().parents[1]

CLAUDE_YNA = (
    "● Bash(rm -rf build/)\n"
    "\n"
    "  Allow Claude to run this command?\n"
    "  Yes (y)   Always (a)   No (n)\n"
)
OTHER_YNA = CLAUDE_YNA.replace("rm -rf build/", "rm -rf dist/")
IDLE = "● Done.\n\n❯ \n"

CODEX = """Would you like to run the following command?

  $ git status

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for this command (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""
CODEX_THEN_DOWNGRADE = CODEX + (
    "\nOur systems are thinking a bit more about this request before responding.\n"
    "› 1. Retry with a faster model\n"
    "  2. Dismiss and keep waiting\n"
)

SHELL_YN = "$ ./cleanup.sh\nDelete 3 files? (y/n)"


def _long_codex(env):
    """A codex approval whose command wraps: the differing part (the command and
    the start of option 2) sits above twenty common wrapped rows, so the last
    500 characters before the footer are the same for every `env`."""
    rows = [f"     --artifact /srv/releases/common/build-{i:02d}/bundle.tar.gz \\" for i in range(20)]
    return "\n".join([
        "Would you like to run the following command?",
        "",
        f"  $ deploy {env} --all \\",
        *[r.replace("     ", "      ", 1) for r in rows],
        "",
        "› 1. Yes, proceed (y)",
        f"  2. Yes, and don't ask again for commands that start with `deploy {env} --all",
        *rows,
        "     ` (p)",
        "  3. No, and tell Codex what to do differently (esc)",
        "",
        "  Press enter to confirm or esc to cancel",
    ])


LONG_STAGING = _long_codex("staging")
LONG_PRODUCTION = _long_codex("production")

PANE_ID = "%1"
_IDENTITY = SimpleNamespace(as_dict=lambda: {"pane_id": PANE_ID})
_UNSET = object()


def _completed(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class _ScannerCase(unittest.TestCase):
    """Real scanner ticks against a faked tmux, with the real park and recheck."""

    SESSION = "fs"
    TARGET = "fs:0.0"

    def setUp(self):
        for name in (
            "autoyes_sessions",
            "autoyes_countdowns",
            "autoyes_answered",
            "autoyes_delays",
            "autoyes_cancelled",
        ):
            p = patch.dict(getattr(state, name), clear=True)
            p.start()
            self.addCleanup(p.stop)
        # Armed by hand, as the runtime map records it; the scanner's fast path
        # skips everything when that map is empty and the switch is off.
        state.autoyes_sessions[self.SESSION] = True
        self.source = "explicit"
        self.project = {"delay": 5, "shell_ok": False}
        self.events = []

    real_get_setting = staticmethod(state.get_setting)

    def settings(self, section, key, *args, **kwargs):
        if (section, key) == ("autoyes", "all_sessions"):
            return "on" if self.source == "global" else "off"
        if (section, key) == ("autoyes", "default_delay"):
            return 5
        return self.real_get_setting(section, key, *args, **kwargs)

    def tick(self, tail, now, *, kind="claude", identity=_IDENTITY, recapture=_UNSET,
             delivery="delivered"):
        """One scan tick. `recapture` is what the pane shows when the answer is
        about to be sent; by default the same as the scan saw."""
        again = tail if recapture is _UNSET else recapture

        def run(argv, **_kwargs):
            if "list-panes" in argv:
                return _completed(f"{self.SESSION}\t0\t0\t123\t{kind}\n")
            if "capture-pane" in argv and PANE_ID in argv:
                if again is None:
                    return _completed("", returncode=1)
                return _completed(again + "\n")
            if "capture-pane" in argv:
                return _completed(tail + "\n")
            raise AssertionError(argv)

        with patch("routes.autoyes.subprocess.run", run), patch(
            "routes.autoyes.time.time", return_value=now
        ), patch("routes.autoyes.resolve_process", return_value=kind), patch(
            "routes.autoyes.refine_with_content", return_value=kind
        ), patch(
            "routes.autoyes.state.autoyes_enabled_for", return_value=(True, self.source)
        ), patch("routes.autoyes.state.get_setting", side_effect=self.settings), patch(
            "routes.autoyes.state.get_project_setting",
            side_effect=lambda _s, _sec, key: self.project[key],
        ), patch(
            "routes.autoyes.expected_target_identity", return_value=identity
        ), patch(
            "routes.autoyes.broadcast_autoyes_event",
            side_effect=lambda *e: self.events.append(e),
        ), patch(
            "routes.autoyes.generation_bound_delivery",
            return_value=DeliveryResult(delivery),
        ) as deliver:
            autoyes._autoyes_scan_tick()
        return deliver

    def countdown(self):
        return state.autoyes_countdowns.get(self.TARGET)


class CancelStaysCancelledTests(_ScannerCase):
    def test_a_failed_identity_probe_does_not_revive_a_cancelled_countdown(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.assertTrue(autoyes.cancel_countdown(self.TARGET))
        self.tick(CLAUDE_YNA, now=101.0, identity=None)
        for now in (102.0, 110.0, 200.0):
            self.tick(CLAUDE_YNA, now=now).assert_not_called()
        self.assertIsNone(self.countdown())

    def test_the_cancel_ends_when_the_prompt_goes_away(self):
        self.tick(CLAUDE_YNA, now=100.0)
        autoyes.cancel_countdown(self.TARGET)
        self.tick(IDLE, now=101.0)
        self.assertNotIn(self.TARGET, state.autoyes_cancelled)
        # The same text again, after the pane moved on, is a new prompt.
        self.tick(CLAUDE_YNA, now=102.0)
        self.assertEqual(self.countdown()["deadline"], 107.0)

    def test_a_cancel_holds_for_the_pane_until_it_shows_no_prompt(self):
        """Judge2 #1: the cancel is per pane, not per identity, so nothing that
        changes an identity mid-prompt can revive it. A different prompt that
        replaces the cancelled one without a gap is left to the human too."""
        self.tick(CLAUDE_YNA, now=100.0)
        autoyes.cancel_countdown(self.TARGET)
        self.tick(OTHER_YNA, now=101.0)
        self.assertIsNone(self.countdown())
        self.tick(OTHER_YNA, now=200.0).assert_not_called()
        # Once the pane has shown no prompt, the next one is armed as usual.
        self.tick(IDLE, now=201.0)
        self.tick(OTHER_YNA, now=202.0)
        self.assertEqual(self.countdown()["deadline"], 207.0)
        self.tick(OTHER_YNA, now=207.0).assert_called_once()

    def test_a_failed_probe_skips_the_tick_and_keeps_the_countdown(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.tick(CLAUDE_YNA, now=105.0, identity=None).assert_not_called()
        self.assertEqual(self.countdown()["deadline"], 105.0)
        self.tick(CLAUDE_YNA, now=106.0).assert_called_once()

    def test_cancel_with_nothing_counting_down_is_a_no(self):
        self.assertFalse(autoyes.cancel_countdown(self.TARGET))

    def test_the_cancel_route_uses_the_same_record(self):
        self.tick(CLAUDE_YNA, now=100.0)
        app = Flask("cancel-route")
        app.register_blueprint(autoyes.autoyes_bp)
        with patch("routes.autoyes.broadcast_autoyes_event"):
            first = app.test_client().post("/autoyes/cancel", json={"target": self.TARGET})
            second = app.test_client().post("/autoyes/cancel", json={"target": self.TARGET})
        self.assertTrue(first.get_json()["cancelled"])
        self.assertFalse(second.get_json()["cancelled"])
        self.assertIn(self.TARGET, state.autoyes_cancelled)

    def test_the_popup_is_not_suppressed_for_a_cancelled_prompt(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.assertTrue(autoyes.autoyes_will_consider(self.TARGET, True, "global", "claude", "claude"))
        autoyes.cancel_countdown(self.TARGET)
        self.assertFalse(autoyes.autoyes_will_consider(self.TARGET, True, "global", "claude", "claude"))


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class RealCaptureIdentityTests(unittest.TestCase):
    """Judge2 #1 on real captures: a pane that scrolls or resizes under an
    unchanged approval. The scanner runs its own tmux argv against a throwaway
    server (never the ambient one); only process identity, the park's pane
    probe and the final keystroke delivery are stubbed.
    """

    SESSION = "cdx"
    TARGET = "cdx:0.0"

    def setUp(self):
        for name in ("autoyes_sessions", "autoyes_countdowns", "autoyes_answered",
                     "autoyes_delays", "autoyes_cancelled", "autoyes_effective"):
            p = patch.dict(getattr(state, name), clear=True)
            p.start()
            self.addCleanup(p.stop)
        state.autoyes_sessions[self.SESSION] = True
        self.tmpdir = tempfile.mkdtemp(prefix="assist-ayid-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        env = patch.dict(os.environ, {"TMUX_TMPDIR": self.tmpdir})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TMUX", None)  # else bare tmux calls reach the real server
        directory = Path(self.tmpdir) / f"tmux-{os.getuid()}"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket = str((directory / "default").resolve())
        self.assertNotEqual(self.socket, str(Path(f"/tmp/tmux-{os.getuid()}/default").resolve()))
        self.addCleanup(subprocess.run, ["tmux", "-S", self.socket, "kill-server"],
                        check=False, capture_output=True, timeout=5)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", self.socket, *args], check=True,
                              capture_output=True, text=True, timeout=5).stdout

    def start(self, dialog, kind):
        """The reviewer's pane: 160x24, 200 rows of history, a dialog, a status
        row, then `cat` with echo off so rows can be appended below it."""
        self.kind = kind
        body = Path(self.tmpdir) / "pane.txt"
        body.write_text("\n".join(f"old history {i:03d}" for i in range(200)) + "\n"
                        + dialog + "\n  Working (3s)")
        self.tmux("new-session", "-d", "-s", self.SESSION, "-x", "160", "-y", "24",
                  f"sh -c 'cat {body}; stty -echo; exec cat'")
        self.pane_id = self.tmux("display-message", "-p", "-t", self.SESSION, "#{pane_id}").strip()
        self.settle("Working (3s)")

    def settle(self, text):
        for _ in range(50):
            if text in self.tmux("capture-pane", "-p", "-t", self.SESSION):
                return
            time.sleep(0.05)
        self.fail(f"pane never showed {text!r}")

    def append_row(self, text):
        self.tmux("send-keys", "-t", self.SESSION, "-l", "\n" + text)
        self.tmux("send-keys", "-t", self.SESSION, "Enter")
        self.settle(text)

    def resize(self, rows):
        self.tmux("resize-window", "-t", self.SESSION, "-y", str(rows))
        for _ in range(50):
            if self.tmux("display-message", "-p", "-t", self.SESSION, "#{pane_height}").strip() == str(rows):
                return
            time.sleep(0.05)
        self.fail("pane never resized")

    def settings(self, section, key, *args, **kwargs):
        if (section, key) == ("autoyes", "all_sessions"):
            return "off"
        if (section, key) == ("autoyes", "detection_depth"):
            return 8
        return state.DEFAULT_SETTINGS[section][key]

    def tick(self, now):
        identity = SimpleNamespace(as_dict=lambda: {"pane_id": self.pane_id})
        with patch("routes.autoyes.time.time", return_value=now), patch(
            "routes.autoyes.resolve_process", return_value=self.kind
        ), patch("routes.autoyes.refine_with_content", return_value=self.kind), patch(
            "routes.autoyes.state.autoyes_enabled_for", return_value=(True, "explicit")
        ), patch("routes.autoyes.state.get_setting", side_effect=self.settings), patch(
            "routes.autoyes.state.get_project_setting", side_effect=lambda *_a: 5
        ), patch("routes.autoyes.expected_target_identity", return_value=identity), patch(
            "routes.autoyes.broadcast_autoyes_event"
        ), patch(
            "routes.autoyes.generation_bound_delivery", return_value=DeliveryResult("delivered")
        ) as deliver:
            autoyes._autoyes_scan_tick()
        return deliver

    def countdown(self):
        return state.autoyes_countdowns.get(self.TARGET)

    CLAUDE_DIALOG = "\n".join([
        "● Bash(git status)",
        "─" * 100,
        " Bash command",
        "",
        "   git status",
        "   Show working tree status",
        "",
        " Do you want to proceed?",
        " ❯ 1. Yes",
        "   2. Yes, and don't ask again for git status commands",
        "   3. No, and tell Claude what to do differently (esc)",
        "",
        " Esc to cancel · Tab to amend",
    ])

    def changes(self):
        return {
            "a row appended below the footer": lambda: self.append_row("  Working (4s)"),
            "a resize from 24 to 25 rows": lambda: self.resize(25),
        }

    def test_the_captures_really_move_while_the_dialog_does_not(self):
        """The reviewer's observation, reproduced: the capture's first row moves."""
        for name, change in self.changes().items():
            with self.subTest(name):
                self.tearDown_pane()
                self.start(CODEX, "codex")
                argv = ["tmux", "capture-pane", "-p", "-t", self.TARGET,
                        "-S", f"-{autoyes.detection_window_lines(8)}"]
                before = subprocess.run(argv, capture_output=True, text=True).stdout
                change()
                after = subprocess.run(argv, capture_output=True, text=True).stdout
                self.assertNotEqual(before.split("\n")[0], after.split("\n")[0])
                self.assertIn(CODEX.strip(), before)
                self.assertIn(CODEX.strip(), after)

    def tearDown_pane(self):
        """A fresh pane and fresh Auto-Yes state for the next subtest."""
        subprocess.run(["tmux", "-S", self.socket, "kill-session", "-t", self.SESSION],
                       capture_output=True, timeout=5)
        for name in ("autoyes_countdowns", "autoyes_answered", "autoyes_cancelled"):
            getattr(state, name).clear()

    def test_an_unchanged_approval_fires_on_its_original_deadline(self):
        for dialog, kind in ((CODEX, "codex"), (self.CLAUDE_DIALOG, "claude")):
            for name, change in self.changes().items():
                with self.subTest(kind=kind, change=name):
                    self.tearDown_pane()
                    self.start(dialog, kind)
                    self.tick(100.0).assert_not_called()
                    self.assertEqual(self.countdown()["deadline"], 105.0)
                    change()
                    self.tick(102.0).assert_not_called()
                    self.assertEqual(self.countdown()["deadline"], 105.0)
                    self.tick(105.0).assert_called_once()

    def test_a_cancelled_approval_stays_cancelled(self):
        for name, change in self.changes().items():
            with self.subTest(change=name):
                self.tearDown_pane()
                self.start(CODEX, "codex")
                self.tick(100.0)
                with patch("routes.autoyes.broadcast_autoyes_event"):
                    self.assertTrue(autoyes.cancel_countdown(self.TARGET))
                change()
                for now in (101.0, 106.0, 200.0):
                    self.tick(now).assert_not_called()
                self.assertIsNone(self.countdown())


class RecheckBeforeSendTests(_ScannerCase):
    def test_a_downgrade_menu_that_arrived_during_the_countdown_gets_no_enter(self):
        self.tick(CODEX, now=100.0, kind="codex")
        deliver = self.tick(CODEX, now=105.0, kind="codex", recapture=CODEX_THEN_DOWNGRADE)
        deliver.assert_not_called()
        self.assertIn((self.TARGET, "prompt_changed", "numbered-yes"), self.events)

    def test_a_different_prompt_on_screen_at_send_time_gets_no_answer(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.tick(CLAUDE_YNA, now=105.0, recapture=OTHER_YNA).assert_not_called()

    def test_a_prompt_that_was_answered_by_hand_gets_no_answer(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.tick(CLAUDE_YNA, now=105.0, recapture=IDLE).assert_not_called()

    def test_a_failed_recapture_gets_no_answer(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.tick(CLAUDE_YNA, now=105.0, recapture=None).assert_not_called()

    def test_the_same_prompt_is_still_answered(self):
        self.tick(CODEX, now=100.0, kind="codex")
        deliver = self.tick(CODEX, now=105.0, kind="codex")
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args.kwargs, {"text": "", "enter": True})

    def test_the_long_fixtures_share_their_last_500_characters(self):
        self.assertNotEqual(LONG_STAGING, LONG_PRODUCTION)
        self.assertEqual(autoyes._prompt_region(LONG_STAGING), autoyes._prompt_region(LONG_PRODUCTION))
        for tail in (LONG_STAGING, LONG_PRODUCTION):
            self.assertEqual(autoyes._detect_autoyes_prompt(tail, "codex")[0], "numbered-yes")

    def test_a_different_long_approval_with_the_same_suffix_gets_no_answer(self):
        """Judge #3: the staging approval was answered by hand and codex put up
        the production one before the delayed send ran."""
        self.tick(LONG_STAGING, now=100.0, kind="codex")
        deliver = self.tick(LONG_STAGING, now=105.0, kind="codex", recapture=LONG_PRODUCTION)
        deliver.assert_not_called()

    def test_a_replacement_long_approval_gets_its_own_countdown(self):
        self.tick(LONG_STAGING, now=100.0, kind="codex")
        self.tick(LONG_PRODUCTION, now=103.0, kind="codex").assert_not_called()
        self.assertEqual(self.countdown()["deadline"], 108.0)
        self.tick(LONG_PRODUCTION, now=105.0, kind="codex").assert_not_called()
        self.tick(LONG_PRODUCTION, now=108.0, kind="codex").assert_called_once()

    def test_cancelling_a_long_approval_withholds_its_replacement_too(self):
        # Was "does not cancel its replacement". A cancel now holds for the
        # pane until no prompt is seen (judge2 #1): withholding is the safe way
        # to be wrong, and the human is already at this pane.
        self.tick(LONG_STAGING, now=100.0, kind="codex")
        autoyes.cancel_countdown(self.TARGET)
        self.tick(LONG_PRODUCTION, now=102.0, kind="codex")
        self.assertIsNone(self.countdown())
        self.tick(LONG_PRODUCTION, now=200.0, kind="codex").assert_not_called()

    def test_the_request_identity_covers_the_command_and_stops_at_the_footer(self):
        self.assertNotEqual(autoyes._prompt_identity(LONG_STAGING, "numbered-yes", "codex"),
                            autoyes._prompt_identity(LONG_PRODUCTION, "numbered-yes", "codex"))
        history = "\n".join(f"old history {i:03d}" for i in range(40))
        self.assertEqual(
            autoyes._prompt_identity(history + "\n" + LONG_STAGING + "\n  Working (3s)",
                                     "numbered-yes", "codex"),
            autoyes._prompt_identity("\n".join(history.split("\n")[7:]) + "\n" + LONG_STAGING
                                     + "\n  Working (9s)\n  Working (10s)", "numbered-yes", "codex"),
        )

    def test_a_request_whose_top_is_out_of_view_is_withheld(self):
        # The header scrolled out of the window: the command cannot be read.
        headless = LONG_STAGING.split("\n", 1)[1]
        self.assertIsNone(autoyes._prompt_identity(headless, "numbered-yes", "codex"))
        self.tick(headless, now=100.0, kind="codex")
        self.assertIsNone(self.countdown())
        self.tick(headless, now=200.0, kind="codex").assert_not_called()

    def test_an_unchanged_long_approval_still_fires(self):
        self.tick(LONG_STAGING, now=100.0, kind="codex")
        self.tick(LONG_STAGING, now=105.0, kind="codex").assert_called_once()

    def test_the_status_area_below_the_request_does_not_change_its_identity(self):
        self.tick(LONG_STAGING + "\n  gpt-6-astra high · 40% left", now=100.0, kind="codex")
        deliver = self.tick(LONG_STAGING + "\n  gpt-6-astra high · 39% left", now=105.0,
                            kind="codex")
        deliver.assert_called_once()

    def test_a_request_that_cannot_be_established_is_withheld(self):
        self.assertIsNone(autoyes._prompt_identity("no prompt terminator on this screen",
                                                   "confirm-yn", "shell"))
        with patch("routes.autoyes._prompt_identity", return_value=None):
            self.tick(LONG_STAGING, now=100.0, kind="codex")
            self.assertIsNone(self.countdown())
            self.tick(LONG_STAGING, now=200.0, kind="codex").assert_not_called()

    def test_the_recheck_runs_inside_the_park_effect(self):
        source = (ROOT / "routes/autoyes.py").read_text()
        body = source[source.index("def _deliver_autoyes_answer"):source.index("def _autoyes_scan_tick")]
        effect = body[body.index("def effect"):body.index("park.perform")]
        self.assertLess(effect.index("still_on_screen"), effect.index("generation_bound_delivery"))


class ShellPanesTests(_ScannerCase):
    def test_a_hand_armed_session_leaves_a_shell_prompt_alone(self):
        self.tick(SHELL_YN, now=100.0, kind="shell")
        self.assertIsNone(self.countdown())
        self.tick(SHELL_YN, now=200.0, kind="shell").assert_not_called()

    def test_shell_ok_restores_the_old_behaviour(self):
        self.project["shell_ok"] = True
        self.tick(SHELL_YN, now=100.0, kind="shell")
        self.assertEqual(self.countdown()["prompt_type"], "confirm-yn")
        self.tick(SHELL_YN, now=105.0, kind="shell").assert_called_once()

    def test_only_a_real_true_counts_as_shell_ok(self):
        self.project["shell_ok"] = "yes"
        self.tick(SHELL_YN, now=100.0, kind="shell")
        self.assertIsNone(self.countdown())

    def test_the_global_switch_never_answers_shells_even_with_shell_ok(self):
        self.source = "global"
        self.project["shell_ok"] = True
        self.tick(SHELL_YN, now=100.0, kind="shell")
        self.assertIsNone(self.countdown())

    def test_agent_panes_are_unaffected(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.tick(CLAUDE_YNA, now=105.0).assert_called_once()

    def test_the_popup_follows_the_same_rule(self):
        with patch("routes.autoyes.state.get_project_setting", return_value=False):
            self.assertFalse(autoyes.autoyes_will_consider(self.TARGET, True, "explicit", "shell", "shell"))
            self.assertTrue(autoyes.autoyes_will_consider(self.TARGET, True, "explicit", "claude", "claude"))
        with patch("routes.autoyes.state.get_project_setting", return_value=True):
            self.assertTrue(autoyes.autoyes_will_consider(self.TARGET, True, "explicit", "shell", "shell"))
            self.assertFalse(autoyes.autoyes_will_consider(self.TARGET, True, "global", "shell", "shell"))

    def test_shell_ok_is_a_project_setting_that_ships_off(self):
        self.assertIs(state.DEFAULT_PROJECT_SETTINGS["autoyes"]["shell_ok"], False)


class AbsoluteSetTests(unittest.TestCase):
    SESSION = "setme"

    def setUp(self):
        for name in ("autoyes_sessions", "autoyes_effective", "autoyes_countdowns",
                     "autoyes_answered", "autoyes_delays", "autoyes_cancelled"):
            p = patch.dict(getattr(state, name), clear=True)
            p.start()
            self.addCleanup(p.stop)
        self.global_on = False
        self.persisted = []
        defaults = copy.deepcopy(state.DEFAULT_PROJECT_SETTINGS)
        real_get_setting = state.get_setting

        def settings(section, key, *args, **kwargs):
            if (section, key) == ("autoyes", "all_sessions"):
                return "on" if self.global_on else "off"
            return real_get_setting(section, key, *args, **kwargs)

        for target, kwargs in (
            ("shared.state.get_setting", {"side_effect": settings}),
            ("shared.state.get_project_settings", {"return_value": defaults}),
            ("shared.state.patch_project_settings",
             {"side_effect": lambda s, patch_: self.persisted.append((s, patch_))}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        app = Flask("autoyes-set")
        app.register_blueprint(autoyes.autoyes_bp)
        self.client = app.test_client()

    def post(self, path, **body):
        return self.client.post(path, json={"session": self.SESSION, **body})

    def test_setting_on_twice_stays_on(self):
        for _ in range(2):
            reply = self.post("/autoyes/set", enabled=True, delay=3).get_json()
            self.assertTrue(reply["enabled"])
        self.assertIs(state.autoyes_sessions[self.SESSION], True)
        self.assertEqual(state.autoyes_delays[self.SESSION], 3.0)

    def test_setting_off_twice_stays_off(self):
        for _ in range(2):
            self.assertFalse(self.post("/autoyes/set", enabled=False).get_json()["enabled"])
        self.assertIs(state.autoyes_sessions[self.SESSION], False)

    def test_a_stale_client_under_the_global_switch_cannot_turn_it_off(self):
        """The rel #4 failure: the server was already on through the switch, the
        browser thought it was off and asked to turn it on."""
        self.global_on = True
        self.assertTrue(self.post("/autoyes/set", enabled=True).get_json()["enabled"])
        self.assertEqual(self.persisted[-1], (self.SESSION, {"autoyes": {"global_opt_out": False}}))

    def test_enabled_is_required_and_must_be_a_bool(self):
        for body in ({}, {"enabled": "yes"}, {"enabled": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/autoyes/set", **body).status_code, 400)

    def test_the_legacy_toggle_still_flips_for_an_old_cached_page(self):
        self.assertTrue(self.post("/autoyes/toggle").get_json()["enabled"])
        self.assertFalse(self.post("/autoyes/toggle").get_json()["enabled"])

    def test_no_caller_uses_the_toggle_any_more(self):
        for path in [*ROOT.glob("js/*.js"), *ROOT.glob("cli/*.py"), ROOT / "bin/assist"]:
            with self.subTest(path=path.name):
                self.assertNotIn("/autoyes/toggle", path.read_text())

    def test_the_browser_no_longer_arms_sessions_on_its_own(self):
        """The server resolves enabled_default at scan time; the browser's own
        auto-enable raced its status fetch and toggled the wrong way."""
        monitor = (ROOT / "js/monitor.js").read_text()
        body = monitor[monitor.index("async function loadProjectSettings"):
                       monitor.index("function renderProjectSettings")]
        self.assertNotIn("AutoYes(", body)


_CANCEL_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
globalThis.SETTINGS = {autoyes: {default_delay: 5}};
globalThis.flashes = [];
globalThis.showFlash = (kind, text) => flashes.push([kind, text]);
globalThis.document = {getElementById: () => null};
const [status, body] = JSON.parse(process.argv[1]);
globalThis.fetch = async () => ({ok: status === 200, json: async () => body});
vm.runInThisContext(fs.readFileSync('js/actions.js', 'utf8'), {filename: 'js/actions.js'});
vm.runInThisContext("_autoyesCountdown = {target: 'fs:0.0', remaining: 3, delay: 5}");
(async () => {
    await cancelAutoYesCountdown();
    console.log(JSON.stringify({kept: vm.runInThisContext('_autoyesCountdown') !== null, flashes}));
})();
"""


def _cancel_in_browser(status, body):
    result = subprocess.run(
        ["node", "-e", _CANCEL_HARNESS, json.dumps([status, body])],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class StopIsHonestTests(unittest.TestCase):
    """Found on a live run: a refused /autoyes/cancel (403) hid the bar and
    flashed "Cancelled" while the scanner went on to answer."""

    def test_a_refused_cancel_keeps_the_bar_and_says_so(self):
        got = _cancel_in_browser(403, {"ok": False})
        self.assertTrue(got["kept"])
        self.assertEqual(got["flashes"][0][0], "error")

    def test_a_confirmed_cancel_clears_the_bar(self):
        got = _cancel_in_browser(200, {"ok": True, "cancelled": True})
        self.assertFalse(got["kept"])
        self.assertEqual(got["flashes"], [["sent", "Cancelled"]])

    def test_a_cancel_that_came_too_late_says_so(self):
        got = _cancel_in_browser(200, {"ok": True, "cancelled": False})
        self.assertEqual(got["flashes"][0][0], "error")


class StopTargetTests(unittest.TestCase):
    def test_a_tap_anywhere_on_the_countdown_bar_cancels(self):
        html = (ROOT / "index.html").read_text()
        bar = html[html.index('id="autoyes-countdown"'):]
        bar = bar[:bar.index("</div>\n        </div>")]
        self.assertIn('onclick="cancelAutoYesCountdown()"', html[html.index('<div class="autoyes-countdown"'):][:200])
        self.assertIn(">STOP</button>", bar)

    def test_stop_is_at_least_44px_tall(self):
        css = (ROOT / "css/terminal.css").read_text()
        rule = css[css.index(".ay-cancel {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("min-height: 44px", rule)

    def test_the_delay_picker_says_what_arming_does(self):
        html = (ROOT / "index.html").read_text()
        self.assertIn('id="ay-pick-hint"', html)
        self.assertIn("Answers this session's permission prompts after", (ROOT / "js/actions.js").read_text())

    def test_shell_panes_sits_in_the_delay_picker_not_the_automate_panel(self):
        # The Automate panel is hidden while Automate is parked, so the toggle
        # lives beside the delay, where it stays reachable.
        html = (ROOT / "index.html").read_text()
        picker = html[html.index('id="autoyes-picker"'):]
        picker = picker[:picker.index('id="autoyes-countdown"')]
        self.assertIn('id="ay-pick-shell"', picker)
        actions = (ROOT / "js/actions.js").read_text()
        self.assertIn("{autoyes: {shell_ok: next}}", actions)
        self.assertIn("data.settings.autoyes.shell_ok === true", actions)
        self.assertNotIn("'shell_ok'", (ROOT / "js/monitor.js").read_text())


if __name__ == "__main__":
    unittest.main()
