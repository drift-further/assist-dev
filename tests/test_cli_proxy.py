import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from routes.poll import poll_bp


class CliProxyTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "ASSIST_CLI_ALLOWED": "allowed",
                "ASSIST_CLI_BIN": "/fake/cli",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(poll_bp)
        self.client = app.test_client()

    def _post_through_inner_gate(self, payload):
        with patch(
            "routes.poll.park.perform", side_effect=lambda _intent, effect: effect()
        ):
            return self.client.post("/api/cli-proxy", json=payload)

    def test_malformed_base64_leaves_no_proxy_temp_directories(self):
        real_mkdtemp = tempfile.mkdtemp

        with tempfile.TemporaryDirectory() as temp_root:
            def make_proxy_temp_dir(*args, **kwargs):
                kwargs["dir"] = temp_root
                return real_mkdtemp(*args, **kwargs)

            with patch("tempfile.mkdtemp", side_effect=make_proxy_temp_dir):
                response = self._post_through_inner_gate(
                    {
                        "args": ["allowed", "--file", "__PROXY_FILE_0__"],
                        "files": [{"name": "bad.txt", "data": "A"}],
                    },
                )

            self.assertEqual(response.status_code, 500)
            self.assertEqual(
                list(Path(temp_root).glob("assist-proxy-*")),
                [],
            )

    @patch("routes.poll.subprocess.run")
    def test_timeout_above_ceiling_is_clamped(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "ok", "")

        response = self._post_through_inner_gate(
            {"args": ["allowed", "-w", "--timeout", "999999"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.call_args.kwargs["timeout"], 600)

    @patch("routes.poll.subprocess.run")
    def test_garbage_timeout_is_rejected(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "ok", "")

        response = self._post_through_inner_gate(
            {"args": ["allowed", "-w", "--timeout", "banana"]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("non-negative integer", response.get_json()["error"])
        run.assert_not_called()

    @patch("routes.poll.subprocess.run")
    def test_negative_timeout_is_rejected(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "ok", "")

        response = self._post_through_inner_gate(
            {"args": ["allowed", "-w", "--timeout", "-1"]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("non-negative integer", response.get_json()["error"])
        run.assert_not_called()

    @patch("routes.poll.subprocess.run")
    def test_empty_allowlist_still_disables_proxy(self, run):
        with patch.dict(os.environ, {"ASSIST_CLI_ALLOWED": ""}):
            response = self._post_through_inner_gate({"args": ["allowed"]})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["error"],
            "CLI proxy disabled (ASSIST_CLI_ALLOWED not set)",
        )
        run.assert_not_called()

    @patch("routes.poll.subprocess.run")
    def test_route_park_returns_409_without_spawning(self, run):
        response = self.client.post(
            "/api/cli-proxy",
            json={"args": ["allowed"]},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "error": "container_launch_parked",
                "reason": (
                    "Container launch automation is temporarily parked while host "
                    "wiring migrates."
                ),
                "intent": "configured_cli_proxy",
            },
        )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
