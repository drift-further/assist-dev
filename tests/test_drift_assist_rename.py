"""The rename to Drift Assist, and the one-release reads of pre-rename names.

A live install made before the rename has only ~/.config/claude-assist/ and,
possibly, only a claude-assist-container image. Both must keep working until
the owner re-runs install.sh or rebuilds, and the new names must win as soon
as they exist.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli import config, proc
from routes import container
from shared import state


ROOT = Path(__file__).resolve().parents[1]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ConfigDirFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.xdg = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(config, "_legacy_notice_shown", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pick(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            chosen = config.user_config_file(self.xdg)
        return chosen, stderr.getvalue()

    def test_new_directory_is_the_default(self):
        chosen, stderr = self._pick()
        self.assertEqual(chosen, self.xdg / "drift-assist" / "config.env")
        self.assertEqual(stderr, "")

    def test_legacy_file_is_read_when_new_directory_is_absent(self):
        _write(self.xdg / "claude-assist" / "config.env", "ASSIST_HOME=/x\n")
        chosen, stderr = self._pick()
        self.assertEqual(chosen, self.xdg / "claude-assist" / "config.env")
        self.assertIn("legacy config", stderr)
        self.assertIn("drift-assist", stderr)

    def test_legacy_notice_is_printed_once(self):
        _write(self.xdg / "claude-assist" / "config.env", "ASSIST_HOME=/x\n")
        self._pick()
        chosen, stderr = self._pick()
        self.assertEqual(chosen, self.xdg / "claude-assist" / "config.env")
        self.assertEqual(stderr, "")

    def test_new_directory_wins_over_legacy(self):
        _write(self.xdg / "claude-assist" / "config.env", "ASSIST_HOME=/old\n")
        _write(self.xdg / "drift-assist" / "config.env", "ASSIST_HOME=/new\n")
        chosen, stderr = self._pick()
        self.assertEqual(chosen, self.xdg / "drift-assist" / "config.env")
        self.assertEqual(stderr, "")

    def test_existing_new_directory_without_file_does_not_fall_back(self):
        # Once the owner has the new directory, a legacy file is ignored even
        # if the new file is missing: no silent split between two configs.
        (self.xdg / "drift-assist").mkdir()
        _write(self.xdg / "claude-assist" / "config.env", "ASSIST_HOME=/old\n")
        chosen, stderr = self._pick()
        self.assertEqual(chosen, self.xdg / "drift-assist" / "config.env")
        self.assertEqual(stderr, "")

    def test_cli_end_to_end_reads_legacy_config(self):
        log_file = self.xdg / "legacy.log"
        log_file.write_text("legacy-last\n", encoding="utf-8")
        _write(
            self.xdg / "claude-assist" / "config.env",
            f"ASSIST_HOME={ROOT}\nASSIST_LOG_FILE={log_file}\n",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("ASSIST_")}
        env["XDG_CONFIG_HOME"] = str(self.xdg)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "assist"), "logs", "1"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "legacy-last\n")
        self.assertEqual(completed.stderr.count("legacy config"), 1)

    def test_installer_writes_the_new_directory(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('$HOME/.config}/drift-assist"', source)
        self.assertNotIn("/claude-assist\"", source)


class ContainerImageFallbackTests(unittest.TestCase):
    def _resolve(self, name, built):
        cfg = {"image": {"name": name}}
        with patch.object(container, "_docker_image_exists", side_effect=lambda n: n in built):
            return container.resolve_image_name(cfg)

    def test_default_image_name_is_drift_assist(self):
        self.assertEqual(
            state.DEFAULT_CONTAINER_CONFIG["image"]["name"], "drift-assist-container"
        )

    def test_legacy_image_used_while_new_one_is_unbuilt(self):
        self.assertEqual(
            self._resolve("drift-assist-container", {"claude-assist-container"}),
            "claude-assist-container",
        )

    def test_new_image_wins_once_built(self):
        self.assertEqual(
            self._resolve(
                "drift-assist-container",
                {"drift-assist-container", "claude-assist-container"},
            ),
            "drift-assist-container",
        )

    def test_explicit_name_never_falls_back(self):
        self.assertEqual(
            self._resolve("my-image", {"claude-assist-container"}), "my-image"
        )

    def test_status_reports_the_resolved_image(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(container, "resolve_image_name", return_value="claude-assist-container"), \
                patch.object(container.subprocess, "run", side_effect=fake_run):
            from flask import Flask
            app = Flask("image-status")
            app.register_blueprint(container.container_bp)
            app.test_client().get("/api/container/status")
        self.assertIn(
            "claude-assist-container",
            [argv[2] for argv in seen if argv[:2] == ["docker", "images"]],
        )


class StudioCliTests(unittest.TestCase):
    def _run(self, available):
        with patch.object(proc.shutil, "which", side_effect=lambda n: n if n in available else None), \
                patch.object(proc.os, "execvp") as execute:
            proc.studio(["status"])
        return execute

    def test_prefers_sto(self):
        execute = self._run({"sto", "studio"})
        execute.assert_called_once_with("sto", ["sto", "status"])

    def test_falls_back_to_studio(self):
        execute = self._run({"studio"})
        execute.assert_called_once_with("studio", ["studio", "status"])


class ProductNameTests(unittest.TestCase):
    def test_no_claude_assist_product_name_left(self):
        # The only pre-rename names allowed are the one-release fallbacks.
        allowed = {
            "cli/config.py",
            "shared/state.py",
            "docker/claude-mount.sh",
            "README.md",
            "tools/release/park_activation_handoff_harness.py",
            "tests/test_drift_assist_rename.py",
        }
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
        offenders = []
        for rel in tracked:
            path = ROOT / rel
            if rel in allowed or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "claude assist" in text.lower() or "claude-assist" in text.lower():
                offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_page_title_and_package_name(self):
        self.assertIn("<title>Drift Assist</title>", (ROOT / "index.html").read_text())
        self.assertIn('name = "drift-assist"', (ROOT / "pyproject.toml").read_text())
        self.assertTrue((ROOT / "README.md").read_text().startswith("# Drift Assist\n"))


if __name__ == "__main__":
    unittest.main()
