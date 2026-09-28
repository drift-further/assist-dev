"""/poll and the terminal stream stay cheap without going stale.

A 40-pane /poll used to ship ~600 KB every 5 s: a 60-line ANSI tail for every
pane, uncompressed, to every browser, and it spent most of a second in one
`pgrep` per shell pane plus a serial `capture-pane` per pane. The streamer
re-captured 2,000 lines four times a second for a pane that had not changed.

Each saving here is pinned in both directions: the cheap path is taken when
nothing changed, and a real change still gets through. A cache that never
refreshes would pass the first half alone and blind the prompt popups.

Run: .venv/bin/python3 -m unittest tests.test_poll_perf
"""

import gzip
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parent.parent

# The socket tmux uses when nothing scopes it — the developer's REAL server.
AMBIENT_TMUX_SOCKET = Path(f"/tmp/tmux-{os.getuid()}/default")

PROMPT_SCRIPT = (
    "for i in $(seq 1 150); do printf '\\033[38;5;110mline %s\\033[0m text\\n' $i; done; "
    "printf '\\033[2m%s\\033[0m\\n' '────────────────────'; "
    "printf ' Do you want to proceed?\\n ❯ 1. Yes\\n   2. No\\n'; "
    "printf ' Enter to select · Esc to cancel\\n'; sleep 600"
)


