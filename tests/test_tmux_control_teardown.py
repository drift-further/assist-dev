"""The two client-side invariants that keep `/type` from killing the tmux server.

`shared/tmux.py` opens one `tmux -C attach-session` control client
per selection and per delivery, and Assist opens a lot of them: the Auto-Yes
scanner calls `expected_target_identity()` for every prompt-bearing pane on
every scan tick.  Two properties of that connection are what stop the server
dying, and both are silent if they regress -- the delivery still succeeds, and
the crash lands on the tmux server minutes later, taking every pane with it.
`tools/release/tmux_teardown_repro.py` is the empirical proof; this is the guard.
"""

import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared import tmux


class ControlConnectionTeardownTests(unittest.TestCase):
    def test_attach_suppresses_control_output(self):
        """`-f no-output` keeps this client out of tmux's output accounting.

        Without it the server queues a %output block for every byte the
        attached session prints, and a client that goes away with blocks
        queued kills the server in control_append_data():
        `fatal: not enough data`.  Reproduced on tmux 3.4 AND 3.7c.
        """
        argv = _attach_argv()
        self.assertIn("-f", argv)
        self.assertEqual(argv[argv.index("-f") + 1], "no-output")
        self.assertLess(argv.index("-f"), argv.index("-t"), "flags precede the target")

    def test_attach_still_addresses_the_pane_over_an_explicit_socket(self):
        argv = _attach_argv()
        self.assertEqual(argv[:4], ["tmux", "-C", "-S", "/tmp/private-sock"])
        self.assertEqual(argv[argv.index("-t") + 1], "%1")
        self.assertIn("attach-session", argv)

    def test_close_waits_for_the_client_before_signalling_it(self):
        """Closing stdin already starts the server's teardown of this client.

        Signalling it in the same breath makes the server run the abrupt
        server_client_lost() path as well, so two cleanups of one client run
        back to back.  wait() must come first; terminate/kill stay as the
        escalation.
        """
        source = inspect.getsource(tmux._TmuxControlConnection.close)
        wait = source.index("self.process.wait(")
        self.assertLess(
            wait,
            source.index("self.process.terminate()"),
            "close() must wait for the ordinary exit before SIGTERM",
        )
        self.assertIn("self.process.kill()", source, "kill stays as the fallback")

    def test_close_escalates_when_the_client_does_not_go(self):
        connection = tmux._TmuxControlConnection.__new__(tmux._TmuxControlConnection)
        connection.process = mock.Mock()
        connection.process.stdin = mock.Mock()
        connection.process.stdout = None
        connection.process.stderr = None
        connection.process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="tmux", timeout=1),
            0,
        ]
        connection._selector = mock.Mock()

        connection.close()

        connection.process.terminate.assert_called_once_with()
        connection.process.kill.assert_not_called()

    def test_failed_paste_deletes_the_private_socket_buffer(self):
        with tempfile.TemporaryDirectory() as raw:
            socket_path = Path(raw) / "private.sock"
            subprocess.run(
                ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", "buffer-cleanup"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            connection = None
            try:
                connection = tmux._TmuxControlConnection(
                    str(socket_path), "buffer-cleanup:0.0"
                )
                with self.assertRaisesRegex(OSError, "tmux control command failed"):
                    connection.send_batch("%999999", text="private-paste-value")

                buffers = subprocess.run(
                    [
                        "tmux",
                        "-S",
                        str(socket_path),
                        "list-buffers",
                        "-F",
                        "#{buffer_name}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertNotIn("assist-v16-", buffers.stdout)
            finally:
                if connection is not None:
                    connection.close()
                subprocess.run(
                    ["tmux", "-S", str(socket_path), "kill-server"],
                    capture_output=True,
                    timeout=5,
                )


def _attach_argv():
    """The argv `_TmuxControlConnection` hands to Popen, with nothing spawned."""
    with mock.patch.object(tmux.subprocess, "Popen") as popen:
        popen.return_value.stdin = None
        popen.return_value.stdout = None
        with mock.patch.object(tmux._TmuxControlConnection, "close", lambda self: None):
            try:
                tmux._TmuxControlConnection("/tmp/private-sock", "%1")
            except OSError:
                pass
    return list(popen.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
