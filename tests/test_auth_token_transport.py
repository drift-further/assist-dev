"""How the shared secret may be presented.

The token gates every endpoint, and every endpoint can start a process. Where
that secret is allowed to travel therefore decides where it gets written down
by something other than Assist: a query string lands in nginx's access log, in
browser history, and in the Referer of any outbound link the page makes.

These tests pin the transport, not the comparison: header and cookie in,
query string out.
"""

import io
import unittest
from pathlib import Path
from unittest import mock

import shared.auth as auth


class _FakeRequest:
    """The three things request_authenticated() reads."""

    def __init__(self, cookies=None, headers=None, args=None):
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.args = args or {}


class _TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class TokenTransportTests(unittest.TestCase):
    TOKEN = "test-token-not-the-real-one"

    def setUp(self):
        # Never read or write the real auth_token: prime the cache instead.
        self._saved = auth._token_cache
        auth._token_cache = self.TOKEN
        self.addCleanup(self._restore)

    def _restore(self):
        auth._token_cache = self._saved

    def test_header_token_is_accepted(self):
        req = _FakeRequest(headers={auth.HEADER_NAME: self.TOKEN})
        self.assertTrue(auth.request_authenticated(req))

    def test_cookie_is_accepted(self):
        req = _FakeRequest(cookies={auth.COOKIE_NAME: auth.cookie_value()})
        self.assertTrue(auth.request_authenticated(req))

    def test_query_string_token_is_rejected(self):
        """?token=<secret> must not authenticate — the secret leaks via the URL."""
        req = _FakeRequest(args={"token": self.TOKEN})
        self.assertFalse(auth.request_authenticated(req))

    def test_query_string_token_is_rejected_even_beside_a_bad_header(self):
        """The query string must not be a fallback when the header is wrong."""
        req = _FakeRequest(
            headers={auth.HEADER_NAME: "wrong"}, args={"token": self.TOKEN}
        )
        self.assertFalse(auth.request_authenticated(req))

    def test_wrong_header_token_is_rejected(self):
        req = _FakeRequest(headers={auth.HEADER_NAME: "wrong"})
        self.assertFalse(auth.request_authenticated(req))

    def test_no_credential_is_rejected(self):
        self.assertFalse(auth.request_authenticated(_FakeRequest()))

    def test_non_tty_startup_notice_prints_path_without_printing_token(self):
        output = io.StringIO()
        token_path = Path("/tmp/assist-test-auth-token")
        with mock.patch.object(auth, "_TOKEN_PATH", token_path), mock.patch.object(
            auth, "get_token", return_value="must-not-be-printed"
        ) as get_token:
            auth.print_startup_token_notice(output)

        self.assertIn(str(token_path), output.getvalue())
        self.assertNotIn("must-not-be-printed", output.getvalue())
        get_token.assert_called_once_with()

    def test_tty_startup_notice_prints_token_and_path(self):
        output = _TTYBuffer()
        token_path = Path("/tmp/assist-test-auth-token")
        with mock.patch.object(auth, "_TOKEN_PATH", token_path), mock.patch.object(
            auth, "get_token", return_value="tty-visible-token"
        ):
            auth.print_startup_token_notice(output)

        self.assertIn("tty-visible-token", output.getvalue())
        self.assertIn(str(token_path), output.getvalue())


if __name__ == "__main__":
    unittest.main()
