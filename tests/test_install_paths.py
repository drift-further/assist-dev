"""Where the process controller keeps its PID file and log, and what it reads first.

The defaults used to be fixed names in the shared /tmp: a second checkout on
the same host (a user trying a new release beside the old one) shared the PID
file, so `assist stop` in the trial copy killed the real server. And
assist-ctl computed the port before sourcing `.env`, so a port set only there
was ignored when the script ran directly.

The live host was started under the old defaults, so a server whose legacy
/tmp PID file names THIS checkout's serve.py is adopted -- and one that runs
anything else is not. Both directions are pinned here.
"""

import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cli import config as cli_config


ROOT = Path(__file__).resolve().parents[1]

# A stand-in for serve.py: answers /health on the port it is given, nothing else.
_FAKE_SERVE = """
import http.server, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *a): pass
print("fake serve up", flush=True)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _wait_for_exec(proc, marker):
    """Popen returns after fork; until exec, /proc shows the PARENT's argv."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker in (cli_config.process_argv(proc.pid) or []):
            return proc
        time.sleep(0.02)
    raise AssertionError(f"process {proc.pid} never exec'd with {marker}")


_SLEEP_SCRIPT = "import time\ntime.sleep(60)\n"


def _spawn(case, argv, executable=None, env=None):
    proc = subprocess.Popen([str(a) for a in argv], executable=executable, env=env)
    case.addCleanup(proc.wait)
    case.addCleanup(proc.kill)
    return _wait_for_exec(proc, str(argv[-1]))


def _spawn_server(case, script, *args, python=None):
    """A process that really RUNS `script` the way assist-ctl does: <python> <script> ..."""
    script = Path(script)
    if not script.exists():
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(_SLEEP_SCRIPT)
    return _spawn(case, [python or sys.executable, script, *args])


