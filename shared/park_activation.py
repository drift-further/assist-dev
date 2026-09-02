"""Fail-closed, new-first activation for the compiled execution park.

The old server remains the published generation until a candidate has completed
its read-only pre-bind observation.  The controller then freezes and snapshots
the exact old PID generation, terminates it, and only then authorises the
candidate to bind.  All paths used by an attempt arrive in one sealed handoff;
the candidate does not re-read ambient configuration.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from shared.agent_identity import _stat_fields


SCHEMA = "assist-park-activation-v16"
ACTIVATION_PATH = "activate-park-v16"
_CANDIDATE_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}


class ActivationError(RuntimeError):
    """A terminal, machine-readable activation refusal."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class ActivationOwnership:
    """Small explicit ownership model shared by controller tests and receipts."""

    state: str = "old_running"
    owner: str = "old"

    def candidate_ready(self) -> None:
        if self.state != "old_running":
            raise ActivationError("activation_order")
        self.state = "candidate_ready"

    def old_frozen(self) -> None:
        if self.state != "candidate_ready":
            raise ActivationError("activation_order")
        self.state = "old_frozen"

    def snapshot_ack(self) -> None:
        if self.state != "old_frozen":
            raise ActivationError("activation_order")
        self.state = "snapshot_ack"

    def old_terminated(self) -> None:
        if self.state != "snapshot_ack":
            raise ActivationError("activation_order")
        self.state = "recovery_required"
        self.owner = "candidate"

    def candidate_bound(self, *, resumed: bool = False) -> None:
        if self.state != "recovery_required":
            raise ActivationError("activation_order")
        if resumed is not True and self.owner != "candidate":
            raise ActivationError("activation_owner")
        self.state = "complete"

    def failure_owner(self) -> str:
        if self.state in {"old_running", "candidate_ready"}:
            return "old_running"
        if self.state in {"old_frozen", "snapshot_ack"}:
            return "resume_exact_old"
        return "candidate_resume_only"


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def process_start_time(pid: int) -> str | None:
    """Return Linux's non-recycled process start identity from /proc."""
    try:
        process_state, _parent_pid, start_time = _stat_fields(int(pid))
    except (OSError, TypeError, ValueError, IndexError):
        return None
    if process_state == "Z":
        return None
    return start_time


def process_identity(pid: int) -> dict[str, object] | None:
    start = process_start_time(pid)
    if not start:
        return None
    return {"pid": int(pid), "start_time": start}


def identity_alive(identity: dict[str, object] | None) -> bool:
    if not identity:
        return False
    try:
        pid = int(identity["pid"])
        expected = str(identity["start_time"])
    except (KeyError, TypeError, ValueError):
        return False
    return process_start_time(pid) == expected


def _descendants(root_pid: int) -> list[dict[str, object]]:
    rows: dict[int, tuple[int, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            _state, parent_pid, start_time = _stat_fields(int(entry.name))
            rows[int(entry.name)] = (parent_pid, start_time)
        except (OSError, IndexError, ValueError):
            continue
    wanted = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _start) in rows.items():
            if parent in wanted and pid not in wanted:
                wanted.add(pid)
                changed = True
    return [
        {"pid": pid, "start_time": rows[pid][1]}
        for pid in sorted(wanted - {int(root_pid)})
        if pid in rows
    ]


def _run_readonly(command: list[str], timeout: float = 4.0) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"reachable": False, "error": type(exc).__name__, "rows": []}
    return {
        "reachable": completed.returncode == 0,
        "returncode": completed.returncode,
        "rows": completed.stdout.splitlines() if completed.returncode == 0 else [],
    }


