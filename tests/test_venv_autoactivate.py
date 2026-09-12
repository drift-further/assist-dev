"""Auto-activation of a project's virtualenv in panes Assist creates.

Activation used to be open-coded at every pane-creating site. The
public-readiness restructure moved two of those sites into park effect
functions and dropped the `source .venv/bin/activate` send from both on the
way, leaving the copy in the saved-command split alive -- so a fresh terminal
stopped entering the venv while `/terminal/projects` kept badging it and the
launch response kept reporting `venv`. Nothing failed; the pane was just
outside the venv.

So these fixtures run in BOTH directions -- must-activate and must-not-activate
-- and one of them is a source-level guard that every creation surface routes
through the single shared helper, because a detector or a send that exists in
only two of three call sites is exactly how this broke.
"""

from __future__ import annotations

import inspect
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import shared.state as state
from shared import tmux as tmux_shared
from routes import commands, settings, terminal

REPO_ROOT = Path(__file__).resolve().parent.parent


def completed(code=0, stdout=""):
    return subprocess.CompletedProcess([], code, stdout, "")


class _FakeIdentity:
    pane_id = "%510"

    def as_dict(self):
        return {"pane_id": self.pane_id}


def _make_venv(project_path, name=".venv"):
    """Create the one file detect_venv() looks for."""
    activate = project_path / name / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text("# fixture\n")
    return activate


class _SendRecorder:
    """One ordered log of every send, whichever module issued it.

    `activate_venv` sends from shared/tmux.py while the init command sends
    from routes/terminal.py's own from-imported names, so ordering between
    the two is only visible if both bindings share a recorder.
    """

    def __init__(self):
        self.log = []

    def text(self, target, text):
        self.log.append(("text", target, text))
        return True

    def keys(self, target, *keys):
        self.log.append(("keys", target, *keys))
        return True

    @property
    def texts(self):
        return [entry[2] for entry in self.log if entry[0] == "text"]


def _switch(value):
    """Patch only server.venv_auto_activate; every other key stays real."""
    real = state.get_setting

    def fake(*keys):
        if keys == ("server", "venv_auto_activate"):
            return value
        return real(*keys)

    return patch.object(tmux_shared, "get_setting", side_effect=fake)


