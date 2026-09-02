#!/usr/bin/env python3
"""Private-socket reproducer for the Assist `/type` control-client teardown race.

Assist delivers text through `generation_bound_delivery()`, which opens one
`tmux -C attach-session` control client, submits a batch, and then
tears the client down by closing its stdin and immediately SIGTERMing it.  On
tmux 3.4 the stdin EOF (`CLIENT_EXIT`) and the abrupt peer loss
(`server_client_lost()` -> `control_stop()`) race while `%output` blocks are
still queued, and the server has been observed to die with a silent `fatal()`.

This harness drives the real delivery path in `shared/tmux.py` against a
PRIVATE tmux server it creates and destroys itself.  It never touches the
default socket: the socket path is generated under a scratch directory, is
asserted to live there, and the server is started with `-f /dev/null` so no
user configuration applies.  The server runs in the foreground (`-D`) so its
exit status is observed directly, and with `-v` so a reproduced fatal names its
own line in `tmux-server-<pid>.log`.

Usage:
    .venv/bin/python3 tools/release/tmux_teardown_repro.py sweep --iterations 40
    .venv/bin/python3 tools/release/tmux_teardown_repro.py landing
    .venv/bin/python3 tools/release/tmux_teardown_repro.py sweep --tmux-bin /path/to/tmux
"""

import argparse
import json
import os
import random
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from shared import tmux as tmux_module  # noqa: E402
from shared.tmux import (  # noqa: E402
    expected_target_identity,
    generation_bound_delivery,
)

SOCKET_PREFIX = "assist-teardown-sock-"

# Payload the target pane echoes twice (line discipline, then `cat`), so a
# large block of %output is queued exactly across the final identity
# round-trips and the close sequence.
PAYLOAD = ("assist-teardown-" + "x" * 49 + "\n").replace("\n", "")
PAYLOAD_BLOCK = (PAYLOAD * 64)[:4096]

BUSY_COMMAND = (
    "sh -c 'while :; do "
    "head -c 3000 /dev/urandom | base64; "
    "done'"
)


