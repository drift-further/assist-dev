"""What a first-time installer sees: prerequisites, the closing steps, the parked flag.

The installer used to recommend `assist container build` (parked, 409), and the
UI and `assist help` offered Automate and Container as working features. One
flag, `execution_park.FEATURES_PARKED`, now hides all of those while the
intents are denied -- pinned both ways, so un-parking brings them back.
"""

import io
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from flask import Flask

from cli import proc
from routes import settings
from shared import execution_park


ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "install.sh").read_text()


def _load_cli():
    loader = SourceFileLoader("assist_cli_under_test", str(ROOT / "bin" / "assist"))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    with mock.patch.dict(os.environ, {"ASSIST_VENV_REEXEC": str(os.getpid())}):
        loader.exec_module(module)
    return module


class ParkedFlagTests(unittest.TestCase):
    def test_flag_follows_the_denied_intents(self):
        self.assertTrue(execution_park.FEATURES_PARKED)
        self.assertIn(execution_park.Intent.AUTOMATE_START, execution_park.DENIED_INTENTS)

    def test_ui_entries_ship_hidden_and_tagged(self):
        html = (ROOT / "index.html").read_text()
        for label in (">Container</button>", ">Automate</button>"):
            line = next(l for l in html.splitlines() if label in l)
            self.assertIn("data-parked", line)
            self.assertIn('style="display:none"', line)
        # Everything else in the deck stays visible.
        for label in (">Projects</button>", ">Git</button>", ">Settings</button>"):
            line = next(l for l in html.splitlines() if label in l)
            self.assertNotIn("data-parked", line)

    def test_state_js_unhides_only_when_the_server_says_unparked(self):
        source = (ROOT / "js" / "state.js").read_text()
        self.assertIn("d.parked === false", source)
        self.assertIn("[data-parked]", source)

    def test_settings_api_reports_the_flag(self):
        app = Flask("parked-flag")
        app.register_blueprint(settings.settings_bp)
        body = app.test_client().get("/api/settings").get_json()
        self.assertIs(body["parked"], True)
        with mock.patch.object(settings.park, "FEATURES_PARKED", False):
            body = app.test_client().get("/api/settings").get_json()
        self.assertIs(body["parked"], False)

    def test_help_hides_container_while_parked_and_shows_it_after(self):
        cli = _load_cli()
        parked = cli.help_text(parked=True)
        self.assertNotIn("container build", parked)
        self.assertNotIn("activate-park-v16", parked)
        self.assertIn("assist", parked)
        self.assertIn("container build", cli.help_text(parked=False))

    def test_new_verbs_are_in_help(self):
        text = _load_cli().help_text()
        for verb in ("expose", "pair", "token", "service install", "--version"):
            self.assertIn(verb, text)


class VersionTests(unittest.TestCase):
    def test_version_comes_from_pyproject(self):
        declared = re.search(
            r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M
        )[1]
        self.assertTrue(_load_cli().version_text().startswith(f"drift-assist {declared}"))


class InstallerTests(unittest.TestCase):
    def test_python_floor_is_310_everywhere(self):
        self.assertIn("sys.version_info >= (3,10)", INSTALL)
        self.assertIn('requires-python = ">=3.10"', (ROOT / "pyproject.toml").read_text())

    def test_no_test_uses_an_api_newer_than_the_floor(self):
        # TestCase.enterContext is 3.11+. Two modules used it and the suite
        # silently stopped passing on the 3.10 floor install.sh accepts.
        needle = "." + "enterContext" + "("  # split, so this file does not match itself
        offenders = [
            path.name for path in sorted((ROOT / "tests").glob("test_*.py"))
            if needle in path.read_text()
        ]
        self.assertEqual(offenders, [])

    def test_tmux_floor_is_checked(self):
        self.assertIn(">= (3, 2)", INSTALL)

    def test_installs_the_hashed_lock(self):
        self.assertIn("--require-hashes -r \"$SCRIPT_DIR/requirements.lock\"", INSTALL)
        lock = (ROOT / "requirements.lock").read_text()
        for name in ("flask==", "flask-sock==", "werkzeug=="):
            self.assertIn(name, lock)
        for line in lock.splitlines():
            if re.match(r"^[a-z0-9-]+==", line):
                self.assertTrue(line.endswith("\\"), f"no hashes for {line}")

    def test_lock_covers_requirements_txt(self):
        lock = (ROOT / "requirements.lock").read_text().lower()
        for requirement in (ROOT / "requirements.txt").read_text().splitlines():
            name = re.split(r"[<>=!~ ]", requirement.strip())[0].lower()
            if name:
                self.assertIn(f"\n{name}==", lock)

    def test_closing_steps_are_start_expose_pair_and_no_container_build(self):
        closing = INSTALL[INSTALL.index("Next steps:"):]
        self.assertNotIn("container build", closing)
        order = [closing.index(s) for s in ("$START_STEP", "assist expose", "assist pair")]
        self.assertEqual(order, sorted(order))

    def test_service_is_opt_in(self):
        section = INSTALL[INSTALL.index("[7/8]"):INSTALL.index("[8/8]")]
        self.assertIn('ans="n"', section)
        self.assertIn("service install", section)

    def test_claude_state_dir_only_when_claude_is_installed(self):
        self.assertIn('if [[ -d "$HOME/.claude" ]]; then', INSTALL)

    def test_path_hint_names_the_actual_bin_dir(self):
        self.assertNotIn('export PATH=\\"\\$HOME/.local/bin', INSTALL)

    def test_installer_parses(self):
        subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=True)


class DoctorTmuxTests(unittest.TestCase):
    def test_version_parse(self):
        for output, expected in (("tmux 3.7c\n", (3, 7)), ("tmux next-3.4\n", (3, 4)),
                                 ("tmux 3.1c\n", (3, 1))):
            with self.subTest(output=output), mock.patch.object(
                proc.subprocess, "run", return_value=SimpleNamespace(stdout=output)
            ):
                self.assertEqual(proc._tmux_version(), expected)

    def test_old_tmux_fails_doctor(self):
        resolved = SimpleNamespace(
            python=sys.executable, home=ROOT, config_file=ROOT / "nope", port=1
        )
        out = io.StringIO()
        with mock.patch.object(proc, "_tmux_version", return_value=(3, 1)), \
                mock.patch.object(proc.http, "get", side_effect=proc.http.server_not_running("x")), \
                redirect_stdout(out):
            code = proc.doctor(resolved)
        self.assertEqual(code, 1)
        self.assertIn("too old", out.getvalue())


if __name__ == "__main__":
    unittest.main()
