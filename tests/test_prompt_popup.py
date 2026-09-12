"""Popups for panes waiting on you: sudo anywhere, agent prompts in tabs not in view.

Seams, each with fixtures in both directions:

1. Sudo detection (shared/tmux.py:sudo_prompt_waiting). A pane qualifies only when
   sudo's own prompt is its last line AND a childless sudo process is in the
   foreground of its tty. The prompt line alone can be printed by anything; an
   authenticated sudo running a silent command leaves the same line on screen but
   has forked a child.
2. Question info (routes/autoyes.py:prompt_popup_info). The fingerprint ignores
   the animated status bar below a prompt and changes with the prompt itself;
   `autoyes` is true only when Auto-Yes is armed AND would answer this prompt, so
   a question it never answers still pops on an armed pane.
3. The Send guard on /type. A tap lands seconds after the poll that raised the
   popup; `expect_prompt_pid` refuses it once that sudo has stopped waiting.
4. The popups (js/prompt-popup.js), run under Node with a stub DOM: once per
   prompt, configurable auto-hide, never take focus. Sudo offers Send (Open tab
   with no stored $sudo); a question popup only ever opens the tab.

Run: .venv/bin/python3 -m unittest tests.test_prompt_popup
"""

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import shared.state as state
from routes import autoyes
from routes.input import input_bp
from shared import tmux
from shared.state import DEFAULT_SETTINGS
from shared.tmux import DeliveryResult, ExpectedTargetIdentity


ROOT = Path(__file__).resolve().parents[1]

PROMPT_TAIL = "daniel@host:~$ sudo apt install tmux\n[sudo] password for daniel: "
# bash (101) is the parent of sudo (202); nothing is sudo's child yet.
WAITING_TTY = "  101 Ss   -bash\n  202 S+   sudo apt install tmux\n"
PARENTS_WAITING = "    1\n  100\n  101\n"
PARENTS_AUTHENTICATED = PARENTS_WAITING + "  202\n"

PERMISSION = """● Bash(git status)
  ⎿  Running…

 Bash command

   git status
   Show working tree status

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for git status commands in /home/x
   3. No, and tell Claude what to do differently (esc)

 Esc to cancel · Tab to add additional instructions
"""

