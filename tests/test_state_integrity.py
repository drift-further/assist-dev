"""State files and settings values that cannot take the server down or be lost.

- A PATCHed setting must match the type and range of its default. Before this,
  `{"limits":{"max_upload_mb":null}}` was saved and the next start raised on
  import, and a string project delay was stored and later rendered as HTML.
- A saved value that is wrong falls back to its default per key at load; the
  valid keys beside it survive.
- A JSON state file that does not parse is moved aside as `.corrupt-<ts>` and
  logged, so the next save cannot silently destroy what was in it.
- `/type` reports success once the text is delivered, even when history
  bookkeeping fails afterwards, so the client does not resend it.

Both directions throughout: bad input is refused, ordinary input still lands.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from routes import settings as settings_routes
from routes.input import input_bp
from shared import state, utils
from shared.tmux import DeliveryResult, ExpectedTargetIdentity


def _identity():
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


class _TempState(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        saved_settings = copy.deepcopy(state._settings)
        saved_projects = copy.deepcopy(state._project_settings)

        def restore():
            state._settings = saved_settings
            state._project_settings = saved_projects
            state._apply_settings()

        self.addCleanup(restore)
        for name, attr in (("settings.json", "SETTINGS_FILE"),
                           ("project_settings.json", "PROJECT_SETTINGS_FILE")):
            patcher = mock.patch.object(state, attr, self.dir / name)
            patcher.start()
            self.addCleanup(patcher.stop)


class SettingsPatchValidationTests(_TempState):
    def setUp(self):
        super().setUp()
        state.load_settings()
        state.load_project_settings()
        app = Flask(__name__)
        app.register_blueprint(settings_routes.settings_bp)
        self.client = app.test_client()

    def _patch(self, body):
        return self.client.patch("/api/settings", json=body)

    def _project(self, body):
        return self.client.patch("/api/project-settings/demo", json=body)

    def test_bad_values_are_rejected_and_not_saved(self):
        cases = [
            ({"limits": {"max_upload_mb": None}}, "limits.max_upload_mb"),
            ({"terminal": {"idle_threshold_sec": "x"}}, "terminal.idle_threshold_sec"),
            ({"autoyes": {"detection_depth": "x"}}, "autoyes.detection_depth"),
            ({"terminal": {"font_size": True}}, "terminal.font_size"),
            ({"terminal": {"font_size": 500}}, "terminal.font_size"),
            ({"limits": {"max_history": 100.5}}, "limits.max_history"),
            ({"ui": {"popup_autohide": "maybe"}}, "ui.popup_autohide"),
            ({"server": {"claude_mode": "rm"}}, "server.claude_mode"),
            ({"studio": {"web_base": "javascript:alert(1)"}}, "studio.web_base"),
            ({"studio": {"web_base": "data:text/html,x"}}, "studio.web_base"),
            ({"studio": {"web_base": "https://"}}, "studio.web_base"),
            ({"studio": {"api_base": "javascript:alert(1)"}}, "studio.api_base"),
        ]
        before = state.get_settings()
        for body, path in cases:
            with self.subTest(path=path, body=body):
                resp = self._patch(body)
                self.assertEqual(resp.status_code, 400)
                self.assertIn(path, resp.get_json()["rejected"])
        self.assertEqual(state.get_settings(), before)

    def test_good_values_still_save(self):
        cases = [
            ({"terminal": {"font_size": 14}}, ("terminal", "font_size"), 14),
            ({"autoyes": {"default_delay": 0.5}}, ("autoyes", "default_delay"), 0.5),
            ({"limits": {"max_history": 3000.0}}, ("limits", "max_history"), 3000),
            ({"ui": {"popup_autohide": "off"}}, ("ui", "popup_autohide"), "off"),
            ({"studio": {"web_base": "https://studio.example"}},
             ("studio", "web_base"), "https://studio.example"),
            ({"studio": {"web_base": ""}}, ("studio", "web_base"), ""),
            ({"server": {"projects_dir": "/srv/p"}}, ("server", "projects_dir"), "/srv/p"),
        ]
        for body, keys, want in cases:
            with self.subTest(body=body):
                resp = self._patch(body)
                self.assertEqual(resp.status_code, 200, resp.get_json())
                self.assertEqual(state.get_setting(*keys), want)
                self.assertIs(type(state.get_setting(*keys)), type(want))

    def test_mixed_patch_keeps_the_good_key(self):
        resp = self._patch({"terminal": {"font_size": 15, "default_cols": "wide"}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["rejected"], ["terminal.default_cols"])
        self.assertEqual(state.get_setting("terminal", "font_size"), 15)

    def test_project_delay_markup_is_refused(self):
        resp = self._project({"autoyes": {"delay": "<img src=x onerror=alert(1)>"}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(state.get_project_setting("demo", "autoyes", "delay"), 5)

    def test_project_bad_types_are_refused(self):
        for body in ({"autoyes": {"enabled_default": "yes"}},
                     {"triggers": {"done_signals": "done"}},
                     {"triggers": {"done_signals": [1, {}]}},
                     {"automate": {"timeout": -1}}):
            with self.subTest(body=body):
                self.assertEqual(self._project(body).status_code, 400)

    def test_project_good_values_still_save(self):
        resp = self._project({"triggers": {"done_signals": ["DONE"], "done_idle_sec": 60}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            state.get_project_setting("demo", "triggers", "done_signals"), ["DONE"]
        )
        resp = self._project({"automate": {"stop_after": "18:30"}})
        self.assertEqual(resp.status_code, 200)


class LoadFallbackTests(_TempState):
    def test_bad_saved_values_fall_back_per_key(self):
        state.SETTINGS_FILE.write_text(json.dumps({
            "limits": {"max_upload_mb": None, "max_history": 300},
            "terminal": {"font_size": 14, "idle_threshold_sec": "x"},
            "autoyes": "x",
        }))
        with self.assertLogs("shared.state", "WARNING"):
            state.load_settings()
        self.assertEqual(state.get_setting("limits", "max_upload_mb"), 2048)
        self.assertEqual(state.get_setting("limits", "max_history"), 300)
        self.assertEqual(state.get_setting("terminal", "font_size"), 14)
        self.assertEqual(state.get_setting("terminal", "idle_threshold_sec"), 300)
        self.assertEqual(state.get_setting("autoyes", "detection_depth"), 8)
        self.assertEqual(state.MAX_UPLOAD_SIZE, 2048 * 1024 * 1024)

    def test_bad_project_values_fall_back_and_opt_outs_survive(self):
        state.PROJECT_SETTINGS_FILE.write_text(json.dumps({
            "p": {"autoyes": {"delay": "<img>", "global_opt_out": True}},
            "q": "not a dict",
        }))
        with self.assertLogs("shared.state", "WARNING"):
            state.load_project_settings()
        self.assertEqual(state.get_project_setting("p", "autoyes", "delay"), 5)
        self.assertIs(state.get_project_setting("p", "autoyes", "global_opt_out"), True)
        self.assertEqual(state.get_project_setting("q", "autoyes", "delay"), 5)

    def test_valid_file_loads_untouched(self):
        state.SETTINGS_FILE.write_text(json.dumps({"terminal": {"font_size": 16}}))
        state.load_settings()
        self.assertEqual(state.get_setting("terminal", "font_size"), 16)
        self.assertEqual(list(self.dir.glob("*.corrupt-*")), [])


class CorruptFileTests(_TempState):
    def _corrupt_copies(self, name):
        return list(self.dir.glob(name + ".corrupt-*"))

    def test_unparseable_settings_are_moved_aside_not_overwritten(self):
        original = '{"studio": {"api_token": "keep-me"'  # truncated
        state.SETTINGS_FILE.write_text(original)
        with self.assertLogs("shared.state", "ERROR") as logs:
            state.load_settings()
        self.assertIn("settings.json", "\n".join(logs.output))
        copies = self._corrupt_copies("settings.json")
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), original)
        self.assertEqual(state.get_setting("studio", "api_token"), "")
        # The next save writes a fresh file; the moved copy is untouched.
        state.patch_settings({"terminal": {"font_size": 12}})
        self.assertEqual(copies[0].read_text(), original)
        self.assertTrue(state.SETTINGS_FILE.exists())

    def test_wrong_top_level_shape_counts_as_corrupt(self):
        state.PROJECT_SETTINGS_FILE.write_text("[1, 2]")
        with self.assertLogs("shared.state", "ERROR"):
            state.load_project_settings()
        self.assertEqual(len(self._corrupt_copies("project_settings.json")), 1)

    def test_missing_file_is_not_an_error(self):
        state.load_settings()
        self.assertEqual(self._corrupt_copies("settings.json"), [])

    def test_history_file_is_moved_aside(self):
        path = self.dir / "history.json"
        path.write_text("[{")
        with self.assertLogs("shared.state", "ERROR"):
            self.assertEqual(utils.load_json(path, default=[]), [])
        self.assertEqual(len(self._corrupt_copies("history.json")), 1)
        self.assertFalse(path.exists())

    def test_tab_state_and_drafts_are_moved_aside(self):
        from shared import drafts, tab_state

        for module, attr, loader in ((tab_state, "TAB_STATE_FILE", "load_tab_state"),
                                     (drafts, "DRAFTS_FILE", "load_drafts")):
            with self.subTest(module=module.__name__):
                path = self.dir / (attr.lower() + ".json")
                path.write_text("{nope")
                saved = copy.deepcopy(getattr(module, "_" + loader.split("_", 1)[1]))
                with mock.patch.object(module, attr, path), \
                        self.assertLogs("shared.state", "ERROR"):
                    getattr(module, loader)()
                setattr(module, "_" + loader.split("_", 1)[1], saved)
                self.assertEqual(len(self._corrupt_copies(path.name)), 1)


class TypeBookkeepingTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(input_bp)
        self.client = app.test_client()

    def _type(self, delivery, history_error=None):
        with mock.patch("routes.input.expected_target_identity", return_value=_identity()), \
                mock.patch("routes.input.generation_bound_delivery", return_value=delivery), \
                mock.patch("routes.input.pane_awaits_secret", return_value=False), \
                mock.patch("routes.input.declare_agent_command"), \
                mock.patch("routes.input.state.touch_activity"), \
                mock.patch("routes.input.add_to_history", side_effect=history_error) as hist:
            resp = self.client.post(
                "/type", json={"text": "hello world", "enter": True, "target": "t:0.0"}
            )
        return resp, hist

    def test_history_failure_after_delivery_still_reports_ok(self):
        with self.assertLogs("routes.input", "ERROR") as logs:
            resp, hist = self._type(
                DeliveryResult("delivered"), ValueError("bad entry: hello world")
            )
        # Nothing typed reaches the log, even via the exception's message.
        self.assertNotIn("hello world", "\n".join(logs.output))
        self.assertTrue(all(r.exc_info is None for r in logs.records))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        hist.assert_called_once()

    def test_delivery_failure_is_still_an_error(self):
        resp, hist = self._type(DeliveryResult("target_absent"))
        self.assertEqual(resp.status_code, 409)
        hist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
