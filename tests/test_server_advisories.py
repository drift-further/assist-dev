"""Regression coverage for server-side public-readiness hardening."""

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

import shared.state as state
from routes import poll
from shared import agent_identity, launch_provenance, park_activation, tmux


ROOT = Path(__file__).resolve().parents[1]


class StartupAndHealthTests(unittest.TestCase):
    def test_control_script_sets_private_umask_before_log_and_pid_writes(self):
        source = (ROOT / "assist-ctl").read_text(encoding="utf-8")
        private_umask = source.index("umask 077")
        self.assertLess(private_umask, source.index('>> "$LOG_FILE"'))
        self.assertLess(private_umask, source.index('> "$PID_FILE"'))

    def test_health_body_carries_only_liveness(self):
        app = Flask(__name__)
        app.register_blueprint(poll.poll_bp)
        with mock.patch.object(state, "tmux_target", "private-session:0.0"):
            response = app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})


class AtomicStateTests(unittest.TestCase):
    def test_concurrent_atomic_writes_use_distinct_private_temp_files(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "state.json"
            barrier = threading.Barrier(2)
            real_dump = state.json.dump
            errors = []

            def synchronized_dump(*args, **kwargs):
                barrier.wait(timeout=2)
                return real_dump(*args, **kwargs)

            def write(value):
                try:
                    state.atomic_write_json(target, {"value": value})
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(state.json, "dump", side_effect=synchronized_dump):
                threads = [threading.Thread(target=write, args=(value,)) for value in (1, 2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(3)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertIn(
                json.loads(target.read_text(encoding="utf-8")),
                ({"value": 1}, {"value": 2}),
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])


class ProcStatParserTests(unittest.TestCase):
    def test_all_process_identity_readers_handle_spaces_in_comm(self):
        fields = ["S", "42", *("0" for _ in range(17)), "987654", "0"]
        fixture = f"123 (tmux: server) {' '.join(fields)}\n"

        with mock.patch("builtins.open", mock.mock_open(read_data=fixture)):
            self.assertEqual(agent_identity._stat_fields(123), ("S", 42, "987654"))
            self.assertEqual(tmux._process_start_time(123), "987654")
            self.assertEqual(launch_provenance._process_start_time(123), "987654")
            self.assertEqual(park_activation.process_start_time(123), "987654")
            with mock.patch.object(
                Path, "iterdir", return_value=[Path("/proc/123")]
            ):
                self.assertEqual(
                    park_activation._descendants(42),
                    [{"pid": 123, "start_time": "987654"}],
                )


if __name__ == "__main__":
    unittest.main()
