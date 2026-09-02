"""Every pane Assist creates is ranked on the memory it actually uses.

The kernel's oom_badness() ADDS oom_score_adj/1000 * (RAM + swap) to a task's
footprint, so a pane inheriting adj=200 carries roughly +7 GB of synthetic
badness and is killed ahead of a genuinely runaway process.  These tests assert
the resulting value in /proc, not that a helper was called.
"""

import subprocess
import sys
import unittest
from pathlib import Path

from shared import tmux
from shared.tmux import ExpectedTargetIdentity


HAS_PROC = Path("/proc/self/oom_score_adj").exists()


def _runner_can_pin_zero():
    if not HAS_PROC:
        return False
    path = Path("/proc/self/oom_score_adj")
    try:
        with path.open("w", encoding="ascii") as target:
            target.write("0")
        return path.read_text(encoding="ascii").strip() == "0"
    except OSError:
        return False


RUNNER_CAN_PIN_ZERO = _runner_can_pin_zero()

# Raises its own adj (always permitted -- it does not cross oom_score_adj_min),
# reports readiness, then idles so the test can act on a live task.
_CHILD = (
    "import sys, time\n"
    "open('/proc/self/oom_score_adj', 'w').write(sys.argv[1])\n"
    "print(open('/proc/self/oom_score_adj').read().strip(), flush=True)\n"
    "time.sleep(30)\n"
)

# As above, but then forks a child AFTER the pin lands and reports the child's
# inherited value -- the agent a pane launches later is what actually gets OOM-killed.
_CHILD_THEN_FORK = (
    "import subprocess, sys, time\n"
    "open('/proc/self/oom_score_adj', 'w').write(sys.argv[1])\n"
    "print(open('/proc/self/oom_score_adj').read().strip(), flush=True)\n"
    "sys.stdin.readline()\n"
    "print(subprocess.run([sys.executable, '-c',\n"
    "    \"print(open('/proc/self/oom_score_adj').read().strip())\"],\n"
    "    capture_output=True, text=True).stdout.strip(), flush=True)\n"
)


def _read_adj(pid):
    return Path(f"/proc/{pid}/oom_score_adj").read_text(encoding="utf-8").strip()


@unittest.skipUnless(HAS_PROC, "oom_score_adj is a Linux /proc interface")
@unittest.skipUnless(
    RUNNER_CAN_PIN_ZERO,
    "runner cannot lower its own oom_score_adj to 0; kernel floor is above zero",
)
class PinOomScoreAdjTests(unittest.TestCase):
    def _spawn(self, adj, program=_CHILD):
        child = subprocess.Popen(
            [sys.executable, "-c", program, str(adj)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        for pipe in (child.stdin, child.stdout):
            self.addCleanup(pipe.close)
        self.assertEqual(child.stdout.readline().strip(), str(adj))
        return child

    def test_pins_a_live_task_to_zero(self):
        child = self._spawn(200)
        self.assertEqual(_read_adj(child.pid), "200")

        pin = tmux.pin_oom_score_adj(child.pid)

        self.assertEqual(_read_adj(child.pid), "0")
        self.assertTrue(pin.ok)
        self.assertEqual((pin.before, pin.after), (200, 0))

    def test_processes_forked_after_the_pin_inherit_zero(self):
        child = self._spawn(200, program=_CHILD_THEN_FORK)

        tmux.pin_oom_score_adj(child.pid)
        child.stdin.write("go\n")
        child.stdin.flush()

        self.assertEqual(child.stdout.readline().strip(), "0")

    def test_a_task_already_at_zero_is_left_alone(self):
        child = self._spawn(0)

        pin = tmux.pin_oom_score_adj(child.pid)

        self.assertEqual(_read_adj(child.pid), "0")
        self.assertTrue(pin.ok)
        self.assertFalse(pin.lowered)

    def test_a_dead_pid_fails_open(self):
        child = self._spawn(200)
        child.kill()
        child.wait()

        pin = tmux.pin_oom_score_adj(child.pid)

        self.assertFalse(pin.ok)
        self.assertIsNone(pin.after)

    def test_creation_pins_both_the_pane_and_its_tmux_server(self):
        server = self._spawn(200)
        pane = self._spawn(200)
        identity = ExpectedTargetIdentity(
            socket_path="/tmp/tmux-1000/default",
            socket_device=1,
            socket_inode=2,
            server_pid=server.pid,
            server_start_time="1",
            session_id="$0",
            window_id="@0",
            pane_id="%0",
            pane_pid=pane.pid,
            pane_start_time="1",
        )

        pins = tmux.pin_created_oom_score_adj(identity)

        self.assertEqual(_read_adj(server.pid), "0")
        self.assertEqual(_read_adj(pane.pid), "0")
        self.assertTrue(pins["server"].ok)
        self.assertTrue(pins["pane"].ok)


class OomScoreAdjFloorTests(unittest.TestCase):
    """oom_score_adj_min is a hard floor only CAP_SYS_RESOURCE may cross.

    It cannot be created unprivileged, so the descent is exercised against a
    simulated floor; the real /proc write path is covered above.
    """

    def _search(self, floor, current):
        attempted = []

        def attempt(value):
            attempted.append(value)
            return value >= floor

        return tmux._lowest_settable(current, attempt), attempted

    def test_descends_to_the_kernel_floor_when_zero_is_refused(self):
        settled, _ = self._search(floor=100, current=200)
        self.assertEqual(settled, 100)

    def test_reaches_zero_when_the_kernel_permits_it(self):
        settled, _ = self._search(floor=0, current=200)
        self.assertEqual(settled, 0)

    def test_never_probes_above_where_the_task_started(self):
        _, attempted = self._search(floor=100, current=200)
        self.assertTrue(attempted)
        self.assertLessEqual(max(attempted), 200)


class CreationPathWiringTests(unittest.TestCase):
    """The pin is on the sole creation chokepoint, before the pane is handed out."""

    def test_create_helper_pins_before_recording_provenance(self):
        source = (Path(__file__).resolve().parents[1] / "shared" / "tmux.py").read_text(
            encoding="utf-8"
        )
        body = source[
            source.index("def _create_tmux_resource") : source.index(
                "def create_tmux_session"
            )
        ]
        self.assertIn("pin_created_oom_score_adj(created_identity)", body)
        self.assertLess(
            body.index("_created_identity("),
            body.index("pin_created_oom_score_adj("),
        )
        self.assertLess(
            body.index("pin_created_oom_score_adj("),
            body.index("registry.record_created("),
        )


if __name__ == "__main__":
    unittest.main()
