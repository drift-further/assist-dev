"""OpenCode output stays explicitly scoped to a live local pane/conversation."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, request

import shared.auth as auth
import shared.state as state
from routes.opencode import opencode_bp
from shared import opencode as oc


SID = "ses_test123"
CONTEXT = {"target": "example:0.0", "generation": "generation-one", "directory": "/project"}


def export_fixture():
    return {
        "info": {"id": SID, "directory": "/project", "title": "Example",
                 "agent": "build", "model": {"id": "example-model", "providerID": "provider", "variant": "high"}},
        "messages": [
            {"info": {"id": "msg_01", "role": "user"},
             "parts": [{"type": "text", "text": "Explain this"}]},
            {"info": {"id": "msg_02", "role": "assistant"},
             "parts": [{"type": "reasoning", "text": "Planning"},
                       {"type": "text", "text": "<script>untrusted()</script>\nAnswer"},
                       {"type": "tool", "tool": "bash", "state": {
                           "status": "completed", "input": {"command": "printf OK"}, "output": "OK"}},
                       {"type": "file", "filename": "example.png", "url": "https://untrusted.invalid/image"},
                       {"type": "step-finish"}]},
        ],
    }


class PaneContextTests(unittest.TestCase):
    def setUp(self):
        self.run = patch.object(oc.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout="%25\t123\topencode\t/project\n")).start()
        self.scan = patch.object(oc, "_scan_process_tree", return_value={
            "agent_kind": "opencode", "agent_pid": 124, "agent_start_time": "500",
            "root_start_time": "400",
        }).start()
        self.env = b"\0".join(key.encode() + b"=" + os.environ.get(key, "").encode()
                                for key in ("HOME", "XDG_DATA_HOME"))
        self.argv = b"opencode\0"

        def process_file(path):
            return self.env if str(path).endswith("/environ") else self.argv

        patch.object(Path, "read_bytes", autospec=True, side_effect=process_file).start()
        patch.object(Path, "is_dir", return_value=True).start()
        self.addCleanup(patch.stopall)

    def assertCode(self, code, function, *args):
        with self.assertRaises(oc.OpenCodeError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_accepts_local_opencode_and_uses_exact_tmux_target(self):
        context = oc.pane_context("example:0.0")
        self.assertEqual(context["directory"], "/project")
        self.assertEqual(len(context["generation"]), 32)
        self.assertIn("=example:0.0", self.run.call_args.args[0])

    def test_accepts_local_attach(self):
        self.argv = b"opencode\0attach\0http://127.0.0.1:4096\0--session\0ses_test123\0"
        self.assertEqual(oc.pane_context("example:0.0")["target"], "example:0.0")

    def test_rejects_remote_attach(self):
        self.argv = b"opencode\0attach\0http://other-host:4096\0"
        self.assertCode("remote_session", oc.pane_context, "example:0.0")

    def test_rejects_different_store(self):
        self.env += b"\0XDG_DATA_HOME=/other/store"
        self.assertCode("different_store", oc.pane_context, "example:0.0")

    def test_rejects_other_agents_and_shell_in_both_directions(self):
        for kind in ("codex", "claude", "shell", None):
            with self.subTest(kind=kind):
                self.scan.return_value["agent_kind"] = kind
                self.assertCode("not_opencode", oc.pane_context, "example:0.0")
        self.scan.return_value["agent_kind"] = "opencode"
        self.assertIn("generation", oc.pane_context("example:0.0"))

    def test_absent_pane_cannot_prefix_match(self):
        self.run.return_value.stdout = "\n"
        self.assertCode("pane_gone", oc.pane_context, "example:0.0")

    def test_rejects_missing_or_ambiguous_target_before_tmux(self):
        for target in (None, "", "example", "%25", "example:0", "example:0.0\n", "--help"):
            with self.subTest(target=target):
                self.assertCode("invalid_target", oc.pane_context, target)
        self.run.assert_not_called()

    def test_process_replacement_changes_generation_even_in_same_pane(self):
        first = oc.pane_context("example:0.0")
        self.scan.return_value["agent_start_time"] = "501"
        second = oc.pane_context("example:0.0")
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertCode("pane_changed", oc.check_generation, second, first["generation"])

    def test_tmux_timeout_is_actionable(self):
        self.run.side_effect = subprocess.TimeoutExpired("tmux", 3)
        self.assertCode("pane_unavailable", oc.pane_context, "example:0.0")


class ExportTests(unittest.TestCase):
    def test_structured_text_tools_and_model_without_attachment_urls(self):
        fixture = export_fixture()
        before = copy.deepcopy(fixture)
        result = oc.normalize_export(fixture, SID, "/project", 50)
        self.assertEqual(result["session"]["model"], "example-model")
        self.assertEqual(result["session"]["agent"], "build")
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "assistant"])
        parts = result["messages"][1]["parts"]
        self.assertEqual([p["type"] for p in parts], ["reasoning", "text", "tool", "file"])
        self.assertIn("<script>", parts[1]["text"])  # retained as text, rendered via textContent
        self.assertIn("printf OK", parts[2]["text"])
        self.assertNotIn("https://", json.dumps(result))
        self.assertEqual(before, fixture)  # cached raw export is never modified

    def test_rejects_wrong_id_prefix_directory_and_descendant_directory(self):
        for key, value in (("id", "ses_someoneElse"), ("directory", "/project-other"),
                           ("directory", "/project/child"), ("directory", "project")):
            fixture = export_fixture()
            fixture["info"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(oc.OpenCodeError) as caught:
                oc.normalize_export(fixture, SID, "/project", 50)
            self.assertEqual(caught.exception.code, "session_mismatch")

    def test_rejects_bad_envelopes(self):
        for data in (None, [], {}, {"info": {}, "messages": None}, {"info": [], "messages": []}):
            with self.subTest(data=data), self.assertRaises(oc.OpenCodeError):
                oc.normalize_export(data, SID, "/project", 50)

    def test_handles_empty_messages_and_unknown_parts(self):
        fixture = export_fixture()
        fixture["messages"] = [None, {"info": None}, {"info": {"role": "system"}},
                               {"info": {"id": "msg_1", "role": "assistant"}, "parts": [None, {"type": "new-format"}]}]
        result = oc.normalize_export(fixture, SID, "/project", 50)
        self.assertEqual(result["messages"], [{"id": "msg_1", "role": "assistant", "parts": []}])

    def test_window_and_per_part_limits_are_disclosed(self):
        fixture = export_fixture()
        fixture["messages"][1]["parts"] = [{"type": "text", "text": "x" * (oc.MAX_PART_CHARS + 1)}]
        result = oc.normalize_export(fixture, SID, "/project", 1)
        self.assertTrue(result["has_older"])
        self.assertTrue(result["clipped"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["shown"], 1)
        self.assertIn("Output clipped", result["messages"][0]["parts"][0]["text"])

    def test_payload_budget_prioritizes_newest_messages(self):
        fixture = export_fixture()
        with patch.object(oc, "MAX_VIEW_CHARS", 10):
            result = oc.normalize_export(fixture, SID, "/project", 50)
        self.assertTrue(result["clipped"])
        self.assertFalse(result["has_older"])
        self.assertEqual(result["shown"], 1)
        self.assertEqual(result["messages"][0]["role"], "assistant")

    def test_revert_hides_inactive_suffix(self):
        fixture = export_fixture()
        fixture["info"]["revert"] = {"messageID": "msg_02"}
        result = oc.normalize_export(fixture, SID, "/project", 50)
        self.assertTrue(result["reverted"])
        self.assertEqual([m["id"] for m in result["messages"]], ["msg_01"])

    def test_malformed_revert_point_fails_cleanly(self):
        fixture = export_fixture()
        fixture["info"]["revert"] = {"messageID": 42}
        with self.assertRaises(oc.OpenCodeError) as caught:
            oc.normalize_export(fixture, SID, "/project", 50)
        self.assertEqual(caught.exception.code, "invalid_export")

    def test_raw_error_headers_are_not_exposed(self):
        fixture = export_fixture()
        fixture["messages"][1]["info"]["error"] = {
            "name": "APIError", "data": {"responseHeaders": {"set-cookie": "private-cookie"}},
        }
        result = oc.normalize_export(fixture, SID, "/project", 50)
        self.assertNotIn("private-cookie", json.dumps(result))
        self.assertIn("APIError", json.dumps(result))

    def test_listing_filters_other_directories_without_guessing(self):
        rows = [{"id": SID, "directory": "/project", "title": "Here"},
                {"id": "ses_other", "directory": "/project-two"},
                {"id": "ses_child", "directory": "/project/child"},
                {"id": "--flag", "directory": "/project"}, None]
        with patch.object(oc, "cached_cli", return_value=(rows, 1)):
            self.assertEqual(oc.list_sessions(CONTEXT), [{"id": SID, "title": "Here"}])

    def test_invalid_session_never_invokes_cli(self):
        with patch.object(oc, "cached_cli") as cli:
            for sid in (None, "--help", "ses_a/b", "ses_a\n", "ses_"):
                with self.subTest(sid=sid), self.assertRaises(oc.OpenCodeError):
                    oc.transcript(CONTEXT, sid, 50)
            cli.assert_not_called()


class BinaryResolutionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ASSIST_OPENCODE_BIN": ""}).start()
        self.which = patch.object(oc.shutil, "which", return_value=None).start()
        self.is_file = patch.object(oc.os.path, "isfile", return_value=False).start()
        self.access = patch.object(oc.os, "access", return_value=True).start()

    def test_override_precedes_path_and_fallbacks(self):
        self.which.return_value = "/path/bin/opencode"
        self.is_file.return_value = True
        with patch.dict(os.environ, {"ASSIST_OPENCODE_BIN": "~/custom tools/opencode"}):
            expected = os.path.expanduser("~/custom tools/opencode")
            self.assertEqual(oc.resolve_binary(), expected)
        self.is_file.assert_called_once_with(expected)
        self.access.assert_called_once_with(expected, os.X_OK)

    def test_path_precedes_fallbacks(self):
        self.which.return_value = "/path/bin/opencode"
        self.is_file.return_value = True
        self.assertEqual(oc.resolve_binary(), "/path/bin/opencode")
        self.which.assert_called_once_with("opencode")

    def test_empty_path_uses_each_fallback_in_order(self):
        locations = [os.path.expanduser(path) for path in (
            "~/.local/bin/opencode", "~/.opencode/bin/opencode",
            "/opt/homebrew/bin/opencode", "/usr/local/bin/opencode",
        )]
        for index, expected in enumerate(locations):
            with self.subTest(location=expected):
                self.is_file.side_effect = lambda path: path in locations[index:]
                self.assertEqual(oc.resolve_binary(), expected)

    def test_skips_missing_directory_and_non_executable_candidates(self):
        self.which.return_value = "/directory/opencode"
        expected = os.path.expanduser("~/.opencode/bin/opencode")
        self.is_file.side_effect = lambda path: path in (
            os.path.expanduser("~/.local/bin/opencode"), expected,
        )
        self.access.side_effect = lambda path, mode: path == expected
        with patch.dict(os.environ, {"ASSIST_OPENCODE_BIN": "/missing/opencode"}):
            self.assertEqual(oc.resolve_binary(), expected)

    def test_nothing_installed_still_yields_cli_missing(self):
        with patch.object(oc.subprocess, "Popen") as launch:
            with self.assertRaises(oc.OpenCodeError) as caught:
                oc.run_cli([], "/tmp")
        self.assertEqual(caught.exception.code, "cli_missing")
        self.assertEqual(caught.exception.status, 503)
        launch.assert_not_called()


class CliAndCacheTests(unittest.TestCase):
    def setUp(self):
        patch.object(state, "opencode_cache", {}).start()
        patch.object(state, "opencode_slots", threading.BoundedSemaphore(2)).start()
        patch.dict(os.environ, {"ASSIST_OPENCODE_BIN": ""}).start()
        self.addCleanup(patch.stopall)

    def python_cli(self, code):
        with tempfile.TemporaryDirectory() as directory, patch.object(oc.shutil, "which", return_value=sys.executable):
            return oc.run_cli(["-c", code], directory)

    def test_real_subprocess_json_and_pure_flag(self):
        result = self.python_cli("import sys,json; print(json.dumps(sys.argv[1:]))")
        self.assertEqual(result, ["--pure"])

    def test_real_subprocess_uses_override_as_one_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "opencode with spaces"
            executable.symlink_to(sys.executable)
            with patch.dict(os.environ, {"ASSIST_OPENCODE_BIN": str(executable)}), \
                    patch.object(oc.shutil, "which", return_value="/missing/opencode"):
                result = oc.run_cli(
                    ["-c", "import sys,json; print(json.dumps(sys.argv[1:]))"], directory,
                )
        self.assertEqual(result, ["--pure"])

    def test_real_subprocess_failure_hides_stderr(self):
        with self.assertRaises(oc.OpenCodeError) as caught:
            self.python_cli("import sys; print('private-provider-error',file=sys.stderr); sys.exit(1)")
        self.assertEqual(caught.exception.code, "export_failed")
        self.assertNotIn("private-provider-error", str(caught.exception))

    def test_real_subprocess_malformed_output(self):
        with self.assertRaises(oc.OpenCodeError) as caught:
            self.python_cli("print('not json')")
        self.assertEqual(caught.exception.code, "invalid_export")

    def test_real_subprocess_output_cap(self):
        with patch.object(oc, "MAX_EXPORT_BYTES", 1000), self.assertRaises(oc.OpenCodeError) as caught:
            self.python_cli("print('x'*10000)")
        self.assertEqual(caught.exception.code, "output_too_large")

    def test_real_subprocess_timeout_reaps_child_and_releases_slot(self):
        processes = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            processes.append(proc)
            return proc

        with patch.object(oc, "CLI_TIMEOUT", .15), patch.object(oc.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(oc.OpenCodeError) as caught:
                self.python_cli("import time; time.sleep(10)")
        self.assertEqual(caught.exception.code, "timeout")
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(state.opencode_slots.acquire(blocking=False))
        self.assertTrue(state.opencode_slots.acquire(blocking=False))

    def test_concurrency_limit(self):
        state.opencode_slots.acquire()
        state.opencode_slots.acquire()
        with patch.object(oc.shutil, "which", return_value=sys.executable), self.assertRaises(oc.OpenCodeError) as caught:
            oc.run_cli([], "/tmp")
        self.assertEqual(caught.exception.status, 429)

    def test_cache_is_bounded_expires_and_separates_generations(self):
        with patch.object(oc, "run_cli", return_value={"data": "example"}) as cli, patch.object(oc.time, "monotonic", return_value=100):
            oc.cached_cli(["export", SID], CONTEXT)
            oc.cached_cli(["export", SID], CONTEXT)
            self.assertEqual(cli.call_count, 1)
            oc.cached_cli(["export", SID], {**CONTEXT, "generation": "new"})
            self.assertEqual(cli.call_count, 2)
            for i in range(10):
                oc.cached_cli(["export", "ses_" + str(i)], CONTEXT)
            self.assertEqual(len(state.opencode_cache), oc.CACHE_ENTRIES)
        with patch.object(oc, "run_cli", return_value={}) as cli, patch.object(oc.time, "monotonic", return_value=104):
            oc.cached_cli(["export", SID], CONTEXT)
            cli.assert_called_once()
            self.assertEqual(len(state.opencode_cache), 1)


class RouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(opencode_bp)

        @app.before_request
        def require_auth():
            if not auth.request_authenticated(request):
                return {"error": "unauthorized"}, 401

        self.client = app.test_client()
        patch.object(auth, "get_token", return_value="test-token").start()
        self.context = patch.object(oc, "pane_context", return_value=CONTEXT).start()
        self.addCleanup(patch.stopall)

    def get(self, path="transcript", **params):
        query = {"target": CONTEXT["target"], "generation": CONTEXT["generation"], "session_id": SID, **params}
        return self.client.get("/terminal/opencode/" + path, query_string=query, headers={"X-Assist-Token": "test-token"})

    def test_auth_and_no_store(self):
        response = self.client.get("/terminal/opencode/sessions")
        self.assertEqual(response.status_code, 401)
        self.context.assert_not_called()
        with patch.object(oc, "list_sessions", return_value=[]):
            response = self.get("sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json["generation"], CONTEXT["generation"])

    def test_successful_transcript(self):
        with patch.object(oc, "transcript", return_value={"messages": [], "captured_at": 1}):
            response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["target"], CONTEXT["target"])

    def test_rejects_stale_generation_before_export(self):
        with patch.object(oc, "transcript") as transcript:
            response = self.get(generation="old")
        self.assertEqual(response.status_code, 409)
        transcript.assert_not_called()

    def test_rechecks_generation_after_export_and_listing(self):
        for path, function, result in (("transcript", "transcript", {"messages": ["private"]}),
                                       ("sessions", "list_sessions", [{"title": "private"}])):
            self.context.side_effect = [CONTEXT, {**CONTEXT, "generation": "replaced"}]
            with self.subTest(path=path), patch.object(oc, function, return_value=result):
                response = self.get(path)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn("private", response.text)

    def test_bad_limits_never_export(self):
        with patch.object(oc, "transcript") as transcript:
            for limit in ("no", "0", "501", "-1"):
                self.assertEqual(self.get(limit=limit).status_code, 400)
            transcript.assert_not_called()


if __name__ == "__main__":
    unittest.main()