class IsolatedTmux:
    """A throwaway tmux server, never the ambient one.

    Every tmux call is scoped to this TMUX_TMPDIR, and cleanup passes the socket
    explicitly with -S. A bare `tmux kill-server` would take down the
    developer's real server and every pane in it.
    """

    def __init__(self, case):
        self.tmpdir = tempfile.mkdtemp(prefix="assist-perf-", dir="/tmp")
        case.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        env = patch.dict(os.environ, {"TMUX_TMPDIR": self.tmpdir})
        env.start()
        case.addCleanup(env.stop)
        # $TMUX outranks TMUX_TMPDIR, so inside tmux it would aim every bare
        # `tmux` call in the code under test at the real server.
        os.environ.pop("TMUX", None)
        directory = Path(self.tmpdir) / f"tmux-{os.getuid()}"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket = (directory / "default").resolve()
        if self.socket == AMBIENT_TMUX_SOCKET.resolve():
            case.fail("sandbox socket resolves to the ambient tmux server")
        case.addCleanup(
            subprocess.run,
            ["tmux", "-S", str(self.socket), "kill-server"],
            check=False, capture_output=True, timeout=5,
        )

    def tmux(self, *args):
        return subprocess.run(
            ["tmux", "-S", str(self.socket), "-f", "/dev/null", *args],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout

    def raw_capture(self, target):
        return self.tmux("capture-pane", "-e", "-p", "-t", target, "-S", "-60").rstrip("\n")


def _settle(seconds=1.1):
    time.sleep(seconds)


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class PollWireTests(unittest.TestCase):
    """The real /poll handler against real panes. Every write it would make to
    the checkout's runtime files (tab order, idle state, drafts) is patched out,
    so running this in the live checkout cannot touch the live server's state.
    """

    def setUp(self):
        import routes.poll as poll
        import shared.state as state

        self.poll = poll
        self.box = IsolatedTmux(self)
        self.box.tmux("new-session", "-d", "-s", "agent", "-x", "120", "-y", "40", PROMPT_SCRIPT)
        self.box.tmux("new-session", "-d", "-s", "busy", "-x", "120", "-y", "20", "bash --norc -i")
        self.box.tmux("new-session", "-d", "-s", "quiet", "-x", "120", "-y", "20", "bash --norc -i")
        # A shell with a background child: pane_current_command stays bash.
        self.box.tmux("send-keys", "-t", "busy", "sleep 600 &", "Enter")
        _settle(0.8)

        self.save_idle = MagicMock()
        patches = [
            patch.object(state, "tmux_target", None),
            patch.object(state, "save_idle_state", self.save_idle),
            patch.object(state, "pane_content_hash", {}),
            patch.object(state, "pane_last_activity", {}),
            patch("routes.poll.tab_state.sweep_agent_declarations"),
            patch("routes.poll.tab_state.sweep_wakes"),
            patch("routes.poll.tab_state.apply_order", side_effect=lambda panes: panes),
            patch("routes.poll.tab_state.get_tab_state", return_value={}),
            patch("routes.poll.drafts.poll_block", return_value=None),
            patch("routes.studio.studio_poll_block", return_value=None),
            patch.object(poll, "_SCAN_CACHE", {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        app = Flask(__name__)
        app.register_blueprint(poll.poll_bp)
        self.client = app.test_client()
        self.calls = []
        real_run = subprocess.run

        def recording_run(args, *a, **k):
            self.calls.append(list(args))
            return real_run(args, *a, **k)

        run_patch = patch("routes.poll.subprocess.run", side_effect=recording_run)
        run_patch.start()
        self.addCleanup(run_patch.stop)

    def get(self, since=None):
        self.calls.clear()
        response = self.client.get("/poll", query_string={"since": since} if since else {})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def scan(self, data):
        return {entry["session"]: entry for entry in data["scan"]}

    def captured(self):
        # Targets arrive in tmux's exact form (`=name:w.p`, see
        # tests/test_exact_targets.py); the session name is what is compared.
        return sorted(
            c[c.index("-t") + 1].removeprefix("=").split(":")[0]
            for c in self.calls if "capture-pane" in c
        )

    def test_every_tail_is_plain_text_and_no_longer_than_the_client_reads(self):
        from routes.autoyes import detection_window, detection_window_lines

        scan = self.scan(self.get())
        self.assertEqual(set(scan), {"agent", "busy", "quiet"})
        for session, entry in scan.items():
            with self.subTest(session=session):
                self.assertNotIn("\x1b", entry["tail"])
                self.assertLessEqual(len(entry["tail"].split("\n")), detection_window_lines())
        # Exactly the rows the detectors read: the shared input window (was a
        # fixed 60, narrower than the server's capture; judge #6).
        self.assertEqual(scan["agent"]["tail"], detection_window(self.box.raw_capture("agent")))
        self.assertIn("Enter to select · Esc to cancel", scan["agent"]["tail"])
        self.assertIn("❯ 1. Yes", scan["agent"]["tail"])

    def test_an_unchanged_tail_is_not_resent_and_a_changed_one_is(self):
        first = self.get()
        self.assertTrue(first["gen"])
        self.box.tmux("send-keys", "-t", "quiet", "echo changed-here", "Enter")
        _settle(0.5)
        second = self.scan(self.get(since=first["gen"]))
        self.assertEqual(set(second), {"agent", "busy", "quiet"})
        self.assertNotIn("tail", second["agent"])
        self.assertNotIn("tail", second["busy"])
        self.assertIn("changed-here", second["quiet"]["tail"])
        # The per-pane answers the popups need still arrive for every pane.
        for entry in second.values():
            self.assertIn("prompt", entry)
            self.assertIn("agent_kind", entry)

    def test_a_since_from_another_server_run_gets_every_tail(self):
        for since in ("0000.1", "garbage", "abc"):
            with self.subTest(since=since):
                scan = self.scan(self.get(since=since))
                self.assertTrue(all("tail" in entry for entry in scan.values()))

    def test_quiet_panes_are_not_recaptured_but_a_changed_one_is(self):
        first = self.scan(self.get())
        _settle()
        self.get()
        _settle()
        self.get()
        self.assertEqual(self.captured(), [], "quiet panes were captured again")
        self.box.tmux("send-keys", "-t", "quiet", "echo after-quiet", "Enter")
        _settle(0.5)
        fresh = self.scan(self.get())
        self.assertEqual(self.captured(), ["quiet"])
        self.assertIn("after-quiet", fresh["quiet"]["tail"])
        # A reused tail is the same tail, not an empty one.
        self.assertEqual(fresh["agent"]["tail"], first["agent"]["tail"])

    def test_a_cached_capture_is_refreshed_after_its_maximum_age(self):
        self.get()
        _settle()
        with patch.object(self.poll, "SCAN_RECAPTURE_SEC", 0):
            self.get()
        self.assertEqual(self.captured(), ["agent", "busy", "quiet"])

    def test_shell_children_come_from_the_proc_snapshot_not_pgrep(self):
        states = self.get()["states"]
        self.assertFalse([c for c in self.calls if c[0] == "pgrep"])
        self.assertEqual(states["busy:0.0"]["state"], "running")
        self.assertEqual(states["quiet:0.0"]["state"], "shell")

    def test_idle_state_is_written_on_change_not_every_poll(self):
        self.get()
        self.assertEqual(self.save_idle.call_count, 1)
        _settle()
        self.get()
        _settle()
        self.get()
        self.assertEqual(self.save_idle.call_count, 1, "an unchanged poll rewrote idle state")
        self.box.tmux("send-keys", "-t", "quiet", "echo moved", "Enter")
        _settle(0.5)
        self.get()
        self.assertEqual(self.save_idle.call_count, 2)
        with patch.object(self.poll, "IDLE_SAVE_INTERVAL_SEC", 0):
            self.get()
        self.assertEqual(self.save_idle.call_count, 3, "the periodic save never came")


class CaptureReuseRuleTests(unittest.TestCase):
    """shared/tmux.py:capture_reusable, the rule both /poll and the streamer use."""

    def setUp(self):
        from shared.tmux import capture_reusable
        self.reusable = capture_reusable
        self.marker = (1000, 50, 3, 4, 80, 24, "0", "%1")

    def test_an_unchanged_pane_captured_after_its_last_output_is_reused(self):
        self.assertTrue(self.reusable((self.marker, 1001.2), self.marker, 1003.0, 30))

    def test_a_capture_in_the_same_second_as_the_output_is_not_trusted(self):
        # window_activity has one-second resolution: output later in the same
        # second leaves the marker unchanged.
        self.assertFalse(self.reusable((self.marker, 1000.9), self.marker, 1003.0, 30))

    def test_a_same_second_capture_is_held_only_for_the_minimum_interval(self):
        self.assertTrue(self.reusable((self.marker, 1000.5), self.marker, 1000.7, 30, 0.3))
        self.assertFalse(self.reusable((self.marker, 1000.5), self.marker, 1000.9, 30, 0.3))
        changed = (1000, 50, 4, 4, 80, 24, "0", "%1")  # the cursor moved: typing
        self.assertFalse(self.reusable((self.marker, 1000.5), changed, 1000.6, 30, 0.3))

    def test_any_marker_change_forces_a_capture(self):
        for i in range(len(self.marker)):
            changed = list(self.marker)
            changed[i] = "x"
            with self.subTest(field=i):
                self.assertFalse(self.reusable((self.marker, 1001.2), tuple(changed), 1003.0, 30))

    def test_an_old_capture_is_refreshed_and_no_capture_is_never_reused(self):
        self.assertFalse(self.reusable((self.marker, 1001.2), self.marker, 1031.3, 30))
        self.assertFalse(self.reusable(None, self.marker, 1003.0, 30))
        self.assertFalse(self.reusable((self.marker, 1001.2), None, 1003.0, 30))


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class StreamCaptureSkipTests(unittest.TestCase):
    def setUp(self):
        self.box = IsolatedTmux(self)
        self.box.tmux("new-session", "-d", "-s", "s", "-x", "80", "-y", "20", "bash --norc -i")
        _settle(0.5)

    def test_an_unchanged_pane_is_skipped_and_new_output_is_not(self):
        from shared.tmux import capture_pane_if_changed

        # max_age is widened so the streamer's 2 s forced refresh, measured
        # from the last real capture, cannot land inside the test.
        content, info, prev = capture_pane_if_changed("s:0.0", 200, max_age=30)
        self.assertIsNotNone(content)
        _settle()
        content, info, prev = capture_pane_if_changed("s:0.0", 200, prev=prev, max_age=30)
        _settle()
        content, info, prev2 = capture_pane_if_changed("s:0.0", 200, prev=prev, max_age=30)
        self.assertIsNone(content)
        self.assertTrue(info["unchanged"])
        self.box.tmux("send-keys", "-t", "s", "echo streamed-out", "Enter")
        _settle(0.3)
        content, info, _prev = capture_pane_if_changed("s:0.0", 200, prev=prev2, max_age=30)
        self.assertIn("streamed-out", content)
        self.assertNotIn("unchanged", info)

    def test_the_forced_refresh_captures_an_unchanged_pane(self):
        from shared.tmux import capture_pane_if_changed

        _content, _info, prev = capture_pane_if_changed("s:0.0", 200)
        _settle()
        _content, _info, prev = capture_pane_if_changed("s:0.0", 200, prev=prev)
        content, _info, _prev = capture_pane_if_changed("s:0.0", 200, prev=prev, max_age=0)
        self.assertIsNotNone(content)

    def test_capture_pane_keeps_its_two_value_contract(self):
        from shared.tmux import capture_pane

        content, info = capture_pane("s:0.0", 200)
        self.assertIsInstance(content, str)
        self.assertIn("width", info)

    def test_streamer_passes_the_last_capture_and_forces_a_slow_refresh(self):
        source = (ROOT / "routes/streaming.py").read_text()
        loop = source[source.index("def _terminal_streamer"):source.index("def _send_to")]
        self.assertIn("capture_pane_if_changed(", loop)
        self.assertIn("STREAM_FORCED_REFRESH_SEC", loop)


class GzipJsonTests(unittest.TestCase):
    def setUp(self):
        from shared.utils import gzip_json_response

        app = Flask(__name__)

        @app.route("/big")
        def big():
            return jsonify({"rows": ["x" * 40] * 400})

        @app.route("/small")
        def small():
            return jsonify({"ok": True})

        @app.route("/page")
        def page():
            return "<p>" + "x" * 20000 + "</p>"

        @app.after_request
        def _gzip(response):
            return gzip_json_response(response, request.headers.get("Accept-Encoding", ""))

        self.client = app.test_client()

    def test_a_large_json_body_is_gzipped_when_the_client_accepts_it(self):
        response = self.client.get("/big", headers={"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", response.headers.get("Vary", ""))
        self.assertEqual(json.loads(gzip.decompress(response.data)), {"rows": ["x" * 40] * 400})
        self.assertEqual(int(response.headers["Content-Length"]), len(response.data))

    def test_small_bodies_other_types_and_non_accepting_clients_are_untouched(self):
        cases = [
            ("/small", {"Accept-Encoding": "gzip"}),
            ("/page", {"Accept-Encoding": "gzip"}),
            ("/big", {}),
            ("/big", {"Accept-Encoding": "identity"}),
            ("/big", {"Accept-Encoding": "gzip;q=0"}),
        ]
        for path, headers in cases:
            with self.subTest(path=path, headers=headers):
                response = self.client.get(path, headers=headers)
                self.assertIsNone(response.headers.get("Content-Encoding"))
                if path == "/big":
                    self.assertEqual(response.get_json(), {"rows": ["x" * 40] * 400})

    def test_the_app_registers_it(self):
        self.assertIn("gzip_json_response(", (ROOT / "serve.py").read_text())


@unittest.skipUnless(shutil.which("git"), "needs git")
class GitMetaCacheTests(unittest.TestCase):
    def setUp(self):
        import routes.poll as poll

        self.poll = poll
        self.repo = Path(tempfile.mkdtemp(prefix="assist-gitmeta-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        (self.repo / "a.txt").write_text("a\n")
        subprocess.run([*git, "add", "a.txt"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        self.git = git
        cache = patch.object(poll, "_GIT_META_CACHE", {})
        cache.start()
        self.addCleanup(cache.stop)
        self.run_git = patch.object(poll, "_run_git", wraps=poll._run_git)
        self.spy = self.run_git.start()
        self.addCleanup(self.run_git.stop)

    def status_calls(self):
        return [c.args[1] for c in self.spy.call_args_list if c.args[1][0] == "status"]

    def test_repeat_reads_are_served_from_cache(self):
        first = self.poll._git_meta(self.repo)
        second = self.poll._git_meta(self.repo)
        self.assertEqual(first, second)
        self.assertEqual(len(self.status_calls()), 1)
        self.assertIn("--untracked-files=normal", self.status_calls()[0])

    def test_an_index_change_or_expiry_reads_git_again(self):
        self.poll._git_meta(self.repo)
        (self.repo / "b.txt").write_text("b\n")
        time.sleep(0.01)
        subprocess.run([*self.git, "add", "b.txt"], check=True)
        meta = self.poll._git_meta(self.repo)
        self.assertEqual(len(self.status_calls()), 2)
        self.assertEqual(meta["staged_files"], 1)
        (self.repo / "c.txt").write_text("c\n")  # untracked: the index does not move
        with patch.object(self.poll, "GIT_META_TTL_SEC", 0):
            meta = self.poll._git_meta(self.repo)
        self.assertEqual(meta["untracked_files"], 1)

    def test_an_untracked_directory_counts_once(self):
        (self.repo / "node_modules" / "x").mkdir(parents=True)
        for i in range(5):
            (self.repo / "node_modules" / "x" / f"{i}.js").write_text("")
        meta = self.poll._git_meta(self.repo)
        self.assertEqual(meta["untracked_files"], 1)
        self.assertEqual(meta["branch"], subprocess.run(
            [*self.git, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True).stdout.strip())


def run_js(files, body):
    harness = r"""
const fs = require('fs');
const vm = require('vm');
for (const f of FILES) vm.runInThisContext(fs.readFileSync(f, 'utf8'), {filename: f});
(async () => {
BODY
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
""".replace("FILES", json.dumps(files)).replace("BODY", body)
    result = subprocess.run(
        ["node", "-e", harness], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "needs node")
class PollSyncClientTests(unittest.TestCase):
    """js/poll-sync.js: response ordering and the client's tail cache."""

    def test_a_response_older_than_one_applied_is_dropped(self):
        result = run_js(["js/poll-sync.js"], r"""
const a = pollBegin(), b = pollBegin();
const newerFirst = pollAccept(b.seq);
const olderAfter = pollAccept(a.seq);
const c = pollBegin();
const nextOne = pollAccept(c.seq);
console.log(JSON.stringify({newerFirst, olderAfter, nextOne}));
""")
        self.assertEqual(result, {"newerFirst": True, "olderAfter": False, "nextOne": True})

    def test_omitted_tails_are_filled_from_what_this_client_holds(self):
        result = run_js(["js/poll-sync.js"], r"""
const first = pollBegin();
pollMergeScan('ep.5', [{target: 'a:0.0', tail: 'A1', tail_rev: 3}, {target: 'b:0.0', tail: 'B1', tail_rev: 5}]);
const second = pollBegin();
const scan = [{target: 'a:0.0'}, {target: 'b:0.0', tail: 'B2', tail_rev: 7}];
pollMergeScan('ep.7', scan);
const third = pollBegin();
console.log(JSON.stringify({firstSince: first.since, secondSince: second.since, thirdSince: third.since,
  tails: scan.map(e => e.tail)}));
""")
        self.assertEqual(result["firstSince"], "")
        self.assertEqual(result["secondSince"], "ep.5")
        self.assertEqual(result["thirdSince"], "ep.7")
        self.assertEqual(result["tails"], ["A1", "B2"])

    def test_an_older_tail_never_replaces_a_newer_one(self):
        result = run_js(["js/poll-sync.js"], r"""
pollMergeScan('ep.9', [{target: 'a:0.0', tail: 'new', tail_rev: 9}]);
const late = [{target: 'a:0.0', tail: 'old', tail_rev: 4}];
pollMergeScan('ep.6', late);
console.log(JSON.stringify({tail: late[0].tail, since: pollBegin().since}));
""")
        self.assertEqual(result, {"tail": "new", "since": "ep.9"})

    def test_a_restarted_server_or_a_missing_tail_resets_to_a_full_fetch(self):
        result = run_js(["js/poll-sync.js"], r"""
pollMergeScan('ep.9', [{target: 'a:0.0', tail: 'held', tail_rev: 9}]);
const restarted = [{target: 'a:0.0', tail: 'fresh', tail_rev: 1}];
pollMergeScan('new.1', restarted);
const afterRestart = pollBegin().since;
const missing = [{target: 'zz:0.0'}];
pollMergeScan('new.2', missing);
console.log(JSON.stringify({tail: restarted[0].tail, afterRestart,
  missingTail: missing[0].tail, afterMissing: pollBegin().since}));
""")
        self.assertEqual(result["tail"], "fresh")
        self.assertEqual(result["afterRestart"], "new.1")
        self.assertEqual(result["missingTail"], "")
        self.assertEqual(result["afterMissing"], "")


class ClientWiringTests(unittest.TestCase):
    """Source guards for wiring the Node tests cannot reach."""

    def setUp(self):
        self.app = (ROOT / "js/app.js").read_text()
        self.terminal = (ROOT / "js/terminal.js").read_text()

    def test_the_poll_is_sequenced_and_sends_since(self):
        poll = self.app[self.app.index("async function consolidatedPoll"):self.app.index("// Per-browser record")]
        self.assertIn("pollBegin()", poll)
        self.assertIn("pollAccept(", poll)
        self.assertIn("pollMergeScan(", poll)
        self.assertLess(poll.index("pollAccept("), poll.index("_applySessionsData("))

    def test_hidden_pages_pause_the_poll_and_the_stream(self):
        self.assertIn("document.hidden", self.app[self.app.index("function _schedulePoll"):])
        self.assertIn("visibilitychange", self.app)
        self.assertIn("visibilitychange", self.terminal)

    def test_detection_reads_only_the_tail_of_a_capture(self):
        self.assertIn("function detectionTail", (ROOT / "js/poll-sync.js").read_text())
        for path in ("js/terminal.js", "js/actions.js", "js/commands.js", "js/app.js"):
            source = (ROOT / path).read_text()
            with self.subTest(path=path):
                self.assertNotRegex(source, r"detectSmartActions\(\s*stripAnsi\(")
        # The dismiss snapshot must be taken in the same form detection sees.
        self.assertIn("dismissedContent = content ? detectionTail(content)",
                      (ROOT / "js/actions.js").read_text())

    def test_detection_tail_matches_the_detector_window(self):
        # Both sides cut to one shipped number, so neither can cut what the
        # other still reads. The behaviour is pinned in test_autoyes_parity.
        self.assertIn("lines.slice(-_detectionWindow)", (ROOT / "js/actions.js").read_text())
        self.assertIn("k < _detectionWindow", (ROOT / "js/poll-sync.js").read_text())

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_detection_tail_keeps_what_the_detector_sees(self):
        # terminal.js needs a DOM, so lift just its stripAnsi pair.
        source = self.terminal
        strip = source[source.index("function _stripOsc"):source.index("// Match http(s) URLs")]
        # js/actions.js owns _detectionWindow; set it as /poll would.
        result = run_js(["js/poll-sync.js"], strip + r"""
globalThis.stripAnsi = stripAnsi;
globalThis._detectionWindow = 93;
const lines = [];
for (let i = 0; i < 2000; i++) lines.push('\x1b[32mrow ' + i + '\x1b[0m');
const raw = lines.join('\n');
const full = stripAnsi(raw).split('\n').slice(-_detectionWindow).join('\n');
console.log(JSON.stringify({same: detectionTail(raw) === full, short: detectionTail('a\nb'),
  empty: detectionTail('')}));
""")
        self.assertEqual(result, {"same": True, "short": "a\nb", "empty": ""})


if __name__ == "__main__":
    unittest.main()