QUESTION = """☐ Scope

Which prompts should pop up?

❯ 1. Permissions
     Allow and deny prompts
  2. Everything
     Same as the action bar
────────────────────────────────────────
  3. Type something.
────────────────────────────────────────
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

CODEX = """Would you like to run the following command?

  $ git status

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for this command (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""


CLAUDE_YNA = (
    "● Bash(ssh build-host uptime)\n"
    "\n"
    "  Allow Claude to run this command?\n"
    "  Yes (y)   Always (a)   No (n)\n"
)

SSH_AFTER_YNA = (
    CLAUDE_YNA
    + "".join(f"  ⎿  connecting to build-host, attempt {i}\n" for i in range(10))
    + "The authenticity of host 'build-host (10.0.0.5)' can't be established.\n"
    + "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
)


def _completed(stdout, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _host(on_tty=WAITING_TTY, parents=PARENTS_WAITING, tty="/dev/pts/9", screen=PROMPT_TAIL,
          echo="-echo"):
    """subprocess.run stand-in for the tmux, ps and stty calls the detector makes.

    `echo` is the pane terminal's echo flag as stty prints it: "-echo" while sudo
    reads the terminal, "echo" when nothing does, None when stty cannot read it.
    """
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[0] == "stty":
            if echo is None:
                return _completed("", 1)
            return _completed("speed 38400 baud; line = 0;\nisig icanon iexten " + echo + " echoe echok\n")
        if argv[:2] == ["ps", "-t"]:
            return _completed(on_tty)
        if argv[:2] == ["ps", "-A"]:
            return _completed(parents)
        if argv[:2] == ["tmux", "display-message"]:
            return _completed(tty + "\n")
        if argv[:2] == ["tmux", "capture-pane"]:
            return _completed(screen + "\n\n")
        raise AssertionError(f"unexpected command {argv}")

    run.calls = calls
    return run


def _no_subprocess(argv, **_kwargs):
    raise AssertionError(f"shelled out when it should not have: {argv}")


class SudoPromptDetectionTests(unittest.TestCase):
    def detect(self, tail, reads_terminal=True, **host):
        with patch("shared.tmux.subprocess.run", _host(**host)), patch(
            "shared.tmux._sudo_reads_terminal", return_value=reads_terminal
        ):
            return tmux.sudo_prompt_waiting("/dev/pts/9", tail)

    def test_a_waiting_sudo_is_reported_with_its_own_argv(self):
        self.assertEqual(
            self.detect(PROMPT_TAIL),
            {"pid": 202, "command": "sudo apt install tmux"},
        )

    def test_escape_sequences_around_the_prompt_do_not_hide_it(self):
        tail = "\x1b[32mdaniel@host\x1b[0m:~$ sudo true\n\x1b[0m[sudo] password for daniel: \x1b[K\n\n"
        self.assertEqual(self.detect(tail)["pid"], 202)

    def test_sudo_by_full_path_still_counts(self):
        on_tty = "  101 Ss   -bash\n  202 S+   /usr/bin/sudo -E make install\n"
        self.assertEqual(
            self.detect(PROMPT_TAIL, on_tty=on_tty)["command"],
            "/usr/bin/sudo -E make install",
        )

    def test_authenticated_sudo_with_its_prompt_still_on_screen_is_not_waiting(self):
        self.assertIsNone(self.detect(PROMPT_TAIL, parents=PARENTS_AUTHENTICATED))

    def test_a_printed_prompt_with_no_sudo_process_is_ignored(self):
        on_tty = "  101 Ss   -bash\n  303 S+   cat\n"
        self.assertIsNone(self.detect(PROMPT_TAIL, on_tty=on_tty))

    def test_a_background_sudo_is_ignored(self):
        on_tty = "  101 Ss+  -bash\n  202 S    sudo apt install tmux\n"
        self.assertIsNone(self.detect(PROMPT_TAIL, on_tty=on_tty, parents="1\n100\n"))

    def test_a_prompt_that_is_no_longer_the_last_line_never_runs_ps(self):
        tail = PROMPT_TAIL + "\nsudo: a password is required\ndaniel@host:~$ "
        with patch("shared.tmux.subprocess.run", _no_subprocess):
            self.assertIsNone(tmux.sudo_prompt_waiting("/dev/pts/9", tail))

    def test_other_password_prompts_do_not_qualify(self):
        for line in (
            "daniel@host's password: ",
            "Enter passphrase for key '/home/daniel/.ssh/id_ed25519': ",
            "echo sudo; Enter password: ",
        ):
            with self.subTest(line=line):
                self.assertIsNone(self.detect("$ sudo ssh host\n" + line))

    def test_macos_sudo_prompt_counts_even_after_the_command_scrolled_away(self):
        self.assertEqual(self.detect("lots of output\nPassword: ")["pid"], 202)

    def test_a_bare_password_prompt_owned_by_something_else_is_ignored(self):
        on_tty = "  101 Ss   -bash\n  303 S+   su -\n"
        self.assertIsNone(self.detect("$ su -\nPassword: ", on_tty=on_tty))


    def test_a_pipeline_feeding_sudo_S_is_two_waiters_and_not_offered(self):
        # `sleep 30 | sudo -S true`: sleep feeds the pipe sudo reads, so what we
        # type into the pane reaches sleep, not sudo. Two foreground waiters.
        on_tty = "  101 Ss   -bash\n  201 S+   sleep 30\n  202 S+   sudo -S true\n"
        self.assertIsNone(self.detect(PROMPT_TAIL, on_tty=on_tty, echo="-echo"))

    def test_the_lone_waiting_sudo_with_echo_off_is_offered(self):
        self.assertEqual(self.detect(PROMPT_TAIL, echo="-echo")["pid"], 202)

    def test_a_sudo_reading_stdin_is_not_offered_even_as_a_lone_waiter(self):
        # The inherited-pipe bypass: one foreground waiter, echo off, but -S.
        self.assertIsNone(self.detect(PROMPT_TAIL, reads_terminal=False, echo="-echo"))

    def test_echo_left_on_is_still_required(self):
        self.assertIsNone(self.detect(PROMPT_TAIL, echo="echo"))

    def test_a_terminal_stty_cannot_read_is_a_no(self):
        self.assertIsNone(self.detect(PROMPT_TAIL, echo=None))


class PromptOwnerGuardTests(unittest.TestCase):
    def guard(self, pid, reads_terminal=True, **host):
        run = _host(**host)
        with patch("shared.tmux.subprocess.run", run), patch(
            "shared.tmux._sudo_reads_terminal", return_value=reads_terminal
        ):
            return tmux.prompt_owner_waiting("%9", pid), run.calls

    def test_the_waiting_sudo_passes_and_the_pane_id_is_used_as_is(self):
        ok, calls = self.guard(202)
        self.assertTrue(ok)
        tmux_targets = [argv[argv.index("-t") + 1] for argv in calls if argv[0] == "tmux"]
        self.assertEqual(tmux_targets, ["%9", "%9"])

    def test_a_different_process_fails(self):
        self.assertFalse(self.guard(999)[0])

    def test_a_sudo_that_authenticated_meanwhile_fails(self):
        self.assertFalse(self.guard(202, parents=PARENTS_AUTHENTICATED)[0])

    def test_a_pane_that_moved_on_to_a_shell_fails(self):
        screen = PROMPT_TAIL + "\nsudo: a password is required\ndaniel@host:~$ "
        self.assertFalse(self.guard(202, screen=screen)[0])

    def test_malformed_pids_fail_without_asking_tmux(self):
        for pid in ("abc", None, True, [202]):
            with self.subTest(pid=pid), patch("shared.tmux.subprocess.run", _no_subprocess):
                self.assertFalse(tmux.prompt_owner_waiting("%9", pid))


    def test_a_pid_that_is_no_longer_sudo_fails(self):
        on_tty = "  101 Ss   -bash\n  202 S+   /usr/bin/ssh root@remote\n"
        for screen in ("root@remote's password: ", "Password: "):
            with self.subTest(screen=screen):
                self.assertFalse(self.guard(202, on_tty=on_tty, screen=screen)[0])

    def test_a_pipeline_feeding_sudo_S_fails_even_with_echo_off(self):
        on_tty = "  101 Ss   -bash\n  201 S+   sleep 30\n  202 S+   sudo -S true\n"
        self.assertFalse(self.guard(202, on_tty=on_tty, echo="-echo")[0])

    def test_a_lone_sudo_reading_stdin_fails(self):
        self.assertFalse(self.guard(202, reads_terminal=False, echo="-echo")[0])

    def test_a_non_sudo_password_prompt_on_screen_fails(self):
        self.assertFalse(self.guard(202, screen="daniel@host's password: ")[0])


class PromptFingerprintTests(unittest.TestCase):
    def test_the_status_bar_below_the_prompt_does_not_change_it(self):
        self.assertEqual(
            autoyes.prompt_fingerprint(PERMISSION + "\n  ✻ Vibing… (3s)"),
            autoyes.prompt_fingerprint(PERMISSION + "\n  ✻ Vibing… (4s)"),
        )

    def test_escape_sequences_do_not_change_it(self):
        colored = PERMISSION.replace("❯ 1. Yes", "\x1b[36m❯ 1. Yes\x1b[0m")
        self.assertEqual(autoyes.prompt_fingerprint(colored), autoyes.prompt_fingerprint(PERMISSION))

    def test_a_different_prompt_changes_it(self):
        other = PERMISSION.replace("git status", "git push --force")
        self.assertNotEqual(autoyes.prompt_fingerprint(other), autoyes.prompt_fingerprint(PERMISSION))

    def test_scrollback_rolling_above_the_dialog_does_not_change_it(self):
        # /poll captures -S -60. A line appended below a waiting prompt scrolls the
        # capture, dropping a leading history line — which must not re-key the
        # notification and re-pop a dismissed card.
        history = "".join(f"old output line {i:03d}\n" for i in range(60))
        before = history + CODEX + "  ✻ Working (3s)"
        after = history.split("\n", 1)[1] + CODEX + "  ✻ Working (3s)\n  ✻ Working (4s)"
        self.assertEqual(autoyes.prompt_fingerprint(before), autoyes.prompt_fingerprint(after))



    def test_claude_permission_rows_end_the_prompt_too(self):
        for row in ("Allow once   Always allow   Deny", "Yes (y)   Always (a)   No (n)"):
            with self.subTest(row=row):
                prompt = "● Bash(git status)\n\n Do you want to run this?\n " + row + "\n"
                self.assertEqual(
                    autoyes.prompt_fingerprint(prompt + "  ✻ Vibing… (3s)"),
                    autoyes.prompt_fingerprint(prompt + "  ✻ Vibing… (4s)"),
                )

    def test_long_prompts_that_differ_near_the_top_do_not_collide(self):
        paths = " ".join(f"/home/daniel/reports/quarterly-report-{i:02d}.txt" for i in range(30))
        self.assertNotEqual(
            autoyes.prompt_fingerprint(CODEX.replace("git status", "cat " + paths)),
            autoyes.prompt_fingerprint(CODEX.replace("git status", "truncate -s 0 " + paths)),
        )


class AutoYesScopeTests(unittest.TestCase):
    TARGET = "scope-test:0.0"

    def tearDown(self):
        with state.autoyes_lock:
            state.autoyes_countdowns.pop(self.TARGET, None)

    def consider(self, enabled=True, source="global", process_kind="claude", agent_kind="claude"):
        return autoyes.autoyes_will_consider(self.TARGET, enabled, source, process_kind, agent_kind)

    def test_an_agent_pane_in_a_globally_armed_session_is_covered(self):
        self.assertTrue(self.consider())

    def test_global_leaves_shells_and_foreground_non_agent_programs_manual(self):
        self.assertFalse(self.consider(process_kind="shell", agent_kind="shell"))
        self.assertFalse(self.consider(process_kind=None, agent_kind="shell"))

    def test_a_session_armed_by_hand_keeps_its_shell_prompts(self):
        self.assertTrue(self.consider(source="explicit", process_kind="shell", agent_kind="shell"))

    def test_a_disabled_session_is_never_covered(self):
        self.assertFalse(self.consider(enabled=False))

    def test_a_cancelled_countdown_leaves_the_prompt_to_the_human(self):
        with state.autoyes_lock:
            state.autoyes_countdowns[self.TARGET] = {"cancelled": True, "prompt_hash": 1}
        self.assertFalse(self.consider())

    def test_the_scanner_still_applies_the_same_two_gates(self):
        source = (ROOT / "routes/autoyes.py").read_text()
        self.assertIn('if source == "global" and process_kind == "shell":', source)
        self.assertIn('if source == "global" and agent_kind not in AGENT_KINDS:', source)
        self.assertIn("autoyes_will_consider(", (ROOT / "routes/poll.py").read_text())


class PromptPopupInfoTests(unittest.TestCase):
    def test_armed_and_answerable_means_auto_yes_will_answer(self):
        info = autoyes.prompt_popup_info(PERMISSION, "claude", autoyes_armed=True)
        self.assertTrue(info["autoyes"])
        self.assertEqual(info["summary"], "Show working tree status")
        self.assertEqual(info["fp"], autoyes.prompt_fingerprint(PERMISSION))

    def test_not_armed_means_the_popup_shows(self):
        self.assertFalse(autoyes.prompt_popup_info(PERMISSION, "claude", autoyes_armed=False)["autoyes"])

    def test_a_question_auto_yes_never_answers_still_pops_on_an_armed_pane(self):
        info = autoyes.prompt_popup_info(QUESTION, "claude", autoyes_armed=True)
        self.assertFalse(info["autoyes"])
        self.assertEqual(info["summary"], "Which prompts should pop up?")

    def test_a_vetoed_codex_prompt_still_pops_on_an_armed_pane(self):
        self.assertIsNotNone(autoyes._detect_autoyes_prompt(CODEX, "codex"), "fixture must be answerable")
        self.assertTrue(autoyes.prompt_popup_info(CODEX, "codex", autoyes_armed=True)["autoyes"])
        luna = CODEX + "\n  gpt-6-luna high · 40% left"
        self.assertFalse(autoyes.prompt_popup_info(luna, "codex", autoyes_armed=True)["autoyes"])

    def test_a_shell_confirmation_is_summarised_by_its_live_prompt_not_the_echo(self):
        tail = (
            "$ read -n 1 -p 'Old question? (y/n) ' a\n"
            "Old question? (y/n) n\n"
            "$ read -n 1 -p 'Delete 3 test files? (y/n) ' a\n"
            "Delete 3 test files? (y/n) "
        )
        self.assertEqual(
            autoyes.prompt_popup_info(tail, "shell", autoyes_armed=False)["summary"],
            "Delete 3 test files?",
        )


    def test_long_dialogs_that_differ_anywhere_get_different_keys(self):
        paths = " ".join(f"/home/daniel/reports/quarterly-report-{i:02d}.txt" for i in range(55))
        cat = CODEX.replace("git status", "cat " + paths)

        def wrapped(dialog, width=120):
            return "\n".join(
                line[i:i + width] or line
                for line in dialog.split("\n")
                for i in range(0, max(len(line), 1), width)
            )

        pairs = {
            "executable": (cat, CODEX.replace("git status", "truncate -s 0 " + paths)),
            "middle argument": (cat, cat.replace("quarterly-report-02.txt", "quarterly-report-99.txt")),
            "wrapped executable": (wrapped(cat), wrapped(CODEX.replace("git status", "tee " + paths))),
        }
        for name, (first, second) in pairs.items():
            with self.subTest(name):
                self.assertNotEqual(
                    autoyes.prompt_popup_info(first, "codex", False)["fp"],
                    autoyes.prompt_popup_info(second, "codex", False)["fp"],
                )


class AutoYesCountdownIdentityTests(unittest.TestCase):
    """A new prompt gets its own countdown, never an old prompt's.

    Round-2 review: with an old Claude y/a/n row still in the capture above a new
    ssh host-key prompt, the countdown hash stopped at the old row, so a real
    scanner tick reused the old deadline and answered the new prompt at once.
    """

    TARGET = "idtest:0.0"

    def setUp(self):
        for name in ("autoyes_sessions", "autoyes_countdowns", "autoyes_answered", "autoyes_delays"):
            p = patch.dict(getattr(state, name), clear=True)
            p.start()
            self.addCleanup(p.stop)
        state.autoyes_sessions["idtest"] = True

    real_get_setting = staticmethod(state.get_setting)

    def settings(self, section, key, *args, **kwargs):
        # Only the all-sessions switch is pinned, so this exercises the explicit
        # path; detection's own settings (its depth, say) stay real.
        if (section, key) == ("autoyes", "all_sessions"):
            return "off"
        return self.real_get_setting(section, key, *args, **kwargs)

    def tick(self, tail, now):
        def run(argv, **_kwargs):
            if "list-panes" in argv:
                return _completed("idtest\t0\t0\t123\tclaude\n")
            if "capture-pane" in argv:
                return _completed(tail + "\n")
            raise AssertionError(argv)

        with patch("routes.autoyes.subprocess.run", run), patch(
            "routes.autoyes.time.time", return_value=now
        ), patch("routes.autoyes.resolve_process", return_value="claude"), patch(
            "routes.autoyes.refine_with_content", return_value="claude"
        ), patch(
            "routes.autoyes.state.autoyes_enabled_for", return_value=(True, "explicit")
        ), patch("routes.autoyes.state.get_setting", side_effect=self.settings), patch(
            "routes.autoyes.state.get_project_setting", return_value=5
        ), patch(
            "routes.autoyes.expected_target_identity",
            return_value=SimpleNamespace(as_dict=lambda: {"pane_id": "%1"}),
        ), patch("routes.autoyes.broadcast_autoyes_event"), patch(
            "routes.autoyes._deliver_autoyes_answer",
            return_value=SimpleNamespace(ok=True, status="delivered"),
        ) as deliver:
            autoyes._autoyes_scan_tick()
        return deliver

    def test_the_fixtures_are_what_they_claim(self):
        self.assertEqual(autoyes._detect_autoyes_prompt(CLAUDE_YNA, "claude")[0], "permission-yna")
        self.assertEqual(autoyes._detect_autoyes_prompt(SSH_AFTER_YNA, "claude")[0], "ssh-host-key")

    def test_a_new_prompt_below_an_old_row_gets_a_fresh_countdown(self):
        self.tick(CLAUDE_YNA, now=100.0).assert_not_called()
        self.tick(SSH_AFTER_YNA, now=105.0).assert_not_called()
        countdown = state.autoyes_countdowns[self.TARGET]
        self.assertEqual(countdown["prompt_type"], "ssh-host-key")
        self.assertEqual(countdown["deadline"], 110.0)

    def test_a_cancelled_old_prompt_does_not_suppress_the_new_one(self):
        self.tick(CLAUDE_YNA, now=100.0)
        state.autoyes_countdowns[self.TARGET]["cancelled"] = True
        self.tick(SSH_AFTER_YNA, now=101.0).assert_not_called()
        countdown = state.autoyes_countdowns[self.TARGET]
        self.assertFalse(countdown["cancelled"])
        self.assertEqual(countdown["prompt_type"], "ssh-host-key")

    def test_the_same_prompt_still_fires_at_its_deadline(self):
        self.tick(CLAUDE_YNA, now=100.0).assert_not_called()
        self.tick(CLAUDE_YNA + "  ✻ Vibing… (5s)", now=105.0).assert_called_once()


def _expected_identity():
    return ExpectedTargetIdentity(
        socket_path="/tmp/assist-test-tmux.sock",
        socket_device=1,
        socket_inode=2,
        server_pid=3,
        server_start_time="4",
        session_id="$1",
        window_id="@1",
        pane_id="%1",
        pane_pid=5,
        pane_start_time="6",
    )


class PromptAnswerRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(input_bp)
        self.client = app.test_client()
        self.expected = _expected_identity()
        patches = [
            patch("routes.input.expected_target_identity", return_value=self.expected),
            patch("routes.input.pane_awaits_secret", return_value=False),
            patch("routes.input.add_to_history"),
            patch("routes.input.declare_agent_command"),
            patch("routes.input.state.touch_activity"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, url, body, waiting=True):
        with patch(
            "routes.input.prompt_owner_waiting", return_value=waiting
        ) as guard, patch(
            "routes.input.generation_bound_delivery",
            return_value=DeliveryResult("delivered"),
        ) as deliver:
            response = self.client.post(url, json=body)
        return response, guard, deliver

    def secret_body(self, **extra):
        return dict(text=" pw with spaces ", enter=True, expand=False, secret=True,
                    target="del_build:0.0", **extra)

    def assertRefused(self, response, deliver):
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "prompt_gone")
        deliver.assert_not_called()

    def test_send_to_a_sudo_that_stopped_waiting_types_nothing(self):
        response, guard, deliver = self.post(
            "/type", self.secret_body(expect_prompt_pid=202), waiting=False
        )
        self.assertRefused(response, deliver)
        guard.assert_called_once_with("%1", 202)

    def test_send_to_a_waiting_sudo_types_the_secret_exactly(self):
        response, _guard, deliver = self.post("/type", self.secret_body(expect_prompt_pid=202))
        self.assertEqual(response.status_code, 200)
        deliver.assert_called_once_with(self.expected, text=" pw with spaces ", enter=True)

    def test_send_through_the_real_guard_refuses_a_wrong_reader(self):
        lone = "  101 Ss   -bash\n  202 S+   sudo true\n"
        pipeline = "  101 Ss   -bash\n  201 S+   sleep 30\n  202 S+   sudo -S true\n"
        cases = [
            (lone, True, 200),       # a lone tty-reading sudo
            (pipeline, False, 409),  # two waiters and -S
            (lone, False, 409),      # one waiter, but -S: inherited-pipe bypass
        ]
        for on_tty, reads_terminal, status in cases:
            with self.subTest(status=status, reads_terminal=reads_terminal), patch(
                "shared.tmux.subprocess.run", _host(on_tty=on_tty, echo="-echo")
            ), patch("shared.tmux._sudo_reads_terminal", return_value=reads_terminal), patch(
                "routes.input.generation_bound_delivery",
                return_value=DeliveryResult("delivered"),
            ) as deliver:
                response = self.client.post("/type", json=self.secret_body(expect_prompt_pid=202))
            self.assertEqual(response.status_code, status)
            self.assertEqual(deliver.call_count, 1 if status == 200 else 0)

    def test_a_type_without_a_named_prompt_is_unchanged(self):
        response, guard, deliver = self.post("/type", self.secret_body(), waiting=False)
        self.assertEqual(response.status_code, 200)
        guard.assert_not_called()
        deliver.assert_called_once()

    def test_no_popup_answer_guard_is_left_on_key_or_by_fingerprint(self):
        source = (ROOT / "routes/input.py").read_text()
        key_route = source[source.index("def _send_key_effect"):source.index("def _prompt_answer_is_stale")]
        self.assertNotIn("_prompt_answer_is_stale", key_route)
        self.assertNotIn("expect_prompt_fp", source)


class PopupSettingsTests(unittest.TestCase):
    def test_popups_auto_hide_after_five_seconds_by_default(self):
        self.assertEqual(DEFAULT_SETTINGS["ui"]["popup_autohide"], "on")
        self.assertEqual(DEFAULT_SETTINGS["ui"]["popup_seconds"], 5)

    def test_both_settings_are_in_the_settings_panel(self):
        settings_js = (ROOT / "js/settings.js").read_text()
        self.assertIn("key: 'popup_autohide'", settings_js)
        self.assertIn("key: 'popup_seconds'", settings_js)


def run_popup_js(body):
    """Execute prompt-popup.js against a stub DOM and return BODY's JSON line."""
    harness = r"""
const fs = require('fs');
const vm = require('vm');

class FakeClassList {
    constructor() { this.names = new Set(); }
    add(name) { this.names.add(name); }
    remove(name) { this.names.delete(name); }
    contains(name) { return this.names.has(name); }
}

class FakeElement {
    constructor(tag) {
        this.tagName = tag;
        this.children = [];
        this.parentNode = null;
        this.classList = new FakeClassList();
        this.style = {};
        this.attributes = {};
        this.listeners = {};
        this.textContent = '';
        this.title = '';
        this.tabIndex = 0;
    }
    set className(value) {
        this.classList = new FakeClassList();
        String(value).split(/\s+/).filter(Boolean).forEach(n => this.classList.add(n));
    }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    remove() {
        if (!this.parentNode) return;
        const siblings = this.parentNode.children;
        siblings.splice(siblings.indexOf(this), 1);
        this.parentNode = null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    focus() { focusCalls += 1; }
    fire(type, event) { for (const fn of this.listeners[type] || []) fn(event); }
    all(out) {
        out = out || [];
        out.push(this);
        for (const child of this.children) child.all(out);
        return out;
    }
    find(name) { return this.all().find(e => e.classList.contains(name)) || null; }
    button(label) { return this.all().find(e => e.tagName === 'button' && e.textContent === label) || null; }
    labels() { return this.find('prompt-popup-actions').children.map(b => b.textContent); }
}

globalThis.focusCalls = 0;
globalThis.timers = [];
globalThis.setTimeout = (fn, ms) => {
    const t = {fn, ms, cleared: false, ran: false};
    timers.push(t);
    return t;
};
globalThis.clearTimeout = t => { if (t) t.cleared = true; };
globalThis.fireTimers = ms => {
    for (const t of timers.slice()) {
        if (t.ms === ms && !t.cleared && !t.ran) { t.ran = true; t.fn(); }
    }
};
globalThis.liveDelays = () => timers.filter(t => !t.cleared && !t.ran).map(t => t.ms);
globalThis.requestAnimationFrame = fn => fn();
globalThis.document = {body: new FakeElement('body'), createElement: tag => new FakeElement(tag)};
globalThis.SETTINGS = {ui: {popup_autohide: 'on', popup_seconds: 5}};
globalThis._sessionPanes = [];
globalThis.shortName = name => name.replace(/^[-\w]+?_/, '');
globalThis.agentDisplayName = pane => pane.agent_name ? pane.agent_name.replace(/-agent$/, '') : null;
globalThis.selectCalls = [];
globalThis.selectTab = target => { selectCalls.push(target); };
globalThis.flashes = [];
globalThis.showFlash = (type, text) => { flashes.push([type, text]); };
globalThis.lastAction = 0;
globalThis.updateStatusTime = () => {};
globalThis.vaultMap = {sudo: 'test-secret'};
globalThis.vaultHas = handle => Object.prototype.hasOwnProperty.call(vaultMap, handle);
globalThis.vaultGet = handle => vaultHas(handle) ? vaultMap[handle] : undefined;
globalThis.fetchReply = {ok: true};
globalThis.fetchCalls = [];
globalThis.fetch = async (url, options) => {
    fetchCalls.push({url, body: JSON.parse(options.body)});
    return {json: async () => fetchReply};
};
globalThis.sudoPrompt = {target: 'del_build:0.0', session: 'del_build', pid: 202,
                         command: 'sudo apt install tmux'};
globalThis.question = overrides => Object.assign({
    target: 'del_api:0.0',
    session: 'del_api',
    detected: {id: 'confirm-yn', desc: 'Confirmation', notifyOnly: false, actions: [
        {label: 'Yes (y)', send: 'y', enter: true, color: 'green'},
        {label: 'No (n)', send: 'n', enter: true, color: 'red'},
    ]},
    prompt: {fp: 'fp1', summary: 'Delete 3 files?', autoyes: false},
}, overrides || {});
globalThis.popups = () => document.body.children[0].children;
globalThis.tick = () => new Promise(resolve => setImmediate(resolve));
globalThis.click = (popup, label) => popup.button(label).fire('click', {stopPropagation() {}});

vm.runInThisContext(fs.readFileSync('js/prompt-popup.js', 'utf8'), {filename: 'js/prompt-popup.js'});

(async () => {
BODY
})().catch(error => {
    console.error(error.stack || error);
    process.exitCode = 1;
});
""".replace("BODY", body)
    result = subprocess.run(
        ["node", "-e", harness], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class SudoPopupClientTests(unittest.TestCase):
    def test_one_popup_per_sudo_process_gone_after_five_seconds(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
_applySudoPrompts([sudoPrompt]);
const shown = popups().length;
const delays = liveDelays();
fireTimers(5000);
fireTimers(200);
const afterTimeout = popups().length;
_applySudoPrompts([sudoPrompt]);
const stillWaiting = popups().length;
_applySudoPrompts([Object.assign({}, sudoPrompt, {pid: 303})]);
const nextSudo = popups().length;
console.log(JSON.stringify({shown, delays, afterTimeout, stillWaiting, nextSudo}));
""")
        self.assertEqual(result["shown"], 1)
        self.assertEqual(result["delays"], [5000])
        self.assertEqual(result["afterTimeout"], 0)
        self.assertEqual(result["stillWaiting"], 0)
        self.assertEqual(result["nextSudo"], 1)

    def test_popup_leaves_as_soon_as_sudo_stops_waiting(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
_applySudoPrompts([]);
fireTimers(200);
console.log(JSON.stringify({left: popups().length}));
""")
        self.assertEqual(result["left"], 0)

    def test_it_says_a_password_is_requested_and_offers_send(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
const popup = popups()[0];
console.log(JSON.stringify({
    title: popup.find('prompt-popup-where').textContent,
    detail: popup.find('prompt-popup-detail').textContent,
    labels: popup.labels(),
}));
""")
        self.assertEqual(result["title"], "Sudo password requested · build")
        self.assertEqual(result["detail"], "apt install tmux")
        self.assertEqual(result["labels"], ["Send"])

    def test_send_types_the_vault_value_as_a_guarded_secret(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
click(popups()[0], 'Send');
await tick();
fireTimers(200);
console.log(JSON.stringify({fetchCalls, flashes, left: popups().length}));
""")
        self.assertEqual(result["fetchCalls"], [{
            "url": "/type",
            "body": {"text": "test-secret", "enter": True, "expand": False, "secret": True,
                     "target": "del_build:0.0", "expect_prompt_pid": 202},
        }])
        self.assertEqual(result["flashes"], [["sent", "Password sent"]])
        self.assertEqual(result["left"], 0)

    def test_without_a_stored_sudo_it_opens_the_tab_and_sends_nothing(self):
        result = run_popup_js(r"""
vaultMap = {};
_applySudoPrompts([sudoPrompt]);
const popup = popups()[0];
const labels = popup.labels();
click(popup, 'Open tab');
await tick();
console.log(JSON.stringify({labels, selectCalls, fetchCalls, flashes}));
""")
        self.assertEqual(result["labels"], ["Open tab"])
        self.assertEqual(result["selectCalls"], ["del_build:0.0"])
        self.assertEqual(result["fetchCalls"], [])
        self.assertEqual(result["flashes"], [])

    def test_a_refused_send_is_reported_not_claimed(self):
        result = run_popup_js(r"""
fetchReply = {ok: false, error: 'prompt_gone'};
_applySudoPrompts([sudoPrompt]);
click(popups()[0], 'Send');
await tick();
console.log(JSON.stringify({flashes}));
""")
        self.assertEqual(result["flashes"], [["error", "sudo is no longer waiting"]])

    def test_popups_never_take_focus(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
_applyQuestionPrompts([question()]);
let prevented = 0;
for (const popup of popups()) popup.fire('pointerdown', {preventDefault() { prevented += 1; }});
const tabIndexes = popups().flatMap(p => p.all().filter(e => e.tagName === 'button').map(b => b.tabIndex));
click(popups()[0], '×');
await tick();
console.log(JSON.stringify({prevented, focusCalls, tabIndexes, selectCalls, fetchCalls}));
""")
        self.assertEqual(result["prevented"], 2)
        self.assertEqual(result["focusCalls"], 0)
        self.assertTrue(result["tabIndexes"])
        self.assertEqual(set(result["tabIndexes"]), {-1})
        self.assertEqual(result["selectCalls"], [])
        self.assertEqual(result["fetchCalls"], [])
        self.assertNotIn(".focus(", (ROOT / "js/prompt-popup.js").read_text())


class QuestionPopupClientTests(unittest.TestCase):
    def test_a_question_says_it_is_pending_and_only_opens_the_tab(self):
        result = run_popup_js(r"""
_applyQuestionPrompts([question()]);
const popup = popups()[0];
const shown = {
    title: popup.find('prompt-popup-where').textContent,
    detail: popup.find('prompt-popup-detail').textContent,
    labels: popup.labels(),
};
click(popup, 'Open tab');
await tick();
console.log(JSON.stringify(Object.assign(shown, {selectCalls, fetchCalls})));
""")
        self.assertEqual(result["title"], "Question pending · api")
        self.assertEqual(result["detail"], "Delete 3 files?")
        self.assertEqual(result["labels"], ["Open tab"])
        self.assertEqual(result["selectCalls"], ["del_api:0.0"])
        self.assertEqual(result["fetchCalls"], [])

    def test_every_kind_of_prompt_gets_the_same_single_button(self):
        result = run_popup_js(r"""
const kinds = [
    {id: 'numbered-options', desc: 'Select option', actions: [
        {label: '1. Yes', optNum: '1', isOption: true, color: 'cyan'},
        {label: '2. No', optNum: '2', isOption: true, color: 'cyan'}]},
    {id: 'opencode-permission', desc: 'OpenCode permission', actions: [
        {label: 'Allow always', keys: ['Right'], enter: true, color: 'cyan'}]},
    {id: 'numbered-options', desc: 'Select option', notifyOnly: true, actions: []},
];
const out = [];
kinds.forEach((detected, i) => {
    _applyQuestionPrompts([question({target: 'del_api:0.' + i, detected,
                                     prompt: {fp: 'fp' + i, summary: null, autoyes: false}})]);
    const popup = popups()[popups().length - 1];
    out.push([popup.labels(), popup.find('prompt-popup-detail').textContent]);
});
console.log(JSON.stringify({out, fetchCalls}));
""")
        self.assertEqual(result["out"], [
            [["Open tab"], "Select option"],
            [["Open tab"], "OpenCode permission"],
            [["Open tab"], "Select option"],
        ])
        self.assertEqual(result["fetchCalls"], [])
        source = (ROOT / "js/prompt-popup.js").read_text()
        self.assertNotIn("'/key'", source)
        self.assertNotIn("expect_prompt_fp", source)

    def test_auto_yes_prompts_are_skipped(self):
        result = run_popup_js(r"""
_applyQuestionPrompts([question({prompt: {fp: 'fp1', summary: 'x', autoyes: true}})]);
console.log(JSON.stringify({shown: popups().length}));
""")
        self.assertEqual(result["shown"], 0)

    def test_one_popup_per_prompt_and_a_new_prompt_replaces_it(self):
        result = run_popup_js(r"""
_applyQuestionPrompts([question()]);
_applyQuestionPrompts([question()]);
const once = popups().length;
_applyQuestionPrompts([question({prompt: {fp: 'fp2', summary: 'Other prompt', autoyes: false}})]);
fireTimers(200);
console.log(JSON.stringify({
    once,
    replaced: popups().length,
    detail: popups()[0].find('prompt-popup-detail').textContent,
}));
""")
        self.assertEqual(result["once"], 1)
        self.assertEqual(result["replaced"], 1)
        self.assertEqual(result["detail"], "Other prompt")

    def test_auto_hide_off_keeps_popups_until_closed_or_gone(self):
        result = run_popup_js(r"""
SETTINGS.ui.popup_autohide = 'off';
_applyQuestionPrompts([question()]);
_applySudoPrompts([sudoPrompt]);
const delays = liveDelays();
const hasTimerBar = popups().some(p => !!p.find('prompt-popup-timer'));
const shown = popups().length;
click(popups()[0], '×');
fireTimers(200);
_applyQuestionPrompts([question()]);
const afterClose = popups().length;
_applySudoPrompts([]);
fireTimers(200);
console.log(JSON.stringify({delays, hasTimerBar, shown, afterClose, afterGone: popups().length, fetchCalls}));
""")
        self.assertEqual(result["delays"], [])
        self.assertFalse(result["hasTimerBar"])
        self.assertEqual(result["shown"], 2)
        self.assertEqual(result["afterClose"], 1)
        self.assertEqual(result["afterGone"], 0)
        self.assertEqual(result["fetchCalls"], [])

    def test_prompts_beyond_three_wait_for_a_slot_instead_of_being_lost(self):
        result = run_popup_js(r"""
SETTINGS.ui.popup_autohide = 'off';
const four = [0, 1, 2, 3].map(i => question({target: 'del_api:0.' + i,
                                             prompt: {fp: 'fp' + i, summary: 'Q' + i, autoyes: false}}));
_applyQuestionPrompts(four);
const first = popups().map(p => p.find('prompt-popup-detail').textContent);
click(popups()[0], '×');
fireTimers(200);
_applyQuestionPrompts(four);
const after = popups().map(p => p.find('prompt-popup-detail').textContent);
console.log(JSON.stringify({first, after}));
""")
        self.assertEqual(result["first"], ["Q0", "Q1", "Q2"])
        self.assertEqual(result["after"], ["Q1", "Q2", "Q3"])

    def test_a_waiting_sudo_is_not_starved_by_newer_questions(self):
        result = run_popup_js(r"""
const questions = round => [0, 1, 2].map(i => question({target: 'del_api:0.' + i,
    prompt: {fp: 'r' + round + 'q' + i, summary: 'round ' + round, autoyes: false}}));
const kinds = [];
for (let round = 0; round < 4; round++) {
    _applyQuestionPrompts(questions(round));
    _applySudoPrompts([sudoPrompt]);
    fireTimers(200);
    kinds.push(popups().map(p => p.classList.contains('sudo') ? 'sudo' : 'question'));
}
console.log(JSON.stringify({kinds}));
""")
        self.assertEqual(result["kinds"][0], ["question", "question", "question"])
        for later in result["kinds"][1:]:
            self.assertIn("sudo", later)

    def test_a_prompt_seen_in_its_tab_does_not_pop_again_on_leaving(self):
        result = run_popup_js(r"""
const inView = () => Object.assign(question(), {inView: true});
_applyQuestionPrompts([question()]);
_applyQuestionPrompts([inView()]);
fireTimers(200);
const openingTabClosesCard = popups().length;
_applyQuestionPrompts([question()]);
const afterLeaving = popups().length;
_applyQuestionPrompts([question({target: 'del_other:0.0', prompt: {fp: 'x1', summary: 'Other', autoyes: false}})]);
console.log(JSON.stringify({openingTabClosesCard, afterLeaving, otherTabPops: popups().length}));
""")
        self.assertEqual(result["openingTabClosesCard"], 0)
        self.assertEqual(result["afterLeaving"], 0)
        self.assertEqual(result["otherTabPops"], 1)

    def test_popup_seconds_sets_how_long_it_stays(self):
        result = run_popup_js(r"""
SETTINGS.ui.popup_seconds = 12;
_applyQuestionPrompts([question()]);
console.log(JSON.stringify({
    delays: liveDelays(),
    bar: popups()[0].find('prompt-popup-timer').style.animationDuration,
}));
""")
        self.assertEqual(result["delays"], [12000])
        self.assertEqual(result["bar"], "12000ms")

    def test_the_page_loads_the_popups_and_the_poll_feeds_them(self):
        html = (ROOT / "index.html").read_text()
        self.assertIn('src="/js/prompt-popup.js', html)
        self.assertNotIn("sudo-prompt.js", html)
        app = (ROOT / "js/app.js").read_text()
        self.assertIn("_applySudoPrompts(data.sudo_prompts", app)
        self.assertIn("_applyQuestionPrompts(questions)", app)
        self.assertIn("inView: true", app)
        poll = (ROOT / "routes/poll.py").read_text()
        self.assertIn('result["sudo_prompts"] = sudo_prompts', poll)
        self.assertIn('"prompt": prompt,', poll)
        self.assertIn("#{pane_tty}", poll)


class SudoArgvReaderTests(unittest.TestCase):
    """_argv_reads_terminal: only -S / --stdin make sudo read stdin, not the tty."""

    def reads(self, argv):
        return tmux._argv_reads_terminal(argv.split())

    def test_a_plain_sudo_reads_the_terminal(self):
        for argv in ("sudo true", "sudo -k true", "sudo -u root true", "sudo -kn true"):
            with self.subTest(argv=argv):
                self.assertTrue(self.reads(argv))

    def test_stdin_flags_read_stdin(self):
        for argv in ("sudo -S true", "sudo -kS true", "sudo -Sk true",
                     "sudo --stdin true", "sudo --std true", "sudo -n -S true"):
            with self.subTest(argv=argv):
                self.assertFalse(self.reads(argv))

    def test_an_s_that_is_an_option_value_is_not_the_stdin_flag(self):
        # -p takes a value, so the S in -pS is that value, and -u S / --prompt S too.
        for argv in ("sudo -pS true", "sudo -u S true", "sudo -uS true", "sudo --prompt S true"):
            with self.subTest(argv=argv):
                self.assertTrue(self.reads(argv))

    def test_a_command_called_S_after_the_options_is_not_the_flag(self):
        # Everything after the first operand (or --) is the command, not options.
        self.assertTrue(self.reads("sudo -- -S"))
        self.assertTrue(self.reads("sudo /usr/local/bin/S --stdin"))


@unittest.skipUnless(
    __import__("shutil").which("tmux") and __import__("os").path.isdir("/proc"),
    "needs tmux and /proc",
)
class RealTerminalOwnershipTests(unittest.TestCase):
    """_reads_pane_tty and _tty_echo_off against a live pane, no sudo needed."""

    def setUp(self):
        import os, shutil, subprocess, tempfile, time
        self.time = time
        self.tmpdir = tempfile.mkdtemp(prefix="assist-tty-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        env = patch.dict(os.environ, {"TMUX_TMPDIR": self.tmpdir})
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("TMUX", None)
        d = os.path.join(self.tmpdir, f"tmux-{os.getuid()}")
        os.makedirs(d, mode=0o700, exist_ok=True)
        self.socket = os.path.realpath(os.path.join(d, "default"))
        self.assertNotEqual(self.socket, os.path.realpath(f"/tmp/tmux-{os.getuid()}/default"))
        self.addCleanup(subprocess.run, ["tmux", "-S", self.socket, "kill-server"],
                        check=False, capture_output=True, timeout=5)

    def tmux(self, *a):
        import subprocess
        return subprocess.run(["tmux", "-S", self.socket, *a], check=True,
                              capture_output=True, text=True, timeout=5).stdout.strip()

    def pane(self, name, command=None):
        args = ["new-session", "-d", "-s", name, "-x", "80", "-y", "10"]
        self.tmux(*(args + [command] if command else args))
        tty = self.tmux("display-message", "-p", "-t", f"={name}:", "#{pane_tty}")
        pid = self.tmux("display-message", "-p", "-t", f"={name}:", "#{pane_pid}")
        return tty, int(pid)

    def foreground_pids(self, tty):
        import subprocess
        out = subprocess.run(["ps", "-t", tty, "-o", "pid=,stat=,comm="],
                             capture_output=True, text=True, timeout=5).stdout
        return {c: int(p) for p, s, c in (r.split(None, 2) for r in out.splitlines() if r.split()) if "+" in s}

    def wait_for(self, tty, comm):
        deadline = self.time.monotonic() + 5
        while self.time.monotonic() < deadline:
            pids = self.foreground_pids(tty)
            if comm in pids:
                return pids[comm]
            self.time.sleep(0.05)
        self.fail(f"{comm} never ran on {tty}")

    def test_a_pipeline_shows_two_foreground_waiters(self):
        # The real ps-level signal _waiting_sudos counts: `x | y` is two.
        tty, _pid = self.pane("pipe", "sleep 60 | cat")
        self.wait_for(tty, "cat")
        self.assertEqual(len(tmux._tty_prompt_waiters(tty)), 2)

    def test_a_single_command_shows_one_foreground_waiter(self):
        tty, _pid = self.pane("one", "sleep 60")
        self.assertEqual(len(tmux._tty_prompt_waiters(tty)), 1)

    def test_sudo_reads_terminal_reads_the_live_proc_cmdline(self):
        import subprocess
        sleeper = "import os; os.execv('/usr/bin/python3', {argv})"
        plain = subprocess.Popen(
            ["python3", "-c", sleeper.format(argv="['x', '-c', 'import time; time.sleep(30)']")]
        )
        stdin = subprocess.Popen(
            ["python3", "-c", sleeper.format(argv="['x', '-S', '-c', 'import time; time.sleep(30)']")]
        )
        self.addCleanup(plain.kill); self.addCleanup(stdin.kill)
        self.time.sleep(0.4)  # let the execv land
        self.assertTrue(tmux._sudo_reads_terminal(plain.pid))
        self.assertFalse(tmux._sudo_reads_terminal(stdin.pid))
        self.assertFalse(tmux._sudo_reads_terminal(999999))

    def test_echo_state_is_read_from_the_live_terminal(self):
        tty, _pid = self.pane("echo")  # a shell, so send-keys reaches it
        self.assertFalse(tmux._tty_echo_off(tty))
        self.tmux("send-keys", "-t", "=echo:", "stty -echo", "Enter")
        deadline = self.time.monotonic() + 5
        while self.time.monotonic() < deadline and not tmux._tty_echo_off(tty):
            self.time.sleep(0.05)
        self.assertTrue(tmux._tty_echo_off(tty))


if __name__ == "__main__":
    unittest.main()
