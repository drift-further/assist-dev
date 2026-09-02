"""Contract tests for the compiled first-party Assist execution park."""

from __future__ import annotations

import inspect
import copy
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from shared import execution_park as park
from shared import tmux as tmux_shared
import shared.state as state
from routes import automate, autoyes, commands, container, input as input_routes, poll, settings, streaming, terminal
from routes import git as git_routes


PARK_REASON = (
    "Container launch automation is temporarily parked while host wiring migrates."
)


def _refusal_body(intent):
    return {
        "ok": False,
        "error": "container_launch_parked",
        "reason": PARK_REASON,
        "intent": intent.value,
    }


class _FakeIdentity:
    pane_id = "%510"

    def as_dict(self):
        return {"pane_id": self.pane_id}

    def __eq__(self, other):
        return isinstance(other, _FakeIdentity)


class ExecutionParkPolicyTests(unittest.TestCase):
    def setUp(self):
        with state.automate_lock:
            self._automate_state = copy.deepcopy(state.automate)
        self._tmux_target = state.tmux_target

    def tearDown(self):
        with state.automate_lock:
            state.automate.clear()
            state.automate.update(self._automate_state)
        state.tmux_target = self._tmux_target

    def test_compiled_policy_and_linearized_effect(self):
        self.assertEqual(park.SCHEMA, "assist-execution-park-v1")
        self.assertEqual(park.POLICY_NAME, "container_launch_park")
        self.assertEqual(park.REFUSAL_REASON, PARK_REASON)
        self.assertFalse(hasattr(park, "COMPILED_POLICY"))
        self.assertFalse(hasattr(park, "DECISION"))
        expected_denied = frozenset(
            {
                park.Intent.AUTOMATE_START,
                park.Intent.AUTOMATE_HARD_RELAUNCH,
                park.Intent.AUTOMATE_SOFT_CLEAR,
                park.Intent.AUTOMATE_SOFT_RESEND,
                park.Intent.AUTOMATE_TRUST_ANSWER,
                park.Intent.AUTOMATE_AUTO_ANSWER,
                park.Intent.CONFIGURED_CLI_PROXY,
                park.Intent.CONFIGURED_IMAGE_BUILD,
            }
        )
        self.assertEqual(park.DENIED_INTENTS, expected_denied)
        self.assertEqual(park.ALLOWED_INTENTS, frozenset(park.Intent) - expected_denied)
        self.assertEqual(set(park.Intent), park.DENIED_INTENTS | park.ALLOWED_INTENTS)
        self.assertFalse(park.DENIED_INTENTS & park.ALLOWED_INTENTS)

        for phase in park.Phase:
            policy = park.ExecutionPark(phase)
            for intent in expected_denied:
                with self.subTest(phase=phase.value, intent=intent.value):
                    effects = []
                    refusal = policy.perform(
                        intent, lambda: effects.append("delivered")
                    )
                    self.assertEqual(effects, [])
                    self.assertIsInstance(refusal, park.Refusal)
                    self.assertEqual(refusal.policy, "container_launch_park")
                    self.assertEqual(refusal.body(), _refusal_body(intent))
                    self.assertEqual(
                        refusal.background(),
                        {
                            "status": "container_launch_parked",
                            "reason": PARK_REASON,
                            "intent": intent.value,
                        },
                    )
                    self.assertEqual(refusal.http_status, 409)

        policy = park.ExecutionPark(park.Phase.DRAINING)
        entered = threading.Event()
        release = threading.Event()
        transitioned = threading.Event()

        def effect():
            entered.set()
            self.assertTrue(release.wait(2))
            return "complete"

        effect_result = []
        worker = threading.Thread(
            target=lambda: effect_result.append(
                policy.perform(park.Intent.OBSERVE, effect)
            )
        )
        worker.start()
        self.assertTrue(entered.wait(1))

        transition = threading.Thread(
            target=lambda: (policy.publish_parked(), transitioned.set())
        )
        transition.start()
        time.sleep(0.05)
        self.assertFalse(transitioned.is_set())
        release.set()
        worker.join(2)
        transition.join(2)
        self.assertEqual(effect_result, ["complete"])
        self.assertTrue(transitioned.is_set())
        self.assertEqual(policy.phase, park.Phase.PARKED)

        public = {
            name
            for name, _ in inspect.getmembers(park.ExecutionPark)
            if not name.startswith("_")
        }
        self.assertEqual(
            public,
            {
                "activation_status",
                "configure_activation",
                "perform",
                "phase",
                "publish_parked",
            },
        )
        self.assertNotIn("command", inspect.signature(policy.perform).parameters)
        self.assertNotIn("path", inspect.signature(policy.perform).parameters)
        self.assertNotIn("pane", inspect.signature(policy.perform).parameters)

    def test_automate_intents_all_refuse_before_effect(self):
        background_calls = (
            (
                park.Intent.AUTOMATE_HARD_RELAUNCH,
                lambda: automate._automate_relaunch(7),
            ),
            (park.Intent.AUTOMATE_SOFT_CLEAR, automate._automate_soft_clear),
            (
                park.Intent.AUTOMATE_SOFT_RESEND,
                lambda: automate._automate_soft_resend(
                    7, "ordinary:0.0", "saved prompt", "/project"
                ),
            ),
            (
                park.Intent.AUTOMATE_TRUST_ANSWER,
                lambda: automate._automate_scheduled_answer(
                    park.Intent.AUTOMATE_TRUST_ANSWER,
                    "project-auto:0.0",
                    "1",
                ),
            ),
            (
                park.Intent.AUTOMATE_AUTO_ANSWER,
                lambda: automate._automate_scheduled_answer(
                    park.Intent.AUTOMATE_AUTO_ANSWER,
                    "ordinary:0.0",
                    "yes",
                ),
            ),
        )

        for phase in park.Phase:
            with self.subTest(phase=phase.value, surface="http-start"), patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ):
                app = Flask(f"automate-park-{phase.value}")
                app.register_blueprint(automate.automate_bp)
                before = copy.deepcopy(state.automate)
                response = app.test_client().post("/api/automate/start", json={})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.get_json(),
                    _refusal_body(park.Intent.AUTOMATE_START),
                )
                self.assertEqual(state.automate, before)

            for intent, invoke in background_calls:
                with self.subTest(phase=phase.value, intent=intent.value), patch.object(
                    park, "_PROCESS_PARK", park.ExecutionPark(phase)
                ), patch.object(automate, "_automate_save") as save, patch.object(
                    automate, "tmux_send_text"
                ) as send_text, patch.object(
                    automate, "tmux_send_keys"
                ) as send_keys, patch.object(
                    automate.subprocess, "run"
                ) as spawn, patch.object(
                    automate.threading, "Thread"
                ) as thread, patch.object(
                    automate.time, "sleep"
                ) as sleep, patch.object(
                    automate, "_automate_cleanup"
                ) as cleanup:
                    with state.automate_lock:
                        state.automate["active"] = True
                        state.automate["status"] = "running"
                        state.automate["iterations_completed"] = 11
                    result = invoke()
                    self.assertIsInstance(result, park.Refusal)
                    self.assertEqual(result.intent, intent)
                    send_text.assert_not_called()
                    send_keys.assert_not_called()
                    spawn.assert_not_called()
                    thread.assert_not_called()
                    sleep.assert_not_called()
                    cleanup.assert_not_called()
                    save.assert_called_once_with()
                    with state.automate_lock:
                        self.assertFalse(state.automate["active"])
                        self.assertEqual(
                            state.automate["status"], "container_launch_parked"
                        )
                        self.assertEqual(state.automate["iterations_completed"], 11)

    def test_terminal_init_and_restart_routes_are_unparked(self):
        completed = lambda code=0, stdout="": subprocess.CompletedProcess(
            [], code, stdout, ""
        )

        with tempfile.TemporaryDirectory(prefix="assist-terminal-") as tmp:
            projects = Path(tmp)
            project_path = projects / "project"
            project_path.mkdir()
            real_get_setting = state.get_setting

            def configured_setting(section, key):
                if (section, key) == ("server", "session_init_cmd"):
                    return "server-owned-launcher --configured"
                return real_get_setting(section, key)

            for phase in park.Phase:
                app = Flask(f"terminal-park-{phase.value}")
                app.register_blueprint(terminal.terminal_bp)
                app.register_blueprint(settings.settings_bp)
                client = app.test_client()

                def tmux_result(command, **_kwargs):
                    if command[1] == "has-session":
                        return completed(1)
                    if command[1] == "display-message":
                        return completed(0, os.fspath(project_path) + "\n")
                    return completed()

                with self.subTest(phase=phase.value, surface="launch-init"), patch.object(
                    park, "_PROCESS_PARK", park.ExecutionPark(phase)
                ), patch.object(state, "PROJECTS_DIR", projects), patch.object(
                    state, "get_setting", side_effect=configured_setting
                ), patch.object(
                    terminal.subprocess, "run", side_effect=tmux_result
                ), patch.object(
                    terminal,
                    "create_tmux_session",
                    return_value=tmux_shared.TmuxCreationResult(
                        "created", identity=_FakeIdentity()
                    ),
                ) as create, patch.object(
                    terminal, "detect_venv", return_value=None
                ), patch.object(
                    terminal, "tmux_send_text"
                ) as send_text, patch.object(
                    terminal, "tmux_send_keys"
                ) as send_keys, patch.object(terminal.time, "sleep") as sleep:
                    response = client.post(
                        "/terminal/launch", json={"project": "project"}
                    )
                    self.assertEqual(response.status_code, 200, response.get_data())
                    self.assertEqual(
                        response.get_json()["init_cmd"],
                        "server-owned-launcher --configured",
                    )
                    create.assert_called_once()
                    send_text.assert_called_once_with(
                        "project:0.0", "server-owned-launcher --configured"
                    )
                    send_keys.assert_called_once_with("project:0.0", "Enter")
                    sleep.assert_called_once_with(0.3)

                with self.subTest(phase=phase.value, surface="duplicate-init"), patch.object(
                    park, "_PROCESS_PARK", park.ExecutionPark(phase)
                ), patch.object(
                    state, "get_setting", side_effect=configured_setting
                ), patch.object(
                    terminal.subprocess, "run", side_effect=tmux_result
                ), patch.object(
                    terminal,
                    "create_tmux_session",
                    return_value=tmux_shared.TmuxCreationResult(
                        "created", identity=_FakeIdentity()
                    ),
                ) as create, patch.object(
                    terminal, "tmux_send_text"
                ) as send_text, patch.object(
                    terminal, "tmux_send_keys"
                ) as send_keys, patch.object(terminal.time, "sleep") as sleep:
                    response = client.post(
                        "/terminal/duplicate",
                        json={"session": "project", "name": "project-copy"},
                    )
                    self.assertEqual(response.status_code, 200, response.get_data())
                    self.assertEqual(
                        response.get_json()["init_cmd"],
                        "server-owned-launcher --configured",
                    )
                    create.assert_called_once()
                    send_text.assert_called_once_with(
                        "project-copy:0.0", "server-owned-launcher --configured"
                    )
                    send_keys.assert_called_once_with("project-copy:0.0", "Enter")
                    sleep.assert_called_once_with(0.3)

                with self.subTest(phase=phase.value, surface="run-init"), patch.object(
                    park, "_PROCESS_PARK", park.ExecutionPark(phase)
                ), patch.object(
                    state, "get_setting", side_effect=configured_setting
                ), patch.object(
                    terminal.subprocess, "run", return_value=completed(0)
                ) as spawn, patch.object(
                    terminal, "tmux_send_text"
                ) as send_text, patch.object(
                    terminal, "tmux_send_keys"
                ) as send_keys:
                    response = client.post(
                        "/terminal/run-init", json={"session": "project"}
                    )
                    self.assertEqual(response.status_code, 200, response.get_data())
                    self.assertTrue(response.get_json()["ok"])
                    spawn.assert_called_once()
                    send_text.assert_called_once_with(
                        "=project:0.0", "server-owned-launcher --configured"
                    )
                    send_keys.assert_called_once_with("=project:0.0", "Enter")

                with self.subTest(phase=phase.value, surface="restart"), patch.object(
                    park, "_PROCESS_PARK", park.ExecutionPark(phase)
                ), patch.object(
                    settings.state,
                    "get_setting",
                    return_value="/usr/bin/assist-restart --configured",
                ), patch.object(
                    settings.shlex,
                    "split",
                    return_value=["/usr/bin/assist-restart", "--configured"],
                ) as split, patch.object(
                    settings.subprocess, "Popen"
                ) as spawn:
                    response = client.post("/api/restart")
                    self.assertEqual(response.status_code, 200, response.get_data())
                    self.assertTrue(response.get_json()["ok"])
                    split.assert_called_once_with(
                        "/usr/bin/assist-restart --configured"
                    )
                    spawn.assert_called_once_with(
                        ["/usr/bin/assist-restart", "--configured"],
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

    def test_remaining_server_controlled_spawns(self):
        for phase in park.Phase:
            app = Flask(f"remaining-spawns-{phase.value}")
            app.register_blueprint(commands.commands_bp)
            app.register_blueprint(git_routes.git_bp)
            app.register_blueprint(poll.poll_bp)
            app.register_blueprint(container.container_bp)
            client = app.test_client()

            with self.subTest(phase=phase.value, intent="saved_command"), patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ), patch.object(commands.subprocess, "run") as spawn:
                response = client.post("/api/commands/run", json={})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "session and cmd required")
                spawn.assert_not_called()

            with self.subTest(phase=phase.value, intent="project_venv"), patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ), patch.object(
                git_routes, "resolve_target", return_value=None
            ), patch.object(git_routes.subprocess, "run") as spawn:
                response = client.post("/api/venv/create", json={})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "No active session")
                spawn.assert_not_called()

            with self.subTest(phase=phase.value, intent="fixed_git"), patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ), patch.object(git_routes.subprocess, "run") as spawn:
                response = client.post("/api/git/run", json={})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "Invalid or missing op")
                spawn.assert_not_called()

            with self.subTest(
                phase=phase.value, intent="configured_cli_proxy"
            ), patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ), patch.object(poll.subprocess, "run") as spawn, patch(
                "tempfile.mkdtemp"
            ) as tempdir, patch.object(
                poll.shutil, "rmtree"
            ) as remove:
                response = client.post(
                    "/api/cli-proxy",
                    json={
                        "args": ["status"],
                        "files": [{"name": "request.txt", "data": "eA=="}],
                    },
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.get_json(),
                    _refusal_body(park.Intent.CONFIGURED_CLI_PROXY),
                )
                spawn.assert_not_called()
                tempdir.assert_not_called()
                remove.assert_not_called()

            build_before = copy.deepcopy(container._build)
            with self.subTest(
                phase=phase.value, intent="configured_image_build"
            ), patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ), patch.object(container.threading, "Thread") as thread, patch.object(
                container, "_run_build"
            ) as build, patch.object(
                container, "_generate_extensions_script"
            ) as generate, patch.object(
                container.state, "patch_container_config"
            ) as mutate:
                response = client.post("/api/container/build")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.get_json(),
                    _refusal_body(park.Intent.CONFIGURED_IMAGE_BUILD),
                )
                thread.assert_not_called()
                build.assert_not_called()
                generate.assert_not_called()
                mutate.assert_not_called()
                self.assertEqual(container._build, build_before)

    def test_native_picker_reaches_platform_effect(self):
        for phase in park.Phase:
            app = Flask(f"native-picker-{phase.value}")
            app.register_blueprint(terminal.terminal_bp)
            client = app.test_client()
            for start in (
                "/literal/helper",
                "/wrapper/osascript",
                "/symlink/zenity",
                "/path/kdialog",
            ):
                with self.subTest(phase=phase.value, start=start), patch.object(
                    park, "_PROCESS_PARK", park.ExecutionPark(phase)
                ), patch("platform.system", return_value="Linux") as platform_system, patch(
                    "shutil.which", return_value=None
                ) as which, patch.object(
                    terminal.subprocess, "run"
                ) as spawn, patch.object(
                    Path, "home"
                ) as home:
                    response = client.post(
                        "/terminal/explore/pick", json={"start": start}
                    )
                    self.assertEqual(response.status_code, 501)
                    self.assertEqual(
                        response.get_json()["error"],
                        "No native dialog tool found — install zenity: sudo apt install zenity",
                    )
                    platform_system.assert_called_once_with()
                    self.assertEqual(which.call_count, 2)
                    spawn.assert_not_called()
                    home.assert_not_called()

    def test_allowed_intents_ignore_runtime_payload_and_phase(self):
        expected_allowed = {
            "terminal_init_launch",
            "terminal_init_duplicate",
            "terminal_run_init",
            "configured_restart",
            "saved_command",
            "project_venv",
            "fixed_git",
            "native_folder_picker",
            "autoyes_answer",
            "operator_interactive",
            "client_session_resume",
            "client_session_restart",
            "bare_terminal",
            "observe",
            "stop",
            "image_config",
        }
        self.assertEqual(
            {intent.value for intent in park.ALLOWED_INTENTS}, expected_allowed
        )

        provenance_traps = (
            "bash /docker/claude-mount.sh",
            "/tmp/wrapper/docker",
            "project-auto:0.0",
            "ordinary:0.0",
            "foreground=docker",
        )
        for phase in park.Phase:
            for intent in park.ALLOWED_INTENTS:
                for payload in provenance_traps:
                    with self.subTest(
                        phase=phase.value, intent=intent.value, payload=payload
                    ):
                        policy = park.ExecutionPark(phase)
                        effects = []
                        result = policy.perform(
                            intent, lambda value=payload: effects.append(value) or value
                        )
                        self.assertEqual(result, payload)
                        self.assertEqual(effects, [payload])

            with self.subTest(phase=phase.value, surface="autoyes"), patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ), patch.object(
                autoyes,
                "generation_bound_delivery",
                return_value=tmux_shared.DeliveryResult("delivered"),
            ) as deliver:
                result = autoyes._deliver_autoyes_answer(
                    _FakeIdentity(), "bash /docker/claude-mount.sh", True
                )
                self.assertTrue(result.ok)
                deliver.assert_called_once_with(
                    _FakeIdentity(),
                    text="bash /docker/claude-mount.sh",
                    enter=True,
                )

            app = Flask(f"operator-delivery-{phase.value}")
            app.register_blueprint(input_routes.input_bp)
            client = app.test_client()
            with self.subTest(phase=phase.value, surface="operator"), patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ), patch.object(
                input_routes,
                "expected_target_identity",
                return_value=_FakeIdentity(),
            ), patch.object(
                input_routes,
                "generation_bound_delivery",
                return_value=tmux_shared.DeliveryResult("delivered"),
            ) as deliver, patch.object(
                input_routes, "pane_awaits_secret", return_value=False
            ), patch.object(
                input_routes, "declare_agent_command"
            ), patch.object(
                input_routes.state, "touch_activity"
            ), patch.object(
                input_routes, "add_to_history"
            ):
                response = client.post(
                    "/type",
                    json={
                        "target": "project-auto:0.0",
                        "text": "bash /docker/claude-mount.sh",
                        "enter": True,
                        "raw": True,
                    },
                )
                self.assertEqual(response.status_code, 200)
                deliver.assert_called_once_with(
                    _FakeIdentity(),
                    text="bash /docker/claude-mount.sh",
                    enter=True,
                )

        source = inspect.getsource(park.ExecutionPark.perform)
        for forbidden in (
            "command",
            "path",
            "pane",
            "target",
            "foreground",
            "endswith",
            "resolve",
        ):
            self.assertNotIn(forbidden, source)

    def test_streaming_resize_clear_have_no_automatic_ctrl_l(self):
        streaming_source = inspect.getsource(streaming)
        terminal_source = inspect.getsource(terminal)
        self.assertNotIn('target=_force_redraw', streaming_source)
        self.assertNotIn('["tmux", "send-keys", "-t", exact, "C-l"]', streaming_source)
        self.assertNotIn('"send-keys", "-t", tmux_exact_target(session), "C-l"', terminal_source)
        self.assertNotIn('"send-keys", "-t", tmux_exact_target(target), "C-l"', terminal_source)

        app = Flask("ctrl-l-no-delivery")
        app.register_blueprint(terminal.terminal_bp)
        client = app.test_client()
        ok = subprocess.CompletedProcess([], 0, "", "")

        with patch.object(terminal.subprocess, "run", return_value=ok) as spawn:
            response = client.post(
                "/terminal/resize",
                json={"session": "project", "cols": 120, "rows": 60},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(spawn.call_args_list), 1)
            self.assertEqual(spawn.call_args.args[0][1], "resize-window")

        with patch.object(terminal.subprocess, "run", return_value=ok) as spawn:
            response = client.post(
                "/terminal/clear", json={"target": "project:0.0"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(spawn.call_args_list), 1)
            self.assertEqual(spawn.call_args.args[0][1], "clear-history")

        with patch.object(
            streaming, "capture_pane", return_value=("captured", {"width": 80})
        ):
            frame, info = streaming._full_frame("project:0.0", 2000)
        self.assertEqual(json.loads(frame)["content"], "captured")
        self.assertEqual(info, {"width": 80})


class _FakeTmuxGeneration:
    def __init__(self):
        pid = os.getpid()
        self.socket_path = "/run/effort510/tmux/default"
        self.socket_identity = (41, 510)
        self.fields = {
            "server_pid": pid,
            "session_id": "$1",
            "window_id": "@2",
            "pane_id": "%3",
            "pane_pid": pid,
        }
        self.connections = []
        self.batches = []
        self.fail_after_text = False
        self.replacement_bytes = []

    def factory(self, socket_path, target):
        connection = _FakeTmuxConnection(self, target)
        self.connections.append(connection)
        return connection


class _FakeTmuxConnection:
    def __init__(self, generation, target):
        self.generation = generation
        self.target = target
        self.socket_path = generation.socket_path
        self.closed = False

    def socket_identity(self):
        return self.generation.socket_identity

    def target_fields(self, target):
        fields = self.generation.fields
        if target not in {"human:0.0", fields["pane_id"]}:
            return None
        return dict(fields)

    def send_batch(self, pane_id, *, text=None, keys=(), enter=False, barrier=None):
        if pane_id != self.generation.fields["pane_id"]:
            raise OSError("pane changed")
        if barrier:
            barrier("before_submit")
        record = {"pane_id": pane_id, "text": text, "keys": tuple(keys), "enter": enter}
        self.generation.batches.append(record)
        if self.generation.fail_after_text:
            # Text belonged only to the original connection.  A replacement is
            # deliberately present but receives no continuation or Enter.
            record["enter"] = False
            raise OSError("original generation died after text acknowledgement")

    def close(self):
        self.closed = True


class GenerationBoundDeliveryTests(unittest.TestCase):
    def test_real_private_tmux_control_connection(self):
        with tempfile.TemporaryDirectory(prefix="effort510-tmux-") as raw:
            root = Path(raw)
            socket_path = root / "private.sock"
            delivered = root / "delivered.txt"
            subprocess.run(
                [
                    "tmux",
                    "-S",
                    str(socket_path),
                    "new-session",
                    "-d",
                    "-s",
                    "effort510",
                ],
                check=True,
                capture_output=True,
            )
            try:
                with patch.dict(
                    os.environ,
                    {"TMUX": f"{socket_path},0,0"},
                    clear=False,
                ):
                    expected = tmux_shared.expected_target_identity("effort510:0.0")
                    self.assertIsNotNone(expected)
                    result = tmux_shared.generation_bound_delivery(
                        expected,
                        text=f"printf delivered > {delivered}",
                        enter=True,
                    )
                self.assertEqual(result.status, "delivered")
                deadline = time.monotonic() + 3
                while not delivered.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(delivered.read_text(encoding="utf-8"), "delivered")
            finally:
                subprocess.run(
                    ["tmux", "-S", str(socket_path), "kill-server"],
                    capture_output=True,
                )

    def test_expected_target_identity_and_one_connection_transaction(self):
        generation = _FakeTmuxGeneration()
        expected = tmux_shared.expected_target_identity(
            "human:0.0", connection_factory=generation.factory
        )
        self.assertIsNotNone(expected)
        self.assertEqual(
            {
                expected.socket_device,
                expected.socket_inode,
                expected.server_pid,
                expected.session_id,
                expected.window_id,
                expected.pane_id,
                expected.pane_pid,
            },
            {41, 510, os.getpid(), "$1", "@2", "%3"},
        )
        selected_connection = generation.connections[-1]
        before = len(generation.connections)
        result = tmux_shared.generation_bound_delivery(
            expected,
            text="answer",
            enter=True,
            connection_factory=generation.factory,
        )
        self.assertEqual(result.status, "delivered")
        self.assertEqual(len(generation.connections), before + 1)
        delivery_connection = generation.connections[-1]
        self.assertIsNot(selected_connection, delivery_connection)
        self.assertTrue(delivery_connection.closed)
        self.assertEqual(
            generation.batches,
            [{"pane_id": "%3", "text": "answer", "keys": (), "enter": True}],
        )

    def test_generation_barriers_fail_closed_without_reconnect(self):
        for barrier_kind in ("post_read", "same_name_pane", "server_restart"):
            generation = _FakeTmuxGeneration()
            expected = tmux_shared.expected_target_identity(
                "human:0.0", connection_factory=generation.factory
            )
            before = len(generation.connections)

            def barrier(stage):
                if stage != "after_identity_before_send":
                    return
                if barrier_kind == "post_read":
                    generation.fields = {**generation.fields, "pane_id": "%4"}
                elif barrier_kind == "same_name_pane":
                    generation.fields = {**generation.fields, "pane_pid": os.getppid()}
                else:
                    generation.socket_identity = (41, 511)

            result = tmux_shared.generation_bound_delivery(
                expected,
                text="never",
                enter=True,
                connection_factory=generation.factory,
                barrier=barrier,
            )
            self.assertEqual(result.status, "target_absent")
            self.assertEqual(generation.batches, [])
            self.assertEqual(len(generation.connections), before + 1)

        generation = _FakeTmuxGeneration()
        expected = tmux_shared.expected_target_identity(
            "human:0.0", connection_factory=generation.factory
        )
        generation.fail_after_text = True
        result = tmux_shared.generation_bound_delivery(
            expected,
            text="accepted-by-A",
            enter=True,
            connection_factory=generation.factory,
        )
        self.assertEqual(result.status, "delivery_failed")
        self.assertFalse(generation.batches[0]["enter"])
        self.assertEqual(generation.replacement_bytes, [])

    def test_all_allowed_callers_carry_identity(self):
        input_source = inspect.getsource(input_routes)
        autoyes_source = inspect.getsource(autoyes)
        terminal_source = inspect.getsource(terminal)
        cli_source = (Path(__file__).parent.parent / "cli" / "session.py").read_text()
        terminal_js = (Path(__file__).parent.parent / "js" / "terminal.js").read_text()
        actions_js = (Path(__file__).parent.parent / "js" / "actions.js").read_text()

        self.assertIn("expected_target_identity(target)", input_source)
        self.assertIn("generation_bound_delivery(expected", input_source)
        self.assertNotIn("xdotool", inspect.getsource(input_routes._type_text_effect))
        self.assertNotIn("xdotool", inspect.getsource(input_routes._send_key_effect))
        self.assertIn('"expected_target_identity": detected_identity.as_dict()', autoyes_source)
        self.assertIn("generation_bound_delivery(", autoyes_source)
        self.assertIn('"expected_target_identity": identity.as_dict()', terminal_source)
        self.assertIn('http.post(\n        "/type"', cli_source)
        self.assertIn("/type/client-resume", terminal_js)
        self.assertIn("expected_target_identity: launchData.expected_target_identity", terminal_js)
        self.assertIn("/type/client-restart", actions_js)
        self.assertIn("expected_target_identity: _termExpectedIdentity", actions_js)

    def test_no_automatic_redraw_delivery(self):
        streaming_source = inspect.getsource(streaming)
        terminal_source = inspect.getsource(terminal)
        self.assertNotIn("_force_redraw", streaming_source)
        self.assertNotIn("_maybe_redraw_async", streaming_source)
        self.assertNotIn('"C-l"', inspect.getsource(terminal.terminal_resize))
        self.assertNotIn('"C-l"', inspect.getsource(terminal.terminal_clear))


if __name__ == "__main__":
    unittest.main()
