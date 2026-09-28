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

import json
import re
import shutil
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
    if source[max(0, start - 6):start] == "async ":
        start -= 6
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

    def test_preview_names_the_directory_it_described(self):
        data = self._preview(str(self.repo)).get_json()
        self.assertEqual(data["dir"], str(self.repo))


class CommitPushDirectoryBindingTests(unittest.TestCase):
    """/api/git/run refuses to commit anywhere but the directory confirmed."""

    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(git_routes.git_bp)
        self.client = app.test_client()

    def _run(self, pane_dir, expect_dir):
        body = {"op": "commit_push", "message": "m", "target": "a:0.0"}
        if expect_dir is not None:
            body["expect_dir"] = expect_dir
        cwd = mock.Mock(returncode=0, stdout=pane_dir + "\n")
        refused = mock.Mock(ok=False, status="stopped_here")
        with mock.patch.object(git_routes.subprocess, "run", return_value=cwd), \
                mock.patch.object(git_routes, "create_tmux_session",
                                  return_value=refused) as create:
            resp = self.client.post("/api/git/run", json=body)
        return resp, create

    def test_pane_moved_since_the_preview_is_refused(self):
        resp, create = self._run("/work/b", "/work/a")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "target_changed")
        create.assert_not_called()

    def test_same_directory_proceeds(self):
        resp, create = self._run("/work/a", "/work/a")
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["cwd"], "/work/a")

    def test_callers_without_a_preview_are_unchanged(self):
        _, create = self._run("/work/b", None)
        create.assert_called_once()


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
        run = body.index("gitRunOp('commit_push', msg, target, info.dir)")
        self.assertLess(preview, ask)
        self.assertLess(ask, run)
        for field in ("info.branch", "info.upstream", "info.changed"):
            self.assertIn(field, body)


_COMMIT_HARNESS = r"""
const calls = [];
const flashes = [];
const dialogs = [];
let previewResolve = null;
let confirmAnswer = true;
let target = 'projectA:0.0';
const box = { value: 'publish reviewed changes' };
const document = { getElementById: id => (id === 'git-commit-msg' ? box : null) };
function getInputTarget() { return target; }
function showFlash(kind, text) { flashes.push([kind, text]); }
function authLost(resp) { return false; }
function updateStatusTime() {}
let lastAction = 0;
function confirm(text) { dialogs.push(text); return confirmAnswer; }
function fetch(url, opts) {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    if (url.startsWith('/api/git/preview')) {
        return new Promise(resolve => { previewResolve = resolve; });
    }
    return Promise.resolve({ json: async () => ({ ok: true }) });
}
const PREVIEW_A = { ok: true, branch: 'branch-A', upstream: 'origin/branch-A',
                    remote_url: 'https://example.invalid/a.git', changed: 2,
                    dir: '/work/a' };
__FUNCTIONS__
async function scenario(name) {
    const run = gitCommitPushConfirm();
    await new Promise(r => setImmediate(r));
    if (name === 'switch') target = 'projectB:0.0';
    if (name === 'edit') box.value = 'something else';
    if (name === 'cancel') confirmAnswer = false;
    previewResolve({ json: async () => PREVIEW_A });
    await run;
    await new Promise(r => setImmediate(r));
    return { calls, flashes, dialogs, box: box.value };
}
scenario(process.argv[2]).then(r => console.log(JSON.stringify(r)));
"""


class CommitPushBindingTests(unittest.TestCase):
    """What the confirm describes is exactly what is dispatched.

    Runs the real gitCommitPushConfirm (app.js) with the real gitRunOp
    (monitor.js) under Node, with the preview held open so the
    selection can move while it is pending.
    """

    def _run(self, scenario):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        app, monitor = _js("app.js"), _js("monitor.js")
        functions = "\n".join((
            _function(app, "gitCommitPushConfirm"),
            _function(monitor, "gitRunOp"),
        ))
        with tempfile.TemporaryDirectory() as raw:
            script = Path(raw) / "harness.js"
            script.write_text(_COMMIT_HARNESS.replace("__FUNCTIONS__", functions))
            out = subprocess.run(
                ["node", str(script), scenario],
                capture_output=True, text=True, timeout=20, check=True,
            )
        return json.loads(out.stdout)

    def _runs(self, result):
        return [c for c in result["calls"] if c["url"] == "/api/git/run"]

    def test_tab_switch_during_preview_dispatches_nothing(self):
        result = self._run("switch")
        preview = result["calls"][0]["url"]
        self.assertIn("projectA", preview)
        self.assertEqual(self._runs(result), [])
        self.assertEqual(result["dialogs"], [])  # no stale confirm shown at all
        self.assertTrue(any(kind == "error" for kind, _ in result["flashes"]))

    def test_unchanged_selection_pushes_exactly_what_was_confirmed(self):
        result = self._run("same")
        runs = self._runs(result)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["body"], {
            "op": "commit_push",
            "target": "projectA:0.0",
            "message": "publish reviewed changes",
            "expect_dir": "/work/a",
        })
        self.assertIn("branch-A", result["dialogs"][0])
        self.assertIn("projectA:0.0", result["dialogs"][0])
        self.assertEqual(result["box"], "")

    def test_message_edited_during_preview_is_not_the_one_sent(self):
        result = self._run("edit")
        runs = self._runs(result)
        self.assertEqual(len(runs), 1)
        # The confirm quoted the captured message, so that is what is sent,
        # and the newer draft in the box is left alone.
        self.assertIn("publish reviewed changes", result["dialogs"][0])
        self.assertEqual(runs[0]["body"]["message"], "publish reviewed changes")
        self.assertEqual(result["box"], "something else")

    def test_declined_confirm_dispatches_nothing(self):
        result = self._run("cancel")
        self.assertEqual(self._runs(result), [])
        self.assertEqual(result["box"], "publish reviewed changes")


class AuthLostTests(unittest.TestCase):
    def test_helper_redirects_only_on_401(self):
        body = _function(_js("state.js"), "authLost")
        self.assertIn("resp.status !== 401", body)
        self.assertIn("location.replace('/login')", body)

    def test_poll_checks_before_parsing(self):
        body = _function(_js("app.js"), "consolidatedPoll")
        # The URL carries perf's `?since=` (js/poll-sync.js), so it is built
        # first; the fetch itself is what must precede the check.
        self.assertIn("'/poll?since='", body)
        fetched = body.index("await fetch(url")
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
