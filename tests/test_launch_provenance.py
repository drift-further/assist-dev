import fcntl
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared import launch_provenance as provenance
from shared import park_activation as activation
from shared import tmux
from routes import automate, commands, git, terminal


ROOT = Path(__file__).resolve().parent.parent

# The socket tmux uses when nothing scopes it — i.e. the developer's REAL server.
AMBIENT_TMUX_SOCKET = Path(f"/tmp/tmux-{os.getuid()}/default")


def _sandbox_tmux_socket() -> Path:
    """Resolve this test's throwaway tmux socket, refusing the ambient server.

    Every tmux invocation in this module must be scoped to the temporary
    TMUX_TMPDIR the individual test builds. A bare `tmux kill-server` inherits
    the ambient environment and destroys the developer's real tmux server and
    every pane running in it. On 2026-08-28 the box lost 28 live panes to a
    memory event and the first suspect was exactly this shape of call, which is
    why the safety is now structural: the socket is passed explicitly with -S,
    and resolving it fails loudly if the sandbox is missing. Do not "simplify"
    this back to a bare ["tmux", ...] argv.
    """
    raw = os.environ.get("TMUX_TMPDIR", "")
    if not raw:
        raise AssertionError(
            "TMUX_TMPDIR is unset — refusing to run tmux against the ambient server"
        )
    # Mirror shared.tmux._socket_path() exactly, so -S addresses the very same
    # server the code under test resolves from TMUX_TMPDIR.
    directory = Path(raw) / f"tmux-{os.getuid()}"
    # tmux creates this directory itself when it derives the path from
    # TMUX_TMPDIR, but NOT when the socket is handed to it via -S. Creating it
    # here is what keeps -S behaviourally identical to the ambient form.
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket = (directory / "default").resolve()
    if socket == AMBIENT_TMUX_SOCKET.resolve():
        raise AssertionError(
            f"TMUX_TMPDIR={raw!r} resolves to the ambient tmux socket "
            f"({AMBIENT_TMUX_SOCKET}) — refusing to operate on the real server"
        )
    return socket


def _kill_sandbox_tmux_server() -> None:
    """Tear down only the sandbox server this test started."""
    subprocess.run(
        ["tmux", "-S", str(_sandbox_tmux_socket()), "kill-server"],
        check=False,
        capture_output=True,
        timeout=5,
    )