def _venv_python(home):
    """<home>/.venv/bin/python as a symlink to a real interpreter, like a real venv."""
    python = Path(home) / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.symlink_to(os.path.realpath(sys.executable))
    return python


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Checkout:
    """A throwaway copy of assist-ctl with its own .env and fake serve.py."""

    def __init__(self, root: Path, env_lines=(), name="checkout"):
        self.home = root / name
        self.home.mkdir()
        script = (ROOT / "assist-ctl").read_text()
        # Hard stop, not a failure: a control script that still defaults to the
        # shared /tmp PID file would read the LIVE server's PID from it, and
        # this class's `stop` would kill that server. That happened once, when
        # these tests were run against the pre-change script.
        if "ASSIST_PID_FILE:-/tmp" in script:
            raise RuntimeError("assist-ctl still defaults to /tmp; refusing to run it")
        shutil.copy2(ROOT / "assist-ctl", self.home / "assist-ctl")
        (self.home / "serve.py").write_text(_FAKE_SERVE)
        (self.home / ".env").write_text("".join(f"{line}\n" for line in env_lines))
        self.state_home = root / "state"
        self.legacy_pid = root / "legacy-assist-server.pid"
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root / "home"),
            "XDG_STATE_HOME": str(self.state_home),
            "ASSIST_LEGACY_PID_FILE": str(self.legacy_pid),
        }

    def ctl(self, *args, timeout=30):
        if args and args[0] in {"start", "stop", "restart", "run"}:
            self._refuse_unless_contained()
        return subprocess.run(
            [str(self.home / "assist-ctl"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _refuse_unless_contained(self):
        # A verb that can kill or start a server runs only when every file it
        # reads or writes is inside this throwaway root and the port is not
        # the live one. Checked through the script's own `paths` verb, which
        # starts and stops nothing.
        root = self.home.parent.resolve()
        paths = self.paths()
        for key in ("pid_file", "log_file"):
            if not Path(paths[key]).resolve().is_relative_to(root):
                raise RuntimeError(f"refusing assist-ctl: {key} {paths[key]} is outside {root}")
        if not Path(self.env["ASSIST_LEGACY_PID_FILE"]).resolve().is_relative_to(root):
            raise RuntimeError("refusing assist-ctl: the legacy PID file is outside the scratch root")
        if paths["port"] == "8089":
            raise RuntimeError("refusing assist-ctl: port 8089 is the live server's")

    def paths(self):
        out = self.ctl("paths")
        assert out.returncode == 0, out.stderr
        return dict(line.split("=", 1) for line in out.stdout.splitlines())


class ControlScriptPathTests(unittest.TestCase):
    def test_defaults_live_in_the_per_user_state_dir(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw))
            paths = checkout.paths()
            state_dir = checkout.state_home / "drift-assist"
            self.assertEqual(paths["pid_file"], str(state_dir / "assist.pid"))
            self.assertEqual(paths["log_file"], str(state_dir / "assist.log"))
            self.assertNotIn("/tmp/assist-server", "".join(paths.values()))

    def test_port_set_only_in_dotenv_is_honoured(self):
        # F10: the port used to be read before .env was sourced.
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), ["ASSIST_PORT=9123"])
            self.assertEqual(checkout.paths()["port"], "9123")

    def test_explicit_paths_in_dotenv_still_win(self):
        with tempfile.TemporaryDirectory() as raw:
            custom = Path(raw) / "custom"
            checkout = _Checkout(
                Path(raw),
                [f"ASSIST_PID_FILE={custom}/a.pid", f"ASSIST_LOG_FILE={custom}/a.log"],
            )
            paths = checkout.paths()
            self.assertEqual(paths["pid_file"], f"{custom}/a.pid")
            self.assertEqual(paths["log_file"], f"{custom}/a.log")

    def test_cli_resolves_the_same_defaults_as_the_control_script(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw))
            env = {
                "HOME": checkout.env["HOME"],
                "XDG_STATE_HOME": checkout.env["XDG_STATE_HOME"],
                "XDG_CONFIG_HOME": str(Path(raw) / "config"),
                "ASSIST_HOME": str(checkout.home),
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                cli_config, "LEGACY_PID_FILE", checkout.legacy_pid
            ):
                resolved = cli_config.resolve()
            paths = checkout.paths()
            self.assertEqual(str(resolved.pid_file), paths["pid_file"])
            self.assertEqual(str(resolved.log_file), paths["log_file"])

    def test_start_creates_a_private_state_dir_rotates_and_stops(self):
        with tempfile.TemporaryDirectory() as raw:
            port = _free_port()
            checkout = _Checkout(
                Path(raw), [f"ASSIST_PORT={port}", "ASSIST_LOG_MAX_BYTES=100"]
            )
            state_dir = checkout.state_home / "drift-assist"
            state_dir.mkdir(parents=True)
            log = state_dir / "assist.log"
            log.write_text("x" * 500)
            try:
                started = checkout.ctl("start")
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                self.assertIn(f"on port {port}", started.stdout)
                self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
                self.assertTrue((state_dir / "assist.pid").is_file())
                # The oversize log moved aside; the new one is fresh.
                self.assertEqual((state_dir / "assist.log.1").read_text(), "x" * 500)
                self.assertNotIn("x" * 500, log.read_text())
            finally:
                stopped = checkout.ctl("stop")
            self.assertIn("Stopped", stopped.stdout)
            self.assertFalse((state_dir / "assist.pid").exists())

    def test_a_small_log_is_not_rotated(self):
        with tempfile.TemporaryDirectory() as raw:
            port = _free_port()
            checkout = _Checkout(Path(raw), [f"ASSIST_PORT={port}"])
            state_dir = checkout.state_home / "drift-assist"
            state_dir.mkdir(parents=True)
            (state_dir / "assist.log").write_text("kept\n")
            try:
                self.assertEqual(checkout.ctl("start").returncode, 0)
            finally:
                checkout.ctl("stop")
            self.assertFalse((state_dir / "assist.log.1").exists())
            self.assertTrue((state_dir / "assist.log").read_text().startswith("kept\n"))


