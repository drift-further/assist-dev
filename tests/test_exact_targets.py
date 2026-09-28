"""Every tmux `-t` resolves exactly, and no pane is addressed as `:0.0`.

tmux resolves a bare session name by PREFIX when no session has that exact
name, so once `dev` is gone, `dev:0.0` quietly means `dev2`: /type and /key
typed into the wrong session, and a WebSocket parked on the dead tab streamed
the other one. `=name:` (shared/tmux.py:tmux_exact_target) or a tmux id
(`%pane`, `$session`) resolves to nothing instead.

`:0.0` / `:0.1` were hard-coded on launch, duplicate, run-init and the saved
command split. Under `base-index 1` / `pane-base-index 1` (a very common
~/.tmux.conf) there is no window 0, so the venv and the init command never
reached a new terminal, the returned target did not exist, and the saved
command's `kill-pane -t s:0.1` killed whatever pane sat at that index,
including a split the user made themselves.

The real-tmux tests run against a throwaway server (TMUX_TMPDIR), never the
ambient one, in both directions: the dead name resolves to nothing, and the
live name still resolves to its own pane.
"""

import ast
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import shared.state as state
import shared.tmux as tmux_shared
from routes import commands, terminal
from shared import launch_provenance as provenance
from shared.tmux import (
    capture_pane,
    expected_target_identity,
    tmux_exact_target,
    tmux_send_keys,
    tmux_send_text,
    tmux_target_exists,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
AMBIENT_TMUX_SOCKET = Path(
    os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / "default"


class ExactFormTests(unittest.TestCase):
    def test_an_exact_target_is_not_wrapped_twice(self):
        # Helpers now apply the exact form themselves, so a caller that already
        # did must not become `==name:`, which matches nothing.
        for target in ("=dev:", "=dev:1.1", "=dev:0"):
            with self.subTest(target=target):
                self.assertEqual(tmux_exact_target(target), target)

    def test_names_and_ids_keep_their_forms(self):
        self.assertEqual(tmux_exact_target("dev"), "=dev:")
        self.assertEqual(tmux_exact_target("dev:1.2"), "=dev:1.2")
        self.assertEqual(tmux_exact_target("%7"), "%7")
        self.assertEqual(tmux_exact_target("$3"), "$3")


# ---------------------------------------------------------------------------
# Source guard: no `-t` argument is a raw name
# ---------------------------------------------------------------------------

_SCANNED = ["serve.py", "routes", "shared", "cli"]
# `-t` means something else to these programs (docker tag, ps terminal).
_NOT_TMUX = {"docker", "ps"}
_ID_SUFFIXES = ("pane_id", "session_id", "window_id")


def _is_exact(node):
    """True when a `-t` value is exact by construction."""
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name == "tmux_exact_target"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("=") or bool(
            re.fullmatch(r"[%@$]\d+", node.value))
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return (isinstance(first, ast.Constant)
                and str(first.value).startswith("="))
    if isinstance(node, ast.Name):
        return node.id.endswith(_ID_SUFFIXES)
    if isinstance(node, ast.Attribute):
        return node.attr.endswith(_ID_SUFFIXES)
    return False


def _raw_t_arguments():
    offenders = []
    for entry in _SCANNED:
        path = REPO_ROOT / entry
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for file in files:
            tree = ast.parse(file.read_text(), filename=str(file))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                elts = node.elts
                first = elts[0] if elts else None
                if (isinstance(first, ast.Constant)
                        and first.value in _NOT_TMUX):
                    continue
                for index, elt in enumerate(elts[:-1]):
                    if isinstance(elt, ast.Constant) and elt.value == "-t":
                        value = elts[index + 1]
                        if not _is_exact(value):
                            offenders.append(
                                f"{file.relative_to(REPO_ROOT)}:{value.lineno}: "
                                f"-t {ast.unparse(value)}")
    return offenders


class RawTargetSourceGuardTests(unittest.TestCase):
    """In the style of the activate_venv single-implementation guard."""

    def test_no_tmux_t_argument_is_a_raw_name(self):
        self.assertEqual(
            _raw_t_arguments(), [],
            "route the name through tmux_exact_target() or address by tmux id")

    def test_the_guard_catches_a_raw_name(self):
        # The guard itself in both directions, so it cannot pass vacuously.
        raw = ast.parse('["tmux", "send-keys", "-t", target, "x"]').body[0].value
        exact = ast.parse(
            '["tmux", "send-keys", "-t", tmux_exact_target(target)]').body[0].value
        self.assertFalse(_is_exact(raw.elts[3]))
        self.assertTrue(_is_exact(exact.elts[3]))
        self.assertFalse(_is_exact(ast.parse('f"{s}:0.0"').body[0].value))
        self.assertTrue(_is_exact(ast.parse('f"={s}"').body[0].value))
        self.assertTrue(_is_exact(ast.parse('identity.pane_id').body[0].value))

    def test_no_pane_is_addressed_by_a_hard_coded_index(self):
        # Launch, duplicate, run-init and the saved-command split address the
        # pane they created (or the session's active pane), never `:0.0`.
        for path, functions in (
            ("routes/terminal.py", ("_existing_terminal_effect",
                                    "_terminal_launch_effect",
                                    "_terminal_duplicate_effect",
                                    "_terminal_run_init_effect")),
            ("routes/commands.py", ("_run_command_effect",
                                    "_stop_command_effect",
                                    "check_split_pane")),
        ):
            tree = ast.parse((REPO_ROOT / path).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in functions:
                    with self.subTest(function=node.name):
                        self.assertNotRegex(ast.unparse(node), r":0\.[01]\b")


# ---------------------------------------------------------------------------
# A real, isolated tmux server with base-index 1 and a dev/dev2 collision
# ---------------------------------------------------------------------------

class _IsolatedTmux(unittest.TestCase):
    """Against a throwaway tmux server, never the ambient one.

    Every bare `tmux` in the code under test resolves through TMUX_TMPDIR to
    this test's socket, and teardown passes that socket explicitly with -S.
    """

    CONF = "set -g base-index 1\nset -g pane-base-index 1\n"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="assist-exact-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        env = patch.dict(os.environ, {"TMUX_TMPDIR": self.tmpdir})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)

        directory = Path(self.tmpdir) / f"tmux-{os.getuid()}"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket = (directory / "default").resolve()
        if self.socket == AMBIENT_TMUX_SOCKET.resolve():
            self.fail("sandbox socket resolves to the ambient tmux server")
        self.addCleanup(
            subprocess.run,
            ["tmux", "-S", str(self.socket), "kill-server"],
            check=False, capture_output=True, timeout=5,
        )
        conf = Path(self.tmpdir) / "tmux.conf"
        conf.write_text(self.CONF)
        subprocess.run(
            ["tmux", "-S", str(self.socket), "-f", str(conf), "start-server",
             ";", "new-session", "-d", "-s", "dev2", "-x", "80", "-y", "20"],
            check=True, capture_output=True, timeout=5,
        )
        self.tmux("new-session", "-d", "-s", "dev", "-x", "80", "-y", "20")
        self.dev2_pane = self.pane_id("dev2")

    def tmux(self, *arguments, check=True):
        return subprocess.run(
            ["tmux", "-S", str(self.socket), *arguments],
            check=check, capture_output=True, text=True, timeout=5,
        ).stdout.strip()

    def pane_id(self, session):
        return self.tmux("display-message", "-p", "-t", f"={session}:",
                         "#{pane_id}")

    def panes(self):
        rows = self.tmux("list-panes", "-a", "-F",
                         "#{session_name}:#{window_index}.#{pane_index} #{pane_id}")
        return dict(line.split() for line in rows.splitlines() if line)

    def screen(self, pane):
        return self.tmux("capture-pane", "-p", "-t", pane)

    def wait_for(self, pane, text, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.screen(pane):
                return True
            time.sleep(0.05)
        return False


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class PrefixCollisionTests(_IsolatedTmux):
    """rel #1: a dead name resolves to nothing, not to the live prefix match."""

    def kill_dev(self):
        self.tmux("kill-session", "-t", "=dev")

    def test_identity_for_a_dead_name_is_absent(self):
        self.kill_dev()
        self.assertIsNone(expected_target_identity("dev:1.1"))
        self.assertIsNone(expected_target_identity("dev"))

    def test_identity_for_a_live_name_is_its_own_pane(self):
        dev_pane = self.pane_id("dev")
        self.assertEqual(expected_target_identity("dev:1.1").pane_id, dev_pane)
        self.assertEqual(expected_target_identity("dev").pane_id, dev_pane)
        self.kill_dev()
        self.assertEqual(expected_target_identity("dev2:1.1").pane_id,
                         self.dev2_pane)

    def test_text_for_a_dead_name_lands_nowhere(self):
        self.kill_dev()
        self.assertFalse(tmux_send_text("dev:1.1", "WRONG-SESSION-TEXT"))
        self.assertFalse(tmux_send_keys("dev:1.1", "Enter"))
        # The long-text path (paste-buffer) too.
        self.assertFalse(tmux_send_text("dev:1.1", "WRONG\nSESSION"))
        time.sleep(0.2)
        self.assertNotIn("WRONG", self.screen(self.dev2_pane))

    def test_text_for_a_live_name_lands(self):
        self.kill_dev()
        self.assertTrue(tmux_send_text("dev2:1.1", "echo RIGHT-SESSION"))
        self.assertTrue(tmux_send_keys("dev2:1.1", "Enter"))
        self.assertTrue(self.wait_for(self.dev2_pane, "RIGHT-SESSION"))

    def test_a_dead_name_does_not_capture_the_other_session(self):
        self.tmux("send-keys", "-t", self.dev2_pane, "echo DEV2-SCREEN", "Enter")
        self.assertTrue(self.wait_for(self.dev2_pane, "DEV2-SCREEN"))
        self.kill_dev()
        self.assertEqual(capture_pane("dev:1.1"), (None, None))
        content, _info = capture_pane("dev2:1.1")
        self.assertIn("DEV2-SCREEN", content)

    def test_target_exists_is_exact(self):
        self.assertTrue(tmux_target_exists("dev:1.1"))
        self.kill_dev()
        self.assertFalse(tmux_target_exists("dev:1.1"))
        self.assertFalse(tmux_target_exists("dev"))
        self.assertTrue(tmux_target_exists("dev2:1.1"))
        self.assertTrue(tmux_target_exists(self.dev2_pane))


class _RouteHarness(_IsolatedTmux):
    """The real routes, a real create, and a throwaway provenance store."""

    INIT = ""

    def setUp(self):
        super().setUp()
        home = Path(self.tmpdir) / "assist-home"
        home.mkdir(mode=0o700)
        registry = home / provenance.ROOT_NAME
        env = patch.dict(os.environ, {
            "ASSIST_HOME": str(home),
            "ASSIST_LAUNCH_PROVENANCE_ROOT": str(registry),
        })
        env.start()
        self.addCleanup(env.stop)
        provenance.initialize_epoch(
            assist_home=home,
            receipt_path=Path(self.tmpdir) / "receipt.json",
            expect_empty=True,
        )
        self.projects = Path(self.tmpdir) / "projects"
        (self.projects / "proj").mkdir(parents=True)

        real_get_setting = state.get_setting

        def configured(section, key):
            if (section, key) == ("server", "session_init_cmd"):
                return self.INIT
            if (section, key) == ("server", "venv_auto_activate"):
                return "off"
            return real_get_setting(section, key)

        for patcher in (
            patch.object(state, "PROJECTS_DIR", self.projects),
            patch.object(state, "get_setting", side_effect=configured),
            patch.object(tmux_shared, "pin_created_oom_score_adj"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        saved_target = state.tmux_target
        self.addCleanup(setattr, state, "tmux_target", saved_target)
        split_panes = dict(commands._command_panes)
        self.addCleanup(commands._command_panes.update, split_panes)
        self.addCleanup(commands._command_panes.clear)

        app = Flask("exact-targets")
        app.register_blueprint(terminal.terminal_bp)
        app.register_blueprint(commands.commands_bp)
        self.client = app.test_client()


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class CreatedPaneTargetTests(_RouteHarness):
    """rel #7: launch, duplicate and run-init address the pane that exists."""

    INIT = "echo INIT-RAN-$((40+2))"

    def assert_listed_target(self, body, session):
        # The tab-list form, and a pane that actually exists.
        panes = self.panes()
        self.assertEqual(body["target"], f"{session}:1.1")
        self.assertIn(body["target"], panes)
        self.assertEqual(panes[body["target"]],
                         body["expected_target_identity"]["pane_id"])

    def test_launch_returns_and_initialises_the_real_pane(self):
        response = self.client.post("/terminal/launch", json={"project": "proj"})
        self.assertEqual(response.status_code, 200, response.get_data())
        body = response.get_json()
        self.assert_listed_target(body, "proj")
        self.assertEqual(state.tmux_target, body["target"])
        self.assertTrue(self.wait_for(self.pane_id("proj"), "INIT-RAN-42"))

    def test_duplicate_returns_and_initialises_the_real_pane(self):
        response = self.client.post(
            "/terminal/duplicate", json={"session": "dev", "name": "dev-copy"})
        self.assertEqual(response.status_code, 200, response.get_data())
        self.assert_listed_target(response.get_json(), "dev-copy")
        self.assertTrue(self.wait_for(self.pane_id("dev-copy"), "INIT-RAN-42"))

    def test_opening_an_existing_session_adopts_its_real_pane(self):
        self.tmux("new-session", "-d", "-s", "proj", "-x", "80", "-y", "20")
        response = self.client.post("/terminal/launch", json={"project": "proj"})
        self.assertEqual(response.status_code, 200, response.get_data())
        body = response.get_json()
        self.assertTrue(body["existed"])
        self.assert_listed_target(body, "proj")

    def test_run_init_lands_in_the_named_session_only(self):
        response = self.client.post("/terminal/run-init", json={"session": "dev"})
        self.assertEqual(response.status_code, 200, response.get_data())
        self.assertTrue(response.get_json()["ok"])
        self.assertTrue(self.wait_for(self.pane_id("dev"), "INIT-RAN-42"))
        self.assertNotIn("INIT-RAN", self.screen(self.dev2_pane))

    def test_run_init_for_a_dead_name_is_not_found(self):
        self.tmux("kill-session", "-t", "=dev")
        response = self.client.post("/terminal/run-init", json={"session": "dev"})
        self.assertEqual(response.status_code, 404)
        time.sleep(0.2)
        self.assertNotIn("INIT-RAN", self.screen(self.dev2_pane))

    def test_run_init_reports_a_failed_send(self):
        with patch.object(terminal, "tmux_send_text", return_value=False):
            response = self.client.post(
                "/terminal/run-init", json={"session": "dev"})
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.get_json()["ok"])


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class SavedCommandSplitTests(_RouteHarness):
    """rel #7: the saved-command split is remembered by id and only it is killed."""

    def user_split(self, session):
        return self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                         "-t", f"={session}:")

    def run_saved(self, session, cmd="echo SAVED-$((6*7))"):
        response = self.client.post(
            "/api/commands/run", json={"session": session, "cmd": cmd})
        self.assertEqual(response.status_code, 200, response.get_data())
        return response.get_json()

    def test_run_returns_the_created_split(self):
        body = self.run_saved("dev")
        panes = self.panes()
        self.assertIn(body["target"], panes)
        self.assertEqual(panes[body["target"]], body["pane_id"])
        self.assertTrue(self.wait_for(body["pane_id"], "SAVED-42"))

    def test_a_user_split_survives_run_rerun_and_stop(self):
        mine = self.user_split("dev")
        first = self.run_saved("dev")
        second = self.run_saved("dev")
        live = set(self.panes().values())
        self.assertIn(mine, live)
        self.assertNotIn(first["pane_id"], live, "a rerun replaces its own split")
        self.assertIn(second["pane_id"], live)
        response = self.client.post("/api/commands/stop", json={"session": "dev"})
        self.assertEqual(response.status_code, 200, response.get_data())
        live = set(self.panes().values())
        self.assertIn(mine, live)
        self.assertNotIn(second["pane_id"], live)

    def test_stop_without_a_command_pane_kills_nothing(self):
        mine = self.user_split("dev")
        before = set(self.panes().values())
        response = self.client.post("/api/commands/stop", json={"session": "dev"})
        self.assertEqual(response.status_code, 200, response.get_data())
        self.assertEqual(set(self.panes().values()), before)
        self.assertIn(mine, before)

    def test_pane_check_follows_the_command_pane_only(self):
        self.user_split("dev")
        check = self.client.get("/api/commands/pane/dev").get_json()
        self.assertFalse(check["exists"], "a user split is not the command pane")
        body = self.run_saved("dev")
        check = self.client.get("/api/commands/pane/dev").get_json()
        self.assertTrue(check["exists"])
        self.tmux("kill-pane", "-t", body["pane_id"])
        check = self.client.get("/api/commands/pane/dev").get_json()
        self.assertFalse(check["exists"])

    def test_a_dead_session_runs_nothing_in_its_prefix_match(self):
        self.tmux("kill-session", "-t", "=dev")
        before = set(self.panes().values())
        response = self.client.post(
            "/api/commands/run", json={"session": "dev", "cmd": "echo NOPE"})
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(set(self.panes().values()), before)


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class DefaultIndexSavedCommandTests(SavedCommandSplitTests):
    """Same guarantees at tmux's default indexes, where a user split IS `:0.1`."""

    CONF = ""


if __name__ == "__main__":
    unittest.main()