class ActivateVenvHelperTests(unittest.TestCase):
    """The shared helper itself: what it sends, and when it stays silent."""

    def test_shipped_default_is_on(self):
        # The posture that regressed. Activation was unconditional before the
        # switch existed, so the switch must not be what turns it off.
        self.assertEqual(
            state.DEFAULT_SETTINGS["server"]["venv_auto_activate"], "on"
        )

    def test_sends_absolute_quoted_source_then_enter(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            activate = _make_venv(project)
            recorder = _SendRecorder()
            with _switch("on"), patch.object(
                tmux_shared, "tmux_send_text", side_effect=recorder.text
            ), patch.object(
                tmux_shared, "tmux_send_keys", side_effect=recorder.keys
            ), patch.object(tmux_shared.time, "sleep"):
                result = tmux_shared.activate_venv("s:0.0", project)

            self.assertEqual(result, ".venv")
            self.assertEqual(
                recorder.log,
                [
                    ("text", "s:0.0", f"source {activate}"),
                    ("keys", "s:0.0", "Enter"),
                ],
            )

    def test_finds_bare_venv_and_env_directories(self):
        for name in ("venv", "env"):
            with self.subTest(venv=name), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp) / "project"
                project.mkdir()
                _make_venv(project, name)
                recorder = _SendRecorder()
                with _switch("on"), patch.object(
                    tmux_shared, "tmux_send_text", side_effect=recorder.text
                ), patch.object(
                    tmux_shared, "tmux_send_keys", side_effect=recorder.keys
                ), patch.object(tmux_shared.time, "sleep"):
                    self.assertEqual(
                        tmux_shared.activate_venv("s:0.0", project), name
                    )
                self.assertTrue(recorder.texts)

    def test_quotes_a_project_path_containing_a_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "my project"
            project.mkdir()
            _make_venv(project)
            recorder = _SendRecorder()
            with _switch("on"), patch.object(
                tmux_shared, "tmux_send_text", side_effect=recorder.text
            ), patch.object(
                tmux_shared, "tmux_send_keys", side_effect=recorder.keys
            ), patch.object(tmux_shared.time, "sleep"):
                tmux_shared.activate_venv("s:0.0", project)

            # Unquoted, the shell splits the path at the space and sources
            # something else entirely. Assert on the parse, not on the quoting
            # style: what matters is the words the shell will actually see.
            sent = recorder.texts[0]
            self.assertEqual(
                shlex.split(sent),
                ["source", os.fspath(project / ".venv" / "bin" / "activate")],
            )

    def test_silent_when_the_switch_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            _make_venv(project)
            recorder = _SendRecorder()
            with _switch("off"), patch.object(
                tmux_shared, "tmux_send_text", side_effect=recorder.text
            ), patch.object(
                tmux_shared, "tmux_send_keys", side_effect=recorder.keys
            ), patch.object(tmux_shared.time, "sleep"):
                self.assertIsNone(tmux_shared.activate_venv("s:0.0", project))
            self.assertEqual(recorder.log, [])

    def test_silent_when_the_project_has_no_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            recorder = _SendRecorder()
            with _switch("on"), patch.object(
                tmux_shared, "tmux_send_text", side_effect=recorder.text
            ), patch.object(
                tmux_shared, "tmux_send_keys", side_effect=recorder.keys
            ), patch.object(tmux_shared.time, "sleep"):
                self.assertIsNone(tmux_shared.activate_venv("s:0.0", project))
            self.assertEqual(recorder.log, [])

    def test_detection_is_independent_of_the_switch(self):
        # The venv badge and the `venv` response field must keep telling the
        # truth about the project while activation is off.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            _make_venv(project)
            with _switch("off"):
                self.assertEqual(tmux_shared.detect_venv(project), ".venv")


