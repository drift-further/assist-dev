"""The stored sudo password is gone, and must stay gone.

`/sudo-send` typed a password held on disk into whatever pane the request
named. That turned a stolen auth cookie — which never expires and is never
revalidated (shared/auth.py:COOKIE_MAX_AGE) — from "run commands as the owner"
into "run commands as root, without ever seeing the password".

The capability it existed for is still there and is strictly better: the
composer detects a live password prompt in the pane
(shared/tmux.py:PASSWORD_PROMPT_RE, routes/input.py `secret`) and sends what
you type byte-for-byte, with no history entry and nothing at rest.

Run: .venv/bin/python3 -m unittest tests.test_sudo_removed
"""

import unittest

from flask import Flask

from routes.input import input_bp


class SudoEndpointsRemovedTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(input_bp)
        self.app = app
        self.client = app.test_client()

    def test_no_sudo_routes_are_registered(self):
        sudo_rules = [
            str(r) for r in self.app.url_map.iter_rules() if "sudo" in str(r)
        ]
        self.assertEqual(sudo_rules, [])

    def test_sudo_send_is_gone(self):
        self.assertEqual(self.client.post("/sudo-send", json={}).status_code, 404)

    def test_sudo_password_setter_is_gone(self):
        response = self.client.post("/sudo-password", json={"password": "hunter2"})
        self.assertEqual(response.status_code, 404)

    def test_sudo_password_reader_is_gone(self):
        self.assertEqual(self.client.get("/sudo-password").status_code, 404)

    def test_the_module_no_longer_reads_a_password_file(self):
        """No helper left behind that a future route could pick back up."""
        import routes.input as module

        self.assertFalse(
            [n for n in dir(module) if "sudo" in n.lower()],
            "routes/input.py still carries sudo-password machinery",
        )

    def test_the_secret_send_replacement_is_still_wired(self):
        """Removing storage must not remove the way to type a password."""
        import routes.input as module

        self.assertTrue(hasattr(module, "pane_awaits_secret"))


if __name__ == "__main__":
    unittest.main()
