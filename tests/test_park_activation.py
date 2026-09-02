import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from shared import park_activation as activation
from shared import launch_provenance as provenance
from shared.tmux import ExpectedTargetIdentity


ROOT = Path(__file__).resolve().parent.parent


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _fixture(path):
    path.write_text(
        json.dumps(
            {
                "tmux": {"reachable": True, "rows": []},
                "docker": {"reachable": True, "rows": []},
            }
        ),
        encoding="utf-8",
    )


class ParkActivationTests(unittest.TestCase):
    def test_full_container_id_stable_across_status_churn(self):
        container_id = "a" * 64
        first = activation.ExecutionObserver._docker_observations(
            {"reachable": True, "rows": [f"{container_id}\tworker\trunning\tUp 59 seconds"]}
        )
        later = activation.ExecutionObserver._docker_observations(
            {"reachable": True, "rows": [f"{container_id}\trenamed\trunning\tUp 2 hours (healthy)"]}
        )
        self.assertEqual(first["rows"][0]["id"], later["rows"][0]["id"])
        self.assertEqual(first["rows"][0]["classification"], "ambient_unowned")
        self.assertNotEqual(first["rows"][0]["status"], later["rows"][0]["status"])

    def test_same_name_different_full_ids_remain_distinct_ambient(self):
        value = activation.ExecutionObserver._docker_observations(
            {
                "reachable": True,
                "rows": [
                    f"{'a' * 64}\tsame\trunning\tUp 1 minute",
                    f"{'b' * 64}\tsame\trunning\tUp 1 minute",
                ],
            }
        )
        self.assertEqual(len(value["rows"]), 2)
        self.assertTrue(all(row["classification"] == "ambient_unowned" for row in value["rows"]))

    def test_malformed_truncated_or_status_keyed_docker_rows_fail(self):
        bad = (
            "abc\tname\trunning\tUp",
            f"{'a' * 64}\tmissing-fields",
            {"id": "Up 2 hours", "name": "x", "state": "running", "status": "x"},
        )
        for row in bad:
            with self.subTest(row=row), self.assertRaisesRegex(
                activation.ActivationError, "docker_observation_malformed"
            ):
                activation.ExecutionObserver._docker_observations(
                    {"reachable": True, "rows": [row]}
                )
        with self.assertRaisesRegex(
            activation.ActivationError, "docker_observation_duplicate"
        ):
            activation.ExecutionObserver._docker_observations(
                {
                    "reachable": True,
                    "rows": [
                        f"{'a' * 64}\tx\trunning\tUp 1",
                        f"{'a' * 64}\ty\trunning\tUp 2",
                    ],
                }
            )
    @staticmethod
    def _provenance_identity(marker=1):
        return ExpectedTargetIdentity(
            socket_path=f"/private/tmux/{marker}",
            socket_device=10,
            socket_inode=20 + marker,
            server_pid=30,
            server_start_time="40",
            session_id=f"${marker}",
            window_id=f"@{marker}",
            pane_id=f"%{marker}",
            pane_pid=50 + marker,
            pane_start_time=str(60 + marker),
        )

    @staticmethod
    def _provenance_store(root):
        home = root / "home"
        home.mkdir(mode=0o700)
        store_root = home / provenance.ROOT_NAME
        with mock.patch.dict(
            os.environ,
            {"ASSIST_LAUNCH_PROVENANCE_ROOT": str(store_root)},
            clear=False,
        ):
            provenance.initialize_epoch(
                assist_home=home,
                receipt_path=root / "init.json",
                expect_empty=True,
            )
        return provenance.LaunchProvenanceStore(
            assist_home=home, root=store_root, lock_timeout=1
        )

    def test_live_owned_unit_refuses_before_candidate_or_signal_and_old_keeps_serving(self):
        with tempfile.TemporaryDirectory() as raw:
            store = self._provenance_store(Path(raw))
            identity = self._provenance_identity(1)
            with store.locked() as registry:
                registry.record_created(identity, surface="fresh_terminal")
            snapshot = activation._activation_preflight(
                activation.process_identity(os.getpid()),
                store.snapshot(),
                tmux_probe=lambda: {
                    "reachable": True,
                    "rows": [{"identity": identity.as_dict(), "alias": "owned:0.0"}],
                },
            )
            self.assertEqual(len(snapshot["owned_units"]), 1)
            self.assertTrue(activation.identity_alive(activation.process_identity(os.getpid())))
            source = __import__("inspect").getsource(activation.controller)
            predicate = source.index("blocking_owned_units")
            self.assertLess(predicate, source.index("subprocess.Popen("))
            self.assertLess(predicate, source.index("_freeze_process_tree("))
            self.assertLess(predicate, source.index("signal.SIGKILL"))

    def test_empty_epoch_and_recorded_absent_do_not_refuse_first_cutover(self):
        with tempfile.TemporaryDirectory() as raw:
            store = self._provenance_store(Path(raw))
            ambient = self._provenance_identity(2)
            empty = activation._activation_preflight(
                {"pid": 999999999, "start_time": "absent"},
                store.snapshot(),
                tmux_probe=lambda: {
                    "reachable": True,
                    "rows": [{"identity": ambient.as_dict(), "alias": "ambient:0.0"}],
                },
            )
            self.assertEqual(empty["owned_units"], [])
            self.assertEqual(len(empty["ambient_unowned"]), 1)
            with store.locked() as registry:
                registry.record_created(
                    self._provenance_identity(3), surface="fresh_terminal"
                )
            absent = activation._activation_preflight(
                {"pid": 999999999, "start_time": "absent"},
                store.snapshot(),
                tmux_probe=lambda: {"reachable": True, "rows": []},
            )
            self.assertEqual(absent["owned_units"], [])
            self.assertEqual(len(absent["recorded_absent"]), 1)

    def test_registry_lock_serializes_creation_receipt_before_owned_preflight(self):
        with tempfile.TemporaryDirectory() as raw:
            store = self._provenance_store(Path(raw))
            identity = self._provenance_identity(4)
            writer_locked = threading.Event()
            release_writer = threading.Event()

            def writer():
                with store.locked() as registry:
                    writer_locked.set()
                    release_writer.wait(2)
                    registry.record_created(identity, surface="fresh_terminal")

            thread = threading.Thread(target=writer)
            thread.start()
            self.assertTrue(writer_locked.wait(1))
            release_writer.set()
            with store.locked() as registry:
                sealed = registry.snapshot()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            snapshot = activation._activation_preflight(
                {"pid": 999999999, "start_time": "absent"},
                sealed,
                tmux_probe=lambda: {
                    "reachable": True,
                    "rows": [{"identity": identity.as_dict(), "alias": "barrier:0.0"}],
                },
            )
            self.assertEqual(len(snapshot["owned_units"]), 1)
            prebind = __import__("inspect").getsource(activation.candidate_prebind)
            self.assertNotIn("LaunchProvenanceStore", prebind)
            self.assertNotIn(".locked(", prebind)

    def test_only_late_old_process_descendants_resolve_after_irreversible_barrier(self):
        identity = activation.process_identity(os.getpid())
        snapshot = {
            "units": [],
            "handoff_process_obligations": [
                {"identity": identity, "outcome": "unresolved"}
            ],
        }
        self.assertFalse(activation.ExecutionObserver.final_quiescent(snapshot))
        snapshot["handoff_process_obligations"][0]["outcome"] = "terminal"
        self.assertTrue(activation.ExecutionObserver.final_quiescent(snapshot))
        source = __import__("inspect").getsource(activation.ExecutionObserver.snapshot)
        self.assertIn("handoff_process_obligations", source)
        self.assertNotIn('"kind": "old_descendant"', source)

    def test_listener_owner_and_authenticated_pid_corroborate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / "server.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="ascii")
            os.chmod(pid_file, 0o600)
            identity = activation.process_identity(os.getpid())
            with mock.patch.object(
                activation, "_listener_owners", return_value=(["4242"], [identity])
            ), mock.patch.object(
                activation, "_authenticated_settings_pid", return_value=os.getpid()
            ):
                result = activation._corroborate_old_generation(
                    port=18089,
                    token_path=root / "auth-token",
                    pid_file=pid_file,
                    timeout=1,
                )
            self.assertEqual(result["identity"], identity)
            self.assertEqual(result["socket_inodes"], ["4242"])
            self.assertEqual(result["pid_event"], "pid_file_confirmed")

    def test_dead_stale_pid_file_repairs_to_valid_assist_port_owner(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / "server.pid"
            pid_file.write_text("999999999\n", encoding="ascii")
            os.chmod(pid_file, 0o600)
            identity = activation.process_identity(os.getpid())
            with mock.patch.object(
                activation, "_listener_owners", return_value=(["5252"], [identity])
            ), mock.patch.object(
                activation, "_authenticated_settings_pid", return_value=os.getpid()
            ):
                result = activation._corroborate_old_generation(
                    port=18089,
                    token_path=root / "auth-token",
                    pid_file=pid_file,
                    timeout=1,
                )
            self.assertEqual(result["pid_event"], "pid_file_repaired")
            self.assertEqual(pid_file.read_text(encoding="ascii"), f"{os.getpid()}\n")
            self.assertEqual(pid_file.stat().st_mode & 0o777, 0o600)

    def test_live_pid_mismatch_nonassist_multiple_or_changed_owner_refuses_before_signal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / "server.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="ascii")
            identity = activation.process_identity(os.getpid())
            other = {"pid": os.getpid() + 1, "start_time": "other"}

            cases = (
                ((["1", "2"], [identity]), os.getpid(), None, None),
                ((["1"], [identity, other]), os.getpid(), None, None),
                ((["1"], [identity]), os.getpid() + 1, None, None),
                ((["1"], [identity]), os.getpid(), other, ["1"]),
                ((["1"], [identity]), os.getpid(), identity, ["changed"]),
            )
            for owners, authenticated, expected, inodes in cases:
                with self.subTest(case=(owners, authenticated, expected, inodes)), mock.patch.object(
                    activation, "_listener_owners", return_value=owners
                ), mock.patch.object(
                    activation, "_authenticated_settings_pid", return_value=authenticated
                ), self.assertRaisesRegex(
                    activation.ActivationError, "old_generation_mismatch"
                ):
                    activation._corroborate_old_generation(
                        port=18089,
                        token_path=root / "auth-token",
                        pid_file=pid_file,
                        timeout=1,
                        expected_identity=expected,
                        expected_inodes=inodes,
                        repair_hint=False if expected is not None else True,
                    )

    def test_owner_change_after_freeze_or_before_kill_resumes_old_and_stops_candidate(self):
        source = __import__("inspect").getsource(activation.controller)
        freeze = source.index("_freeze_process_tree(")
        kill = source.index("os.kill(old_pid, signal.SIGKILL)")
        corroborations = [
            index
            for index in range(len(source))
            if source.startswith("_corroborate_old_generation(", index)
        ]
        revalidations = [
            index
            for index in range(len(source))
            if source.startswith("_revalidate_frozen_owner(", index)
        ]
        self.assertEqual(len(corroborations), 1)
        self.assertEqual(len(revalidations), 2)
        self.assertLess(corroborations[0], source.index("subprocess.Popen("))
        self.assertLess(freeze, revalidations[0])
        self.assertLess(revalidations[0], revalidations[1])
        self.assertLess(revalidations[1], kill)
        self.assertIn("signal.SIGCONT", source[source.index("except BaseException"):])
        self.assertIn("candidate.terminate()", source[source.index("except BaseException"):])

    def test_handoff_reader_preserves_coalesced_frames(self):
        left, right = socket.socketpair()
        try:
            right.sendall(b'{"n":1}\n{"n":2}\n')
            self.assertEqual(activation._recv(left, 1), {"n": 1})
            self.assertEqual(activation._recv(left, 1), {"n": 2})
        finally:
            left.close()
            right.close()

    def test_prebind_observer_has_no_application_side_effects(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = root / "observer.json"
            _fixture(fixture)
            control = root / "control"
            control.mkdir(mode=0o700)
            auth_token = root / "auth-token"
            parent, child = socket.socketpair()
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(root / "home"),
                    "XDG_CONFIG_HOME": str(root / "xdg"),
                    "ASSIST_PARK_OBSERVER_FIXTURE": str(fixture),
                    "ASSIST_AUTH_TOKEN_PATH": str(auth_token),
                    "ASSIST_HOME": str(root),
                    "ASSIST_LAUNCH_PROVENANCE_ROOT": str(root / "provenance"),
                }
            )
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "serve.py"), "--park-handoff-fd", str(child.fileno())],
                env=env,
                pass_fds=(child.fileno(),),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            child.close()
            sealed = {
                "schema": activation.SCHEMA,
                "activation_path": activation.ACTIVATION_PATH,
                "old_identity": activation.process_identity(os.getpid()),
                "assist_home": str(ROOT),
                "assist_ctl": str(ROOT / "assist-ctl"),
                "serve_script": str(ROOT / "serve.py"),
                "pid_file": str(root / "server.pid"),
                "log_file": str(root / "server.log"),
                "control_dir": str(control),
                "auth_token": str(auth_token),
                "port": _free_port(),
            }
            try:
                activation._send(parent, sealed)
                ready = activation._recv(parent, 8)
                self.assertEqual(ready["event"], "candidate_ready")
                self.assertFalse(auth_token.exists())
                self.assertEqual(list(control.iterdir()), [])
                self.assertFalse((root / "server.pid").exists())
                self.assertFalse((root / "provenance").exists())
                self.assertFalse(
                    (root / provenance.INITIALIZATION_RECEIPT_NAME).exists()
                )
                source = (ROOT / "serve.py").read_text(encoding="utf-8")
                self.assertLess(
                    source.index("handoff = candidate_prebind"),
                    source.index("app = create_app()", source.index("if __name__ == \"__main__\"")),
                )
            finally:
                parent.close()
                proc.terminate()
                proc.communicate(timeout=5)

    def test_new_ready_freeze_snapshot_kill_bind_order(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = root / "observer.json"
            _fixture(fixture)
            control = root / "control"
            pid_file = root / "server.pid"
            port = _free_port()
            old = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pid_file.write_text(f"{old.pid}\n", encoding="ascii")
            env = {
                "ASSIST_HOME": str(ROOT),
                "ASSIST_ACTIVATION_HOME": str(ROOT),
                "ASSIST_ACTIVATION_CTL": str(ROOT / "assist-ctl"),
                "ASSIST_ACTIVATION_SERVE": str(ROOT / "serve.py"),
                "ASSIST_PID_FILE": str(pid_file),
                "ASSIST_LOG_FILE": str(root / "server.log"),
                "ASSIST_CONTROL_DIR": str(control),
                "ASSIST_AUTH_TOKEN_PATH": str(root / "auth-token"),
                "ASSIST_PORT": str(port),
                "ASSIST_PARK_OBSERVER_FIXTURE": str(fixture),
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "xdg"),
                "ASSIST_LAUNCH_PROVENANCE_ROOT": str(root / "provenance"),
            }
            candidate_pid = None
            try:
                try:
                    with mock.patch.dict(os.environ, env, clear=False):
                        provenance.initialize_epoch(
                            assist_home=ROOT,
                            receipt_path=root / "provenance-init.json",
                            expect_empty=True,
                        )
                        old_identity = activation.process_identity(old.pid)
                        corroborated = {
                            "identity": old_identity,
                            "socket_inodes": ["fixture-socket"],
                            "pid_event": "pid_file_confirmed",
                        }
                        with mock.patch.object(
                            activation,
                            "_corroborate_old_generation",
                            return_value=corroborated,
                        ), mock.patch.object(
                            activation, "_revalidate_frozen_owner", return_value=None
                        ):
                            receipt = activation.controller(timeout=12)
                except Exception as exc:
                    log = (root / "server.log").read_text(encoding="utf-8", errors="replace")
                    self.fail(f"controller failed: {exc}\ncandidate log:\n{log}")
                names = [event["name"] for event in receipt["events"]]
                self.assertEqual(
                    [
                        name
                        for name in names
                        if name
                        in {
                            "candidate_ready",
                            "old_frozen",
                            "snapshot_ack",
                            "old_dead",
                            "candidate_bound",
                        }
                    ],
                    ["candidate_ready", "old_frozen", "snapshot_ack", "old_dead", "candidate_bound"],
                )
                self.assertEqual(receipt["state"], "complete")
                candidate_pid = int(pid_file.read_text(encoding="ascii").strip())
                self.assertTrue(activation.process_start_time(candidate_pid))
                self.assertFalse(activation.process_start_time(old.pid))
            finally:
                if candidate_pid is None:
                    try:
                        saved = json.loads((control / "activation-receipt.json").read_text())
                        candidate_pid = int(saved["candidate_identity"]["pid"])
                    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                        pass
                try:
                    old.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    old.kill()
                    old.wait(timeout=2)
                if candidate_pid and activation.process_start_time(candidate_pid):
                    os.kill(candidate_pid, signal.SIGTERM)
                    deadline = time.monotonic() + 5
                    while activation.process_start_time(candidate_pid) and time.monotonic() < deadline:
                        time.sleep(0.02)
                if candidate_pid:
                    activation.reap_candidate(candidate_pid)

    def test_failure_branch_terminal_ownership(self):
        ownership = activation.ActivationOwnership()
        self.assertEqual(ownership.failure_owner(), "old_running")
        ownership.candidate_ready()
        self.assertEqual(ownership.failure_owner(), "old_running")
        ownership.old_frozen()
        self.assertEqual(ownership.failure_owner(), "resume_exact_old")
        ownership.snapshot_ack()
        self.assertEqual(ownership.failure_owner(), "resume_exact_old")
        ownership.old_terminated()
        self.assertEqual(ownership.failure_owner(), "candidate_resume_only")
        ownership.candidate_bound(resumed=True)
        self.assertEqual(ownership.state, "complete")

        with self.assertRaises(activation.ActivationError):
            activation.ActivationOwnership().old_terminated()
        self.assertFalse(activation.identity_alive({"pid": os.getpid(), "start_time": "reused"}))