class LaunchProvenanceTests(unittest.TestCase):
    @staticmethod
    def _run_tmux(*arguments):
        return subprocess.run(
            ["tmux", "-S", str(_sandbox_tmux_socket()), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )

    @staticmethod
    def _write_json(path, payload):
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    @staticmethod
    def _identity(marker):
        return tmux.ExpectedTargetIdentity(
            socket_path=f"/private/tmux/{marker}",
            socket_device=100,
            socket_inode=200 + marker,
            server_pid=300,
            server_start_time="400",
            session_id=f"${marker}",
            window_id=f"@{marker}",
            pane_id=f"%{marker}",
            pane_pid=500 + marker,
            pane_start_time=str(600 + marker),
        )

    @staticmethod
    def _initialize(root):
        home = root / "assist-home"
        home.mkdir(mode=0o700)
        registry_root = home / provenance.ROOT_NAME
        receipt = root / "initialization.json"
        with mock.patch.dict(
            os.environ,
            {"ASSIST_LAUNCH_PROVENANCE_ROOT": str(registry_root)},
            clear=False,
        ):
            provenance.initialize_epoch(
                assist_home=home,
                receipt_path=receipt,
                expect_empty=True,
            )
        return provenance.LaunchProvenanceStore(
            assist_home=home,
            root=registry_root,
            lock_timeout=0.1,
        ), receipt

    def test_initialize_epoch_and_verify_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "assist-home"
            home.mkdir(mode=0o700)
            registry_root = home / provenance.ROOT_NAME
            receipt = root / "initialization.json"
            environment = os.environ.copy()
            environment.update(
                {
                    "ASSIST_LAUNCH_PROVENANCE_ROOT": str(registry_root),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                }
            )
            initialize = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "shared.launch_provenance",
                    "initialize",
                    "--assist-home",
                    str(home),
                    "--expect-empty",
                    "--receipt",
                    str(receipt),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(initialize.returncode, 0, initialize.stderr)
            self.assertEqual(initialize.stdout, "")
            self.assertEqual(initialize.stderr, "")
            verify = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "shared.launch_provenance",
                    "verify-initialization",
                    "--assist-home",
                    str(home),
                    "--receipt",
                    str(receipt),
                    "--require-empty",
                    "--require-coverage",
                    provenance.COVERAGE,
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertEqual(verify.stdout, "")
            self.assertEqual(verify.stderr, "")
            self.assertEqual(registry_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((registry_root / "origins").stat().st_mode & 0o777, 0o700)
            self.assertEqual((registry_root / "events").stat().st_mode & 0o777, 0o700)
            self.assertEqual((registry_root / "epoch.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((registry_root / "registry.lock").stat().st_mode & 0o777, 0o600)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            repeat = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "shared.launch_provenance",
                    "initialize",
                    "--assist-home",
                    str(home),
                    "--expect-empty",
                    "--receipt",
                    str(receipt),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(repeat.returncode, 2)
            self.assertIn("provenance_already_initialized", repeat.stderr)

    def test_origin_no_replace_and_adoption_never_upgrades(self):
        with tempfile.TemporaryDirectory() as raw:
            store, _receipt = self._initialize(Path(raw))
            adopted_identity = self._identity(1)
            first = store.record_adoption(
                adopted_identity,
                surface="existing_terminal",
                diagnostic_alias="same-name:0.0",
            )
            self.assertEqual(first["origin"]["origin"], "adopted")
            with store.locked() as registry:
                with self.assertRaisesRegex(
                    provenance.ProvenanceError, "provenance_origin_already_recorded"
                ):
                    registry.record_created(
                        adopted_identity,
                        surface="fresh_terminal",
                        diagnostic_alias="same-name:0.0",
                    )
            second = store.record_adoption(
                adopted_identity,
                surface="automate_reconnect",
                diagnostic_alias="renamed:0.0",
            )
            self.assertEqual(second["origin"]["origin"], "adopted")

            created_identity = self._identity(2)
            with store.locked() as registry:
                created = registry.record_created(
                    created_identity,
                    surface="fresh_terminal",
                    diagnostic_alias="same-name:0.0",
                )
            self.assertEqual(created["origin"], "created")
            adopted_created = store.record_adoption(
                created_identity,
                surface="startup_recovery",
                diagnostic_alias="later-name:0.0",
            )
            self.assertEqual(adopted_created["origin"]["origin"], "created")
            with store.locked() as registry:
                with self.assertRaisesRegex(
                    provenance.ProvenanceError, "provenance_origin_already_recorded"
                ):
                    registry.record_created(
                        created_identity,
                        surface="duplicate",
                        diagnostic_alias="later-name:0.0",
                    )
            snapshot = store.snapshot()
            origins = {
                item["identity_digest"]: item["origin"]
                for item in snapshot["origins"]
            }
            self.assertEqual(
                origins[provenance.identity_digest(adopted_identity)], "adopted"
            )
            self.assertEqual(
                origins[provenance.identity_digest(created_identity)], "created"
            )
            self.assertEqual(len(snapshot["events"]), 3)

    def test_missing_corrupt_symlink_mode_and_lock_timeout_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "missing-home"
            home.mkdir(mode=0o700)
            missing = provenance.LaunchProvenanceStore(
                assist_home=home,
                root=home / provenance.ROOT_NAME,
            )
            with self.assertRaisesRegex(
                provenance.ProvenanceError, "provenance_state_missing"
            ):
                missing.snapshot()

        with tempfile.TemporaryDirectory() as raw:
            store, _receipt = self._initialize(Path(raw))
            store.epoch_path.write_text("{broken\n", encoding="utf-8")
            store.epoch_path.chmod(0o600)
            with self.assertRaisesRegex(
                provenance.ProvenanceError, "provenance_state_corrupt"
            ):
                store.snapshot()

        with tempfile.TemporaryDirectory() as raw:
            store, _receipt = self._initialize(Path(raw))
            real = Path(raw) / "real-origins"
            real.mkdir(mode=0o700)
            store.origins.rmdir()
            store.origins.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(
                provenance.ProvenanceError, "provenance_state_type"
            ):
                store.snapshot()

        with tempfile.TemporaryDirectory() as raw:
            store, _receipt = self._initialize(Path(raw))
            store.epoch_path.chmod(0o644)
            with self.assertRaisesRegex(
                provenance.ProvenanceError, "provenance_state_mode"
            ):
                store.snapshot()

        with tempfile.TemporaryDirectory() as raw:
            store, _receipt = self._initialize(Path(raw))
            lock_fd = os.open(store.lock_path, os.O_RDWR)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(
                    provenance.ProvenanceError, "provenance_lock_timeout"
                ):
                    store.snapshot(timeout=0.03)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def test_origin_and_event_durability_crash_matrix(self):
        with tempfile.TemporaryDirectory() as raw:
            store, _receipt = self._initialize(Path(raw))
            before_origin = self._identity(10)
            with store.locked() as registry, mock.patch.object(
                provenance.os, "fsync", side_effect=OSError("before publication")
            ):
                with self.assertRaisesRegex(
                    provenance.ProvenanceError, "provenance_publish_failed"
                ):
                    registry.record_created(
                        before_origin,
                        surface="fresh_terminal",
                        diagnostic_alias="before:0.0",
                    )
            self.assertFalse(
                (store.origins / f"{provenance.identity_digest(before_origin)}.json").exists()
            )

            after_origin = self._identity(11)
            fsync_calls = 0
            real_fsync = os.fsync

            def fail_origin_parent(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("after publication")
                return real_fsync(fd)

            with store.locked() as registry, mock.patch.object(
                provenance.os, "fsync", side_effect=fail_origin_parent
            ):
                with self.assertRaisesRegex(
                    provenance.ProvenanceError, "provenance_publish_failed"
                ):
                    registry.record_created(
                        after_origin,
                        surface="fresh_terminal",
                        diagnostic_alias="after:0.0",
                    )
            snapshot = store.snapshot()
            self.assertEqual(len(snapshot["origins"]), 1)
            self.assertEqual(snapshot["origins"][0]["identity"], after_origin.as_dict())

            before_events = len(snapshot["events"])
            with mock.patch.object(
                provenance.os, "fsync", side_effect=OSError("before event publication")
            ):
                with self.assertRaisesRegex(
                    provenance.ProvenanceError, "provenance_publish_failed"
                ):
                    store.record_adoption(
                        after_origin,
                        surface="startup_recovery",
                        diagnostic_alias="after:0.0",
                    )
            self.assertEqual(len(store.snapshot()["events"]), before_events)

            fsync_calls = 0

            def fail_event_parent(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("after event publication")
                return real_fsync(fd)

            with mock.patch.object(
                provenance.os, "fsync", side_effect=fail_event_parent
            ):
                with self.assertRaisesRegex(
                    provenance.ProvenanceError, "provenance_publish_failed"
                ):
                    store.record_adoption(
                        after_origin,
                        surface="automate_reconnect",
                        diagnostic_alias="after:0.0",
                    )
            self.assertEqual(len(store.snapshot()["events"]), before_events + 1)

    def test_gitignore_contains_runtime_root_and_startup_receipt(self):
        entries = [
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        matching = [line for line in entries if "assist-launch-provenance-v1" in line]
        self.assertEqual(
            matching,
            [
                "/.assist-launch-provenance-v1/",
                "/.assist-launch-provenance-v1-initialization.json",
            ],
        )

    def test_normal_startup_initializes_empty_home_once_and_creation_succeeds(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "assist-home"
            home.mkdir(mode=0o700)
            tmux_root = root / "tmux"
            tmux_root.mkdir(mode=0o700)
            registry_root = home / provenance.ROOT_NAME
            environment = {
                "ASSIST_HOME": str(home),
                "ASSIST_LAUNCH_PROVENANCE_ROOT": str(registry_root),
                "TMUX": "",
                "TMUX_TMPDIR": str(tmux_root),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                self.assertTrue(provenance.initialize_for_startup())
                receipt = home / provenance.INITIALIZATION_RECEIPT_NAME
                provenance.verify_initialization(
                    assist_home=home,
                    receipt_path=receipt,
                    require_empty=True,
                    require_coverage=provenance.COVERAGE,
                )
                epoch_before = (registry_root / "epoch.json").read_bytes()
                receipt_before = receipt.read_bytes()
                self.assertFalse(provenance.initialize_for_startup())
                self.assertEqual((registry_root / "epoch.json").read_bytes(), epoch_before)
                self.assertEqual(receipt.read_bytes(), receipt_before)
                try:
                    created = tmux.create_tmux_session(
                        session_name="fresh-startup",
                        cwd=root,
                        cols=80,
                        rows=60,
                        surface="fresh_terminal",
                    )
                    self.assertTrue(created.ok, created)
                    snapshot = provenance.LaunchProvenanceStore().snapshot()
                    self.assertEqual(len(snapshot["origins"]), 1)
                    self.assertEqual(snapshot["origins"][0]["origin"], "created")
                finally:
                    _kill_sandbox_tmux_server()

    def test_normal_startup_does_not_reinitialize_an_existing_corrupt_store(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            registry_root = home / provenance.ROOT_NAME
            registry_root.mkdir(mode=0o700)
            with mock.patch.dict(
                os.environ,
                {
                    "ASSIST_HOME": str(home),
                    "ASSIST_LAUNCH_PROVENANCE_ROOT": str(registry_root),
                },
                clear=False,
            ), mock.patch.object(provenance, "initialize_epoch") as initialize:
                self.assertFalse(provenance.initialize_for_startup())
                initialize.assert_not_called()
                with self.assertRaisesRegex(
                    provenance.ProvenanceError, "provenance_state_missing"
                ):
                    provenance.LaunchProvenanceStore().snapshot()

    def test_direct_server_start_initializes_only_the_non_handoff_path(self):
        source = (ROOT / "serve.py").read_text(encoding="utf-8")
        main = source[source.index('if __name__ == "__main__":') :]
        guard = main.index("if args.park_handoff_fd is None:")
        initialize = main.index("initialize_for_startup()", guard)
        create_app = main.index("app = create_app()")
        self.assertLess(guard, initialize)
        self.assertLess(initialize, create_app)
        handoff_branch = main[
            main.index("if args.park_handoff_fd is not None:") : guard
        ]
        self.assertNotIn("initialize_for_startup", handoff_branch)

    def test_all_six_creation_surfaces_receipt_before_delivery_or_success(self):
        surfaces = (
            (terminal._terminal_launch_effect, "fresh_terminal"),
            (terminal._terminal_duplicate_effect, "duplicate"),
            (commands._run_command_effect, "saved_command_split"),
            (git._git_run_effect, "temporary_git"),
            (automate._automate_start_inner, "automate_start"),
            (automate._automate_relaunch_effect, "automate_hard_relaunch"),
        )
        for function, surface in surfaces:
            with self.subTest(surface=surface):
                source = inspect.getsource(function)
                if surface == "saved_command_split":
                    receipt = source.index("create_tmux_split(")
                else:
                    receipt = source.index("create_tmux_session(")
                self.assertIn(f'surface="{surface}"', source)
                delivery = source.find("tmux_send_text(", receipt)
                if delivery >= 0:
                    self.assertLess(receipt, delivery)
                success = source.rfind('"ok": True')
                if success >= 0:
                    self.assertLess(receipt, success)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "assist-home"
            home.mkdir(mode=0o700)
            tmux_root = root / "tmux"
            tmux_root.mkdir(mode=0o700)
            registry_root = home / provenance.ROOT_NAME
            environment = {
                "ASSIST_HOME": str(home),
                "ASSIST_LAUNCH_PROVENANCE_ROOT": str(registry_root),
                "TMUX": "",
                "TMUX_TMPDIR": str(tmux_root),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                provenance.initialize_epoch(
                    assist_home=home,
                    receipt_path=root / "init.json",
                    expect_empty=True,
                )
                try:
                    created = tmux.create_tmux_session(
                        session_name="receipt-first",
                        cwd=root,
                        cols=80,
                        rows=60,
                        surface="fresh_terminal",
                    )
                    self.assertTrue(created.ok)
                    snapshot = provenance.LaunchProvenanceStore().snapshot()
                    self.assertEqual(len(snapshot["origins"]), 1)
                    self.assertEqual(snapshot["origins"][0]["origin"], "created")
                    self.assertEqual(
                        snapshot["origins"][0]["identity"], created.identity.as_dict()
                    )
                finally:
                    _kill_sandbox_tmux_server()

    def test_existing_terminal_reconnect_and_recovery_are_adoptions(self):
        checks = (
            (terminal._existing_terminal_effect, "existing_terminal", "state.tmux_target"),
            (automate.automate_reconnect, "automate_reconnect", "_automate_save()"),
            (
                automate.automate_recover,
                "automate_startup_recovery",
                "state.automate.update(",
            ),
        )
        for function, surface, later_effect in checks:
            with self.subTest(surface=surface):
                source = inspect.getsource(function)
                adoption = source.index("record_tmux_adoption(")
                self.assertIn(f'surface="{surface}"', source)
                self.assertLess(adoption, source.index(later_effect, adoption))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "assist-home"
            home.mkdir(mode=0o700)
            tmux_root = root / "tmux"
            tmux_root.mkdir(mode=0o700)
            registry_root = home / provenance.ROOT_NAME
            with mock.patch.dict(
                os.environ,
                {
                    "ASSIST_HOME": str(home),
                    "ASSIST_LAUNCH_PROVENANCE_ROOT": str(registry_root),
                    "TMUX": "",
                    "TMUX_TMPDIR": str(tmux_root),
                },
                clear=False,
            ):
                provenance.initialize_epoch(
                    assist_home=home,
                    receipt_path=root / "init.json",
                    expect_empty=True,
                )
                try:
                    self._run_tmux("new-session", "-d", "-s", "adopt-me")
                    adopted = tmux.record_tmux_adoption(
                        "adopt-me:0.0", surface="existing_terminal"
                    )
                    self.assertTrue(adopted.ok)
                    tmux.record_tmux_adoption(
                        "adopt-me:0.0", surface="automate_reconnect"
                    )
                    snapshot = provenance.LaunchProvenanceStore().snapshot()
                    self.assertEqual(len(snapshot["origins"]), 1)
                    self.assertEqual(snapshot["origins"][0]["origin"], "adopted")
                    self.assertEqual(len(snapshot["events"]), 2)
                finally:
                    _kill_sandbox_tmux_server()

    def test_record_failure_cleans_exact_created_generation_and_never_succeeds(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "assist-home"
            home.mkdir(mode=0o700)
            tmux_root = root / "tmux"
            tmux_root.mkdir(mode=0o700)
            registry_root = home / provenance.ROOT_NAME
            with mock.patch.dict(
                os.environ,
                {
                    "ASSIST_HOME": str(home),
                    "ASSIST_LAUNCH_PROVENANCE_ROOT": str(registry_root),
                    "TMUX": "",
                    "TMUX_TMPDIR": str(tmux_root),
                },
                clear=False,
            ):
                provenance.initialize_epoch(
                    assist_home=home,
                    receipt_path=root / "init.json",
                    expect_empty=True,
                )
                try:
                    self._run_tmux("new-session", "-d", "-s", "keep-me")
                    with mock.patch.object(
                        provenance.LockedRegistry,
                        "record_created",
                        side_effect=provenance.ProvenanceError("injected_failure"),
                    ):
                        failed = tmux.create_tmux_session(
                            session_name="remove-me",
                            cwd=root,
                            cols=80,
                            rows=60,
                            surface="fresh_terminal",
                        )
                    self.assertEqual(failed.status, "provenance_record_failed")
                    self.assertTrue(failed.cleanup_succeeded)
                    self.assertEqual(
                        self._run_tmux("has-session", "-t", "=keep-me").returncode, 0
                    )
                    absent = subprocess.run(
                        ["tmux", "has-session", "-t", "=remove-me"],
                        capture_output=True,
                        timeout=5,
                    )
                    self.assertNotEqual(absent.returncode, 0)

                    with mock.patch.object(
                        provenance.LockedRegistry,
                        "record_created",
                        side_effect=provenance.ProvenanceError("injected_failure"),
                    ), mock.patch.object(
                        tmux, "_cleanup_exact_created", return_value=False
                    ):
                        cleanup_failed = tmux.create_tmux_session(
                            session_name="cleanup-fails",
                            cwd=root,
                            cols=80,
                            rows=60,
                            surface="fresh_terminal",
                        )
                    self.assertEqual(cleanup_failed.status, "provenance_record_failed")
                    self.assertFalse(cleanup_failed.ok)
                    self.assertFalse(cleanup_failed.cleanup_succeeded)
                finally:
                    _kill_sandbox_tmux_server()

    def test_rename_name_reuse_pane_pid_reuse_and_server_restart_do_not_transfer_origin(self):
        with tempfile.TemporaryDirectory() as raw:
            store, _receipt = self._initialize(Path(raw))
            original = self._identity(30)
            with store.locked() as registry:
                registry.record_created(
                    original,
                    surface="fresh_terminal",
                    diagnostic_alias="colliding:0.0",
                )
            sealed = store.snapshot()

            def classify(identity, alias="colliding:0.0"):
                observer = activation.ExecutionObserver(
                    old_identity={"pid": 999999999, "start_time": "absent"},
                    tmux_probe=lambda: {
                        "reachable": True,
                        "rows": [{"identity": identity, "alias": alias}],
                    },
                    docker_probe=lambda: {"reachable": True, "rows": []},
                    provenance_snapshot=sealed,
                )
                observer.ready()
                return observer.snapshot()

            renamed = classify(original.as_dict(), "renamed:7.4")
            self.assertEqual(len(renamed["owned_units"]), 1)
            self.assertEqual(renamed["owned_units"][0]["alias"], "renamed:7.4")

            for field, replacement in (
                ("session_id", "$999"),
                ("pane_id", "%999"),
                ("pane_pid", 999001),
                ("pane_start_time", "reused"),
                ("server_pid", 999002),
                ("server_start_time", "restarted"),
                ("socket_inode", 999003),
            ):
                with self.subTest(field=field):
                    changed = original.as_dict()
                    changed[field] = replacement
                    snapshot = classify(changed)
                    self.assertEqual(snapshot["owned_units"], [])
                    self.assertEqual(len(snapshot["ambient_unowned"]), 1)
                    self.assertEqual(len(snapshot["recorded_absent"]), 1)

            pre_epoch = classify(self._identity(31).as_dict(), "same-text:0.0")
            self.assertEqual(pre_epoch["owned_units"], [])
            self.assertEqual(len(pre_epoch["ambient_unowned"]), 1)

    def test_bad_registry_or_incomplete_live_identity_refuses_activation(self):
        with tempfile.TemporaryDirectory() as raw:
            store, _receipt = self._initialize(Path(raw))
            sealed = store.snapshot()
            identity = self._identity(40).as_dict()
            corrupt = dict(sealed)
            corrupt["coverage"] = "invented"
            observer = activation.ExecutionObserver(
                old_identity={"pid": 999999999, "start_time": "absent"},
                tmux_probe=lambda: {
                    "reachable": True,
                    "rows": [{"identity": identity, "alias": "x:0.0"}],
                },
                docker_probe=lambda: {"reachable": True, "rows": []},
                provenance_snapshot=corrupt,
            )
            observer.ready()
            with self.assertRaisesRegex(
                activation.ActivationError, "provenance_registry_invalid"
            ):
                observer.snapshot()

            incomplete = dict(identity)
            incomplete.pop("pane_start_time")
            observer = activation.ExecutionObserver(
                old_identity={"pid": 999999999, "start_time": "absent"},
                tmux_probe=lambda: {
                    "reachable": True,
                    "rows": [{"identity": incomplete, "alias": "x:0.0"}],
                },
                docker_probe=lambda: {"reachable": True, "rows": []},
                provenance_snapshot=sealed,
            )
            observer.ready()
            with self.assertRaisesRegex(
                activation.ActivationError, "tmux_identity_incomplete"
            ):
                observer.snapshot()

    def test_populated_ambient_server_counts_only_recorded_created_units(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tmux_root = root / "tmux"
            tmux_root.mkdir(mode=0o700)
            provenance_root = root / "provenance"

            environment = {
                "ASSIST_HOME": str(ROOT),
                "TMUX": "",
                "TMUX_TMPDIR": str(tmux_root),
                "ASSIST_LAUNCH_PROVENANCE_ROOT": str(provenance_root),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                try:
                    provenance.initialize_epoch(
                        assist_home=ROOT,
                        receipt_path=root / "init.json",
                        expect_empty=True,
                    )
                    for index in range(6):
                        self._run_tmux(
                            "new-session",
                            "-d",
                            "-s",
                            f"denominator-{index}",
                            "-c",
                            str(root),
                            "/bin/sh",
                            "-c",
                            "exec sleep 120",
                        )

                    for index in (6, 7):
                        created = tmux.create_tmux_session(
                            session_name=f"denominator-{index}",
                            cwd=root,
                            cols=80,
                            rows=60,
                            surface="fresh_terminal",
                            diagnostic_alias=f"denominator-{index}:0.0",
                        )
                        self.assertTrue(created.ok)

                    observer = activation.ExecutionObserver(
                        old_identity={"pid": 999999999, "start_time": "absent"},
                        docker_probe=lambda: {"reachable": True, "rows": []},
                    )
                    observer.ready()
                    snapshot = observer.snapshot()
                    self.assertEqual(len(snapshot["tmux"]["rows"]), 8)
                    self.assertEqual(len(snapshot["units"]), 2)
                finally:
                    _kill_sandbox_tmux_server()