class HarnessContainmentTests(unittest.TestCase):
    """The helper itself refuses to start or stop anything it cannot contain."""

    def test_the_live_port_is_refused_before_the_script_runs(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), ["ASSIST_PORT=8089"])
            with mock.patch.object(subprocess, "run", wraps=subprocess.run) as run:
                with self.assertRaisesRegex(RuntimeError, "8089"):
                    checkout.ctl("stop")
            self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["paths"])

    def test_a_pid_file_outside_the_scratch_root_is_refused(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as elsewhere:
            checkout = _Checkout(
                Path(raw), [f"ASSIST_PORT={_free_port()}", f"ASSIST_PID_FILE={elsewhere}/a.pid"]
            )
            with self.assertRaisesRegex(RuntimeError, "pid_file"):
                checkout.ctl("start")


class LegacyPidAdoptionTests(unittest.TestCase):
    """A server started under the /tmp defaults, before this change."""

    def _spawn(self, script_path):
        return _spawn_server(self, script_path, "--port", str(_free_port()))

    def test_this_checkouts_legacy_server_is_adopted_and_migrated(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), [f"ASSIST_PORT={_free_port()}"])
            proc = self._spawn(checkout.home / "serve.py")
            checkout.legacy_pid.write_text(f"{proc.pid}\n")

            with mock.patch.dict(
                os.environ,
                {"HOME": checkout.env["HOME"], "XDG_STATE_HOME": checkout.env["XDG_STATE_HOME"],
                 "XDG_CONFIG_HOME": str(Path(raw) / "config"), "ASSIST_HOME": str(checkout.home)},
                clear=True,
            ), mock.patch.object(cli_config, "LEGACY_PID_FILE", checkout.legacy_pid):
                self.assertEqual(cli_config.resolve().pid_file, checkout.legacy_pid)

            status = checkout.ctl("status")
            self.assertIn(f"PID {proc.pid}", status.stdout)
            new_pid = checkout.state_home / "drift-assist" / "assist.pid"
            self.assertEqual(new_pid.read_text().strip(), str(proc.pid))
            self.assertFalse(checkout.legacy_pid.exists())

            stopped = checkout.ctl("stop")
            self.assertIn(f"Stopped (PID {proc.pid})", stopped.stdout)
            proc.wait(timeout=5)

    def test_another_checkouts_legacy_server_is_left_alone(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), [f"ASSIST_PORT={_free_port()}"])
            other = Path(raw) / "other-checkout" / "serve.py"
            proc = self._spawn(other)
            checkout.legacy_pid.write_text(f"{proc.pid}\n")

            with mock.patch.dict(
                os.environ,
                {"HOME": checkout.env["HOME"], "XDG_STATE_HOME": checkout.env["XDG_STATE_HOME"],
                 "XDG_CONFIG_HOME": str(Path(raw) / "config"), "ASSIST_HOME": str(checkout.home)},
                clear=True,
            ), mock.patch.object(cli_config, "LEGACY_PID_FILE", checkout.legacy_pid):
                self.assertNotEqual(cli_config.resolve().pid_file, checkout.legacy_pid)

            stopped = checkout.ctl("stop")
            self.assertIn("not running", stopped.stdout)
            time.sleep(0.2)
            self.assertIsNone(proc.poll(), "another checkout's server was killed")
            self.assertTrue(checkout.legacy_pid.exists())