class LaunchSurfaceActivationTests(unittest.TestCase):
    """The two surfaces that lost the send: /terminal/launch and /duplicate."""

    def setUp(self):
        self._tmux_target = state.tmux_target
        self.app = Flask("venv-autoactivate")
        self.app.register_blueprint(terminal.terminal_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        state.tmux_target = self._tmux_target

    def _launch(self, switch, init_cmd="", with_venv=True):
        recorder = _SendRecorder()
        real_get_setting = state.get_setting

        def configured(section, key):
            if (section, key) == ("server", "session_init_cmd"):
                return init_cmd
            return real_get_setting(section, key)

        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            project = projects / "project"
            project.mkdir()
            if with_venv:
                _make_venv(project)

            def tmux_result(command, **_kwargs):
                if command[1] == "has-session":
                    return completed(1)
                if command[1] == "display-message":
                    return completed(0, os.fspath(project) + "\n")
                return completed()

            with _switch(switch), patch.object(
                state, "PROJECTS_DIR", projects
            ), patch.object(
                state, "get_setting", side_effect=configured
            ), patch.object(
                terminal.subprocess, "run", side_effect=tmux_result
            ), patch.object(
                terminal,
                "create_tmux_session",
                return_value=tmux_shared.TmuxCreationResult(
                    "created", identity=_FakeIdentity()
                ),
            ), patch.object(
                terminal, "tmux_send_text", side_effect=recorder.text
            ), patch.object(
                terminal, "tmux_send_keys", side_effect=recorder.keys
            ), patch.object(
                tmux_shared, "tmux_send_text", side_effect=recorder.text
            ), patch.object(
                tmux_shared, "tmux_send_keys", side_effect=recorder.keys
            ), patch.object(terminal.time, "sleep"), patch.object(
                tmux_shared.time, "sleep"
            ):
                launch = self.client.post(
                    "/terminal/launch", json={"project": "project"}
                )
                duplicate = self.client.post(
                    "/terminal/duplicate",
                    json={"session": "project", "name": "project-copy"},
                )
            expected = os.fspath(project / ".venv" / "bin" / "activate")
        return launch, duplicate, recorder, expected

    def test_launch_activates_and_reports_it(self):
        launch, _dup, recorder, expected = self._launch("on")
        self.assertEqual(launch.status_code, 200, launch.get_data())
        body = launch.get_json()
        self.assertEqual(body["venv"], ".venv")
        self.assertTrue(body["venv_activated"])
        self.assertIn(f"source {expected}", recorder.texts)

    def test_duplicate_activates_and_reports_it(self):
        _launch, duplicate, recorder, expected = self._launch("on")
        self.assertEqual(duplicate.status_code, 200, duplicate.get_data())
        body = duplicate.get_json()
        self.assertEqual(body["venv"], ".venv")
        self.assertTrue(body["venv_activated"])
        self.assertEqual(
            recorder.texts.count(f"source {expected}"),
            2,
            "launch and duplicate must each activate, not one of the two",
        )

    def test_activation_precedes_the_init_command(self):
        # Ordering is the point: an init command that runs before activation
        # runs against the system interpreter.
        launch, _dup, recorder, expected = self._launch(
            "on", init_cmd="configured-launcher --go"
        )
        self.assertEqual(launch.status_code, 200, launch.get_data())
        texts = recorder.texts
        self.assertLess(
            texts.index(f"source {expected}"),
            texts.index("configured-launcher --go"),
        )

    def test_switch_off_activates_nothing_on_either_surface(self):
        launch, duplicate, recorder, expected = self._launch("off")
        self.assertEqual(launch.status_code, 200, launch.get_data())
        self.assertEqual(duplicate.status_code, 200, duplicate.get_data())
        self.assertNotIn(f"source {expected}", recorder.texts)
        self.assertFalse(launch.get_json()["venv_activated"])
        self.assertFalse(duplicate.get_json()["venv_activated"])
        # Detection still reports honestly with the switch off.
        self.assertEqual(launch.get_json()["venv"], ".venv")

    def test_no_venv_activates_nothing_with_the_switch_on(self):
        launch, duplicate, recorder, _expected = self._launch(
            "on", with_venv=False
        )
        self.assertEqual(launch.status_code, 200, launch.get_data())
        self.assertEqual(duplicate.status_code, 200, duplicate.get_data())
        self.assertFalse(
            [text for text in recorder.texts if "bin/activate" in text]
        )
        self.assertIsNone(launch.get_json()["venv"])
        self.assertFalse(launch.get_json()["venv_activated"])


class SingleActivationPathTests(unittest.TestCase):
    """Structural guards: one helper, reachable from the phone."""

    _SURFACES = (
        (terminal._terminal_launch_effect, "fresh_terminal"),
        (terminal._terminal_duplicate_effect, "duplicate"),
        (commands._run_command_effect, "saved_command_split"),
    )

    def test_every_project_pane_surface_calls_the_shared_helper(self):
        for function, surface in self._SURFACES:
            with self.subTest(surface=surface):
                self.assertIn("activate_venv(", inspect.getsource(function))

    def test_no_route_open_codes_the_activation_send(self):
        # routes/git.py is the one legitimate exception: /api/venv/create
        # activates the venv it just built, which is the user asking for it
        # directly rather than the launch-time switch.
        offenders = []
        for path in sorted((REPO_ROOT / "routes").glob("*.py")):
            if path.name == "git.py":
                continue
            if "bin/activate" in path.read_text():
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_the_switch_survives_the_settings_patch_filter(self):
        filtered, rejected = settings._filter_patch(
            {"server": {"venv_auto_activate": "off"}},
            state.DEFAULT_SETTINGS,
            blocked=settings._API_BLOCKED_KEYS,
        )
        self.assertEqual(filtered, {"server": {"venv_auto_activate": "off"}})
        self.assertEqual(rejected, [])

    def test_the_settings_panel_renders_the_switch(self):
        # A server setting with no row in the panel cannot be changed from the
        # phone, which is the only place this tool is used.
        panel = (REPO_ROOT / "js" / "settings.js").read_text()
        self.assertIn("venv_auto_activate", panel)
        self.assertIn("Auto-Activate venv", panel)


if __name__ == "__main__":
    unittest.main()
