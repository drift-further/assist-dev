"""First phone sign-in (`assist pair`, `assist token`) and the service unit.

`pair` opens the existing open-access window over the header token and prints
a URL plus a QR code of it. It must refuse, without opening anything, when no
phone could reach the server. `token` prints the secret only to a terminal.

`assist service` writes a systemd --user unit (launchd on macOS); once one is
installed for THIS checkout, start/stop/restart/status go through it -- and a
unit belonging to another checkout is neither driven nor overwritten.
"""

import hashlib
import io
import os
import plistlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cli import pair, proc, qr, service


def _bits(grid):
    return "".join("1" if cell else "0" for row in grid for cell in row)


class QrEncoderTests(unittest.TestCase):
    # Golden outputs. Each grid was decoded back to its input with zxing-cpp
    # on 2026-09-28 (versions 1, 2 and 6, the last with two interleaved RS
    # blocks). A change here must be re-verified with a real decoder.
    GOLDEN = {
        "x": (21, "bde62c08cc481a13206bb3b7acd1be1c1cd0353ba5986de928a1af0f297e56ca"),
        "http://10.20.30.40:8089/": (
            25,
            "7a359e8cac634539ca41cc067bde6c81e8d5c07258650d56664ea3a11f32abba",
        ),
        "b" * 134: (41, "e8537809da43555e233ec9cf305efd21d3cc0bc9b998b24352be0849285656a0"),
    }

    def test_golden_grids(self):
        for text, (size, digest) in self.GOLDEN.items():
            with self.subTest(text=text[:20]):
                grid = qr.encode(text)
                self.assertEqual(len(grid), size)
                self.assertEqual(hashlib.sha256(_bits(grid).encode()).hexdigest(), digest)

    def test_finder_patterns_sit_in_three_corners(self):
        grid = qr.encode("http://192.168.1.50:8089/")
        size = len(grid)
        for x0, y0 in ((0, 0), (size - 7, 0), (0, size - 7)):
            ring = [grid[y0][x0 + i] for i in range(7)] + [grid[y0 + 6][x0 + i] for i in range(7)]
            self.assertTrue(all(ring))
            self.assertTrue(grid[y0 + 3][x0 + 3])
            self.assertFalse(grid[y0 + 1][x0 + 1])

    def test_too_long_is_refused(self):
        with self.assertRaises(ValueError):
            qr.encode("z" * 135)

    def test_render_is_two_rows_per_line(self):
        grid = qr.encode("x")
        lines = qr.render(grid, quiet=2).splitlines()
        self.assertEqual(len(lines), (21 + 4 + 1) // 2)


class PhoneUrlTests(unittest.TestCase):
    def test_bind_wins(self):
        env = {"ASSIST_BIND": "10.20.30.40", "ASSIST_ALLOWED_ORIGINS": "http://assist.lan"}
        self.assertEqual(pair.phone_url(8089, env), "http://10.20.30.40:8089/")

    def test_first_non_loopback_origin_for_a_proxy_install(self):
        env = {"ASSIST_ALLOWED_ORIGINS": "http://localhost:8089, http://assist.lan/ ,http://x"}
        self.assertEqual(pair.phone_url(8089, env), "http://assist.lan/")

    def test_loopback_only_is_none(self):
        env = {"ASSIST_ALLOWED_ORIGINS": "http://127.0.0.1:8089,http://localhost:8089"}
        self.assertIsNone(pair.phone_url(8089, env))
        self.assertIsNone(pair.phone_url(8089, {}))


class PairCommandTests(unittest.TestCase):
    def _resolved(self):
        return SimpleNamespace(port=8089, auth_token_path=Path("/x/auth_token"))

    def test_opens_the_window_and_prints_url_and_qr(self):
        out = io.StringIO()
        with mock.patch.object(pair.http, "post", return_value={
            "ok": True, "access": {"open": True, "remaining_sec": 300}
        }) as post, mock.patch.object(pair.shutil, "which", return_value=None), \
                redirect_stdout(out):
            code = pair.command(self._resolved(), 5, url="http://10.20.30.40:8089/")
        self.assertEqual(code, 0)
        post.assert_called_once_with("/access/open", {"minutes": 5})
        text = out.getvalue()
        self.assertIn("http://10.20.30.40:8089/", text)
        self.assertIn("5 min", text)
        self.assertIn("█", text)

    def test_loopback_only_refuses_without_opening(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            pair.http, "post"
        ) as post, redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = pair.command(self._resolved(), 5)
        self.assertEqual(code, 1)
        post.assert_not_called()
        self.assertIn("assist expose", err.getvalue())


class TokenCommandTests(unittest.TestCase):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    def _resolved(self):
        return SimpleNamespace(auth_token_path=Path("/x/auth_token"), token="s3cret-value")

    def test_terminal_gets_path_and_value(self):
        stream = self._Tty()
        self.assertEqual(pair.token(self._resolved(), stream), 0)
        self.assertIn("/x/auth_token", stream.getvalue())
        self.assertIn("s3cret-value", stream.getvalue())

    def test_a_pipe_gets_the_path_only(self):
        stream = io.StringIO()
        self.assertEqual(pair.token(self._resolved(), stream), 0)
        self.assertIn("/x/auth_token", stream.getvalue())
        self.assertNotIn("s3cret-value", stream.getvalue())


class LoginPageTests(unittest.TestCase):
    def test_login_page_points_a_first_time_user_at_pair(self):
        from flask import Flask
        from routes import static

        app = Flask("login-hint")
        app.register_blueprint(static.static_bp)
        page = app.test_client().get("/login").get_data(as_text=True)
        self.assertIn("No device signed in yet? Run <code>assist pair</code> on the host.", page)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "checkout"
        self.home.mkdir()
        self.resolved = SimpleNamespace(home=self.home, port=8089)
        env = mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(self.root / "config"), "PATH": "/opt/agent/bin:/usr/bin",
             "HOME": str(self.root / "home"), "USER": "someone"},
        )
        env.start()
        self.addCleanup(env.stop)
        platform = mock.patch.object(service, "_platform", return_value="linux")
        platform.start()
        self.addCleanup(platform.stop)

    def _run_recorder(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(list(command))
            return SimpleNamespace(returncode=0, stdout="Linger=no\n")

        return calls, mock.patch.object(service.subprocess, "run", side_effect=fake_run)

    def test_systemd_unit_runs_this_checkout_with_the_install_path(self):
        unit = service.systemd_unit(self.home, "/opt/agent/bin:/usr/bin")
        self.assertIn(f"ExecStart={self.home / 'assist-ctl'} run\n", unit)
        self.assertIn("Environment=PATH=/opt/agent/bin:/usr/bin\n", unit)
        self.assertIn("WantedBy=default.target", unit)
        # tmux started under the service must outlive it (test_service_lifecycle).
        self.assertIn("KillMode=process\n", unit)

    def test_launchd_plist_runs_this_checkout(self):
        data = plistlib.loads(service.launchd_plist(self.home, "/usr/bin"))
        self.assertEqual(data["ProgramArguments"], [str(self.home / "assist-ctl"), "run"])
        self.assertTrue(data["RunAtLoad"])
        self.assertIs(data["AbandonProcessGroup"], True)

    def test_install_stops_a_manual_server_writes_enables_and_hints_linger(self):
        calls, patched = self._run_recorder()
        stop_manual = mock.Mock()
        out = io.StringIO()
        with patched, redirect_stdout(out):
            self.assertEqual(service.install(self.resolved, stop_manual), 0)
        stop_manual.assert_called_once()
        unit = self.root / "config" / "systemd" / "user" / "drift-assist.service"
        self.assertTrue(unit.is_file())
        self.assertIn(["systemctl", "--user", "enable", "--now", "drift-assist.service"], calls)
        self.assertIn("enable-linger someone", out.getvalue())
        self.assertTrue(service.installed_for(self.home))

    def test_install_refuses_to_overwrite_another_checkouts_unit(self):
        unit = service.unit_path()
        unit.parent.mkdir(parents=True)
        unit.write_text("ExecStart=/elsewhere/assist-ctl run\n")
        calls, patched = self._run_recorder()
        with patched, redirect_stderr(io.StringIO()):
            self.assertEqual(service.install(self.resolved, mock.Mock()), 1)
        self.assertEqual(calls, [])
        self.assertIn("/elsewhere/", unit.read_text())

    def test_uninstall_disables_and_removes(self):
        calls, patched = self._run_recorder()
        with patched, redirect_stdout(io.StringIO()):
            service.install(self.resolved, mock.Mock())
            self.assertEqual(service.uninstall(self.resolved), 0)
        self.assertFalse(service.unit_path().exists())
        self.assertIn(["systemctl", "--user", "disable", "--now", "drift-assist.service"], calls)

    def test_process_verbs_defer_to_an_installed_unit(self):
        with mock.patch.object(service, "installed_for", return_value=True), mock.patch.object(
            service, "control", return_value=0
        ) as control, mock.patch.object(proc, "_control") as ctl, redirect_stdout(io.StringIO()):
            for verb, function in (("start", proc.start), ("stop", proc.stop),
                                   ("restart", proc.restart), ("status", proc.status)):
                function(self.resolved)
                control.assert_called_with(verb)
        ctl.assert_not_called()

    def test_process_verbs_use_assist_ctl_without_a_unit(self):
        with mock.patch.object(service, "control") as control, mock.patch.object(
            proc, "_control", return_value=0
        ) as ctl, redirect_stdout(io.StringIO()):
            proc.start(self.resolved)
            proc.restart(self.resolved)
        control.assert_not_called()
        self.assertEqual([c.args[1] for c in ctl.call_args_list], ["start", "restart"])

    def test_another_checkouts_unit_is_not_driven(self):
        unit = service.unit_path()
        unit.parent.mkdir(parents=True)
        unit.write_text("ExecStart=/elsewhere/assist-ctl run\n")
        self.assertFalse(service.installed_for(self.home))


if __name__ == "__main__":
    unittest.main()