class PidOwnershipTests(unittest.TestCase):
    """The per-user PID file is shared by every checkout of the same user.

    Moving it out of /tmp did not by itself stop a trial checkout from reading
    the main checkout's record and signalling that PID. Ownership is now
    checked on every read and again right before the signal, by exact argv.
    """

    def _pair(self, raw, extra=()):
        main = _Checkout(Path(raw), [f"ASSIST_PORT={_free_port()}", *extra], name="main")
        trial = _Checkout(Path(raw), [f"ASSIST_PORT={_free_port()}", *extra], name="trial")
        return main, trial

    def _alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _spawn(self, *argv):
        # NOT a server: python -c sleep, with whatever arguments it is given.
        return _spawn(self, [sys.executable, "-c", "import time; time.sleep(60)", *argv])

    def _check_trial_cannot_touch_main(self, main, trial, pid_file):
        started = main.ctl("start")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        try:
            record = pid_file.read_text()
            main_pid = int(record)
            for verb in ("stop", "restart", "start"):
                with self.subTest(verb=verb):
                    result = trial.ctl(verb)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("another checkout", result.stderr)
                    self.assertTrue(self._alive(main_pid), f"trial `{verb}` killed main")
                    self.assertEqual(pid_file.read_text(), record, f"trial `{verb}` rewrote it")
            self.assertIn("another checkout", trial.ctl("status").stdout)
        finally:
            stopped = main.ctl("stop")
        # ...and the owner still controls its own server.
        self.assertIn(f"Stopped (PID {main_pid})", stopped.stdout)

    def test_a_second_checkout_cannot_stop_restart_or_overwrite_the_default_record(self):
        with tempfile.TemporaryDirectory() as raw:
            main, trial = self._pair(raw)
            pid_file = main.state_home / "drift-assist" / "assist.pid"
            self._check_trial_cannot_touch_main(main, trial, pid_file)

    def test_the_same_holds_for_a_shared_explicit_pid_file(self):
        with tempfile.TemporaryDirectory() as raw:
            shared = Path(raw) / "shared" / "a.pid"
            main, trial = self._pair(raw, [f"ASSIST_PID_FILE={shared}"])
            self._check_trial_cannot_touch_main(main, trial, shared)

    def test_a_reused_pid_is_never_signalled_and_the_record_is_replaced(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), [f"ASSIST_PORT={_free_port()}"])
            pid_file = checkout.state_home / "drift-assist" / "assist.pid"
            pid_file.parent.mkdir(parents=True)
            unrelated = self._spawn("--some-other-program")
            pid_file.write_text(f"{unrelated.pid}\n")

            stopped = checkout.ctl("stop")
            self.assertEqual(stopped.returncode, 0)
            self.assertIn("not running", stopped.stdout)
            time.sleep(0.2)
            self.assertIsNone(unrelated.poll(), "a reused PID was signalled")
            self.assertFalse(pid_file.exists())

            pid_file.write_text(f"{unrelated.pid}\n")
            try:
                started = checkout.ctl("start")
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                self.assertNotEqual(pid_file.read_text().strip(), str(unrelated.pid))
            finally:
                checkout.ctl("stop")
            self.assertIsNone(unrelated.poll())

    def test_a_path_that_only_contains_this_serve_py_is_not_ours(self):
        # Substring matching took `<home>/serve.py.orig` for this server.
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), [f"ASSIST_PORT={_free_port()}"])
            pid_file = checkout.state_home / "drift-assist" / "assist.pid"
            pid_file.parent.mkdir(parents=True)
            lookalike = self._spawn(str(checkout.home / "serve.py") + ".orig")
            pid_file.write_text(f"{lookalike.pid}\n")
            checkout.ctl("stop")
            time.sleep(0.2)
            self.assertIsNone(lookalike.poll())
            self.assertFalse(cli_config.runs_checkout(lookalike.pid, checkout.home))

    def test_the_cli_uses_the_same_exact_rule(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "main"
            ours = _spawn_server(self, home / "serve.py", "--port", "1")
            other = _spawn_server(self, Path(raw) / "main2" / "serve.py")
            carried = self._spawn(str(home / "serve.py"))  # the path as data only
            self.assertEqual(cli_config.server_owner(ours.pid, home), "ours")
            self.assertEqual(cli_config.server_owner(other.pid, home), "foreign")
            self.assertEqual(cli_config.server_owner(carried.pid, home), "stale")


class InvocationIdentityTests(unittest.TestCase):
    """Ours means the process RUNS this serve.py, not that it names it.

    Checked in both classifiers -- assist-ctl's `owner` verb and
    cli.config.server_owner -- through /proc and through the `ps` fallback
    that macOS uses, where argv arrives joined with spaces.
    """

    def _both(self, checkout, pid, proc_root=None):
        env = dict(checkout.env)
        if proc_root is not None:
            env["ASSIST_PROC_ROOT"] = proc_root
        shell = subprocess.run(
            [str(checkout.home / "assist-ctl"), "owner", str(pid)],
            env=env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        root = Path(proc_root) if proc_root is not None else cli_config.PROC_ROOT
        with mock.patch.object(cli_config, "PROC_ROOT", root):
            python = cli_config.server_owner(pid, checkout.home)
        return shell, python

    def _each_branch(self, checkout, pid, expected):
        for branch, proc_root in (("/proc", None), ("ps fallback", "/nonexistent-proc")):
            with self.subTest(branch=branch):
                self.assertEqual(self._both(checkout, pid, proc_root), (expected, expected))

    def test_the_legacy_invocation_is_ours(self):
        # How main runs today: <venv>/bin/python <checkout>/serve.py --port 8089.
        # (The fake serve.py is not started; a sleep script stands in.)
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw))
            (checkout.home / "serve.py").write_text(_SLEEP_SCRIPT)
            proc = _spawn_server(
                self, checkout.home / "serve.py", "--port", "8089",
                python=_venv_python(checkout.home),
            )
            self._each_branch(checkout, proc.pid, "ours")

    def test_a_reader_of_the_script_is_not_ours(self):
        tail = shutil.which("tail")
        if tail is None:
            self.skipTest("no tail")
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw))
            reader = _spawn(self, [tail, "-f", checkout.home / "serve.py"])
            self._each_branch(checkout, reader.pid, "stale")

    def test_an_editor_on_the_script_is_not_ours(self):
        # argv[0] "vim" with the exact path as argv[1]: the shape of an editor.
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw))
            (checkout.home / "serve.py").write_text(_SLEEP_SCRIPT)
            # PYTHONHOME: some interpreters find their stdlib from argv[0], which is "vim" here.
            editor = _spawn(
                self, ["vim", checkout.home / "serve.py"], executable=sys.executable,
                env={**os.environ, "PYTHONHOME": sys.base_prefix},
            )
            self._each_branch(checkout, editor.pid, "stale")

    def test_python_carrying_the_path_as_an_argument_is_not_ours(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw))
            carrier = _spawn(
                self, [sys.executable, "-c", "import time; time.sleep(60)", checkout.home / "serve.py"]
            )
            self._each_branch(checkout, carrier.pid, "stale")

    def test_a_reader_of_the_interpreter_and_the_script_is_not_ours(self):
        # judge3's probe: `tail -f <dir>/python <dir>/serve.py`. Flattened by
        # ps, its text ENDS with "<python> <serve.py>"; only an exact match
        # from the start counts. Here with this checkout's own two paths.
        tail = shutil.which("tail")
        if tail is None:
            self.skipTest("no tail")
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), name="My Assist")
            python = _venv_python(checkout.home)
            reader = _spawn(self, [tail, "-f", python, checkout.home / "serve.py"])
            self._each_branch(checkout, reader.pid, "stale")

    def test_the_resolved_venv_interpreter_is_ours_through_ps(self):
        # A launcher that resolved the venv symlink before exec.
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), name="My Assist")
            (checkout.home / "serve.py").write_text(_SLEEP_SCRIPT)
            real = os.path.realpath(_venv_python(checkout.home))
            proc = _spawn_server(self, checkout.home / "serve.py", "--port", "1", python=real)
            self._each_branch(checkout, proc.pid, "ours")

    def test_an_interpreter_that_is_not_this_checkouts_is_not_ours_through_ps(self):
        # ps text cannot prove which interpreter ran; only known paths count.
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw))
            (checkout.home / "serve.py").write_text(_SLEEP_SCRIPT)
            elsewhere = Path(raw) / "elsewhere" / "python"
            elsewhere.parent.mkdir()
            elsewhere.symlink_to(os.path.realpath(sys.executable))
            proc = _spawn_server(self, checkout.home / "serve.py", python=elsewhere)
            self.assertEqual(
                self._both(checkout, proc.pid, "/nonexistent-proc"), ("stale", "stale")
            )

    def test_a_checkout_reached_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw))
            (checkout.home / "serve.py").write_text(_SLEEP_SCRIPT)
            link = Path(raw) / "link"
            link.symlink_to(checkout.home)
            via_link = _spawn_server(self, link / "serve.py", "--port", "1")
            self.assertEqual(self._both(checkout, via_link.pid), ("ours", "ours"))

    def test_checkouts_with_spaces_through_both_branches(self):
        with tempfile.TemporaryDirectory() as raw:
            mine = _Checkout(Path(raw), name="My Assist")
            (mine.home / "serve.py").write_text(_SLEEP_SCRIPT)
            owner = _spawn_server(
                self, mine.home / "serve.py", "--port", "1", python=_venv_python(mine.home)
            )
            other = _spawn_server(self, Path(raw) / "Other Assist" / "serve.py", "--port", "1")
            self._each_branch(mine, owner.pid, "ours")
            self._each_branch(mine, other.pid, "foreign")

    def test_legacy_adoption_with_spaces_through_the_ps_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = _Checkout(Path(raw), [f"ASSIST_PORT={_free_port()}"], name="My Assist")
            proc = _spawn_server(
                self, checkout.home / "serve.py", "--port", str(_free_port()),
                python=_venv_python(checkout.home),
            )
            checkout.legacy_pid.write_text(f"{proc.pid}\n")
            with mock.patch.object(cli_config, "PROC_ROOT", Path("/nonexistent-proc")), \
                    mock.patch.object(cli_config, "LEGACY_PID_FILE", checkout.legacy_pid):
                self.assertEqual(cli_config._legacy_server_pid(checkout.home), proc.pid)
            checkout.env["ASSIST_PROC_ROOT"] = "/nonexistent-proc"
            status = checkout.ctl("status")
            self.assertIn(f"PID {proc.pid}", status.stdout)
            self.assertFalse(checkout.legacy_pid.exists())


if __name__ == "__main__":
    unittest.main()
