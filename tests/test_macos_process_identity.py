"""macOS process identity must gate real pane delivery without requiring /proc."""

import ctypes
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest import mock

from flask import Flask

from routes import input as input_routes
from shared import agent_identity, launch_provenance, park_activation, tmux


def _bsdinfo_response(pid, flavor, arg, buffer, size, *, status=2, micros=123456,
                      seconds=1788880000, returned_pid=None, returned_size=136):
    """Independent byte fixture for Apple's public 136-byte proc_bsdinfo ABI.

    Do not build this with the implementation's ctypes structure: that would
    hide an incorrect field offset or width in the native reader.
    """
    if (flavor, arg, size) != (3, 0, 136):
        raise AssertionError((flavor, arg, size))
    payload = bytearray(136)
    struct.pack_into("=5I", payload, 0, 0, status, 0,
                     pid if returned_pid is None else returned_pid, 42)
    struct.pack_into("=16s32s", payload, 48, b"tmux: server", b"process with spaces")
    struct.pack_into("=QQ", payload, 120, seconds, micros)
    ctypes.memmove(buffer, bytes(payload), size)
    return returned_size


class DarwinProcessReaderTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(agent_identity, "_IS_MAC", True))
        self.proc_open = self.enterContext(mock.patch.object(
            agent_identity, "open", side_effect=FileNotFoundError("macOS has no /proc"), create=True
        ))
        agent_identity._darwin_pidinfo.cache_clear()
        self.addCleanup(agent_identity._darwin_pidinfo.cache_clear)
        self.pidinfo = mock.Mock(side_effect=_bsdinfo_response)
        self.loader = self.enterContext(mock.patch.object(
            ctypes, "CDLL", return_value=mock.Mock(proc_pidinfo=self.pidinfo)
        ))

    def test_all_generation_readers_use_native_start_time_without_proc(self):
        self.assertEqual(agent_identity._stat_fields(123), ("R", 42, "darwin:1788880000.123456"))
        for read in (tmux._process_start_time, launch_provenance._process_start_time,
                     park_activation.process_start_time):
            with self.subTest(reader=read.__module__):
                self.assertEqual(read(123), "darwin:1788880000.123456")
        self.proc_open.assert_not_called()
        self.loader.assert_called_once_with("/usr/lib/libproc.dylib", use_errno=True)
        self.assertEqual(self.pidinfo.argtypes,
                         [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int])
        self.assertIs(self.pidinfo.restype, ctypes.c_int)

    def test_same_pid_reused_within_one_second_has_a_different_identity(self):
        first = tmux._process_start_time(123)
        self.pidinfo.side_effect = partial(_bsdinfo_response, micros=123457)
        self.assertNotEqual(tmux._process_start_time(123), first)

    def test_zombies_missing_short_and_invalid_observations_fail_closed(self):
        cases = (
            {"status": 5}, {"returned_size": 0}, {"returned_size": 135},
            {"returned_size": 144}, {"returned_pid": 999}, {"seconds": 0},
            {"micros": 1000000}, {"status": 0},
        )
        for fields in cases:
            self.pidinfo.side_effect = partial(_bsdinfo_response, **fields)
            for read in (tmux._process_start_time, launch_provenance._process_start_time,
                         park_activation.process_start_time):
                with self.subTest(fields=fields, reader=read.__module__):
                    self.assertIsNone(read(123))

    def test_missing_native_library_or_symbol_fails_closed(self):
        self.loader.side_effect = OSError("libproc unavailable")
        self.assertIsNone(tmux._process_start_time(123))
        self.loader.side_effect = None
        self.loader.return_value = object()
        self.assertIsNone(tmux._process_start_time(123))

    def test_invalid_pid_never_reaches_native_code(self):
        for pid in (0, -1, 2**31, "invalid"):
            with self.subTest(pid=pid):
                self.assertIsNone(tmux._process_start_time(pid))
        self.pidinfo.assert_not_called()

    def test_linux_keeps_its_existing_start_ticks_and_does_not_load_libproc(self):
        fields = ["S", "42", *("0" for _ in range(17)), "987654", "0"]
        with mock.patch.object(agent_identity, "_IS_MAC", False), mock.patch.object(
            agent_identity, "open", mock.mock_open(read_data=f"123 (tmux: server) {' '.join(fields)}\n")
        ):
            self.assertEqual(tmux._process_start_time(123), "987654")
        self.loader.assert_not_called()


class ExistingMacPaneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="t561-")))
        self.socket = self.root / "private.sock"
        self.enterContext(mock.patch.dict(os.environ, {
            "TMUX": f"{self.socket},0,0",
            "ASSIST_HOME": str(self.root),
            "ASSIST_LAUNCH_PROVENANCE_ROOT": str(self.root / "registry"),
        }))
        self.run_tmux("-f", "/dev/null", "new-session", "-d", "-s", "t561-existing", "cat")
        self.addCleanup(self.run_tmux, "kill-server", check=False)
        self.target = self.run_tmux(
            "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"
        ).stdout.strip()
        self.pane_id = self.run_tmux("display-message", "-p", "-t", self.target, "#{pane_id}").stdout.strip()
        self.enterContext(mock.patch.object(agent_identity, "_IS_MAC", True))
        self.enterContext(mock.patch.object(
            agent_identity, "open", side_effect=FileNotFoundError("macOS has no /proc"), create=True
        ))
        # Only the OS call is simulated. Decoding, pane/socket observation,
        # control clients, routes and generation comparisons are real.
        agent_identity._darwin_pidinfo.cache_clear()
        self.addCleanup(agent_identity._darwin_pidinfo.cache_clear)
        self.native = mock.Mock(side_effect=_bsdinfo_response)
        self.enterContext(mock.patch.object(
            ctypes, "CDLL", return_value=mock.Mock(proc_pidinfo=self.native)
        ))
        self.enterContext(mock.patch.object(input_routes.state, "touch_activity"))
        self.app = Flask(__name__)
        self.app.register_blueprint(input_routes.input_bp)

    def run_tmux(self, *args, check=True):
        return subprocess.run(
            ["tmux", "-S", str(self.socket), *args], capture_output=True,
            text=True, check=check, timeout=5,
        )

    def test_existing_unregistered_pane_sends_without_proc(self):
        # A pull/restart creates an empty epoch around panes that already exist.
        # Neither that initialization nor ordinary sends may adopt those panes.
        self.assertTrue(launch_provenance.initialize_for_startup())
        self.assertFalse(launch_provenance.initialize_for_startup())
        for target in (self.target, self.pane_id):
            with self.subTest(target=target):
                response = self.app.test_client().post("/type", json={
                    "target": target, "text": "T561-DELIVERED", "enter": True, "no_history": True,
                })
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertIn("T561-DELIVERED", self.run_tmux("capture-pane", "-p", "-t", target).stdout)
                response = self.app.test_client().post("/key", json={"target": target, "keys": "ctrl+u"})
                self.assertEqual(response.status_code, 200, response.get_json())
        snapshot = launch_provenance.LaunchProvenanceStore().snapshot()
        self.assertEqual(snapshot["origins"], [])
        self.assertEqual(snapshot["events"], [])

    def test_unavailable_process_identity_still_refuses_delivery(self):
        self.native.side_effect = ProcessLookupError("process exited")
        response = self.app.test_client().post("/type", json={
            "target": self.target, "text": "T561-MUST-NOT-LAND", "enter": True, "no_history": True,
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json(), {"ok": False, "error": "target_absent"})
        self.assertNotIn("T561-MUST-NOT-LAND", self.run_tmux("capture-pane", "-p", "-t", self.target).stdout)

    def test_delivery_rejects_process_replacement_within_the_same_second(self):
        expected = tmux.expected_target_identity(self.target)
        self.assertIsNotNone(expected)
        self.native.side_effect = partial(_bsdinfo_response, micros=123457)
        result = tmux.generation_bound_delivery(expected, text="T561-MUST-NOT-LAND", enter=True)
        self.assertEqual(result.status, "target_absent")
        self.assertNotIn("T561-MUST-NOT-LAND", self.run_tmux("capture-pane", "-p", "-t", self.target).stdout)

    def test_missing_or_corrupt_registry_does_not_gate_operator_input(self):
        for registry_state in ("missing", "corrupt"):
            with self.subTest(registry_state=registry_state):
                if registry_state == "corrupt":
                    launch_provenance.initialize_for_startup()
                    (self.root / "registry/epoch.json").write_text("{broken")
                with self.assertRaises(launch_provenance.ProvenanceError):
                    launch_provenance.LaunchProvenanceStore().snapshot()
                response = self.app.test_client().post("/type", json={
                    "target": self.target, "text": "T561-INDEPENDENT", "enter": True, "no_history": True,
                })
                self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(list((self.root / "registry/origins").iterdir()), [])
        self.assertEqual(list((self.root / "registry/events").iterdir()), [])

    def test_creation_and_adoption_record_native_creator_and_pane_identity(self):
        launch_provenance.initialize_for_startup()
        created = tmux.create_tmux_session(
            session_name="t561-created", cwd=self.root, cols=80, rows=24, surface="fresh_terminal"
        )
        self.assertTrue(created.ok, created)
        adopted = tmux.record_tmux_adoption(self.target, surface="existing_terminal")
        self.assertTrue(adopted.ok, adopted)
        snapshot = launch_provenance.LaunchProvenanceStore().snapshot()
        self.assertEqual({origin["origin"] for origin in snapshot["origins"]}, {"created", "adopted"})
        for origin in snapshot["origins"]:
            self.assertEqual(origin["creator"]["start_time"], "darwin:1788880000.123456")
            self.assertEqual(origin["identity"]["pane_start_time"], "darwin:1788880000.123456")


@unittest.skipUnless(sys.platform == "darwin", "requires macOS libproc (Linux uses ABI fixtures)")
class NativeDarwinProcessTests(unittest.TestCase):
    def test_real_child_identity_is_stable_then_absent_after_exit(self):
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as child:
            try:
                first = agent_identity._stat_fields(child.pid)
                self.assertNotEqual(first[0], "Z")
                self.assertEqual(first[1], os.getpid())
                self.assertRegex(first[2], r"^darwin:\d+\.\d{6}$")
                self.assertEqual(agent_identity._stat_fields(child.pid)[2], first[2])
            finally:
                child.terminate()
                child.wait(timeout=5)
            self.assertIsNone(tmux._process_start_time(child.pid))


if __name__ == "__main__":
    unittest.main()
