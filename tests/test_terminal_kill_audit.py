"""Route-level coverage for the durable /terminal/kill audit record."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes import terminal


class TerminalKillAuditTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(terminal.terminal_bp)
        self.client = app.test_client()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audit_file = Path(tmp.name) / "kill-audit.log"
        patcher = patch.object(terminal, "KILL_AUDIT_FILE", self.audit_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self):
        return json.loads(self.audit_file.read_text(encoding="utf-8"))

    @patch.object(terminal.subprocess, "run")
    def test_success_records_target_and_client_context(self, run):
        run.return_value = SimpleNamespace(returncode=0, stderr="")

        response = self.client.post(
            "/terminal/kill",
            json={"session": "del-example"},
            headers={
                "X-Real-IP": "10.0.0.233",
                "X-Forwarded-For": "198.51.100.9, 127.0.0.1",
                "User-Agent": "Assist audit test",
                "Referer": "https://assist.example/",
            },
        )

        self.assertEqual(response.status_code, 200)
        run.assert_called_once_with(
            ["tmux", "kill-session", "-t", "=del-example"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        record = self._record()
        self.assertEqual(record["session"], "del-example")
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["stderr"], "")
        self.assertEqual(record["source_ip"], "10.0.0.233")
        self.assertEqual(record["user_agent"], "Assist audit test")
        self.assertEqual(record["referrer"], "https://assist.example/")
        self.assertRegex(record["timestamp"], r"^\d{4}-\d{2}-\d{2}T.*[+-]\d{2}:\d{2}$")
        self.assertEqual(self.audit_file.stat().st_mode & 0o777, 0o600)

    @patch.object(terminal.subprocess, "run")
    def test_failed_kill_records_tmux_stderr(self, run):
        run.return_value = SimpleNamespace(returncode=1, stderr="no such session\n")

        response = self.client.post(
            "/terminal/kill",
            json={"session": "missing"},
            headers={"X-Forwarded-For": "10.0.0.44, 127.0.0.1"},
        )

        self.assertEqual(response.status_code, 500)
        record = self._record()
        self.assertEqual(record["session"], "missing")
        self.assertEqual(record["outcome"], "failure")
        self.assertEqual(record["stderr"], "no such session\n")
        # The head of X-Forwarded-For is client-written; the loopback peer is
        # what is known (tests/test_request_trust.py pins the trust rule).
        self.assertEqual(record["source_ip"], "127.0.0.1")

    @patch.object(terminal.subprocess, "run")
    def test_kill_exception_is_audited_before_original_500(self, run):
        run.side_effect = terminal.subprocess.TimeoutExpired(
            ["tmux", "kill-session"], 5, stderr="tmux timed out"
        )

        response = self.client.post(
            "/terminal/kill", json={"session": "stalled"}
        )

        self.assertEqual(response.status_code, 500)
        record = self._record()
        self.assertEqual(record["session"], "stalled")
        self.assertEqual(record["outcome"], "failure")
        self.assertEqual(record["stderr"], "tmux timed out")

    @patch.object(terminal.os, "open", side_effect=OSError("disk unavailable"))
    @patch.object(terminal.subprocess, "run")
    def test_audit_write_failure_does_not_change_success(self, run, _open):
        run.return_value = SimpleNamespace(returncode=0, stderr="")

        response = self.client.post(
            "/terminal/kill", json={"session": "still-killed"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "session": "still-killed"})
        run.assert_called_once()

    @patch.object(terminal.subprocess, "run")
    def test_missing_session_records_failed_attempt_without_tmux(self, run):
        response = self.client.post("/terminal/kill", json={})

        self.assertEqual(response.status_code, 400)
        run.assert_not_called()
        record = self._record()
        self.assertEqual(record["session"], "")
        self.assertEqual(record["outcome"], "failure")
        self.assertEqual(record["stderr"], "No session specified")


if __name__ == "__main__":
    unittest.main()
