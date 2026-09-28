"""A session rename carries everything keyed by its name, server and client.

`/terminal/rename` re-keyed tab_state and drafts only. Auto-Yes (the runtime
maps and the persisted project settings) stayed under the old name, so with the
all-sessions switch on, a session the user opted out of was auto-answered again
under its new name, and with the switch off a hand-armed session silently
disarmed. The renaming browser kept `_termTarget` on the new name but never
re-subscribed its stream (the view froze on the dead name) and never marked the
choice, so a /poll in flight dropped it on the first tab.

Both directions: what belongs to the renamed session moves, and a session whose
name merely shares a prefix (`work` vs `work2`) is left alone.
"""

import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import shared.state as state
from routes import autoyes, commands, terminal

ROOT = Path(__file__).resolve().parent.parent


class _IsolatedState(unittest.TestCase):
    def setUp(self):
        maps = ("autoyes_sessions", "autoyes_delays", "autoyes_countdowns",
                "autoyes_answered", "autoyes_cancelled", "autoyes_effective",
                "autoyes_sources")
        for name in maps:
            patcher = patch.object(state, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            patch.object(state, "_project_settings", {}),
            patch.object(state, "_save_project_settings_locked"),
            patch.object(state, "tmux_target", None),
            patch.object(commands, "_command_panes", {}),
            patch.object(terminal.tab_state, "rename_session"),
            patch.object(terminal.drafts, "rename_session"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        real_get_setting = state.get_setting
        self.global_switch = "off"

        def configured(section, key):
            if (section, key) == ("autoyes", "all_sessions"):
                return self.global_switch
            return real_get_setting(section, key)

        patcher = patch.object(state, "get_setting", side_effect=configured)
        patcher.start()
        self.addCleanup(patcher.stop)


class RenameHookTests(_IsolatedState):
    def test_a_global_opt_out_survives_the_rename(self):
        self.global_switch = "on"
        state.patch_project_settings("work", {"autoyes": {"global_opt_out": True}})
        self.assertEqual(state.autoyes_enabled_for("work"), (False, "explicit"))
        terminal.on_session_renamed("work", "work-api")
        self.assertEqual(state.autoyes_enabled_for("work-api"), (False, "explicit"))
        # The old name no longer carries it: a new session reusing it is fresh.
        self.assertEqual(state.autoyes_enabled_for("work"), (True, "global"))

    def test_a_hand_armed_session_stays_armed(self):
        state.patch_project_settings(
            "work", {"autoyes": {"enabled_default": True, "delay": 7}})
        state.autoyes_sessions["work"] = True
        state.autoyes_delays["work"] = 7
        terminal.on_session_renamed("work", "work-api")
        self.assertEqual(state.autoyes_enabled_for("work-api"), (True, "explicit"))
        self.assertEqual(state.autoyes_delays, {"work-api": 7})
        self.assertEqual(state.autoyes_enabled_for("work"), (False, "explicit"))

    def test_runtime_maps_move_and_a_prefix_sibling_does_not(self):
        cancelled = {"prompt_hash": "h", "deadline": 1, "cancelled": True,
                     "prompt_type": "yes_no"}
        sibling = {"prompt_hash": "s", "deadline": 1, "cancelled": False,
                   "prompt_type": "yes_no"}
        state.autoyes_sessions.update({"work": False, "work2": True})
        state.autoyes_effective.update({"work": False, "work2": True})
        state.autoyes_sources.update({"work": "explicit", "work2": "explicit"})
        state.autoyes_countdowns.update({"work:1.1": cancelled, "work2:1.1": sibling})
        state.autoyes_answered.update({"work:1.2": ("h", 5), "work2:1.1": ("s", 5)})
        state.autoyes_cancelled.update(
            {"work:1.1": {("h", "yes_no")}, "work2:1.1": {("s", "yes_no")}})
        state.patch_project_settings("work2", {"autoyes": {"enabled_default": True}})

        terminal.on_session_renamed("work", "work-api")

        self.assertEqual(state.autoyes_sessions, {"work-api": False, "work2": True})
        self.assertEqual(state.autoyes_effective, {"work-api": False, "work2": True})
        self.assertEqual(state.autoyes_sources,
                         {"work-api": "explicit", "work2": "explicit"})
        # A cancelled countdown stays cancelled under the new name.
        self.assertEqual(state.autoyes_countdowns,
                         {"work-api:1.1": cancelled, "work2:1.1": sibling})
        self.assertEqual(state.autoyes_answered,
                         {"work-api:1.2": ("h", 5), "work2:1.1": ("s", 5)})
        # The cancel record moves too, or the scanner prunes it as a dead
        # target and the renamed pane's prompt counts down again.
        self.assertEqual(state.autoyes_cancelled,
                         {"work-api:1.1": {("h", "yes_no")},
                          "work2:1.1": {("s", "yes_no")}})
        self.assertTrue(state.get_project_settings("work2")["autoyes"]["enabled_default"])
        self.assertNotIn("work-api", state._project_settings)

    def test_tab_state_drafts_target_and_command_pane_follow(self):
        state.tmux_target = "work:1.2"
        commands._command_panes.update({"work": "%9", "work2": "%10"})
        terminal.on_session_renamed("work", "work-api")
        terminal.tab_state.rename_session.assert_called_once_with("work", "work-api")
        terminal.drafts.rename_session.assert_called_once_with("work", "work-api")
        self.assertEqual(state.tmux_target, "work-api:1.2")
        self.assertEqual(commands._command_panes, {"work-api": "%9", "work2": "%10"})

    def test_an_unrelated_target_is_left_alone(self):
        state.tmux_target = "work2:1.1"
        terminal.on_session_renamed("work", "work-api")
        self.assertEqual(state.tmux_target, "work2:1.1")

    def test_the_route_runs_the_hook_only_after_tmux_renamed(self):
        app = Flask("rename")
        app.register_blueprint(terminal.terminal_bp)
        client = app.test_client()
        ok = subprocess.CompletedProcess([], 0, "", "")
        failed = subprocess.CompletedProcess([], 1, "", "can't find session")
        with patch.object(terminal, "on_session_renamed") as hook, patch.object(
            terminal.subprocess, "run", return_value=failed
        ):
            response = client.post("/terminal/rename",
                                   json={"session": "work", "name": "work-api"})
            self.assertEqual(response.status_code, 500)
            hook.assert_not_called()
        with patch.object(terminal, "on_session_renamed") as hook, patch.object(
            terminal.subprocess, "run", return_value=ok
        ) as run:
            response = client.post("/terminal/rename",
                                   json={"session": "work", "name": "work.api"})
            self.assertEqual(response.status_code, 200)
            hook.assert_called_once_with("work", "work-api")
            self.assertIn("=work", run.call_args.args[0])


def _run_rename_js(body):
    source = (ROOT / "js" / "tabs.js").read_text()
    # Load the module's functions without its DOM wiring.
    source = re.sub(r"^_(init\w+|migrateLegacyTabState)\(\);$", "", source,
                    flags=re.MULTILINE)
    harness = r"""
const vm = require('vm');
globalThis.calls = [];
globalThis.window = globalThis;
globalThis.matchMedia = () => ({matches: false, addEventListener() {}});
globalThis.document = {getElementById: () => null, querySelectorAll: () => []};
globalThis.localStorage = {setItem: (k, v) => calls.push(['store', k, v])};
globalThis.noteTargetChosen = (t, created) => calls.push(['chosen', t, created]);
globalThis.selectTab = (t, auto) => calls.push(['select', t, auto, _termTarget]);
globalThis._lastTabTapTime = Date.now();
vm.runInThisContext(SOURCE, {filename: 'js/tabs.js'});
BODY
console.log(JSON.stringify({calls, target: globalThis._termTarget}));
""".replace("SOURCE", json.dumps(source)).replace("BODY", body)
    result = subprocess.run(["node", "-e", harness], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ClientRenameTests(unittest.TestCase):
    def test_the_view_follows_the_renamed_session(self):
        result = _run_rename_js(r"""
globalThis._termTarget = 'work:1.2';
_followRenamedSession('work', 'work-api');
""")
        self.assertEqual(result["calls"], [
            ["chosen", "work-api:1.2", True],
            # selectTab re-subscribes the stream and re-posts /terminal/target.
            # _termTarget already moved, so the composer is not swapped out.
            ["select", "work-api:1.2", True, "work-api:1.2"],
        ])

    def test_another_session_rename_does_not_move_the_view(self):
        result = _run_rename_js(r"""
globalThis._termTarget = 'work2:1.1';
_followRenamedSession('work', 'work-api');
""")
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["target"], "work2:1.1")


if __name__ == "__main__":
    unittest.main()