class ExecutionObserver:
    """The only component allowed to run before candidate bind authorisation."""

    def __init__(
        self,
        *,
        old_identity: dict[str, object],
        tmux_probe: Callable[[], dict[str, object]] | None = None,
        docker_probe: Callable[[], dict[str, object]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        metadata: dict[str, object] | None = None,
        provenance_snapshot: dict[str, object] | None = None,
    ) -> None:
        self.old_identity = dict(old_identity)
        self._tmux_probe = tmux_probe or self._probe_tmux
        self._docker_probe = docker_probe or self._probe_docker
        self._clock = clock
        self._metadata = dict(metadata or {})
        self._provenance_snapshot = provenance_snapshot
        self.watermark: dict[str, object] | None = None

    @staticmethod
    def _probe_tmux() -> dict[str, object]:
        return _run_readonly(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pid}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}",
            ]
        )

    @staticmethod
    def _probe_docker() -> dict[str, object]:
        return _run_readonly(
            [
                "docker", "ps", "-a", "--no-trunc", "--format",
                "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}",
            ]
        )

    @staticmethod
    def _docker_observations(probe: dict[str, object]) -> dict[str, object]:
        if not probe.get("reachable"):
            return {"reachable": False, "rows": []}
        by_id = {}
        for raw in probe.get("rows") or ():
            if isinstance(raw, dict):
                row = {key: raw.get(key) for key in ("id", "name", "state", "status")}
            else:
                fields = str(raw).split("\t")
                if len(fields) != 4:
                    raise ActivationError("docker_observation_malformed")
                row = dict(zip(("id", "name", "state", "status"), fields))
            container_id = row.get("id")
            if (
                not isinstance(container_id, str)
                or len(container_id) != 64
                or any(character not in "0123456789abcdef" for character in container_id)
                or not all(isinstance(row.get(key), str) for key in ("name", "state", "status"))
            ):
                raise ActivationError("docker_observation_malformed")
            normalized = {
                "classification": "ambient_unowned",
                "id": container_id,
                "name": row["name"],
                "state": row["state"],
                "status": row["status"],
            }
            if container_id in by_id and by_id[container_id] != normalized:
                raise ActivationError("docker_observation_duplicate")
            by_id[container_id] = normalized
        return {"reachable": True, "rows": [by_id[key] for key in sorted(by_id)]}

    def ready(self) -> dict[str, object]:
        docker = self._docker_probe()
        docker_observations = self._docker_observations(docker)
        self.watermark = {
            "monotonic": self._clock(),
            "docker_reachable": bool(docker.get("reachable")),
            "docker_rows": list(docker_observations["rows"]),
            "adapter": self._metadata,
        }
        return dict(self.watermark)

    def snapshot(self) -> dict[str, object]:
        if self.watermark is None:
            raise ActivationError("observer_not_ready")
        old_pid = int(self.old_identity["pid"])
        panes = self._tmux_probe()
        docker = self._docker_probe()
        descendants = _descendants(old_pid) if identity_alive(self.old_identity) else []
        provenance = self._validated_provenance()
        observations = self._tmux_observations(panes)
        partition = self._partition(observations, provenance)
        return {
            "watermark": dict(self.watermark),
            "old": dict(self.old_identity),
            "old_descendants": descendants,
            "handoff_process_obligations": [
                {"identity": dict(item), "outcome": "unresolved"}
                for item in descendants
            ],
            "tmux": panes,
            "tmux_observations": observations,
            "docker": docker,
            "docker_observations": self._docker_observations(docker),
            "provenance": provenance,
            **partition,
            "units": list(partition["owned_units"]),
            "snapshot_monotonic": self._clock(),
        }

    def _validated_provenance(self) -> dict[str, object]:
        from shared.launch_provenance import (
            LaunchProvenanceStore,
            ProvenanceError,
            validate_snapshot,
        )

        try:
            value = self._provenance_snapshot
            if value is None:
                value = LaunchProvenanceStore().snapshot()
            return validate_snapshot(value)
        except ProvenanceError as exc:
            raise ActivationError("provenance_registry_invalid", exc.code) from exc

    @staticmethod
    def _tmux_observations(panes: dict[str, object]) -> list[dict[str, object]]:
        from shared.tmux import (
            ExpectedTargetIdentity,
            _process_start_time,
            _socket_identity,
            _socket_path,
        )

        if not panes.get("reachable"):
            raise ActivationError("tmux_observation_unavailable")
        observations = []
        for row in panes.get("rows") or ():
            if isinstance(row, dict):
                try:
                    identity = ExpectedTargetIdentity.from_value(row["identity"])
                    alias = str(row.get("alias") or "")
                except (KeyError, TypeError, ValueError) as exc:
                    raise ActivationError("tmux_identity_incomplete") from exc
            else:
                fields = str(row).split("\t")
                if len(fields) != 6 or not fields[0].isdigit() or not fields[4].isdigit():
                    raise ActivationError("tmux_identity_incomplete")
                socket_path = _socket_path()
                socket_value = _socket_identity(socket_path)
                server_start = _process_start_time(int(fields[0]))
                pane_start = _process_start_time(int(fields[4]))
                if socket_value is None or server_start is None or pane_start is None:
                    raise ActivationError("tmux_identity_incomplete")
                identity = ExpectedTargetIdentity(
                    socket_path=socket_path,
                    socket_device=int(socket_value[0]),
                    socket_inode=int(socket_value[1]),
                    server_pid=int(fields[0]),
                    server_start_time=server_start,
                    session_id=fields[1],
                    window_id=fields[2],
                    pane_id=fields[3],
                    pane_pid=int(fields[4]),
                    pane_start_time=pane_start,
                )
                alias = fields[5]
            observations.append({"identity": identity.as_dict(), "alias": alias})
        return observations

    @staticmethod
    def _partition(observations, provenance):
        from shared.launch_provenance import identity_digest

        origins = {
            item["identity_digest"]: item for item in provenance.get("origins") or ()
        }
        live_digests = set()
        owned = []
        ambient = []
        for observation in observations:
            identity = dict(observation["identity"])
            digest = identity_digest(identity)
            origin = origins.get(digest)
            if origin is not None and origin.get("identity") != identity:
                raise ActivationError("provenance_identity_mismatch")
            live_digests.add(digest)
            if origin is not None and origin.get("origin") == "created":
                owned.append(
                    {
                        "kind": "tmux_pane",
                        "classification": "owned_unit",
                        "identity": identity,
                        "identity_digest": digest,
                        "origin": dict(origin),
                        "alias": observation.get("alias"),
                        "outcome": "unresolved",
                    }
                )
            else:
                ambient.append(
                    {
                        "classification": "ambient_unowned",
                        "identity": identity,
                        "identity_digest": digest,
                        "origin": dict(origin) if origin is not None else None,
                        "alias": observation.get("alias"),
                    }
                )
        absent = [
            {
                "classification": "recorded_absent",
                "identity": dict(origin["identity"]),
                "identity_digest": digest,
                "origin": dict(origin),
            }
            for digest, origin in origins.items()
            if digest not in live_digests
        ]
        return {
            "coverage": provenance["coverage"],
            "provenance_epoch": provenance["epoch"]["epoch"],
            "owned_units": owned,
            "ambient_unowned": ambient,
            "recorded_absent": absent,
        }

    def settle(self, snapshot: dict[str, object]) -> bool:
        """Resolve only terminal evidence; ambiguous survivors remain pending."""
        panes = self._tmux_probe()
        live_identities = {
            json.dumps(item["identity"], sort_keys=True, separators=(",", ":"))
            for item in self._tmux_observations(panes)
        }
        for unit in snapshot.get("units") or ():
            if unit.get("outcome") != "unresolved":
                continue
            if unit.get("kind") == "old_descendant":
                if not identity_alive(unit.get("identity")):
                    unit["outcome"] = "terminal"
            elif unit.get("kind") == "tmux_pane":
                encoded = json.dumps(
                    unit.get("identity"), sort_keys=True, separators=(",", ":")
                )
                pane_process = {
                    "pid": unit["identity"]["pane_pid"],
                    "start_time": unit["identity"]["pane_start_time"],
                }
                if encoded not in live_identities and not identity_alive(pane_process):
                    unit["outcome"] = "terminal"

        for obligation in snapshot.get("handoff_process_obligations") or ():
            if obligation.get("outcome") == "unresolved" and not identity_alive(
                obligation.get("identity")
            ):
                obligation["outcome"] = "terminal"

        snapshot["docker_observations"] = self._docker_observations(self._docker_probe())
        return self.final_quiescent(snapshot)

    def await_quiescence(self, snapshot: dict[str, object], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        unreachable_confirmations = 0
        while time.monotonic() < deadline:
            if self.settle(snapshot):
                docker = self._docker_probe()
                if docker.get("reachable"):
                    return
                unreachable_confirmations += 1
                if unreachable_confirmations >= 2:
                    return
            else:
                unreachable_confirmations = 0
            time.sleep(0.05)
        raise ActivationError("park_drain_pending")

    @staticmethod
    def final_quiescent(snapshot: dict[str, object]) -> bool:
        return all(unit.get("outcome") == "terminal" for unit in snapshot.get("units") or ()) and all(
            unit.get("outcome") == "terminal"
            for unit in snapshot.get("handoff_process_obligations") or ()
        )


@dataclass
class CandidateHandoff:
    sealed: dict[str, object]
    snapshot: dict[str, object]
    channel: socket.socket | None

    @property
    def control_dir(self) -> Path:
        return Path(str(self.sealed["control_dir"]))

    def notify_bound(self) -> None:
        payload = {
            "event": "candidate_bound",
            "candidate": process_identity(os.getpid()),
            "at": time.monotonic(),
        }
        _atomic_json(self.control_dir / "candidate-bound.json", payload)
        if self.channel is not None:
            try:
                _send(self.channel, payload)
            except OSError:
                pass


def _send(channel: socket.socket, payload: object) -> None:
    channel.sendall(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n")


_RECV_BUFFERS: "weakref.WeakKeyDictionary[socket.socket, bytearray]" = weakref.WeakKeyDictionary()


def _recv(channel: socket.socket, timeout: float) -> dict[str, object]:
    channel.settimeout(timeout)
    chunks = _RECV_BUFFERS.setdefault(channel, bytearray())
    while b"\n" not in chunks:
        piece = channel.recv(65536)
        if not piece:
            _RECV_BUFFERS.pop(channel, None)
            raise ActivationError("handoff_closed")
        chunks.extend(piece)
        if len(chunks) > 4 * 1024 * 1024:
            _RECV_BUFFERS.pop(channel, None)
            raise ActivationError("handoff_oversize")
    line, _separator, remainder = bytes(chunks).partition(b"\n")
    chunks[:] = remainder
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError("handoff_invalid") from exc
    if not isinstance(value, dict):
        raise ActivationError("handoff_invalid")
    return value


def candidate_prebind(control_fd: int, *, timeout: float = 30.0) -> CandidateHandoff:
    """Run only the observer, returning after the old generation is dead."""
    channel = socket.socket(fileno=control_fd)
    sealed = _recv(channel, timeout)
    if sealed.get("schema") != SCHEMA or sealed.get("activation_path") != ACTIVATION_PATH:
        raise ActivationError("sealed_handoff_invalid")
    expected_serve = Path(str(sealed["serve_script"])).resolve()
    if expected_serve != Path(sys.argv[0]).resolve():
        raise ActivationError("sealed_serve_mismatch")
    fixture_path = os.environ.get("ASSIST_PARK_OBSERVER_FIXTURE")
    if fixture_path:
        try:
            fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
            tmux_value = dict(fixture["tmux"])
            docker_value = dict(fixture["docker"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ActivationError("observer_fixture_invalid") from exc
        observer = ExecutionObserver(
            old_identity=dict(sealed["old_identity"]),
            tmux_probe=lambda: dict(tmux_value),
            docker_probe=lambda: dict(docker_value),
            metadata=dict(fixture.get("metadata") or {}),
            provenance_snapshot=sealed.get("provenance_snapshot"),
        )
    else:
        observer = ExecutionObserver(
            old_identity=dict(sealed["old_identity"]),
            provenance_snapshot=sealed.get("provenance_snapshot"),
        )
    watermark = observer.ready()
    _send(channel, {"event": "candidate_ready", "watermark": watermark, "at": time.monotonic()})
    command = _recv(channel, timeout)
    if command.get("command") != "snapshot":
        raise ActivationError("snapshot_command_missing")
    snapshot = observer.snapshot()
    _atomic_json(Path(str(sealed["control_dir"])) / "snapshot.json", snapshot)
    _send(channel, {"event": "snapshot_ack", "snapshot": snapshot, "at": time.monotonic()})
    command = _recv(channel, timeout)
    if command.get("command") != "old_dead":
        raise ActivationError("old_dead_command_missing")
    observer.await_quiescence(snapshot, float(sealed.get("drain_timeout", 600.0)))
    _atomic_json(Path(str(sealed["control_dir"])) / "snapshot.json", snapshot)
    try:
        command = _recv(channel, timeout)
    except ActivationError as exc:
        if exc.code != "handoff_closed":
            raise
        # Once old_dead was received, this candidate is the sole recovery
        # owner.  It does not exit or self-authorise when the controller dies;
        # only the receipted --resume path can create this exact file.
        channel.close()
        channel = None
        authorise = Path(str(sealed["control_dir"])) / "resume-authorise.json"
        deadline = time.monotonic() + 300.0
        command = {}
        while time.monotonic() < deadline:
            try:
                candidate = json.loads(authorise.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if candidate.get("candidate_identity") == process_identity(os.getpid()):
                command = {"command": "bind"}
                break
            raise ActivationError("resume_candidate_mismatch")
    if command.get("command") != "bind":
        raise ActivationError("bind_not_authorised")
    return CandidateHandoff(sealed=sealed, snapshot=snapshot, channel=channel)


def _identity(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    digest = None
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return {
        "path": os.fspath(resolved),
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "sha256": digest,
    }


def _wait_event(channel: socket.socket, name: str, timeout: float) -> dict[str, object]:
    event = _recv(channel, timeout)
    if event.get("event") != name:
        raise ActivationError("handoff_event_mismatch", f"wanted {name}")
    return event


def _wait_dead(identity: dict[str, object], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not identity_alive(identity):
            return True
        time.sleep(0.02)
    return not identity_alive(identity)


def _freeze_process_tree(old_identity: dict[str, object]) -> list[dict[str, object]]:
    old_pid = int(old_identity["pid"])
    os.kill(old_pid, signal.SIGSTOP)
    frozen: dict[tuple[int, str], dict[str, object]] = {}
    stable_rounds = 0
    while stable_rounds < 2:
        changed = False
        for identity in _descendants(old_pid):
            key = (int(identity["pid"]), str(identity["start_time"]))
            if key in frozen:
                continue
            if identity_alive(identity):
                try:
                    os.kill(key[0], signal.SIGSTOP)
                except ProcessLookupError:
                    continue
                if identity_alive(identity):
                    frozen[key] = dict(identity)
                    changed = True
        stable_rounds = 0 if changed else stable_rounds + 1
    if not identity_alive(old_identity):
        raise ActivationError("old_generation_moved_at_freeze")
    return [frozen[key] for key in sorted(frozen)]


def _signal_identities(identities, signum) -> None:
    for identity in identities:
        if identity_alive(identity):
            try:
                os.kill(int(identity["pid"]), signum)
            except ProcessLookupError:
                pass


def reap_candidate(pid: int, timeout: float = 5.0) -> None:
    """Test/harness cleanup for a candidate created in this controller process."""
    candidate = _CANDIDATE_PROCESSES.pop(int(pid), None)
    if candidate is None:
        return
    try:
        candidate.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        candidate.kill()
        candidate.wait(timeout=timeout)


def _publish_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(f"{pid}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _event(receipt: dict[str, object], name: str, **extra: object) -> None:
    events = receipt.setdefault("events", [])
    assert isinstance(events, list)
    events.append({"name": name, "at": time.monotonic(), **extra})


def _listener_owners(port: int) -> tuple[list[str], list[dict[str, object]]]:
    wanted = f"{int(port):04X}"
    inodes = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            rows = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) <= 9 or fields[3] != "0A":
                continue
            address, separator, port_hex = fields[1].rpartition(":")
            if not separator or port_hex.upper() != wanted:
                continue
            if address not in {"0100007F", "00000000000000000000000001000000"}:
                continue
            inodes.add(fields[9])
    owners = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            links = list((entry / "fd").iterdir())
        except OSError:
            continue
        owns_socket = False
        for link in links:
            try:
                target = os.readlink(link)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                owns_socket = True
                break
        if owns_socket:
            identity = process_identity(int(entry.name))
            if identity is not None:
                owners[(identity["pid"], identity["start_time"])] = identity
    return sorted(inodes), [owners[key] for key in sorted(owners)]


def _authenticated_settings_pid(port: int, token_path: Path, timeout: float) -> int:
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("empty token")
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/api/settings",
            headers={"X-Assist-Token": token},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read())
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise ValueError("invalid response")
        return int(value["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        raise ActivationError("old_generation_mismatch", "authenticated_settings") from exc


def _corroborate_old_generation(
    *,
    port: int,
    token_path: Path,
    pid_file: Path,
    timeout: float,
    expected_identity: dict[str, object] | None = None,
    expected_inodes: list[str] | None = None,
    repair_hint: bool = True,
) -> dict[str, object]:
    inodes, owners = _listener_owners(port)
    if len(inodes) != 1 or len(owners) != 1:
        raise ActivationError("old_generation_mismatch", "listener_owner")
    identity = owners[0]
    if _authenticated_settings_pid(port, token_path, timeout) != identity["pid"]:
        raise ActivationError("old_generation_mismatch", "authenticated_pid")
    if expected_identity is not None and identity != expected_identity:
        raise ActivationError("old_generation_mismatch", "owner_changed")
    if expected_inodes is not None and inodes != expected_inodes:
        raise ActivationError("old_generation_mismatch", "socket_changed")
    event = "pid_file_confirmed"
    try:
        hint_pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        hint_pid = 0
    hint_identity = process_identity(hint_pid) if hint_pid > 0 else None
    if hint_identity is not None and hint_identity != identity:
        raise ActivationError("old_generation_mismatch", "live_pid_hint")
    if hint_identity is None:
        if not repair_hint:
            raise ActivationError("old_generation_mismatch", "pid_hint_changed")
        _publish_pid(pid_file, int(identity["pid"]))
        event = "pid_file_repaired"
    return {"identity": identity, "socket_inodes": inodes, "pid_event": event}


def _revalidate_frozen_owner(
    *,
    port: int,
    pid_file: Path,
    expected_identity: dict[str, object],
    expected_inodes: list[str],
) -> None:
    inodes, owners = _listener_owners(port)
    if inodes != expected_inodes or owners != [expected_identity]:
        raise ActivationError("old_generation_mismatch", "frozen_owner_changed")
    try:
        hint_pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as exc:
        raise ActivationError("old_generation_mismatch", "pid_hint_changed") from exc
    if hint_pid != int(expected_identity["pid"]) or not identity_alive(expected_identity):
        raise ActivationError("old_generation_mismatch", "frozen_generation_changed")


def _sealed_from_env(old_identity: dict[str, object]) -> dict[str, object]:
    required = {
        "assist_home": "ASSIST_ACTIVATION_HOME",
        "assist_ctl": "ASSIST_ACTIVATION_CTL",
        "serve_script": "ASSIST_ACTIVATION_SERVE",
        "pid_file": "ASSIST_PID_FILE",
        "log_file": "ASSIST_LOG_FILE",
        "control_dir": "ASSIST_CONTROL_DIR",
        "auth_token": "ASSIST_AUTH_TOKEN_PATH",
        "port": "ASSIST_PORT",
    }
    values: dict[str, object] = {}
    for key, variable in required.items():
        raw = os.environ.get(variable)
        if not raw:
            raise ActivationError("sealed_path_missing", variable)
        values[key] = int(raw) if key == "port" else os.fspath(Path(raw).resolve())
    home = Path(str(values["assist_home"]))
    for key in ("assist_ctl", "serve_script"):
        path = Path(str(values[key]))
        if path.parent != home:
            raise ActivationError("sealed_path_outside_home", key)
        values[f"{key}_identity"] = _identity(path)
    for key in ("pid_file", "log_file", "control_dir", "auth_token"):
        path = Path(str(values[key]))
        if not path.is_absolute():
            raise ActivationError("sealed_relative_path", key)
    return {
        "schema": SCHEMA,
        "activation_path": ACTIVATION_PATH,
        "old_identity": old_identity,
        "drain_timeout": float(os.environ.get("ASSIST_ACTIVATION_DRAIN_TIMEOUT", "600")),
        **values,
    }


def _activation_preflight(
    old_identity: dict[str, object],
    provenance_snapshot: dict[str, object],
    *,
    tmux_probe: Callable[[], dict[str, object]] | None = None,
) -> dict[str, object]:
    observer = ExecutionObserver(
        old_identity=old_identity,
        docker_probe=lambda: {"reachable": False, "rows": []},
        tmux_probe=tmux_probe,
        provenance_snapshot=provenance_snapshot,
    )
    observer.ready()
    return observer.snapshot()


def controller(*, timeout: float = 20.0) -> dict[str, object]:
    """Own one complete activation attempt and return its durable receipt."""
    pid_file = Path(os.environ["ASSIST_PID_FILE"])
    control_dir = Path(os.environ["ASSIST_CONTROL_DIR"])
    lock_path = control_dir / "controller.lock"
    receipt_path = control_dir / "activation-receipt.json"
    control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActivationError("concurrent_controller") from exc
        corroborated = _corroborate_old_generation(
            port=int(os.environ["ASSIST_PORT"]),
            token_path=Path(os.environ["ASSIST_AUTH_TOKEN_PATH"]),
            pid_file=pid_file,
            timeout=timeout,
        )
        old_identity = dict(corroborated["identity"])
        old_pid = int(old_identity["pid"])
        sealed = _sealed_from_env(old_identity)
        from shared.launch_provenance import LaunchProvenanceStore, ProvenanceError

        receipt: dict[str, object] = {
            "schema": SCHEMA,
            "activation_path": ACTIVATION_PATH,
            "sealed": sealed,
            "old_identity": old_identity,
            "owner": "controller",
            "state": "launching_candidate",
            "events": [],
        }
        _event(receipt, "port_owner_corroborated", socket_inodes=corroborated["socket_inodes"])
        _event(receipt, str(corroborated["pid_event"]))
        registry_context = LaunchProvenanceStore().locked()
        registry_held = False
        try:
            registry = registry_context.__enter__()
            registry_held = True
        except ProvenanceError as exc:
            raise ActivationError("provenance_registry_invalid", exc.code) from exc
        try:
            sealed["provenance_snapshot"] = registry.snapshot()
            preflight = _activation_preflight(old_identity, sealed["provenance_snapshot"])
            receipt["preflight_snapshot"] = preflight
            blockers = list(preflight.get("owned_units") or ())
            if blockers:
                receipt["state"] = "refused_pre_mutation"
                receipt["error"] = "owned_units_live_before_cutover"
                receipt["blocking_owned_units"] = blockers
                _event(receipt, "live_owned_predicate", count=len(blockers))
                _event(receipt, "owned_units_live_before_cutover")
                _atomic_json(receipt_path, receipt)
                registry_context.__exit__(None, None, None)
                registry_held = False
                raise ActivationError("owned_units_live_before_cutover")
            _event(receipt, "live_owned_predicate", count=0)
        except BaseException:
            if registry_held:
                registry_context.__exit__(*sys.exc_info())
                registry_held = False
            raise
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        candidate: subprocess.Popen[bytes] | None = None
        frozen = False
        old_dead = False
        try:
            candidate_env = os.environ.copy()
            candidate_env.update(
                {
                    "ASSIST_PARK_HANDOFF_FD": str(child.fileno()),
                    "ASSIST_HOME": str(sealed["assist_home"]),
                    "ASSIST_PORT": str(sealed["port"]),
                    "ASSIST_PID_FILE": str(sealed["pid_file"]),
                    "ASSIST_LOG_FILE": str(sealed["log_file"]),
                    "ASSIST_CONTROL_DIR": str(sealed["control_dir"]),
                    "ASSIST_AUTH_TOKEN_PATH": str(sealed["auth_token"]),
                }
            )
            log_path = Path(str(sealed["log_file"]))
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                candidate = subprocess.Popen(
                    [
                        sys.executable,
                        str(sealed["serve_script"]),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(sealed["port"]),
                        "--park-handoff-fd",
                        str(child.fileno()),
                    ],
                    env=candidate_env,
                    pass_fds=(child.fileno(),),
                    stdout=log_fd,
                    stderr=log_fd,
                    start_new_session=True,
                )
            finally:
                os.close(log_fd)
            _CANDIDATE_PROCESSES[candidate.pid] = candidate
            child.close()
            receipt["candidate_identity"] = process_identity(candidate.pid)
            _send(parent, sealed)
            ready = _wait_event(parent, "candidate_ready", timeout)
            _event(receipt, "candidate_ready", detail=ready)
            if not identity_alive(old_identity):
                raise ActivationError("old_generation_moved_before_freeze")
            frozen_descendants = _freeze_process_tree(old_identity)
            frozen = True
            _revalidate_frozen_owner(
                port=int(sealed["port"]),
                pid_file=pid_file,
                expected_identity=old_identity,
                expected_inodes=list(corroborated["socket_inodes"]),
            )
            receipt["frozen_descendants"] = frozen_descendants
            _event(receipt, "old_frozen", descendants=frozen_descendants)
            _send(parent, {"command": "snapshot"})
            ack = _wait_event(parent, "snapshot_ack", timeout)
            receipt["snapshot"] = ack.get("snapshot")
            _event(receipt, "snapshot_ack")
            registry_context.__exit__(None, None, None)
            registry_held = False
            if not identity_alive(old_identity):
                raise ActivationError("old_generation_moved_before_termination")
            _revalidate_frozen_owner(
                port=int(sealed["port"]),
                pid_file=pid_file,
                expected_identity=old_identity,
                expected_inodes=list(corroborated["socket_inodes"]),
            )
            os.kill(old_pid, signal.SIGKILL)
            if not _wait_dead(old_identity, timeout):
                raise ActivationError("old_generation_would_not_terminate")
            old_dead = True
            frozen = False
            _signal_identities(frozen_descendants, signal.SIGCONT)
            _event(receipt, "old_dead")
            receipt["owner"] = "candidate"
            receipt["state"] = "draining_recovery_owner"
            _atomic_json(receipt_path, receipt)
            _send(parent, {"command": "old_dead"})
            _send(parent, {"command": "bind"})
            bound = _wait_event(parent, "candidate_bound", timeout)
            candidate_identity = process_identity(candidate.pid)
            if candidate_identity != receipt.get("candidate_identity"):
                raise ActivationError("candidate_generation_moved")
            _event(receipt, "candidate_bound", detail=bound)
            try:
                receipt["snapshot"] = json.loads(
                    (control_dir / "snapshot.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ActivationError("final_snapshot_missing") from exc
            _publish_pid(pid_file, candidate.pid)
            receipt["state"] = "complete"
            receipt["owner"] = "candidate"
            _atomic_json(receipt_path, receipt)
            return receipt
        except BaseException:
            if frozen and identity_alive(old_identity):
                _signal_identities(locals().get("frozen_descendants", ()), signal.SIGCONT)
                os.kill(old_pid, signal.SIGCONT)
            if not old_dead and candidate is not None and process_start_time(candidate.pid):
                try:
                    candidate.terminate()
                    candidate.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        candidate.kill()
                    except OSError:
                        pass
                _CANDIDATE_PROCESSES.pop(candidate.pid, None)
            if old_dead:
                receipt["owner"] = "candidate"
                receipt["state"] = "recovery_required"
                _atomic_json(receipt_path, receipt)
            raise
        finally:
            if registry_held:
                registry_context.__exit__(None, None, None)
            parent.close()
            child.close()
    finally:
        os.close(lock_fd)


def resume(*, timeout: float = 20.0) -> dict[str, object]:
    """Finish the sole candidate-owned receipt after old termination."""
    pid_file = Path(os.environ["ASSIST_PID_FILE"])
    control_dir = Path(os.environ["ASSIST_CONTROL_DIR"])
    receipt_path = control_dir / "activation-receipt.json"
    lock_fd = os.open(control_dir / "controller.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActivationError("concurrent_controller") from exc
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ActivationError("resume_receipt_missing") from exc
        if (
            receipt.get("schema") != SCHEMA
            or receipt.get("activation_path") != ACTIVATION_PATH
            or receipt.get("owner") != "candidate"
            or receipt.get("state") not in {"recovery_required", "draining_recovery_owner"}
        ):
            raise ActivationError("resume_receipt_not_owned")
        candidate_identity = receipt.get("candidate_identity")
        if not isinstance(candidate_identity, dict) or not identity_alive(candidate_identity):
            raise ActivationError("resume_candidate_absent")
        if identity_alive(receipt.get("old_identity")):
            raise ActivationError("resume_old_generation_alive")
        _atomic_json(
            control_dir / "resume-authorise.json",
            {"candidate_identity": candidate_identity, "receipt": os.fspath(receipt_path)},
        )
        bound_path = control_dir / "candidate-bound.json"
        deadline = time.monotonic() + timeout
        bound = None
        while time.monotonic() < deadline:
            try:
                candidate = json.loads(bound_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.02)
                continue
            if candidate.get("candidate") != candidate_identity:
                raise ActivationError("resume_bound_generation_mismatch")
            bound = candidate
            break
        if bound is None:
            raise ActivationError("resume_bind_ack_missing")
        _event(receipt, "candidate_bound", detail=bound, resumed=True)
        _publish_pid(pid_file, int(candidate_identity["pid"]))
        receipt["state"] = "complete"
        _atomic_json(receipt_path, receipt)
        return receipt
    finally:
        os.close(lock_fd)


def _main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assist parked activation controller")
    parser.add_argument("action", choices=("controller", "resume"))
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        receipt = (
            resume(timeout=args.timeout)
            if args.action == "resume"
            else controller(timeout=args.timeout)
        )
    except (ActivationError, KeyError, OSError) as exc:
        code = exc.code if isinstance(exc, ActivationError) else "activation_failed"
        print(f"{code}: {exc}", file=sys.stderr)
        return 1
    names = [event["name"] for event in receipt.get("events", [])]
    print(json.dumps({"ok": True, "activation_path": ACTIVATION_PATH, "events": names}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
