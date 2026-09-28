"""Client paths that acted without the user, or left them stranded.

- The git commit box ran `git add -A && git commit && git push` on the phone
  keyboard's Send key. Enter must not submit it; the button says what it does
  and confirms with the branch, the remote and the file count from
  /api/git/preview, which is exercised here against a real repository.
- After a token rotation every tab sat on a frozen terminal. A 401 from /poll,
  or a WebSocket that never opens because its handshake was refused, sends the
  browser to /login.
- A project setting reached innerHTML unescaped in `_projStepper`.

The JS checks are source guards: the suite has no browser. Each pins the call
that carries the behaviour, so a refactor that drops it fails here.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from routes import git as git_routes

ROOT = Path(__file__).resolve().parents[1]


def _js(name):
    return (ROOT / "js" / name).read_text(encoding="utf-8")


def _function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[start:i + 1]
    raise AssertionError(f"unterminated function {name}")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True,
    )


class GitPreviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.remote = base / "remote.git"
        self.repo = base / "work"
        _git(base, "init", "-q", "--bare", str(self.remote))
        _git(base, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "a.txt").write_text("a\n")
        _git(self.repo, "add", "a.txt")
        _git(self.repo, "commit", "-q", "-m", "init")
        remote_url = "https://user:secret-token@example.invalid/r.git"
        _git(self.repo, "remote", "add", "origin", remote_url)
        _git(self.repo, "config", "remote.origin.pushurl", str(self.remote))
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        app = Flask(__name__)
        app.register_blueprint(git_routes.git_bp)
        self.client = app.test_client()

    def _preview(self, cwd):
        with mock.patch("routes.studio._pane_cwd", return_value=cwd):
            return self.client.get("/api/git/preview?target=s:0.0")

    def test_reports_branch_upstream_and_changed_count(self):
        (self.repo / "a.txt").write_text("changed\n")
        (self.repo / "new").mkdir()
        (self.repo / "new" / "b.txt").write_text("b\n")
        (self.repo / "new" / "c.txt").write_text("c\n")
        resp = self._preview(str(self.repo))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["branch"], "main")
        self.assertEqual(data["upstream"], "origin/main")
        self.assertEqual(data["remote"], "origin")
        self.assertEqual(data["changed"], 3)  # untracked files counted one by one

    def test_remote_credentials_are_not_shown(self):
        data = self._preview(str(self.repo)).get_json()
        self.assertEqual(data["remote_url"], "https://example.invalid/r.git")
        self.assertNotIn("secret-token", str(data))

    def test_clean_tree_and_missing_upstream(self):
        _git(self.repo, "checkout", "-q", "-b", "topic")
        data = self._preview(str(self.repo)).get_json()
        self.assertEqual(data["changed"], 0)
        self.assertEqual(data["branch"], "topic")
        self.assertEqual(data["upstream"], "")

    def test_not_a_repository(self):
        with tempfile.TemporaryDirectory() as plain:
            resp = self._preview(plain)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_pane(self):
        self.assertEqual(self._preview(None).status_code, 400)


class GitCommitBoxTests(unittest.TestCase):
    def test_enter_does_not_submit_the_commit_box(self):
        app = _js("app.js")
        start = app.index("getElementById('git-commit-msg').addEventListener('keydown'")
        handler = app[start:app.index("});", start)]
        self.assertNotIn("gitCommitPush", handler)
        index = (ROOT / "index.html").read_text(encoding="utf-8")
        box = re.search(r'<input[^>]*id="git-commit-msg"[^>]*>', index).group(0)
        self.assertNotIn('enterkeyhint="send"', box)

    def test_button_says_what_it_does_and_confirms_first(self):
        index = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('onclick="gitCommitPushConfirm()">Commit &amp; push</button>', index)
        self.assertNotIn("C&amp;P", index)
        body = _function(_js("app.js"), "gitCommitPushConfirm")
        preview = body.index("/api/git/preview")
        ask = body.index("if (!confirm(")
        run = body.index("gitCommitPush()")
        self.assertLess(preview, ask)
        self.assertLess(ask, run)
        for field in ("info.branch", "info.upstream", "info.changed"):
            self.assertIn(field, body)


class AuthLostTests(unittest.TestCase):
    def test_helper_redirects_only_on_401(self):
        body = _function(_js("state.js"), "authLost")
        self.assertIn("resp.status !== 401", body)
        self.assertIn("location.replace('/login')", body)

    def test_poll_checks_before_parsing(self):
        body = _function(_js("app.js"), "consolidatedPoll")
        fetched = body.index("fetch('/poll'")
        checked = body.index("if (authLost(resp)) return;")
        parsed = body.index("await resp.json()")
        self.assertLess(fetched, checked)
        self.assertLess(checked, parsed)

    def test_websocket_that_never_opened_checks_auth(self):
        terminal = _js("terminal.js")
        body = _function(terminal, "connectTerminalWs")
        self.assertIn("opened = true;", body)
        self.assertIn("if (!opened) _wsCheckAuth();", body)
        probe = _function(terminal, "_wsCheckAuth")
        self.assertIn("authLost(resp)", probe)
        self.assertIn("clearTimeout(_termWsReconnectTimer)", probe)


class ProjectStepperEscapeTests(unittest.TestCase):
    def test_stepper_value_is_escaped(self):
        body = _function(_js("monitor.js"), "_projStepper")
        self.assertIn("${escHtml(String(val))}", body)
        self.assertNotRegex(body, r">\$\{val\}<")


if __name__ == "__main__":
    unittest.main()
