"""A service restart or stop must not take the user's tmux sessions with it.

When no tmux server exists yet, the first pane Assist opens starts one -- as a
child of the service, so it sits in the service's cgroup. systemd's default
KillMode=control-group then kills that tmux server, and every agent running in
it, on `assist restart`, `assist expose`, `assist service` stop, or failure
cleanup. A test against a pre-existing tmux server cannot see this.

This runs the real thing: a transient `systemd-run --user` service built from
the [Service] lines `cli.service.systemd_unit()` generates, running a scratch
copy of this checkout on a free port with its own HOME and tmux socket. It
opens a pane through /terminal/launch starting from NO tmux server, restarts
and stops the unit, and checks the pane's process. The same harness with
KillMode=control-group must lose the pane, which proves it detects the defect.
Skipped where there is no systemd user manager.
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

from cli import service


ROOT = Path(__file__).resolve().parents[1]


def _user_systemd():
    if not shutil.which("systemd-run") or not shutil.which("tmux"):
        return False
    probe = subprocess.run(
        ["systemctl", "--user", "show", "--property=Version"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0 and "Version=" in probe.stdout


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _service_properties(unit_text):
    """The [Service] settings systemd-run can take as -p, from the generated unit."""
    props = {}
    section = None
    for line in unit_text.splitlines():
        if line.startswith("["):
            section = line
            continue
        if section == "[Service]" and "=" in line:
            key, value = line.split("=", 1)
            if key not in {"ExecStart", "WorkingDirectory", "Environment"}:
                props[key] = value
    return props


@unittest.skipUnless(_user_systemd(), "needs a systemd --user manager, systemd-run and tmux")
class ServiceKeepsTmuxAliveTests(unittest.TestCase):
    def setUp(self):
        # Short root: a tmux socket path must fit in sun_path.
        self.root = Path(tempfile.mkdtemp(prefix="a621-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "app"
        files = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode().split("\0")
        for rel in filter(None, files):
            if rel.startswith("tests/") or not (ROOT / rel).is_file():
                continue
            target = self.home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, target)
        (self.home / ".venv").symlink_to(ROOT / ".venv")
        (self.root / "home").mkdir()
        self.tmux_dir = self.root / "t"
        self.tmux_dir.mkdir(mode=0o700)
        self.socket = self.tmux_dir / f"tmux-{os.getuid()}" / "default"
        self.port = _free_port()
        self.unit = f"assist-lifecycle-{uuid.uuid4().hex[:10]}"
        self.addCleanup(self._teardown)

    def _teardown(self):
        subprocess.run(["systemctl", "--user", "stop", self.unit], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", self.unit], capture_output=True)
        if self.socket.exists():
            subprocess.run(["tmux", "-S", str(self.socket), "kill-server"], capture_output=True)

    def _env(self):
        return {
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "TMUX_TMPDIR": str(self.tmux_dir),
            "ASSIST_PORT": str(self.port),
            "ASSIST_PID_FILE": str(self.root / "state" / "a.pid"),
            "ASSIST_LOG_FILE": str(self.root / "state" / "a.log"),
            "ASSIST_AUTH_TOKEN_PATH": str(self.root / "token"),
            "ASSIST_PROJECTS_DIR": str(self.root / "home"),
        }

    def _start_unit(self, kill_mode=None):
        path_env = os.environ.get("PATH", "/usr/bin:/bin")
        props = _service_properties(service.systemd_unit(self.home, path_env))
        if kill_mode is not None:
            props["KillMode"] = kill_mode
        command = ["systemd-run", "--user", "--quiet", f"--unit={self.unit}",
                   f"--working-directory={self.home}", f"--setenv=PATH={path_env}"]
        command += [f"-p{key}={value}" for key, value in props.items()]
        command += [f"--setenv={k}={v}" for k, v in self._env().items()]
        command += [str(self.home / "assist-ctl"), "run"]
        subprocess.run(command, check=True, capture_output=True)
        self._wait_healthy()

    def _wait_healthy(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        log = self.root / "state" / "a.log"
        self.fail("scratch Assist never became healthy:\n" + (log.read_text() if log.exists() else ""))

    def _launch_pane(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/terminal/launch",
            data=json.dumps({"project": "lifecycle", "skip_init": True,
                             "cwd": str(self.root / "home")}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Assist-Token": (self.root / "token").read_text().strip()},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            self.assertEqual(response.status, 200)
        panes = subprocess.run(
            ["tmux", "-S", str(self.socket), "list-panes", "-a", "-F", "#{pane_pid}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(len(panes), 1, panes)
        return int(panes[0])

    def _in_unit_cgroup(self, pid):
        return f"/{self.unit}.service" in Path(f"/proc/{pid}/cgroup").read_text()

    def _lifecycle(self, kill_mode=None):
        self.assertFalse(self.socket.exists(), "the test must start with no tmux server")
        self._start_unit(kill_mode)
        pane = self._launch_pane()
        # The precondition of the defect: the pane lives in the service's cgroup.
        self.assertTrue(self._in_unit_cgroup(pane))

        subprocess.run(["systemctl", "--user", "restart", self.unit], check=True)
        self._wait_healthy()
        after_restart = _alive(pane)
        subprocess.run(["systemctl", "--user", "stop", self.unit], check=True)
        time.sleep(0.5)
        return after_restart, _alive(pane)

    def test_the_generated_unit_keeps_the_pane_through_restart_and_stop(self):
        self.assertEqual(
            _service_properties(service.systemd_unit(self.home, "/bin")).get("KillMode"),
            "process",
        )
        after_restart, after_stop = self._lifecycle()
        self.assertTrue(after_restart, "assist restart killed the tmux pane")
        self.assertTrue(after_stop, "stopping the service killed the tmux pane")

    def test_the_default_kill_mode_loses_the_pane(self):
        # Control: the harness really does detect the defect it guards against.
        after_restart, _after_stop = self._lifecycle(kill_mode="control-group")
        self.assertFalse(after_restart)


if __name__ == "__main__":
    unittest.main()
