"""shared/tmux.py — tmux observation and generation-bound delivery helpers."""

import os
import platform
import re
import selectors
import stat
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from shared.agent_identity import (
    _VERSION_CMD_RE,
    _has_wrapper_descendant,
    _stat_fields,
    refine_with_content,
    resolve_process,
)
from shared.state import WS_SEND_TIMEOUT

_IS_MAC = platform.system() == "Darwin"

# tmux `send-keys -l` has an internal command buffer limit around 16 KB
# (fails with "command too long"). Above this threshold we fall back to
# `load-buffer` (stdin) + `paste-buffer -p`, which has no practical limit
# and, via bracketed paste, is also the semantically correct way to deliver
# large paste operations to bash readline / Claude Code.
_SEND_KEYS_BYTE_LIMIT = 8192


@dataclass(frozen=True, slots=True)
class ExpectedTargetIdentity:
    """Complete immutable identity of one selected tmux pane generation."""

    socket_path: str
    socket_device: int
    socket_inode: int
    server_pid: int
    server_start_time: str
    session_id: str
    window_id: str
    pane_id: str
    pane_pid: int
    pane_start_time: str

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_value(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("expected target identity is absent")
        return cls(
            socket_path=str(value["socket_path"]),
            socket_device=int(value["socket_device"]),
            socket_inode=int(value["socket_inode"]),
            server_pid=int(value["server_pid"]),
            server_start_time=str(value["server_start_time"]),
            session_id=str(value["session_id"]),
            window_id=str(value["window_id"]),
            pane_id=str(value["pane_id"]),
            pane_pid=int(value["pane_pid"]),
            pane_start_time=str(value["pane_start_time"]),
        )


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: str

    @property
    def ok(self):
        return self.status == "delivered"


@dataclass(frozen=True, slots=True)
class TmuxCreationResult:
    """Result of the sole provenance-receipted tmux creation helper."""

    status: str
    identity: ExpectedTargetIdentity | None = None
    stderr: str = ""
    cleanup_succeeded: bool | None = None

    @property
    def ok(self):
        return self.status == "created" and self.identity is not None


@dataclass(frozen=True, slots=True)
class TmuxAdoptionResult:
    """Result of immutable adoption for an already-existing pane."""

    status: str
    identity: ExpectedTargetIdentity | None = None

    @property
    def ok(self):
        return self.status == "adopted" and self.identity is not None


def _process_start_time(pid):
    try:
        process_state, _parent_pid, start_time = _stat_fields(int(pid))
    except (OSError, TypeError, ValueError, IndexError):
        return None
    if process_state == "Z":
        return None
    return start_time


# The kernel's oom_badness() ADDS oom_score_adj/1000 * (RAM + swap) to a task's
# real footprint -- it is an addend, not a multiplier.  At 27 G RAM + 8 G swap an
# adj of 200 is worth +7,205 MB of synthetic badness, which is how five Claude
# panes (383 MB each) were killed ahead of a 7,164 MB runaway on 2026-08-30.
#
# A pane inherits its adj from the tmux SERVER (measured -- the client's value is
# irrelevant to an already-running server), and the server inherits from whatever
# started it: 200 under the systemd user manager on this host.  So we lower both
# after every create.  0 is the target, but oom_score_adj_min is a hard floor that
# only CAP_SYS_RESOURCE may cross, and it is inherited by fork -- under the user
# manager (user@1000.service ships OOMScoreAdjust=100, stamped by root's systemd)
# that floor is 100.  Descend as far as the kernel actually permits: 200 -> 100
# already flips the ranking against a multi-GB hog, which is the whole point.
_OOM_SCORE_ADJ_TARGET = 0


@dataclass(frozen=True, slots=True)
class OomScoreAdjPin:
    """Outcome of one best-effort oom_score_adj descent."""

    pid: int
    before: int | None = None
    after: int | None = None

    @property
    def ok(self):
        return self.after is not None and self.after <= _OOM_SCORE_ADJ_TARGET

    @property
    def lowered(self):
        return (
            self.before is not None
            and self.after is not None
            and self.after < self.before
        )


def _oom_score_adj_path(pid):
    return Path(f"/proc/{int(pid)}/oom_score_adj")


def read_oom_score_adj(pid):
    """Return one task's oom_score_adj, or None when it cannot be read."""
    try:
        return int(_oom_score_adj_path(pid).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _try_set_oom_score_adj(pid, value):
    """Attempt one write; False on EACCES (below oom_score_adj_min) or a dead pid."""
    try:
        _oom_score_adj_path(pid).write_text(str(int(value)), encoding="utf-8")
    except (OSError, ValueError):
        return False
    return True


def _lowest_settable(current, attempt):
    """Smallest value in [target, current] that ``attempt`` accepts.

    ``current`` is achievable by definition (the task is already there), so it is
    the standing upper bound and no probe ever raises the task above where it
    started.  Separated from /proc so the floor search is testable on a kernel
    that grants everything.
    """
    low, high = _OOM_SCORE_ADJ_TARGET, current
    while low < high:
        middle = (low + high) // 2
        if attempt(middle):
            high = middle
        else:
            low = middle + 1
    return high


def pin_oom_score_adj(pid):
    """Lower one task's oom_score_adj toward 0, as far as the kernel allows.

    Fails open: an unreadable, dead or floored task yields a result carrying what
    was achieved, never an exception.
    """
    before = read_oom_score_adj(pid)
    if before is None:
        return OomScoreAdjPin(pid=int(pid))
    if before <= _OOM_SCORE_ADJ_TARGET:
        return OomScoreAdjPin(pid=int(pid), before=before, after=before)
    if _try_set_oom_score_adj(pid, _OOM_SCORE_ADJ_TARGET):
        return OomScoreAdjPin(
            pid=int(pid), before=before, after=read_oom_score_adj(pid)
        )
    floor = _lowest_settable(before, lambda value: _try_set_oom_score_adj(pid, value))
    _try_set_oom_score_adj(pid, floor)
    return OomScoreAdjPin(pid=int(pid), before=before, after=read_oom_score_adj(pid))


def pin_created_oom_score_adj(identity):
    """Pin the new pane and its tmux server so neither outranks a real memory hog.

    The server is pinned as well as the pane because a pane inherits the server's
    value at fork: lowering the server is the only thing that covers panes created
    later on an already-running server.  Never fatal -- a pane that could not be
    pinned is still a pane, and the shortfall is logged instead.
    """
    pins = {
        "server": pin_oom_score_adj(identity.server_pid),
        "pane": pin_oom_score_adj(identity.pane_pid),
    }
    short = {label: pin for label, pin in pins.items() if not pin.ok}
    if short:
        detail = ", ".join(
            f"{label} pid={pin.pid} {pin.before}->{pin.after}"
            for label, pin in short.items()
        )
        print(
            f"[assist] oom_score_adj not fully pinned ({detail}); "
            "kernel floor is oom_score_adj_min and needs CAP_SYS_RESOURCE to cross",
            flush=True,
        )
    return pins


def _socket_path():
    inherited = os.environ.get("TMUX", "").split(",", 1)[0]
    if inherited:
        return os.fspath(Path(inherited).resolve())
    base = Path(os.environ.get("TMUX_TMPDIR", "/tmp"))
    return os.fspath((base / f"tmux-{os.getuid()}" / "default").resolve())


def _socket_identity(path):
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISSOCK(info.st_mode):
        return None
    return info.st_dev, info.st_ino


class _TmuxControlConnection:
    """One tmux control-mode client; it never reconnects."""

    _FORMAT = "#{pid}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}"

    def __init__(self, socket_path, target):
        self.socket_path = os.fspath(Path(socket_path).resolve())
        self.process = subprocess.Popen(
            # `-f no-output` keeps this connection out of tmux's control-output
            # accounting.  Without it the server queues a %output block for
            # every byte the attached session's panes print for as long as we
            # are attached, and a client that goes away with blocks still
            # queued is what kills the server: tmux 3.4 and 3.7c both die in
            # control_append_data() with `fatal: not enough data` once the
            # pane's retained input has been drained past this client's offset.
            # We never read %output -- _read_response() discards every %-line
            # that is not %begin/%end/%error -- so nothing is lost by it.
            [
                "tmux", "-C", "-S", self.socket_path,
                "attach-session", "-f", "no-output", "-t", target,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise OSError("tmux control connection unavailable")
        self._read_buffer = bytearray()
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ)
        # Consume the attach command's own acknowledgement.  Every later
        # response then corresponds one-for-one with a command we submit.
        try:
            self._read_response()
        except BaseException:
            self.close()
            raise

    def socket_identity(self):
        return _socket_identity(self.socket_path)

    def _read_response(self):
        output = []
        began = False
        deadline = __import__("time").monotonic() + 5
        while __import__("time").monotonic() < deadline:
            while b"\n" not in self._read_buffer:
                remaining = deadline - __import__("time").monotonic()
                if not self._selector.select(max(0, remaining)):
                    raise TimeoutError("tmux control acknowledgement timed out")
                piece = os.read(self.process.stdout.fileno(), 65536)
                if not piece:
                    raise OSError("tmux control connection closed")
                self._read_buffer.extend(piece)
            raw, _separator, remainder = bytes(self._read_buffer).partition(b"\n")
            self._read_buffer[:] = remainder
            line = raw.decode("utf-8", errors="replace")
            if line.startswith("%begin "):
                began = True
                output = []
            elif began and line.startswith("%end "):
                return output
            elif began and line.startswith("%error "):
                raise OSError("tmux control command failed")
            elif began and not line.startswith("%"):
                output.append(line)
        raise TimeoutError("tmux control acknowledgement timed out")

    @staticmethod
    def _quote(argument):
        value = str(argument)
        escaped = []
        for character in value:
            if character == "\\":
                escaped.append("\\\\")
            elif character == '"':
                escaped.append('\\"')
            elif character == "$":
                escaped.append("\\$")
            elif character == "\n":
                escaped.append("\\n")
            elif character == "\r":
                escaped.append("\\r")
            elif character == "\t":
                escaped.append("\\t")
            elif ord(character) < 32 or ord(character) == 127:
                escaped.append(f"\\{ord(character):03o}")
            else:
                escaped.append(character)
        return '"' + "".join(escaped) + '"'

    def _submit(self, commands):
        for command in commands:
            line = " ".join(self._quote(part) for part in command) + "\n"
            self.process.stdin.write(line.encode("utf-8"))
        self.process.stdin.flush()
        return [self._read_response() for _command in commands]

    def target_fields(self, target):
        responses = self._submit(
            [["display-message", "-p", "-t", target, self._FORMAT]]
        )
        if not responses or not responses[0]:
            return None
        fields = responses[0][-1].split("\t")
        if len(fields) != 5 or not fields[0].isdigit() or not fields[4].isdigit():
            return None
        return {
            "server_pid": int(fields[0]),
            "session_id": fields[1],
            "window_id": fields[2],
            "pane_id": fields[3],
            "pane_pid": int(fields[4]),
        }

    def send_batch(self, pane_id, *, text=None, keys=(), enter=False, barrier=None):
        commands = []
        buffer_name = None
        if text:
            buffer_name = f"assist-v16-{uuid.uuid4().hex}"
            commands.append(["set-buffer", "-b", buffer_name, "--", text])
            commands.append(["paste-buffer", "-p", "-d", "-b", buffer_name, "-t", pane_id])
        for key in keys:
            commands.append(["send-keys", "-t", pane_id, key])
        if enter:
            commands.append(["send-keys", "-t", pane_id, "Enter"])
        if barrier:
            barrier("before_submit")
        try:
            self._submit(commands)
        except BaseException:
            if buffer_name is not None:
                try:
                    self._submit([["delete-buffer", "-b", buffer_name]])
                except BaseException:
                    pass
            # The batch was submitted once.  Never reconnect or retry it.
            raise

    def close(self):
        try:
            if self.process.stdin:
                self.process.stdin.close()
            # Closing stdin already tells the server this client is finished:
            # it marks the client CLIENT_EXIT and drives its own teardown.
            # Signalling the client at the same moment makes the server take
            # the abrupt server_client_lost() path as well, so two cleanups of
            # one client run back to back.  Wait for the ordinary exit first
            # and keep terminate/kill as the escalation.
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.process.kill()
            except OSError:
                pass
        finally:
            self._selector.close()
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()


def _control_factory(socket_path, target):
    return _TmuxControlConnection(socket_path, target)


def _identity_from_connection(connection, target):
    socket_before = connection.socket_identity()
    fields = connection.target_fields(target)
    if socket_before is None or fields is None:
        return None
    server_start_before = _process_start_time(fields["server_pid"])
    pane_start_before = _process_start_time(fields["pane_pid"])
    fields_after = connection.target_fields(fields["pane_id"])
    socket_after = connection.socket_identity()
    server_start_after = _process_start_time(fields["server_pid"])
    pane_start_after = _process_start_time(fields["pane_pid"])
    if (
        fields_after != fields
        or socket_after != socket_before
        or not server_start_before
        or server_start_after != server_start_before
        or not pane_start_before
        or pane_start_after != pane_start_before
    ):
        return None
    return ExpectedTargetIdentity(
        socket_path=str(connection.socket_path),
        socket_device=int(socket_before[0]),
        socket_inode=int(socket_before[1]),
        server_pid=int(fields["server_pid"]),
        server_start_time=server_start_before,
        session_id=str(fields["session_id"]),
        window_id=str(fields["window_id"]),
        pane_id=str(fields["pane_id"]),
        pane_pid=int(fields["pane_pid"]),
        pane_start_time=pane_start_before,
    )


def expected_target_identity(target, *, connection_factory=None):
    """Select a target once through one bracketed control connection."""
    socket_path = _socket_path()
    factory = connection_factory or _control_factory
    connection = None
    try:
        connection = factory(socket_path, target)
        return _identity_from_connection(connection, target)
    except (OSError, TimeoutError, ValueError, KeyError):
        return None
    finally:
        if connection is not None:
            connection.close()


_CREATE_FORMAT = "#{pid}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}"


def _created_identity(row):
    fields = str(row).split("\t")
    if (
        len(fields) != 5
        or not fields[0].isdigit()
        or not fields[4].isdigit()
        or not fields[1].startswith("$")
        or not fields[2].startswith("@")
        or not fields[3].startswith("%")
    ):
        return None
    socket_path = _socket_path()
    socket_identity = _socket_identity(socket_path)
    server_pid = int(fields[0])
    pane_pid = int(fields[4])
    server_start = _process_start_time(server_pid)
    pane_start = _process_start_time(pane_pid)
    if socket_identity is None or server_start is None or pane_start is None:
        return None
    socket_after = _socket_identity(socket_path)
    server_start_after = _process_start_time(server_pid)
    pane_start_after = _process_start_time(pane_pid)
    if (
        socket_after != socket_identity
        or server_start_after != server_start
        or pane_start_after != pane_start
    ):
        return None
    return ExpectedTargetIdentity(
        socket_path=socket_path,
        socket_device=int(socket_identity[0]),
        socket_inode=int(socket_identity[1]),
        server_pid=server_pid,
        server_start_time=server_start,
        session_id=fields[1],
        window_id=fields[2],
        pane_id=fields[3],
        pane_pid=pane_pid,
        pane_start_time=pane_start,
    )


def _cleanup_exact_created(identity, *, creation_kind):
    """Stop only the exact generation returned by the immediately prior create."""
    connection = None
    try:
        connection = _control_factory(identity.socket_path, identity.pane_id)
        current = _identity_from_connection(connection, identity.pane_id)
        if current != identity:
            return False
        if creation_kind == "session":
            command = ["kill-session", "-t", identity.session_id]
        elif creation_kind == "pane":
            command = ["kill-pane", "-t", identity.pane_id]
        else:
            return False
        connection._submit([command])
        return True
    except (OSError, TimeoutError, ValueError, KeyError):
        return False
    finally:
        if connection is not None:
            connection.close()


def _create_tmux_resource(command, *, surface, diagnostic_alias, creation_kind):
    """Linearize registry lock, tmux create, exact capture, and durable origin."""
    from shared.launch_provenance import LaunchProvenanceStore, ProvenanceError

    identity = None
    created_identity = None
    created = False
    try:
        store = LaunchProvenanceStore()
        with store.locked() as registry:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                return TmuxCreationResult("tmux_create_failed", stderr=proc.stderr)
            created = True
            rows = [line for line in proc.stdout.splitlines() if line]
            created_identity = _created_identity(rows[0]) if len(rows) == 1 else None
            identity = created_identity
            if created_identity is None:
                return TmuxCreationResult(
                    "provenance_record_failed",
                    cleanup_succeeded=False,
                )
            pin_created_oom_score_adj(created_identity)
            try:
                registry.record_created(
                    identity,
                    surface=surface,
                    diagnostic_alias=diagnostic_alias,
                )
            except ProvenanceError:
                cleaned = _cleanup_exact_created(identity, creation_kind=creation_kind)
                return TmuxCreationResult(
                    "provenance_record_failed",
                    identity=identity,
                    cleanup_succeeded=cleaned,
                )
            return TmuxCreationResult("created", identity=identity)
    except ProvenanceError:
        if created and created_identity is not None:
            cleaned = _cleanup_exact_created(
                created_identity, creation_kind=creation_kind
            )
            return TmuxCreationResult(
                "provenance_record_failed",
                identity=created_identity,
                cleanup_succeeded=cleaned,
            )
        return TmuxCreationResult("provenance_record_failed", cleanup_succeeded=None)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        if created and created_identity is not None:
            cleaned = _cleanup_exact_created(
                created_identity, creation_kind=creation_kind
            )
            return TmuxCreationResult(
                "provenance_record_failed",
                identity=created_identity,
                cleanup_succeeded=cleaned,
            )
        return TmuxCreationResult("tmux_create_failed")


def create_tmux_session(
    *, session_name, cwd, cols, rows, surface, diagnostic_alias=None
):
    """Create and receipt one new session before callers perform any later effect."""
    command = [
        "tmux",
        "new-session",
        "-d",
        "-P",
        "-F",
        _CREATE_FORMAT,
        "-s",
        str(session_name),
        "-c",
        str(cwd),
        "-x",
        str(cols),
        "-y",
        str(rows),
    ]
    return _create_tmux_resource(
        command,
        surface=surface,
        diagnostic_alias=diagnostic_alias or f"{session_name}:0.0",
        creation_kind="session",
    )


def create_tmux_split(
    *, target, height, surface, cwd=None, diagnostic_alias=None
):
    """Create and receipt one split pane before callers deliver command bytes."""
    command = [
        "tmux",
        "split-window",
        "-d",
        "-P",
        "-F",
        _CREATE_FORMAT,
        "-v",
        "-l",
        str(height),
        "-t",
        str(target),
    ]
    if cwd is not None:
        command.extend(["-c", str(cwd)])
    return _create_tmux_resource(
        command,
        surface=surface,
        diagnostic_alias=diagnostic_alias,
        creation_kind="pane",
    )


def record_tmux_adoption(target, *, surface, diagnostic_alias=None):
    """Record one-way adoption; observation and later interaction never call this."""
    from shared.launch_provenance import LaunchProvenanceStore, ProvenanceError

    try:
        store = LaunchProvenanceStore()
        with store.locked() as registry:
            identity = expected_target_identity(target)
            if identity is None:
                return TmuxAdoptionResult("target_absent")
            registry.record_adoption(
                identity,
                surface=surface,
                diagnostic_alias=diagnostic_alias or str(target),
            )
            return TmuxAdoptionResult("adopted", identity=identity)
    except ProvenanceError:
        return TmuxAdoptionResult("provenance_record_failed")


def generation_bound_delivery(
    expected,
    *,
    text=None,
    keys: Iterable[str] = (),
    enter=False,
    connection_factory=None,
    barrier=None,
):
    """Compare and submit one complete delivery through one connection.

    The expected pane id, never its human name, addresses every operation.  A
    loss after submission is terminal ``delivery_failed`` and is never retried.
    """
    try:
        expected = ExpectedTargetIdentity.from_value(expected)
    except (KeyError, TypeError, ValueError):
        return DeliveryResult("target_absent")
    factory = connection_factory or _control_factory
    connection = None
    submitted = False
    try:
        connection = factory(expected.socket_path, expected.pane_id)
        current = _identity_from_connection(connection, expected.pane_id)
        if current != expected:
            return DeliveryResult("target_absent")
        if barrier:
            barrier("after_identity_before_send")
            current = _identity_from_connection(connection, expected.pane_id)
            if current != expected:
                return DeliveryResult("target_absent")
        submitted = True
        connection.send_batch(
            expected.pane_id,
            text=text,
            keys=tuple(keys),
            enter=bool(enter),
            barrier=barrier,
        )
        if connection.socket_identity() != (
            expected.socket_device,
            expected.socket_inode,
        ):
            return DeliveryResult("delivery_failed")
        final = _identity_from_connection(connection, expected.pane_id)
        if final != expected:
            return DeliveryResult("delivery_failed")
        return DeliveryResult("delivered")
    except (OSError, TimeoutError, ValueError, KeyError):
        return DeliveryResult("delivery_failed" if submitted else "target_absent")
    finally:
        if connection is not None:
            connection.close()

# Client key name -> tmux send-keys name
TMUX_KEY_MAP = {
    "ctrl+c": "C-c",
    "ctrl+d": "C-d",
    "ctrl+l": "C-l",
    "ctrl+r": "C-r",
    "ctrl+o": "C-o",
    "ctrl+t": "C-t",
    "ctrl+a": "C-a",
    "ctrl+e": "C-e",
    "ctrl+u": "C-u",
    "ctrl+k": "C-k",
    "ctrl+w": "C-w",
    "ctrl+g": "C-g",
    "ctrl+z": "C-z",
    "ctrl+shift+v": None,  # handled as literal paste
    # Codex / Cursor surface. C-b is safe: send-keys injects into the
    # pane's tty, so the tmux prefix — which only applies to an attached client's
    # own keystrokes — never sees it.
    "ctrl+j": "C-j",
    "ctrl+y": "C-y",
    "ctrl+b": "C-b",
    "ctrl+f": "C-f",
    "ctrl+s": "C-s",
    "ctrl+slash": "C-_",
    "alt+r": "M-r",
    "alt+comma": "M-,",
    "alt+period": "M-.",
    "alt+Up": "M-Up",
    "shift+Tab": "BTab",
    "Escape": "Escape",
    "Return": "Enter",
    "Up": "Up",
    "Down": "Down",
    "Left": "Left",
    "Right": "Right",
    "Tab": "Tab",
    "Page_Up": "PPage",
    "Page_Down": "NPage",
    "End": "End",
    "Home": "Home",
    # Codex's transcript pager (Ctrl+T) quits on a literal q, and its own footer
    # says so: "q to quit". Verified through this exact path — send-keys WITHOUT
    # -l, which is what tmux_send_keys() does — on codex-cli 0.147.0: the pager
    # closed and no stray q was left in the composer.
    "q": "q",
}

def prettify_command(cmd):
    """Human-readable pane command: version-named binaries render as `claude`."""
    if cmd and _VERSION_CMD_RE.fullmatch(cmd):
        return "claude"
    return cmd


def get_clipboard():
    """Read the system clipboard (pbpaste on macOS, xclip -o on Linux). Returns text or None."""
    try:
        if _IS_MAC:
            cmd = ["pbpaste"]
        else:
            cmd = ["xclip", "-selection", "clipboard", "-o"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return proc.stdout if proc.returncode == 0 else None
    except Exception:
        return None


def tmux_send_keys(target, *keys):
    """Send key(s) to a tmux pane."""
    proc = subprocess.run(
        ["tmux", "send-keys", "-t", target] + list(keys),
        timeout=5,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def tmux_send_text(target, text):
    """Send literal text to a tmux pane (no key interpretation).

    For small text without newlines, uses `send-keys -l` (fast path).
    For text over ~8 KB or containing a newline, uses `load-buffer -` +
    `paste-buffer -p` to bypass tmux's internal `send-keys` command buffer
    limit (~16 KB). The `-p` flag
    enables bracketed paste when the receiving app has requested it, so
    Claude Code / readline treat large pastes as a single paste block
    instead of N individual keystrokes.
    """
    byte_len = len(text.encode("utf-8", errors="replace"))

    if byte_len <= _SEND_KEYS_BYTE_LIMIT and "\n" not in text:
        proc = subprocess.run(
            # `--` terminates tmux's own option parsing. Without it any payload
            # starting with a dash is read as flags and the send fails outright
            # (a password beginning with `-` returned "send-keys failed", never
            # reaching the pane). Verified on tmux 3.4: `--` is consumed, not sent.
            ["tmux", "send-keys", "-t", target, "-l", "--", text],
            timeout=5,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    # Large text: stage into a unique paste buffer via stdin, then paste it
    # into the target pane using bracketed paste where supported.
    buf_name = f"assist-{uuid.uuid4().hex[:12]}"
    # Generous timeout scaled by size: ~1s per 100 KB on top of a 10s floor.
    timeout = max(10, byte_len // 100_000)

    load = subprocess.run(
        ["tmux", "load-buffer", "-b", buf_name, "-"],
        input=text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if load.returncode != 0:
        return False

    paste = subprocess.run(
        ["tmux", "paste-buffer", "-b", buf_name, "-t", target, "-p", "-d"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if paste.returncode != 0:
        # `paste-buffer -d` only deletes on success; clean up orphan buffer.
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buf_name],
            capture_output=True,
            timeout=2,
        )
        return False
    return True


def tmux_target_exists(target):
    """Check if a tmux target (session:window.pane) exists."""
    proc = subprocess.run(
        ["tmux", "has-session", "-t", target.split(":")[0]],
        timeout=5,
        capture_output=True,
    )
    return proc.returncode == 0


def detect_venv(project_path):
    """Detect virtualenv directory in a project. Returns relative venv path or None."""
    for venv_dir in ("venv", ".venv", "env"):
        if (project_path / venv_dir / "bin" / "activate").exists():
            return venv_dir
    return None


def capture_pane(target, lines=2000, tui=None):
    """Capture tmux pane content and info. Returns (content, info) or (None, None).

    When the pane is on the alternate screen (a TUI like Claude Code is
    running), capture only the current screen — alt-screen content does not
    flow into scrollback, and historical main-screen scrollback (e.g. prior
    Claude launch banners) would just be noise. When on the main screen,
    capture up to `lines` of scrollback so shell history is preserved.

    `tui` overrides that detection for this capture: True forces
    visible-screen-only, False forces full scrollback, None (default) keeps
    the automatic behaviour. The frontend sends it when the user has pinned
    a pane's mode from the TUI chip, so the capture range always matches the
    mode the UI is showing — mode is one bundle, not just gestures.

    Wrapper sessions (claude inside docker) are captured WITH full
    scrollback like any main-screen pane — the periodic Ctrl+L self-heal in
    the streamer keeps the live region clean, so there's no reason to hide
    history. We only expose `info["is_wrapper"]` so the streamer knows to
    use Ctrl+L (not a resize toggle) when it self-heals.
    """
    info_proc = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            target,
            "-p",
            "#{pane_current_command}\t#{pane_width}\t#{pane_height}\t#{cursor_y}\t#{alternate_on}\t#{pane_pid}\t#{session_attached}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    info = {}
    alternate_on = False
    pane_pid = None
    if info_proc.returncode == 0 and info_proc.stdout.strip():
        parts = info_proc.stdout.strip().split("\t")
        if len(parts) >= 3:
            info = {
                "command": parts[0],
                "width": int(parts[1]),
                "height": int(parts[2]),
            }
            if len(parts) >= 4:
                info["cursor_y"] = int(parts[3])
            if len(parts) >= 5:
                alternate_on = parts[4] == "1"
            if len(parts) >= 6:
                try:
                    pane_pid = int(parts[5])
                except ValueError:
                    pass
            if len(parts) >= 7:
                try:
                    info["session_attached"] = int(parts[6])
                except ValueError:
                    info["session_attached"] = 0
            info["command_display"] = prettify_command(info.get("command", ""))
            info["alternate_on"] = alternate_on

    # Capture range and redraw behavior must use process identity here: content
    # fingerprints are not available until after this function captures.
    process_kind = resolve_process(target, pane_pid, info.get("command", ""))

    # Wrapper detection exposed to the streamer so periodic self-heal can
    # send Ctrl+L (which reaches the in-container TUI) instead of a
    # resize-window toggle (which doesn't). Does NOT affect the capture
    # range — wrapper sessions keep full scrollback.
    info["is_wrapper"] = (not alternate_on) and _has_wrapper_descendant(target, pane_pid)
    # Native claude on the main screen needs the same Ctrl+L heal — its
    # diff renderer leaves stale cells that SIGWINCH redraws can't clear.
    info["is_native_tui"] = (not alternate_on) and process_kind == "claude"

    capture_args = ["tmux", "capture-pane", "-e", "-p", "-t", target]
    # Claude writes its transcript into tmux scrollback even while on the
    # alternate screen (unlike a true TUI such as opencode), so keep full
    # scrollback for it — mirrors the frontend's never-TUI exemption.
    claude_pane = process_kind == "claude"
    as_tui = (alternate_on and not claude_pane) if tui is None else bool(tui)
    info["capture_tui"] = as_tui
    if as_tui:
        capture_args += ["-S", "0"]  # true TUI: visible viewport only
    else:
        capture_args += ["-S", f"-{lines}"]

    # Pane content is arbitrary bytes; under a non-UTF8 locale a bare
    # text=True raises UnicodeDecodeError and silently freezes the stream.
    proc = subprocess.run(
        capture_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    if proc.returncode != 0:
        return None, None

    content = proc.stdout
    lines_list = content.split("\n")
    while lines_list and not lines_list[-1]:
        lines_list.pop()
    content = "\n".join(lines_list)
    info["agent_kind"] = refine_with_content(process_kind, content)
    info["is_native_tui"] = (
        not alternate_on and info["agent_kind"] == "claude"
    )
    return content, info


def tmux_exact_target(target):
    """Return the exact-match form of a `session[:window.pane]` target.

    Exact matching matters: a dead target must resolve to nothing, not
    prefix-match into another live session. BUT tmux 3.4 only honors the
    `=` prefix when the session part is delimited by `:` — bare `=name`
    makes pane-target commands fail ("can't find pane") and display-message
    silently expand every format variable EMPTY. `=name:` pins exact
    session matching and resolves to the session's active pane.
    """
    name, _sep, rest = target.partition(":")
    return f"={name}:{rest}"


# A pane waiting on a typed secret. Mirrors _PASSWORD_PROMPT_RE in js/input.js.
# Matched against the last non-empty visible line, so a `(y/n)`-style prompt or a
# password word merely printed mid-screen does not qualify. Covers:
#     [sudo] password for user:
#     user@host's password:              <- OpenSSH: lowercase p, and no "for"
#     Enter passphrase for key '/home/user/.ssh/id_ed25519':
#     Password:  /  Enter password:
# Not matched: "Permission denied (publickey,password)." — no trailing colon.
PASSWORD_PROMPT_RE = re.compile(
    r"(?:^|[\s'\"])(?:password|passphrase)(?:\s+for\b[^:]*)?:\s*$",
    re.IGNORECASE,
)


def pane_awaits_secret(target):
    """True when `target`'s last visible non-empty line is a password prompt.

    The browser sends its own verdict on /type, but it reads the pane copy it
    last rendered — which is deliberately frozen while streaming is paused — so a
    stale view would let a password through as an ordinary prompt and into
    history. This asks tmux at send time instead. Best-effort: False on any
    failure, because refusing to send is worse than recording one prompt.
    """
    try:
        proc = subprocess.run(
            # No -e: capture-pane strips escape sequences and trailing blanks for
            # us, so the last non-empty row is the prompt as the user sees it.
            ["tmux", "capture-pane", "-p", "-t", tmux_exact_target(target)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=2,
        )
        if proc.returncode != 0:
            return False
        for line in reversed(proc.stdout.split("\n")):
            if line.strip():
                return bool(PASSWORD_PROMPT_RE.search(line))
        return False
    except Exception:
        return False


def set_ws_send_timeout(ws):
    """Set a send timeout on the underlying socket so ws.send() fails fast.

    Uses SO_SNDTIMEO (send-only timeout) so the receive loop in the handler
    thread is not affected.
    """
    try:
        import socket
        import struct

        sock = ws.sock
        timeval = struct.pack("ll", WS_SEND_TIMEOUT, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, timeval)
    except Exception:
        pass
