"""Which request facts the server believes: peer address, Host, Origin.

`client_ip()` decides who may file an approval request or win an open-access
window, and the kill audit records it. `X-Real-IP` is only true when a proxy
on this host wrote it; from any other peer it is whatever the client typed.

The Host allowlist is the DNS-rebinding fence: a hostile page rebound to this
address still sends its own name in Host. The live nginx forwards `Host $host`,
which drops the port, so matching is by hostname.

Both directions are pinned: the spoof is refused, and the live shape (nginx on
loopback, Host = the LAN name, loopback scripts) keeps working.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from flask import Flask, jsonify

import shared.auth as auth
from shared import security


class _Req:
    def __init__(self, remote_addr, headers=None):
        self.remote_addr = remote_addr
        self.headers = headers or {}


class ClientIpTests(unittest.TestCase):
    def test_proxy_header_from_loopback_peer_is_honoured(self):
        req = _Req("127.0.0.1", {"X-Real-IP": "10.0.0.233"})
        self.assertEqual(auth.client_ip(req), "10.0.0.233")

    def test_proxy_header_from_ipv6_loopback_is_honoured(self):
        req = _Req("::1", {"X-Real-IP": "192.168.1.5"})
        self.assertEqual(auth.client_ip(req), "192.168.1.5")

    def test_spoofed_header_from_non_loopback_peer_is_ignored(self):
        req = _Req("203.0.113.9", {"X-Real-IP": "192.168.1.77"})
        self.assertEqual(auth.client_ip(req), "203.0.113.9")

    def test_lan_peer_cannot_claim_another_lan_address(self):
        req = _Req("10.0.0.50", {"X-Real-IP": "10.0.0.2"})
        self.assertEqual(auth.client_ip(req), "10.0.0.50")

    def test_loopback_without_header_is_loopback(self):
        self.assertEqual(auth.client_ip(_Req("127.0.0.1")), "127.0.0.1")


class KillAuditPeerTests(unittest.TestCase):
    """The kill audit records the same trusted address, never the XFF head."""

    def setUp(self):
        from routes import terminal

        self.terminal = terminal
        app = Flask(__name__)
        app.register_blueprint(terminal.terminal_bp)
        self.client = app.test_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audit = Path(tmp.name) / "kill-audit.log"
        patcher = mock.patch.object(terminal, "KILL_AUDIT_FILE", self.audit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _kill(self, remote_addr, headers):
        with mock.patch.object(self.terminal.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stderr="")
            self.client.post(
                "/terminal/kill",
                json={"session": "del-x"},
                headers=headers,
                environ_base={"REMOTE_ADDR": remote_addr},
            )
        return json.loads(self.audit.read_text(encoding="utf-8"))["source_ip"]

    def test_spoofed_header_from_lan_peer_is_not_recorded(self):
        ip = self._kill(
            "10.0.0.60",
            {"X-Real-IP": "10.0.0.1", "X-Forwarded-For": "10.0.0.2, 10.0.0.60"},
        )
        self.assertEqual(ip, "10.0.0.60")

    def test_proxied_request_records_the_proxy_header(self):
        ip = self._kill("127.0.0.1", {"X-Real-IP": "10.0.0.233"})
        self.assertEqual(ip, "10.0.0.233")

    def test_forwarded_for_head_is_never_trusted(self):
        ip = self._kill("127.0.0.1", {"X-Forwarded-For": "198.51.100.9, 127.0.0.1"})
        self.assertEqual(ip, "127.0.0.1")


class _Configured(unittest.TestCase):
    """Rebuild the allowlists from a controlled environment, restore after."""

    PORT = 8089
    EXTRA = "http://assist.example.lan,http://10.20.30.40:8089"

    def setUp(self):
        env = {"ASSIST_ALLOWED_ORIGINS": self.EXTRA}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        security.configure(self.PORT)
        self.addCleanup(security.configure)


class LoopbackOriginPortTests(_Configured):
    PORT = 9000

    def test_loopback_origins_follow_the_configured_port(self):
        self.assertTrue(security.origin_allowed("http://localhost:9000"))
        self.assertTrue(security.origin_allowed("http://127.0.0.1:9000"))

    def test_the_old_hardcoded_port_is_not_implied(self):
        self.assertFalse(security.origin_allowed("http://localhost:8089"))

    def test_env_origins_still_count(self):
        self.assertTrue(security.origin_allowed("http://assist.example.lan"))
        self.assertFalse(security.origin_allowed("http://evil.example"))

    def test_port_is_read_from_the_environment_by_default(self):
        with mock.patch.dict(os.environ, {"ASSIST_PORT": "8113"}):
            security.configure()
            self.assertTrue(security.origin_allowed("http://127.0.0.1:8113"))


class HostAllowlistTests(_Configured):
    def test_live_nginx_host_shapes_are_allowed(self):
        # nginx `proxy_set_header Host $host` drops the port.
        for host in ("assist.example.lan", "10.20.30.40", "10.20.30.40:8089",
                     "ASSIST.example.lan", "assist.example.lan."):
            with self.subTest(host=host):
                self.assertTrue(security.host_allowed(host))

    def test_loopback_scripts_are_allowed(self):
        for host in ("127.0.0.1:8089", "localhost:8089", "localhost", "[::1]:8089"):
            with self.subTest(host=host):
                self.assertTrue(security.host_allowed(host))

    def test_rebound_and_unknown_names_are_refused(self):
        for host in ("rebound.evil:8089", "evil.example", "10.0.0.102", "", None,
                     "assist.example.lan.evil", "bad host", "127.0.0.1:notaport"):
            with self.subTest(host=host):
                self.assertFalse(security.host_allowed(host))


class RequestGuardTests(_Configured):
    def setUp(self):
        super().setUp()
        from routes import static

        app = Flask(__name__)
        security.register_request_guards(app)
        app.register_blueprint(static.static_bp)

        @app.route("/probe", methods=["GET", "POST"])
        def probe():
            return jsonify({"ok": True})

        self.client = app.test_client()

    def test_foreign_host_gets_a_readable_421(self):
        resp = self.client.get("/probe", headers={"Host": "rebound.evil:8089"})
        self.assertEqual(resp.status_code, 421)
        body = resp.get_data(as_text=True)
        self.assertIn("ASSIST_ALLOWED_ORIGINS", body)
        self.assertIn("rebound.evil", body)
        self.assertTrue(resp.content_type.startswith("text/plain"))

    def test_lan_name_and_loopback_pass(self):
        for host in ("assist.example.lan", "127.0.0.1:8089"):
            with self.subTest(host=host):
                resp = self.client.get("/probe", headers={"Host": host})
                self.assertEqual(resp.status_code, 200)

    def test_disallowed_origin_on_api_post_stays_json_403(self):
        resp = self.client.post(
            "/probe", headers={"Host": "assist.example.lan", "Origin": "http://x.test"}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "Origin not allowed")

    def test_disallowed_origin_on_login_renders_the_fix(self):
        resp = self.client.post(
            "/login",
            data={"token": "x"},
            headers={"Host": "assist.example.lan",
                     "Origin": "http://192.168.1.9:8089"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.content_type.startswith("text/html"))
        body = resp.get_data(as_text=True)
        self.assertIn("http://192.168.1.9:8089", body)
        self.assertIn("ASSIST_ALLOWED_ORIGINS", body)
        self.assertIn("assist restart", body)
        self.assertIn('<form method="POST" action="/login">', body)

    def test_login_origin_message_is_escaped(self):
        resp = self.client.post(
            "/login",
            data={"token": "x"},
            headers={"Host": "assist.example.lan",
                     "Origin": "http://<img src=x onerror=alert(1)>"},
        )
        body = resp.get_data(as_text=True)
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;img", body)

    def test_allowed_origin_login_reaches_the_token_check(self):
        with mock.patch("routes.static.token_matches", return_value=False):
            resp = self.client.post(
                "/login",
                data={"token": "x"},
                headers={"Host": "assist.example.lan",
                         "Origin": "http://assist.example.lan"},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Invalid token.", resp.get_data(as_text=True))


class DoctorOriginsTests(unittest.TestCase):
    def _output(self, env):
        import contextlib
        import io

        from cli import proc

        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(out):
            proc._print_origins(9000)
        self.addCleanup(security.configure)
        return out.getvalue()

    def test_doctor_lists_configured_and_loopback_origins(self):
        text = self._output({"ASSIST_ALLOWED_ORIGINS": "http://assist.example.lan"})
        self.assertIn("http://assist.example.lan", text)
        self.assertIn("http://localhost:9000", text)
        self.assertNotIn("only loopback", text)

    def test_doctor_warns_when_only_loopback_is_allowed(self):
        text = self._output({"ASSIST_ALLOWED_ORIGINS": ""})
        self.assertIn("only loopback origins", text)
        self.assertIn("ASSIST_ALLOWED_ORIGINS", text)


if __name__ == "__main__":
    unittest.main()
