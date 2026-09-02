import os
import copy
import importlib.util
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from shared import park_activation as activation
from shared import execution_park as park
import shared.state as state
from routes import automate
from tests.test_execution_park import (
    ExecutionParkPolicyTests,
    GenerationBoundDeliveryTests,
)
from tests.test_execution_park_inventory import ExecutionParkInventoryTests
from tests.test_launch_provenance import LaunchProvenanceTests
from tests.test_launch_provenance_inventory import LaunchProvenanceInventoryTests
from tests.test_park_activation import ParkActivationTests


class ContainerBoundaryV16Tests(unittest.TestCase):
    @staticmethod
    def _run_case(test_class, method):
        case = test_class(method)
        result = unittest.TestResult()
        case.run(result)
        if result.errors or result.failures:
            details = "\n".join(message for _test, message in result.errors + result.failures)
            raise AssertionError(details)

    def test_activation_state_machine_unit_ownership(self):
        observer = activation.ExecutionObserver(
            old_identity=activation.process_identity(os.getpid()),
            tmux_probe=lambda: {
                "reachable": True,
                "rows": ["$1\t@1\t%1\t999999"],
            },
            docker_probe=lambda: {"reachable": False, "rows": []},
            clock=iter((1.0, 2.0)).__next__,
        )
        watermark = observer.ready()
        self.assertFalse(watermark["docker_reachable"])
        snapshot = observer.snapshot()
        self.assertEqual(len(snapshot["units"]), 1)
        self.assertEqual(snapshot["units"][0]["outcome"], "unresolved")
        self.assertFalse(observer.final_quiescent(snapshot))
        snapshot["units"][0]["outcome"] = "terminal"
        self.assertTrue(observer.final_quiescent(snapshot))

    def test_case_1_direct_automate_refusal(self):
        original = copy.deepcopy(state.automate)
        original_target = state.tmux_target
        try:
            for phase in park.Phase:
                for payload, active in (
                    ({}, False),
                    ({"prompt": "valid"}, False),
                    ({"prompt": "valid"}, True),
                ):
                    with self.subTest(phase=phase.value, payload=payload, active=active):
                        with state.automate_lock:
                            state.automate.clear()
                            state.automate.update(copy.deepcopy(original))
                            state.automate["active"] = active
                        state.tmux_target = None
                        before = copy.deepcopy(state.automate)
                        app = Flask(f"case1-{phase.value}-{active}-{len(payload)}")
                        app.register_blueprint(automate.automate_bp)
                        with patch.object(
                            park, "_PROCESS_PARK", park.ExecutionPark(phase)
                        ), patch.object(automate.subprocess, "run") as spawn, patch.object(
                            automate, "tmux_send_text"
                        ) as text_send, patch.object(
                            automate, "tmux_send_keys"
                        ) as key_send, patch.object(
                            automate.threading, "Thread"
                        ) as thread:
                            response = app.test_client().post(
                                "/api/automate/start", json=payload
                            )
                        self.assertEqual(response.status_code, 409)
                        self.assertEqual(
                            response.get_json(),
                            {
                                "ok": False,
                                "error": "container_launch_parked",
                                "reason": (
                                    "Container launch automation is temporarily parked "
                                    "while host wiring migrates."
                                ),
                                "intent": "automate_start",
                            },
                        )
                        self.assertEqual(state.automate, before)
                        spawn.assert_not_called()
                        text_send.assert_not_called()
                        key_send.assert_not_called()
                        thread.assert_not_called()

                with state.automate_lock:
                    state.automate.clear()
                    state.automate.update(copy.deepcopy(original))
                    state.automate.update(
                        active=True,
                        status="running",
                        iterations_completed=7,
                    )
                with patch.object(
                    park, "_PROCESS_PARK", park.ExecutionPark(phase)
                ), patch.object(automate, "_automate_cleanup") as cleanup, patch.object(
                    automate, "tmux_send_text"
                ) as text_send:
                    result = automate._automate_relaunch(91)
                self.assertIsInstance(result, park.Refusal)
                self.assertEqual(result.intent, park.Intent.AUTOMATE_HARD_RELAUNCH)
                cleanup.assert_not_called()
                text_send.assert_not_called()
                with state.automate_lock:
                    self.assertFalse(state.automate["active"])
                    self.assertEqual(state.automate["status"], "container_launch_parked")
                    self.assertEqual(state.automate["iterations_completed"], 7)
        finally:
            with state.automate_lock:
                state.automate.clear()
                state.automate.update(original)
            state.tmux_target = original_target

    def test_case_2_post_dispatch_activation_barrier(self):
        tracker = activation.DrainTracker(docker_reachable=True)
        tracker.add_unit(
            "post-send-pre-docker",
            identity={"pane": "%1", "pid": 101, "start_time": "a"},
            survivor_eligible=True,
            input_delivered=True,
        )
        tracker.observe_descendant(
            "post-send-pre-docker", {"pid": 102, "start_time": "b"}
        )
        for _scan in range(25):
            self.assertFalse(tracker.can_publish())
        tracker.observe_docker_event(
            "post-send-pre-docker", {"container_id": "exact", "started": 7}
        )
        tracker.resolve_survivor(
            "post-send-pre-docker",
            exact_container=True,
            final_foreground=True,
            input_queue_empty=True,
        )
        self.assertTrue(tracker.can_publish())

        stopped = activation.DrainTracker(docker_reachable=True)
        stopped.add_unit(
            "ordinary-pane",
            identity={"pane": "%2", "pid": 201, "start_time": "c"},
            survivor_eligible=False,
            input_delivered=True,
        )
        with self.assertRaises(activation.ActivationError):
            stopped.resolve_survivor(
                "ordinary-pane",
                exact_container=True,
                final_foreground=True,
                input_queue_empty=True,
            )
        stopped.resolve_terminal(
            "ordinary-pane",
            pane_and_process_gone=True,
            no_unclassified_event=True,
            final_rescan_empty=True,
        )
        self.assertTrue(stopped.can_publish())

        no_pane = activation.DrainTracker(docker_reachable=True)
        no_pane.add_unit(
            "configured-restart-child",
            identity={"pid": 301, "start_time": "d"},
        )
        no_pane.observe_descendant(
            "configured-restart-child", {"pid": 302, "start_time": "e"}
        )
        self.assertFalse(no_pane.can_publish())

        allowed_delivery = activation.DrainTracker(docker_reachable=True)
        allowed_delivery.add_unit(
            "human-launcher-pane",
            identity={"pane": "%3", "pid": 401, "start_time": "f"},
            input_delivered=True,
        )
        allowed_delivery.observe_descendant(
            "human-launcher-pane", {"pid": 402, "start_time": "g"}
        )
        self.assertFalse(allowed_delivery.can_publish())

        unreachable_empty = activation.DrainTracker(docker_reachable=False)
        unreachable_empty.record_unreachable_probe()
        self.assertFalse(unreachable_empty.can_publish())
        unreachable_empty.record_unreachable_probe()
        self.assertTrue(unreachable_empty.can_publish())
        unreachable_pending = activation.DrainTracker(docker_reachable=False)
        unreachable_pending.add_unit("pending", identity={"pane": "%4"})
        unreachable_pending.record_unreachable_probe()
        unreachable_pending.record_unreachable_probe()
        self.assertFalse(unreachable_pending.can_publish())

    def test_case_3_automate_soft_non_delivery(self):
        for phase in park.Phase:
            with patch.object(
                park, "_PROCESS_PARK", park.ExecutionPark(phase)
            ), patch.object(automate, "tmux_send_text") as send_text, patch.object(
                automate, "tmux_send_keys"
            ) as send_keys, patch.object(
                automate, "declare_agent_command"
            ) as declare, patch.object(
                automate.subprocess, "run"
            ) as spawn:
                clear = automate._automate_soft_clear()
                resend = automate._automate_soft_resend(
                    1,
                    "legacy:0.0",
                    "bash /opt/assist/docker/claude-mount.sh -n noop",
                    "/project",
                )
                trust = automate._automate_scheduled_answer(
                    park.Intent.AUTOMATE_TRUST_ANSWER, "legacy:0.0", "1"
                )
                answer = automate._automate_scheduled_answer(
                    park.Intent.AUTOMATE_AUTO_ANSWER, "legacy:0.0", "yes"
                )
            self.assertEqual(
                [value.intent for value in (clear, resend, trust, answer)],
                [
                    park.Intent.AUTOMATE_SOFT_CLEAR,
                    park.Intent.AUTOMATE_SOFT_RESEND,
                    park.Intent.AUTOMATE_TRUST_ANSWER,
                    park.Intent.AUTOMATE_AUTO_ANSWER,
                ],
            )
            send_text.assert_not_called()
            send_keys.assert_not_called()
            declare.assert_not_called()
            spawn.assert_not_called()
        source = inspect.getsource(automate)
        self.assertNotIn("Intent.AUTOYES_ANSWER", source)
        self.assertNotIn("Intent.OPERATOR_INTERACTIVE", source)

    def test_case_4_configured_command_routes(self):
        self._run_case(
            ExecutionParkPolicyTests,
            "test_terminal_init_and_restart_routes_are_unparked",
        )
        terminal_source = (Path(__file__).parent.parent / "routes" / "terminal.py").read_text()
        settings_source = (Path(__file__).parent.parent / "routes" / "settings.py").read_text()
        self.assertIn("Intent.TERMINAL_INIT_LAUNCH", terminal_source)
        self.assertIn("Intent.TERMINAL_INIT_DUPLICATE", terminal_source)
        self.assertIn("Intent.TERMINAL_RUN_INIT", terminal_source)
        self.assertIn("Intent.CONFIGURED_RESTART", settings_source)
        self.assertNotIn("claude-mount.sh", inspect.getsource(park.ExecutionPark.perform))

    def test_case_5_spawn_inventory_and_native_picker(self):
        self._run_case(
            ExecutionParkPolicyTests, "test_remaining_server_controlled_spawns"
        )
        self._run_case(
            ExecutionParkPolicyTests,
            "test_native_picker_reaches_platform_effect",
        )
        self._run_case(
            ExecutionParkInventoryTests,
            "test_every_process_thread_and_pty_site_is_classified",
        )
        self._run_case(
            ExecutionParkInventoryTests,
            "test_park_bindings_name_only_compiled_provenance",
        )
        self._run_case(
            ExecutionParkInventoryTests,
            "test_direct_flask_returns_can_only_use_allowed_intents",
        )
        self._run_case(
            ExecutionParkInventoryTests,
            "test_denied_http_bindings_convert_refusals_to_responses",
        )

    def test_case_6_generation_bound_delivery(self):
        self._run_case(
            GenerationBoundDeliveryTests,
            "test_expected_target_identity_and_one_connection_transaction",
        )
        self._run_case(
            GenerationBoundDeliveryTests,
            "test_generation_barriers_fail_closed_without_reconnect",
        )
        self._run_case(
            GenerationBoundDeliveryTests,
            "test_all_allowed_callers_carry_identity",
        )
        self._run_case(
            GenerationBoundDeliveryTests,
            "test_no_automatic_redraw_delivery",
        )

    def test_case_7_survivor_exclusions_and_docker_tree(self):
        policy = park.ExecutionPark(park.Phase.DRAINING)
        policy.configure_activation(
            activation.ACTIVATION_PATH,
            [{"identity": "survivor", "outcome": "pre_boundary_survivor"}],
        )
        policy.publish_parked()
        status = policy.activation_status()
        self.assertEqual(status["park_phase"], "parked")
        self.assertEqual(status["activation_path"], "activate-park-v16")
        self.assertEqual(status["unresolved_units"], [])
        self.assertNotIn("docker", inspect.getsource(park.ExecutionPark.perform).lower())

        checker_path = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "release"
            / "check_container_boundary_v16_docker_tree.py"
        )
        spec = importlib.util.spec_from_file_location("container_boundary_checker", checker_path)
        checker = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(checker)
        baseline = __import__("json").loads(checker.BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(checker.payload(), baseline)

    def test_docker_tree_mode_only_pins_executable_bits(self):
        checker_path = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "release"
            / "check_container_boundary_v16_docker_tree.py"
        )
        spec = importlib.util.spec_from_file_location("container_boundary_checker", checker_path)
        checker = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(checker)

        self.assertEqual(checker._mode(0o100775), "0111")
        self.assertEqual(checker._mode(0o100755), "0111")
        self.assertEqual(checker._mode(0o100664), "0000")
        self.assertEqual(checker._mode(0o100644), "0000")

    # Replacements for the legacy activation/survivor cases.
    def test_activation_state_machine_unit_ownership(self):
        self._run_case(
            LaunchProvenanceTests,
            "test_populated_ambient_server_counts_only_recorded_created_units",
        )

    def test_case_2_post_dispatch_activation_barrier(self):
        self._run_case(
            LaunchProvenanceTests,
            "test_populated_ambient_server_counts_only_recorded_created_units",
        )
        for method in (
            "test_live_owned_unit_refuses_before_candidate_or_signal_and_old_keeps_serving",
            "test_empty_epoch_and_recorded_absent_do_not_refuse_first_cutover",
            "test_registry_lock_serializes_creation_receipt_before_owned_preflight",
            "test_only_late_old_process_descendants_resolve_after_irreversible_barrier",
        ):
            self._run_case(ParkActivationTests, method)

    def test_case_7_survivor_exclusions_and_docker_tree(self):
        for method in (
            "test_full_container_id_stable_across_status_churn",
            "test_same_name_different_full_ids_remain_distinct_ambient",
            "test_malformed_truncated_or_status_keyed_docker_rows_fail",
        ):
            self._run_case(ParkActivationTests, method)
        self._run_case(
            LaunchProvenanceTests,
            "test_existing_terminal_reconnect_and_recovery_are_adoptions",
        )
        self._run_case(
            LaunchProvenanceInventoryTests,
            "test_no_production_survivor_or_container_origin_path",
        )
        checker_path = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "release"
            / "check_container_boundary_v16_docker_tree.py"
        )
        spec = importlib.util.spec_from_file_location("container_boundary_checker", checker_path)
        checker = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(checker)
        baseline = __import__("json").loads(checker.BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(checker.payload(), baseline)