class PrivateTmuxServer:
    """One throwaway tmux server on its own socket, in the foreground.

    Foreground (`-D`) means this process *is* the server, so `poll()` returns
    the exit status the incident is defined by (silent status 1).
    """

    def __init__(self, workdir, tmux_bin, verbose=True):
        self.tmux_bin = tmux_bin
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.socket_path = str(self.workdir / f"{SOCKET_PREFIX}{uuid.uuid4().hex[:12]}")
        self._assert_private()
        self.process = None
        self.target_pane = None
        self.busy_pane = None

    def _assert_private(self):
        name = Path(self.socket_path).name
        if not name.startswith(SOCKET_PREFIX):
            raise RuntimeError(f"refusing non-private socket {self.socket_path!r}")
        if Path(self.socket_path).exists():
            raise RuntimeError(f"socket already exists: {self.socket_path!r}")
        default = os.fspath(Path(f"/tmp/tmux-{os.getuid()}/default"))
        if os.path.realpath(self.socket_path) == os.path.realpath(default):
            raise RuntimeError("refusing to operate on the default tmux socket")

    def tmux(self, *args, **kwargs):
        return subprocess.run(
            [self.tmux_bin, "-S", self.socket_path, "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            timeout=kwargs.pop("timeout", 10),
            **kwargs,
        )

    @staticmethod
    def _enable_core():
        """A reproduced crash must leave evidence; core_pattern here is `core`."""
        try:
            _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
            resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))
        except (ValueError, OSError):
            pass

    def start(self, target_command="cat", busy=True, verbose=True, windows=1):
        flags = ["-D"]
        if verbose:
            flags.insert(0, "-v")
        self.process = subprocess.Popen(
            [self.tmux_bin, *flags, "-S", self.socket_path, "-f", "/dev/null"],
            cwd=str(self.workdir),
            preexec_fn=self._enable_core,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(self.socket_path).exists():
                break
            if self.process.poll() is not None:
                raise RuntimeError("private tmux server exited before its socket appeared")
            time.sleep(0.05)
        else:
            raise RuntimeError("private tmux server never created its socket")

        made = self.tmux(
            "new-session", "-d", "-s", "e521", "-x", "200", "-y", "50",
            target_command,
        )
        if made.returncode != 0:
            raise RuntimeError(f"new-session failed: {made.stderr.strip()}")
        self.target_pane = self._pane_id("e521:0.0")
        self.busy_windows = []
        if busy:
            for index in range(windows):
                spawned = self.tmux(
                    "new-window", "-d", "-t", "e521:", BUSY_COMMAND,
                )
                if spawned.returncode != 0:
                    raise RuntimeError(f"busy window failed: {spawned.stderr.strip()}")
                self.busy_windows.append(index + 1)
            self.busy_pane = self._pane_id("e521:1.0")
        return self

    def respawn_busy_window(self):
        """Kill and re-create one heavy-output window (chaos: wp->fd == -1)."""
        if not self.busy_windows:
            return
        index = random.choice(self.busy_windows)
        self.tmux("kill-window", "-t", f"e521:{index}", timeout=5)
        self.tmux("new-window", "-d", "-t", "e521:", BUSY_COMMAND, timeout=5)

    def _pane_id(self, target):
        got = self.tmux("display-message", "-p", "-t", target, "#{pane_id}")
        if got.returncode != 0:
            raise RuntimeError(f"cannot resolve {target}: {got.stderr.strip()}")
        return got.stdout.strip()

    @property
    def server_log(self):
        if self.process is None:
            return None
        candidate = self.workdir / f"tmux-server-{self.process.pid}.log"
        return candidate if candidate.exists() else None

    def log_size(self):
        log = self.server_log
        return log.stat().st_size if log else 0

    def trim_log(self, keep_bytes=2 * 1024 * 1024):
        """Keep the tail; a fatal is written last, so the tail is what matters."""
        log = self.server_log
        if log is None:
            return
        size = log.stat().st_size
        if size <= keep_bytes:
            return
        with open(log, "rb") as handle:
            handle.seek(size - keep_bytes)
            tail = handle.read()
        # tmux holds the file open at its own offset; truncating in place keeps
        # the file sparse rather than rewinding it.
        (self.workdir / f"tail-{self.process.pid}.log").write_bytes(tail)
        os.truncate(log, 0)

    def fatal_evidence(self, tail_lines=40):
        log = self.server_log
        if log is None:
            return []
        with open(log, "rb") as handle:
            size = log.stat().st_size
            handle.seek(max(0, size - 512 * 1024))
            blob = handle.read().decode("utf-8", errors="replace")
        lines = [line for line in blob.splitlines() if line.strip("\x00")]
        fatal = [line for line in lines if "fatal" in line.lower()]
        return fatal or lines[-tail_lines:]

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def status(self):
        return None if self.process is None else self.process.poll()

    def stop(self):
        """Destroy this private server and only this one."""
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                self.tmux("kill-server", timeout=5)
            except (subprocess.SubprocessError, OSError):
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Teardown variants.  `live` is whatever shared/tmux.py::close() does right
# now; the others pin one specific EOF-vs-SIGTERM ordering so the sweep can
# vary that one dimension while everything else stays identical.
# --------------------------------------------------------------------------

def _finish(connection):
    connection._selector.close()
    for stream in (connection.process.stdout, connection.process.stderr):
        if stream is not None:
            stream.close()


def _make_connection_class(mode, gap, hook=None):
    base = tmux_module._TmuxControlConnection

    class HarnessConnection(base):
        def close(self):
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                if hook is not None:
                    hook()
                if mode == "immediate":
                    self.process.terminate()
                    self.process.wait(timeout=1)
                elif mode == "gap":
                    time.sleep(gap)
                    self.process.terminate()
                    self.process.wait(timeout=1)
                elif mode == "grace":
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.process.terminate()
                        self.process.wait(timeout=1)
                else:
                    raise AssertionError(f"unknown teardown mode {mode!r}")
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                except OSError:
                    pass
            finally:
                _finish(self)

    return HarnessConnection


def factory_for(mode, gap=0.0, hook=None):
    if mode == "live":
        if hook is not None:
            raise ValueError("the live teardown cannot carry a chaos hook")
        return None  # shared/tmux.py's own _control_factory
    connection_class = _make_connection_class(
        "gap" if mode == "gap" else mode, gap, hook
    )

    def factory(socket_path, target):
        return connection_class(socket_path, target)

    return factory


# --------------------------------------------------------------------------


class PaneScanner(threading.Thread):
    """Mimic Assist's 10 Hz Auto-Yes scan: repeated short-lived tmux clients."""

    daemon = True

    def __init__(self, server, interval=0.1):
        super().__init__()
        self.server = server
        self.interval = interval
        self.stopping = threading.Event()
        self.scans = 0

    def run(self):
        while not self.stopping.is_set():
            try:
                self.server.tmux(
                    "capture-pane", "-p", "-t", self.server.target_pane,
                    "-S", "-60", timeout=5,
                )
                self.scans += 1
            except (subprocess.SubprocessError, OSError, RuntimeError):
                pass
            self.stopping.wait(self.interval)

    def stop(self):
        self.stopping.set()


class IdentityProber(threading.Thread):
    """Mimic what Auto-Yes actually does to a live server.

    `routes/autoyes.py:675` calls `expected_target_identity(target)` for every
    scanned pane that currently shows a prompt, on every scan tick.  That is a
    full `tmux -C attach-session` open-and-close at the scan rate (10 Hz by
    default) running concurrently with any `/type` delivery — the same teardown
    under test, from a second direction.
    """

    daemon = True

    def __init__(self, server, factory, interval=0.1):
        super().__init__()
        self.server = server
        self.factory = factory
        self.interval = interval
        self.stopping = threading.Event()
        self.probes = 0

    def run(self):
        while not self.stopping.is_set():
            try:
                expected_target_identity(
                    self.server.target_pane, connection_factory=self.factory
                )
                self.probes += 1
            except (OSError, TimeoutError, ValueError, KeyError):
                pass
            self.stopping.wait(self.interval)

    def stop(self):
        self.stopping.set()


def one_delivery(server, factory, text, enter):
    identity = expected_target_identity(
        server.target_pane, connection_factory=factory
    )
    if identity is None:
        return "identity_absent"
    result = generation_bound_delivery(
        identity,
        text=text,
        enter=enter,
        connection_factory=factory,
    )
    return result.status


def run_trials(mode, gap, iterations, tmux_bin, workroot, batch, enter, stress,
               max_deaths=3):
    """Run `iterations` deliveries under one teardown mode; report deaths.

    `stress` selects the load the private server carries: heavy-output windows,
    an Auto-Yes-shaped scan loop, concurrent deliveries, and an optional chaos
    hook that destroys a busy window inside the close window itself.
    """
    deaths = []
    statuses = {}
    lock = threading.Lock()
    completed = 0
    server = None
    scanners = []

    def teardown_hook():
        if stress["chaos"] and random.random() < stress["chaos"]:
            live = server
            if live is not None:
                live.respawn_busy_window()

    factory = factory_for(mode, gap, teardown_hook if stress["chaos"] else None)

    def record_death(live, at):
        deaths.append(
            {
                "iteration": at,
                "exit_status": live.status(),
                "fatal": live.fatal_evidence(),
                "log": str(live.server_log or ""),
                "workdir": str(live.workdir),
            }
        )

    def stop_scanners():
        for scanner in scanners:
            scanner.stop()
        for scanner in scanners:
            scanner.join(timeout=5)
        scanners.clear()

    def deliver_many(count):
        nonlocal completed
        for _ in range(count):
            status = one_delivery(server, factory, PAYLOAD_BLOCK, enter)
            with lock:
                statuses[status] = statuses.get(status, 0) + 1
                completed += 1
            if not server.alive():
                return

    try:
        while completed < iterations:
            if server is None or not server.alive():
                if server is not None:
                    stop_scanners()
                    record_death(server, completed)
                    server.stop()
                    server = None
                    if len(deaths) >= max_deaths:
                        break
                server = PrivateTmuxServer(
                    Path(workroot) / f"srv-{uuid.uuid4().hex[:8]}", tmux_bin
                )
                server.start(windows=stress["windows"], busy=stress["windows"] > 0)
                os.environ["TMUX"] = server.socket_path
                for _ in range(stress["scanners"]):
                    scanner = PaneScanner(server)
                    scanner.start()
                    scanners.append(scanner)
                for _ in range(stress.get("probers", 0)):
                    prober = IdentityProber(server, factory)
                    prober.start()
                    scanners.append(prober)
            remaining = min(batch, iterations - completed)
            workers = max(1, stress["concurrency"])
            if workers == 1:
                deliver_many(remaining)
            else:
                share = max(1, remaining // workers)
                threads = [
                    threading.Thread(target=deliver_many, args=(share,))
                    for _ in range(workers)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            if server is not None and server.alive():
                server.trim_log()
        if server is not None and not server.alive():
            stop_scanners()
            record_death(server, completed)
    finally:
        stop_scanners()
        if server is not None:
            server.stop()
        os.environ.pop("TMUX", None)
    return {
        "mode": mode,
        "gap": gap,
        "stress": dict(stress),
        "iterations": completed,
        "delivery_statuses": statuses,
        "deaths": deaths,
        "death_count": len(deaths),
    }


def cmd_sweep(args):
    workroot = Path(args.workroot)
    workroot.mkdir(parents=True, exist_ok=True)
    modes = []
    for spec in args.modes.split(","):
        spec = spec.strip()
        if spec.startswith("gap:"):
            modes.append(("gap", float(spec.split(":", 1)[1])))
        else:
            modes.append((spec, 0.0))
    report = {
        "tmux_bin": args.tmux_bin,
        "tmux_version": subprocess.run(
            [args.tmux_bin, "-V"], capture_output=True, text=True
        ).stdout.strip(),
        "iterations_per_mode": args.iterations,
        "results": [],
    }
    stress = {
        "windows": args.windows,
        "scanners": args.scanners,
        "concurrency": args.concurrency,
        "probers": args.probers,
        "chaos": args.chaos,
    }
    report["stress"] = stress
    for mode, gap in modes:
        started = time.monotonic()
        result = run_trials(
            mode,
            gap,
            args.iterations,
            args.tmux_bin,
            workroot,
            args.batch,
            not args.no_enter,
            stress,
            max_deaths=args.max_deaths,
        )
        result["seconds"] = round(time.monotonic() - started, 1)
        report["results"].append(result)
        print(
            f"[{report['tmux_version']}] mode={mode}{'' if gap == 0 else f':{gap}'} "
            f"iterations={result['iterations']} deaths={result['death_count']} "
            f"statuses={result['delivery_statuses']} ({result['seconds']}s)",
            flush=True,
        )
        for death in result["deaths"]:
            print("  DEATH", death["exit_status"], death["fatal"][:3], flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def cmd_landing(args):
    """Functional check: a delivery through the real path must reach the pane."""
    workroot = Path(args.workroot)
    workroot.mkdir(parents=True, exist_ok=True)
    outcome = {"mode": args.modes, "checks": []}
    for spec in args.modes.split(","):
        spec = spec.strip()
        gap = 0.0
        mode = spec
        if spec.startswith("gap:"):
            mode, gap = "gap", float(spec.split(":", 1)[1])
        server = PrivateTmuxServer(
            workroot / f"land-{uuid.uuid4().hex[:8]}", args.tmux_bin
        )
        sink = server.workdir / "delivered.txt"
        server.start(target_command=f"sh -c 'cat > {sink}'", busy=True, verbose=False)
        os.environ["TMUX"] = server.socket_path
        marker = f"e521-landing-{uuid.uuid4().hex[:8]}"
        try:
            factory = factory_for(mode, gap)
            status = one_delivery(server, factory, marker, True)
            deadline = time.monotonic() + 5
            landed = False
            while time.monotonic() < deadline:
                if sink.exists() and marker in sink.read_text(errors="replace"):
                    landed = True
                    break
                time.sleep(0.1)
            outcome["checks"].append(
                {
                    "mode": spec,
                    "delivery_status": status,
                    "landed_in_pane": landed,
                    "server_alive": server.alive(),
                }
            )
            print(
                f"landing mode={spec} delivery={status} landed={landed} "
                f"server_alive={server.alive()}",
                flush=True,
            )
        finally:
            os.environ.pop("TMUX", None)
            server.stop()
    if args.json:
        Path(args.json).write_text(json.dumps(outcome, indent=2), encoding="utf-8")
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmux-bin", default=shutil.which("tmux") or "/usr/bin/tmux")
    parser.add_argument(
        "--workroot",
        default=os.environ.get("E521_WORKROOT", tempfile.gettempdir() + "/e521-work"),
    )
    parser.add_argument("--json", default=None)
    parser.add_argument("--no-output", choices=("on", "off"), default="on",
                        help="off strips `-f no-output` from the attach argv, "
                             "reproducing the pre-patch connection")
    sub = parser.add_subparsers(dest="command", required=True)

    sweep = sub.add_parser("sweep", help="vary EOF-vs-SIGTERM timing")
    sweep.add_argument("--iterations", type=int, default=40)
    sweep.add_argument("--batch", type=int, default=25,
                       help="deliveries per private server before it is rotated")
    sweep.add_argument("--modes", default="immediate,gap:0.001,gap:0.02,grace,live")
    sweep.add_argument("--no-enter", action="store_true")
    sweep.add_argument("--windows", type=int, default=1,
                       help="heavy-output windows in the session (0 = quiet)")
    sweep.add_argument("--scanners", type=int, default=0,
                       help="Auto-Yes-shaped 10 Hz capture-pane loops")
    sweep.add_argument("--concurrency", type=int, default=1,
                       help="concurrent deliveries against the one server")
    sweep.add_argument("--probers", type=int, default=0,
                       help="Auto-Yes-shaped 10 Hz expected_target_identity() "
                            "loops — the same control client, from a second "
                            "direction (see routes/autoyes.py:675)")
    sweep.add_argument("--max-deaths", type=int, default=3,
                       help="stop a mode after this many server deaths")
    sweep.add_argument("--chaos", type=float, default=0.0,
                       help="probability of destroying a busy window inside "
                            "the close window (0-1); not available for --modes live")
    sweep.set_defaults(func=cmd_sweep)

    landing = sub.add_parser("landing", help="prove a delivery still reaches the pane")
    landing.add_argument("--modes", default="immediate,grace,live")
    landing.set_defaults(func=cmd_landing)

    args = parser.parse_args(argv)
    # shared/tmux.py spawns the bare name `tmux`; make the chosen build win.
    # The shim is also how `--no-output off` is measured: it strips the
    # `-f no-output` pair back out of the argv, so the pre-patch attach can be
    # compared against the patched one without editing the code under test.
    shim = Path(args.workroot) / f"shim-{uuid.uuid4().hex[:8]}"
    shim.mkdir(parents=True, exist_ok=True)
    real = os.path.abspath(os.path.expanduser(args.tmux_bin))
    link = shim / "tmux"
    # Both settings go through an identical wrapper, so the only difference
    # between them is the argv — not an extra process in one arm.
    strip = "yes" if getattr(args, "no_output", "on") == "off" else "no"
    link.write_text(
        "#!/bin/bash\n"
        f'strip={strip}\n'
        "args=()\n"
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$strip" = yes ] && [ "$1" = "-f" ] && [ "$2" = "no-output" ]; then\n'
        "    shift 2; continue\n"
        "  fi\n"
        '  args+=("$1"); shift\n'
        "done\n"
        f'exec {real} "${{args[@]}}"\n',
        encoding="utf-8",
    )
    link.chmod(0o755)
    os.environ["PATH"] = f"{shim}{os.pathsep}{os.environ.get('PATH', '')}"
    args.tmux_bin = str(link)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
