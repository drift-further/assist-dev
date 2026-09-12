"""tmux ids pass through tmux_exact_target untouched, so a pane addressed by id
still resolves.

tmux_exact_target() pins session NAMES as `=name:` so a dead name cannot
prefix-match another session. That form is for names only: on tmux 3.7c `%297`
resolves while `=%297:` resolves to nothing, and `@258` resolves while `=@258:`
does not. routes/input.py asks pane_awaits_secret(expected.pane_id), so the
server-side password-prompt check on /type returned False for every request,
and a password typed without the client's secret flag went through strip,
first-word case fix, [handle] expansion and history. The route tests never saw
it because they patch pane_awaits_secret out, which is why the last class here
runs it against a real tmux server.

Run: .venv/bin/python3 -m unittest tests.test_tmux_id_targets
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.tmux import pane_awaits_secret, tmux_exact_target


# The socket tmux uses when nothing scopes it — the developer's REAL server.
AMBIENT_TMUX_SOCKET = Path(f"/tmp/tmux-{os.getuid()}/default")


class ExactTargetFormTests(unittest.TestCase):
    def test_ids_pass_through_unchanged(self):
        for target in ("%12", "@3", "$4"):
            with self.subTest(target=target):
                self.assertEqual(tmux_exact_target(target), target)

    def test_names_are_still_pinned_exactly(self):
        self.assertEqual(tmux_exact_target("work"), "=work:")
        self.assertEqual(tmux_exact_target("work:1.2"), "=work:1.2")

    def test_a_name_that_only_starts_like_an_id_is_still_pinned(self):
        self.assertEqual(tmux_exact_target("%notanid"), "=%notanid:")


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class RealTmuxIdTargetTests(unittest.TestCase):
    """Against a throwaway tmux server, never the ambient one.

    Every tmux call is scoped to this test's TMUX_TMPDIR, and teardown passes
    the socket explicitly with -S. A bare `tmux kill-server` here would take
    down the developer's real server and every pane in it.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="assist-tmuxid-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        env = patch.dict(os.environ, {"TMUX_TMPDIR": self.tmpdir})
        env.start()
        self.addCleanup(env.stop)
        # $TMUX outranks TMUX_TMPDIR, so inside tmux it would aim every bare
        # `tmux` call in the code under test at the real server.
        os.environ.pop("TMUX", None)

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

        self.tmux("new-session", "-d", "-s", "prompt", "-x", "80", "-y", "10",
                  "printf '[sudo] password for test: '; sleep 60")
        self.tmux("new-session", "-d", "-s", "plain", "-x", "80", "-y", "10",
                  "printf 'hello\\n'; sleep 60")
        self.wait_for_output("prompt", "password for test")
        self.wait_for_output("plain", "hello")

    def tmux(self, *arguments):
        return subprocess.run(
            ["tmux", "-S", str(self.socket), *arguments],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()

    def wait_for_output(self, session, text):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if text in self.tmux("capture-pane", "-p", "-t", f"={session}:"):
                return
            time.sleep(0.05)
        self.fail(f"{session} never printed {text!r}")

    def ids(self, session):
        return self.tmux("display-message", "-p", "-t", f"={session}:",
                         "#{pane_id} #{window_id}").split()

    def test_a_password_prompt_is_seen_when_the_pane_is_addressed_by_id(self):
        pane_id, _window_id = self.ids("prompt")
        self.assertTrue(pane_awaits_secret(pane_id))

    def test_the_session_name_form_still_works(self):
        self.assertTrue(pane_awaits_secret("prompt"))

    def test_a_pane_without_a_prompt_is_not_a_secret_prompt_by_id(self):
        pane_id, _window_id = self.ids("plain")
        self.assertFalse(pane_awaits_secret(pane_id))

    def test_a_window_id_resolves_through_the_exact_form(self):
        pane_id, window_id = self.ids("prompt")
        resolved = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_exact_target(window_id),
             "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        self.assertEqual(resolved, pane_id)


if __name__ == "__main__":
    unittest.main()
