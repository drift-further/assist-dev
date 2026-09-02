import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from cli import proc
from routes import container, terminal


ROOT = Path(__file__).resolve().parents[1]


class PublicPortabilityTests(unittest.TestCase):
    def test_missing_projects_directory_is_an_empty_success(self):
        app = Flask("missing-projects-directory")
        app.register_blueprint(terminal.terminal_bp)
        client = app.test_client()

        with tempfile.TemporaryDirectory() as raw:
            missing_dir = Path(raw) / "projects-not-created-yet"
            with patch.object(terminal.state, "PROJECTS_DIR", missing_dir):
                response = client.get("/terminal/projects")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["projects"], [])
        self.assertIn(str(missing_dir), payload["note"])

    def test_logs_uses_resolved_log_file(self):
        with tempfile.TemporaryDirectory() as raw:
            log_file = Path(raw) / "configured.log"
            log_file.write_text("ready\n", encoding="utf-8")
            resolved = SimpleNamespace(log_file=log_file)
            with patch.object(proc.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertEqual(proc.logs(resolved, lines="17"), 0)
            run.assert_called_once_with(["tail", "-n", "17", str(log_file)])

    def test_assist_logs_honors_environment_log_path_end_to_end(self):
        with tempfile.TemporaryDirectory() as raw:
            scratch = Path(raw)
            log_file = scratch / "configured.log"
            log_file.write_text("first\nconfigured-last\n", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "ASSIST_HOME": str(ROOT),
                    "ASSIST_LOG_FILE": str(log_file),
                    "XDG_CONFIG_HOME": str(scratch / "xdg"),
                }
            )
            completed = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "assist"), "logs", "1"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "configured-last\n")

    def test_studio_never_falls_back_to_an_estate_cli(self):
        stderr = io.StringIO()
        with patch.object(proc.shutil, "which", return_value=None) as which, \
                patch.object(proc.os, "execvp") as execute, \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(proc.studio(["status"]), 1)
        which.assert_called_once_with("studio")
        execute.assert_not_called()
        self.assertIn("no studio CLI on PATH", stderr.getvalue())

    def test_installer_prompts_before_adding_a_missing_statusline(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        branch = source.split('elif [[ -z "$sl_cmd" ]]', 1)[1].split(
            'elif [[ "$sl_cmd" == "$STATUSLINE_BIN" ]]', 1
        )[0]
        self.assertIn("prompt_replace_statusline", branch)
        self.assertLess(
            branch.index("prompt_replace_statusline"),
            branch.index("write_assist_statusline"),
        )

    def test_container_base_has_no_private_estate_bridges(self):
        mount = (ROOT / "docker" / "claude-mount.sh").read_text(encoding="utf-8")
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        for private_value in (
            ".claude-messages",
            "drift-further_daic",
            "drift-further_mathpolitics",
            "MP_DB_HOST",
        ):
            self.assertNotIn(private_value, mount)
        self.assertNotIn("link-agents.sh", dockerfile)
        self.assertFalse((ROOT / "docker" / "scripts" / "link-agents.sh").exists())
        base_stack = "psycopg2-binary fastapi uvicorn httpx beautifulsoup4 pyyaml"
        self.assertNotIn(base_stack, dockerfile)
        extension = json.loads(
            (ROOT / "docker" / "extensions" / "python-web-stack.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(any(base_stack in command for command in extension["install"]))
        self.assertFalse(extension["enabled"])
        self.assertNotIn("karen", entrypoint)

    def test_removed_redraw_helpers_stay_removed(self):
        streaming = (ROOT / "routes" / "streaming.py").read_text(encoding="utf-8")
        tmux = (ROOT / "shared" / "tmux.py").read_text(encoding="utf-8")
        park = (ROOT / "shared" / "execution_park.py").read_text(encoding="utf-8")
        for name in ("_force_redraw", "_maybe_redraw_async", "_stable_since"):
            self.assertNotIn(name, streaming)
        self.assertNotIn("pane_wants_ctrl_l_heal", tmux)
        self.assertNotIn("OPERATOR_TMUX_NON_CLAIM", park)


class ExtensionIdTests(unittest.TestCase):
    def setUp(self):
        app = Flask("extension-id")
        app.register_blueprint(container.container_bp)
        self.client = app.test_client()

    def test_rejects_invalid_supplied_and_derived_ids(self):
        payloads = [
            {"name": "Valid Name", "id": "../escape"},
            {"name": "Valid Name", "id": "Uppercase"},
            {"name": "Valid Name", "id": "_leading"},
            {"name": "Valid Name", "id": ""},
            {"name": "Valid Name", "id": "a" * 65},
            {"name": "slash/name"},
            {"name": ".hidden"},
        ]
        with patch.object(container.state, "add_extension") as add:
            for payload in payloads:
                with self.subTest(payload=payload):
                    response = self.client.post(
                        "/api/container/extensions", json=payload
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.get_json()["error"], "Invalid extension id")
            add.assert_not_called()

    def test_rejects_non_string_name_without_raising(self):
        with patch.object(container.state, "add_extension") as add:
            for payload in ({"name": 123}, ["not", "an", "object"]):
                with self.subTest(payload=payload):
                    response = self.client.post(
                        "/api/container/extensions", json=payload
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.get_json()["error"], "Name is required")
        add.assert_not_called()

    def test_accepts_valid_supplied_and_derived_ids(self):
        with patch.object(container.state, "add_extension", side_effect=lambda ext: [ext]):
            supplied = self.client.post(
                "/api/container/extensions",
                json={"name": "Web Stack", "id": "web.stack_2-x"},
            )
            derived = self.client.post(
                "/api/container/extensions", json={"name": "My Web Stack"}
            )
        self.assertEqual(supplied.status_code, 200)
        self.assertEqual(supplied.get_json()["extensions"][0]["id"], "web.stack_2-x")
        self.assertEqual(derived.status_code, 200)
        self.assertEqual(derived.get_json()["extensions"][0]["id"], "my-web-stack")


if __name__ == "__main__":
    unittest.main()
