"""Arbitrary key combos on /key: the grammar, the route, and the bytes that land.

`/key` takes `{"combo": {"ctrl", "alt", "shift", "key"}}` beside its fixed
`keys` allowlist. shared/key_combo.py turns one combo into one tmux key name.
The mappings below are what tmux 3.7c was measured to deliver to a raw pane:
`S-a` arrives as `a` and `S-Tab` as a plain Tab, so Shift is folded into the
uppercase letter and into BTab. A name tmux cannot parse (`C-BSpace`, `C-#`) is
typed into the pane as literal text, so those combos are refused.

The last class runs combos through the real route into a throwaway tmux server
and reads the bytes a raw-mode pane received. It also pins the control-mode
quoting of `~`: tmux expands a word-leading `~` to $HOME even inside double
quotes, so the `~` key typed "/home/<user>" and /type text such as `~/notes`
arrived as `/home/<user>/notes`.

Run: .venv/bin/python3 -m unittest tests.test_key_combo
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from routes import input as input_routes
from shared import tmux
from shared.key_combo import ComboError, combo_to_tmux


def combo(key, *, ctrl=False, alt=False, shift=False):
    return {"ctrl": ctrl, "alt": alt, "shift": shift, "key": key}


def _enter(case, manager):
    """TestCase.enterContext, which only exists from Python 3.11; the floor is 3.10."""
    value = manager.__enter__()
    case.addCleanup(manager.__exit__, None, None, None)
    return value


class GrammarAcceptsTests(unittest.TestCase):
    CASES = [
        (combo("a"), "a"),
        (combo("a", ctrl=True), "C-a"),
        (combo("a", alt=True), "M-a"),
        (combo("a", shift=True), "A"),
        (combo("A"), "A"),
        (combo("a", ctrl=True, shift=True), "C-A"),
        (combo("x", ctrl=True, alt=True, shift=True), "C-M-X"),
        (combo("/", ctrl=True), "C-/"),
        (combo(";", alt=True), "M-;"),
        (combo('"'), '"'),
        (combo("~"), "~"),
        (combo(" "), "Space"),
        (combo("Space", ctrl=True), "C-Space"),
        (combo("F1"), "F1"),
        (combo("F12", shift=True), "S-F12"),
        (combo("F5", ctrl=True, shift=True), "C-S-F5"),
        (combo("Left", ctrl=True, alt=True, shift=True), "C-M-S-Left"),
        (combo("Up", alt=True), "M-Up"),
        (combo("Home", shift=True), "S-Home"),
        (combo("End"), "End"),
        (combo("PgUp"), "PPage"),
        (combo("PgDn", ctrl=True), "C-NPage"),
        (combo("Insert"), "IC"),
        (combo("Delete", ctrl=True), "C-DC"),
        (combo("Tab"), "Tab"),
        (combo("Tab", shift=True), "BTab"),
        (combo("Tab", ctrl=True, shift=True), "C-BTab"),
        (combo("Escape"), "Escape"),
        (combo("Escape", alt=True), "M-Escape"),
        (combo("Enter", shift=True), "S-Enter"),
        (combo("BSpace", alt=True), "M-BSpace"),
        ({"key": "q"}, "q"),
    ]

    def test_each_combo_maps_to_the_exact_tmux_name(self):
        for value, expected in self.CASES:
            with self.subTest(combo=value):
                self.assertEqual(combo_to_tmux(value), expected)


class GrammarRejectsTests(unittest.TestCase):
    CASES = [
        "not a dict",
        ["ctrl", "a"],
        {},
        {"key": ""},
        {"key": None},
        {"key": 5},
        {"key": "ab"},
        {"key": "echo hi"},
        {"key": "C-a"},
        {"key": "F13"},
        {"key": "F0"},
        {"key": "f5"},
        {"key": "Page_Up"},
        {"key": "é"},
        {"key": "\n"},
        {"key": "\x1b"},
        {"key": "\x7f"},
        {"key": "a", "ctrl": "true"},
        {"key": "a", "alt": 1},
        {"key": "a", "meta": True},
        {"key": "a", "text": "rm -rf ~"},
        combo("1", shift=True),
        combo("/", shift=True),
        combo("#", ctrl=True),
        combo("*", ctrl=True, alt=True),
        combo("BSpace", ctrl=True),
        combo("Escape", ctrl=True),
    ]

    def test_input_outside_the_grammar_is_refused(self):
        for value in self.CASES:
            with self.subTest(combo=value), self.assertRaises(ComboError):
                combo_to_tmux(value)


def _app():
    app = Flask(__name__)
    app.register_blueprint(input_routes.input_bp)
    return app.test_client()


class KeyRouteComboTests(unittest.TestCase):
    def setUp(self):
        self.client = _app()
        self.expected = object()
        _enter(self, mock.patch.object(
            input_routes, "expected_target_identity", return_value=self.expected))
        self.deliver = _enter(self, mock.patch.object(
            input_routes, "generation_bound_delivery",
            return_value=tmux.DeliveryResult("delivered")))
        _enter(self, mock.patch.object(input_routes.state, "touch_activity"))

    def test_a_combo_is_delivered_as_one_tmux_key(self):
        response = self.client.post("/key", json={
            "target": "work", "combo": combo("F5", ctrl=True, shift=True)})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["sent"], "C-S-F5")
        self.deliver.assert_called_once_with(self.expected, keys=("C-S-F5",))

    def test_an_invalid_combo_is_400_and_nothing_is_sent(self):
        for value in ({"key": "echo hi"}, combo("#", ctrl=True), {"key": "a", "ctrl": "yes"}, "C-a"):
            with self.subTest(combo=value):
                response = self.client.post("/key", json={"target": "work", "combo": value})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
        self.deliver.assert_not_called()

    def test_combo_and_keys_together_are_refused(self):
        response = self.client.post("/key", json={
            "target": "work", "keys": "ctrl+c", "combo": combo("a", ctrl=True)})
        self.assertEqual(response.status_code, 400)
        self.deliver.assert_not_called()

    def test_a_combo_for_a_missing_target_is_409(self):
        with mock.patch.object(input_routes, "expected_target_identity", return_value=None):
            response = self.client.post("/key", json={"target": "gone", "combo": combo("a")})
        self.assertEqual(response.status_code, 409)
        self.deliver.assert_not_called()

    def test_the_fixed_allowlist_is_unchanged(self):
        for keys, sent in (("ctrl+c", ("C-c",)), ("shift+Tab", ("BTab",)),
                           ("Escape Escape", ("Escape", "Escape")), ("q", ("q",))):
            with self.subTest(keys=keys):
                self.deliver.reset_mock()
                response = self.client.post("/key", json={"target": "work", "keys": keys})
                self.assertEqual(response.status_code, 200)
                self.deliver.assert_called_once_with(self.expected, keys=sent)
        response = self.client.post("/key", json={"target": "work", "keys": "ctrl+q"})
        self.assertEqual(response.status_code, 403)


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class RealTmuxComboTests(unittest.TestCase):
    """Against a throwaway tmux server, never the ambient one.

    $TMUX names the private socket, so every call in the code under test goes
    there; teardown passes the socket with -S. The pane runs a raw-mode reader
    that appends every byte it receives to a file.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="assist-keycombo-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.socket = self.root / "private.sock"
        _enter(self, mock.patch.dict(os.environ, {
            "TMUX": f"{self.socket},0,0",
            "ASSIST_HOME": str(self.root),
            "ASSIST_LAUNCH_PROVENANCE_ROOT": str(self.root / "registry"),
        }))
        self.received = self.root / "received"
        self.received.touch()
        reader = (
            "import os, tty\n"
            "tty.setraw(0)\n"
            f"out = open({str(self.received)!r}, 'ab', 0)\n"
            "out.write(b'ready')\n"
            "while True:\n"
            "    data = os.read(0, 1024)\n"
            "    if not data:\n"
            "        break\n"
            "    out.write(data)\n"
        )
        self.run_tmux("-f", "/dev/null", "new-session", "-d", "-s", "keys",
                      "-x", "80", "-y", "10", "python3", "-c", reader)
        self.addCleanup(self.run_tmux, "kill-server", check=False)
        self.pane_id = self.run_tmux(
            "display-message", "-p", "-t", "=keys:", "#{pane_id}").stdout.strip()
        # setraw() flushes pending input, so nothing is sent before it says so.
        self.wait_for(b"ready")
        _enter(self, mock.patch.object(input_routes.state, "touch_activity"))
        self.client = _app()

    def run_tmux(self, *args, check=True):
        return subprocess.run(
            ["tmux", "-S", str(self.socket), *args], capture_output=True,
            text=True, check=check, timeout=5,
        )

    def wait_for(self, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            data = self.received.read_bytes()
            if data == expected:
                self.received.write_bytes(b"")
                return
            time.sleep(0.02)
        self.fail(f"pane received {self.received.read_bytes()!r}, wanted {expected!r}")

    def test_combos_arrive_as_the_bytes_a_terminal_would_send(self):
        cases = [
            (combo("a", ctrl=True), b"\x01"),
            (combo("x", alt=True), b"\x1bx"),
            (combo("a", shift=True), b"A"),
            (combo("/", ctrl=True), b"\x1f"),
            (combo(";"), b";"),
            (combo("$"), b"$"),
            (combo("~"), b"~"),
            (combo("F5"), b"\x1b[15~"),
            (combo("F5", ctrl=True, shift=True), b"\x1b[15;6~"),
            (combo("F12"), b"\x1b[24~"),
            (combo("Tab", shift=True), b"\x1b[Z"),
            (combo("Left", ctrl=True, alt=True, shift=True), b"\x1b[1;8D"),
            (combo("Delete"), b"\x1b[3~"),
            (combo("BSpace", alt=True), b"\x1b\x7f"),
            (combo("Space", ctrl=True), b"\x00"),
        ]
        for value, expected in cases:
            with self.subTest(combo=value):
                response = self.client.post("/key", json={"target": self.pane_id, "combo": value})
                self.assertEqual(response.status_code, 200, response.get_json())
                self.wait_for(expected)

    def test_a_refused_combo_types_nothing(self):
        response = self.client.post("/key", json={
            "target": self.pane_id, "combo": combo("BSpace", ctrl=True)})
        self.assertEqual(response.status_code, 400)
        self.run_tmux("send-keys", "-t", self.pane_id, "-l", "|")
        self.wait_for(b"|")

    def test_a_leading_tilde_in_text_is_not_expanded(self):
        expected = tmux.expected_target_identity(self.pane_id)
        result = tmux.generation_bound_delivery(expected, text="~/notes ~ a~b")
        self.assertEqual(result.status, "delivered")
        self.wait_for(b"~/notes ~ a~b")


if __name__ == "__main__":
    unittest.main()
