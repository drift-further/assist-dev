"""`assist expose` and the ASSIST_BIND listener it configures.

The phone path used to need a hand-written nginx vhost. `assist expose` binds
Flask to the host's LAN address instead -- in ADDITION to loopback, so the CLI
and /health keep working -- and adds the matching origin to .env.

Pinned in both directions: the address it will write, and the ones it must
refuse (a wildcard would put every endpoint on every interface, and a public
address is the internet), plus that .env keeps everything it did not touch.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cli import expose, http
from shared import listen


ROOT = Path(__file__).resolve().parents[1]


class ListenAddressTests(unittest.TestCase):
    def test_unset_means_loopback_only(self):
        self.assertEqual(listen.bind_addresses("127.0.0.1", ""), ["127.0.0.1"])
        self.assertEqual(listen.bind_addresses("127.0.0.1", None), ["127.0.0.1"])

    def test_a_lan_address_is_added_beside_loopback(self):
        self.assertEqual(
            listen.bind_addresses("127.0.0.1", "192.168.1.50"),
            ["127.0.0.1", "192.168.1.50"],
        )

    def test_loopback_bind_adds_nothing(self):
        self.assertEqual(listen.bind_addresses("127.0.0.1", "127.0.0.1"), ["127.0.0.1"])

    def test_wildcards_and_names_are_refused(self):
        for value in ("0.0.0.0", "::", "assist.lan", "192.168.1.300"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                listen.bind_addresses("127.0.0.1", value)

    def test_serve_uses_the_helper(self):
        source = (ROOT / "serve.py").read_text()
        self.assertIn("listen.bind_addresses(", source)
        self.assertIn('os.environ.get("ASSIST_BIND")', source)


class ExposeAddressTests(unittest.TestCase):
    def test_private_and_cgnat_addresses_are_accepted(self):
        for value in ("10.0.0.101", "192.168.1.50", "172.20.1.2", "100.101.102.103", "fd00::5"):
            with self.subTest(value=value):
                self.assertEqual(expose.check_address(value), value)

    def test_wildcard_loopback_public_and_names_are_refused(self):
        for value in ("0.0.0.0", "::", "127.0.0.1", "8.8.8.8", "assist.lan", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                expose.check_address(value)

    def test_origin_brackets_ipv6(self):
        self.assertEqual(expose.origin_for("fd00::5", 8089), "http://[fd00::5]:8089")
        self.assertEqual(expose.origin_for("10.0.0.5", 9000), "http://10.0.0.5:9000")


class EnvFileTests(unittest.TestCase):
    def _env(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".env"
        path.write_text(text)
        return path

    def test_adds_bind_and_origin_and_keeps_the_rest(self):
        path = self._env(
            "# comment kept\n"
            "ASSIST_PORT=8089\n"
            "# ASSIST_ALLOWED_ORIGINS=http://commented.example\n"
            "ASSIST_ALLOWED_ORIGINS=http://assist.lan,http://10.0.0.9:8089\n"
        )
        expose.write_exposure(path, "10.0.0.101", 8089)
        text = path.read_text()
        self.assertIn("# comment kept\n", text)
        self.assertIn("ASSIST_PORT=8089\n", text)
        self.assertIn("# ASSIST_ALLOWED_ORIGINS=http://commented.example\n", text)
        self.assertIn("ASSIST_BIND=10.0.0.101\n", text)
        self.assertIn(
            "ASSIST_ALLOWED_ORIGINS=http://assist.lan,http://10.0.0.9:8089,"
            "http://10.0.0.101:8089\n",
            text,
        )

    def test_is_idempotent(self):
        path = self._env("")
        expose.write_exposure(path, "10.0.0.101", 8089)
        first = path.read_text()
        expose.write_exposure(path, "10.0.0.101", 8089)
        self.assertEqual(path.read_text(), first)
        self.assertEqual(first.count("http://10.0.0.101:8089"), 1)

    def test_replaces_an_existing_bind(self):
        path = self._env("ASSIST_BIND=10.0.0.5\n")
        expose.write_exposure(path, "10.0.0.101", 8089)
        self.assertNotIn("10.0.0.5\n", path.read_text())
        self.assertEqual(path.read_text().count("ASSIST_BIND="), 1)

    def test_off_removes_the_bind_but_keeps_the_origins(self):
        path = self._env("ASSIST_BIND=10.0.0.101\nASSIST_ALLOWED_ORIGINS=http://10.0.0.101:8089\n")
        expose.remove_exposure(path)
        text = path.read_text()
        self.assertNotIn("ASSIST_BIND=10", text)
        self.assertIn("ASSIST_ALLOWED_ORIGINS=http://10.0.0.101:8089\n", text)


class ExposeCommandTests(unittest.TestCase):
    def _resolved(self, tmp):
        home = Path(tmp)
        (home / ".env").write_text("")
        return SimpleNamespace(home=home, port=8089)

    def _run(self, resolved, running, ip="10.0.0.101"):
        out = io.StringIO()
        health = mock.Mock(return_value={"status": "ok"})
        if not running:
            health.side_effect = http.server_not_running("http://127.0.0.1:8089")
        with mock.patch.object(expose.http, "get", health), mock.patch.object(
            expose.proc, "restart", return_value=0
        ) as restart, redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = expose.command(resolved, ip)
        return code, restart, out.getvalue()

    def test_restarts_a_running_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = self._resolved(tmp)
            code, restart, _out = self._run(resolved, running=True)
            self.assertEqual(code, 0)
            restart.assert_called_once_with(resolved)

    def test_leaves_a_stopped_server_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, restart, out = self._run(self._resolved(tmp), running=False)
            self.assertEqual(code, 0)
            restart.assert_not_called()
            self.assertIn("assist start", out)

    def test_refuses_a_wildcard_without_touching_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = self._resolved(tmp)
            code, restart, _out = self._run(resolved, running=True, ip="0.0.0.0")
            self.assertEqual(code, 1)
            restart.assert_not_called()
            self.assertEqual((resolved.home / ".env").read_text(), "")

    def test_detects_the_address_when_none_is_given(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            expose, "detect_lan_ip", return_value="192.168.1.50"
        ):
            resolved = self._resolved(tmp)
            code, _restart, _out = self._run(resolved, running=False, ip=None)
            self.assertEqual(code, 0)
            self.assertIn("ASSIST_BIND=192.168.1.50", (resolved.home / ".env").read_text())



class ExposedOriginPassesTheRequestFences(unittest.TestCase):
    """What `assist expose` writes must satisfy the Host and Origin fences.

    With ASSIST_BIND set the phone reaches Flask directly, so its Host header
    is `<lan-ip>:<port>` and its Origin is the one expose added. The Host
    fence (shared/security.py) derives its allowlist from
    ASSIST_ALLOWED_ORIGINS; if the two stopped agreeing, every page from the
    phone would be a 421 right after a successful expose.
    """

    def _configure_from(self, env_file):
        from shared import security

        values = dict(
            line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line
        )
        with mock.patch.dict(os.environ, values):
            security.configure(8120)
        self.addCleanup(security.configure)
        return security

    def test_the_exposed_address_is_let_in_and_a_rebound_name_is_not(self):
        for address, host in (("10.0.0.101", "10.0.0.101:8120"),
                              ("fd00::1", "[fd00::1]:8120")):
            with self.subTest(address=address), tempfile.TemporaryDirectory() as raw:
                env_file = Path(raw) / ".env"
                env_file.write_text("ASSIST_ALLOWED_ORIGINS=http://assist.lan\n")
                origin = expose.write_exposure(env_file, address, 8120)
                security = self._configure_from(env_file)
                self.assertTrue(security.host_allowed(host))
                self.assertTrue(security.origin_allowed(origin))
                # What was there before survives, and nothing else gets in.
                self.assertTrue(security.host_allowed("assist.lan"))
                self.assertFalse(security.host_allowed("rebound.evil:8120"))
                self.assertFalse(security.origin_allowed("http://10.0.0.102:8120"))

    def test_off_keeps_the_origin_so_a_proxy_in_front_still_works(self):
        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            expose.write_exposure(env_file, "10.0.0.101", 8120)
            expose.remove_exposure(env_file)
            security = self._configure_from(env_file)
            self.assertTrue(security.host_allowed("10.0.0.101"))


if __name__ == "__main__":
    unittest.main()
