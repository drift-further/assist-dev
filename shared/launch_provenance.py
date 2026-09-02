"""Durable, no-replace provenance for first-party Assist tmux creation.

This is an ordinary-lifecycle, same-UID correctness boundary.  Normal server
startup initializes a missing store once; every runtime reader still fails
closed when state is missing or invalid.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator

from shared.agent_identity import _stat_fields
from shared.tmux import ExpectedTargetIdentity


ROOT_NAME = ".assist-launch-provenance-v1"
INITIALIZATION_RECEIPT_NAME = ".assist-launch-provenance-v1-initialization.json"
EPOCH_SCHEMA = "assist-launch-provenance-v1-epoch"
ORIGIN_SCHEMA = "assist-launch-provenance-v1-origin"
EVENT_SCHEMA = "assist-launch-provenance-v1-event"
INITIALIZATION_SCHEMA = "assist-launch-provenance-v1-initialization"
SNAPSHOT_SCHEMA = "assist-launch-provenance-v1-snapshot"
COVERAGE = "post_epoch_created_only"
_MAX_JSON_BYTES = 1024 * 1024


class ProvenanceError(RuntimeError):
    """Machine-readable fail-closed provenance error."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def identity_digest(identity: object) -> str:
    return _digest(_normalize_identity(identity))


def _absolute(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assist_home(value: os.PathLike[str] | str | None = None) -> Path:
    selected = value or os.environ.get("ASSIST_HOME")
    if selected is None:
        selected = Path(__file__).resolve().parent.parent
    try:
        return Path(selected).resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("assist_home_invalid", os.fspath(selected)) from exc


def provenance_root(
    assist_home: os.PathLike[str] | str | None = None,
) -> Path:
    override = os.environ.get("ASSIST_LAUNCH_PROVENANCE_ROOT")
    if override:
        return _absolute(override)
    return _assist_home(assist_home) / ROOT_NAME


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _checked_stat(path: Path, *, kind: str, mode: int) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ProvenanceError("provenance_state_missing", os.fspath(path)) from exc
    if kind == "directory":
        correct_type = stat.S_ISDIR(info.st_mode)
    elif kind == "regular":
        correct_type = stat.S_ISREG(info.st_mode)
    else:
        raise ValueError(kind)
    if not correct_type or stat.S_ISLNK(info.st_mode):
        raise ProvenanceError("provenance_state_type", os.fspath(path))
    if info.st_uid != os.geteuid():
        raise ProvenanceError("provenance_state_owner", os.fspath(path))
    if stat.S_IMODE(info.st_mode) != mode:
        raise ProvenanceError("provenance_state_mode", os.fspath(path))
    return info


def _read_json(path: Path) -> dict[str, object]:
    _checked_stat(path, kind="regular", mode=0o600)
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_JSON_BYTES + 1)
    except OSError as exc:
        raise ProvenanceError("provenance_state_unreadable", os.fspath(path)) from exc
    if len(data) > _MAX_JSON_BYTES:
        raise ProvenanceError("provenance_state_oversize", os.fspath(path))
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("provenance_state_corrupt", os.fspath(path)) from exc
    if not isinstance(value, dict):
        raise ProvenanceError("provenance_state_corrupt", os.fspath(path))
    return value


def _publish_json_no_replace(path: Path, payload: object) -> None:
    """Publish immutable canonical JSON without replacing an existing inode."""
    encoded = _canonical_bytes(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    published = False
    try:
        fd = os.open(temporary, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
            published = True
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise ProvenanceError(
                    "provenance_duplicate_unreadable", os.fspath(path)
                ) from exc
            if existing != encoded:
                raise ProvenanceError("provenance_duplicate_unequal", os.fspath(path))
        _fsync_directory(path.parent)
    except ProvenanceError:
        raise
    except OSError as exc:
        stage = "after_publication" if published else "before_publication"
        raise ProvenanceError("provenance_publish_failed", stage) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_json_replace(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(temporary, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(_canonical_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _process_start_time(pid: int) -> str | None:
    try:
        process_state, _parent_pid, start_time = _stat_fields(int(pid))
    except (OSError, TypeError, ValueError, IndexError):
        return None
    if process_state == "Z":
        return None
    return start_time


def _actor_identity() -> dict[str, object]:
    start = _process_start_time(os.getpid())
    if start is None:
        raise ProvenanceError("creator_identity_unavailable")
    return {"pid": os.getpid(), "start_time": start}


def _normalize_identity(value: object) -> dict[str, object]:
    try:
        identity = ExpectedTargetIdentity.from_value(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProvenanceError("provenance_identity_incomplete") from exc
    normalized = identity.as_dict()
    if isinstance(value, dict) and set(value) != set(normalized):
        raise ProvenanceError("provenance_identity_incomplete")
    return normalized


def _validate_epoch(value: dict[str, object]) -> dict[str, object]:
    required = {
        "schema",
        "epoch",
        "coverage",
        "assist_home",
        "initialized_at_ns",
        "owner_uid",
    }
    if set(value) != required or value.get("schema") != EPOCH_SCHEMA:
        raise ProvenanceError("provenance_epoch_invalid")
    if value.get("coverage") != COVERAGE:
        raise ProvenanceError("provenance_epoch_invalid")
    if not isinstance(value.get("epoch"), str) or len(str(value["epoch"])) != 32:
        raise ProvenanceError("provenance_epoch_invalid")
    if not isinstance(value.get("assist_home"), str):
        raise ProvenanceError("provenance_epoch_invalid")
    if not isinstance(value.get("initialized_at_ns"), int):
        raise ProvenanceError("provenance_epoch_invalid")
    if value.get("owner_uid") != os.geteuid():
        raise ProvenanceError("provenance_epoch_invalid")
    return dict(value)


def _validate_origin(value: dict[str, object], epoch: dict[str, object]) -> dict[str, object]:
    required = {
        "schema",
        "epoch",
        "origin",
        "resource_kind",
        "surface",
        "creator",
        "published_at_ns",
        "identity",
        "identity_digest",
        "diagnostic_alias",
    }
    if set(value) != required or value.get("schema") != ORIGIN_SCHEMA:
        raise ProvenanceError("provenance_origin_invalid")
    if value.get("epoch") != epoch["epoch"]:
        raise ProvenanceError("provenance_origin_wrong_epoch")
    if value.get("origin") not in {"created", "adopted"}:
        raise ProvenanceError("provenance_origin_invalid")
    if value.get("resource_kind") != "tmux_pane":
        raise ProvenanceError("provenance_origin_invalid")
    if not isinstance(value.get("surface"), str) or not value["surface"]:
        raise ProvenanceError("provenance_origin_invalid")
    creator = value.get("creator")
    if not isinstance(creator, dict) or set(creator) != {"pid", "start_time"}:
        raise ProvenanceError("provenance_origin_invalid")
    if not isinstance(value.get("published_at_ns"), int):
        raise ProvenanceError("provenance_origin_invalid")
    identity = _normalize_identity(value.get("identity"))
    if value.get("identity_digest") != identity_digest(identity):
        raise ProvenanceError("provenance_origin_digest_mismatch")
    alias = value.get("diagnostic_alias")
    if alias is not None and not isinstance(alias, str):
        raise ProvenanceError("provenance_origin_invalid")
    result = dict(value)
    result["identity"] = identity
    return result


def _validate_event(value: dict[str, object], epoch: dict[str, object]) -> dict[str, object]:
    required = {
        "schema",
        "epoch",
        "event",
        "resource_kind",
        "surface",
        "actor",
        "published_at_ns",
        "identity",
        "identity_digest",
        "diagnostic_alias",
    }
    if set(value) != required or value.get("schema") != EVENT_SCHEMA:
        raise ProvenanceError("provenance_event_invalid")
    if value.get("epoch") != epoch["epoch"] or value.get("event") != "adoption":
        raise ProvenanceError("provenance_event_invalid")
    if value.get("resource_kind") != "tmux_pane":
        raise ProvenanceError("provenance_event_invalid")
    if not isinstance(value.get("surface"), str) or not value["surface"]:
        raise ProvenanceError("provenance_event_invalid")
    actor = value.get("actor")
    if not isinstance(actor, dict) or set(actor) != {"pid", "start_time"}:
        raise ProvenanceError("provenance_event_invalid")
    if not isinstance(value.get("published_at_ns"), int):
        raise ProvenanceError("provenance_event_invalid")
    identity = _normalize_identity(value.get("identity"))
    if value.get("identity_digest") != identity_digest(identity):
        raise ProvenanceError("provenance_event_digest_mismatch")
    alias = value.get("diagnostic_alias")
    if alias is not None and not isinstance(alias, str):
        raise ProvenanceError("provenance_event_invalid")
    result = dict(value)
    result["identity"] = identity
    return result


def validate_snapshot(value: object) -> dict[str, object]:
    """Validate a sealed in-memory snapshot without acquiring its registry lock."""
    if not isinstance(value, dict):
        raise ProvenanceError("provenance_snapshot_invalid")
    if set(value) != {"schema", "epoch", "coverage", "origins", "events"}:
        raise ProvenanceError("provenance_snapshot_invalid")
    if value.get("schema") != SNAPSHOT_SCHEMA or value.get("coverage") != COVERAGE:
        raise ProvenanceError("provenance_snapshot_invalid")
    epoch_value = value.get("epoch")
    if not isinstance(epoch_value, dict):
        raise ProvenanceError("provenance_snapshot_invalid")
    epoch = _validate_epoch(epoch_value)
    origins_value = value.get("origins")
    events_value = value.get("events")
    if not isinstance(origins_value, list) or not isinstance(events_value, list):
        raise ProvenanceError("provenance_snapshot_invalid")
    origins = []
    seen = set()
    for item in origins_value:
        if not isinstance(item, dict):
            raise ProvenanceError("provenance_snapshot_invalid")
        origin = _validate_origin(item, epoch)
        digest = str(origin["identity_digest"])
        if digest in seen:
            raise ProvenanceError("provenance_duplicate_identity")
        seen.add(digest)
        origins.append(origin)
    events = []
    for item in events_value:
        if not isinstance(item, dict):
            raise ProvenanceError("provenance_snapshot_invalid")
        events.append(_validate_event(item, epoch))
    return {
        "schema": SNAPSHOT_SCHEMA,
        "epoch": epoch,
        "coverage": COVERAGE,
        "origins": origins,
        "events": events,
    }


class LockedRegistry:
    """Operations permitted only while the process-shared lock is held."""

    def __init__(self, store: "LaunchProvenanceStore", epoch: dict[str, object]) -> None:
        self.store = store
        self.epoch = epoch

    def _origin_path(self, identity: object) -> Path:
        return self.store.origins / f"{identity_digest(identity)}.json"

    def _read_existing_origin(self, identity: object) -> dict[str, object] | None:
        path = self._origin_path(identity)
        if not os.path.lexists(path):
            return None
        origin = _validate_origin(_read_json(path), self.epoch)
        normalized = _normalize_identity(identity)
        if origin["identity"] != normalized:
            raise ProvenanceError("provenance_origin_identity_mismatch")
        return origin

    def record_created(
        self, identity: object, *, surface: str, diagnostic_alias: str | None = None
    ) -> dict[str, object]:
        normalized = _normalize_identity(identity)
        if self._read_existing_origin(normalized) is not None:
            raise ProvenanceError("provenance_origin_already_recorded")
        receipt = {
            "schema": ORIGIN_SCHEMA,
            "epoch": self.epoch["epoch"],
            "origin": "created",
            "resource_kind": "tmux_pane",
            "surface": str(surface),
            "creator": _actor_identity(),
            "published_at_ns": time.time_ns(),
            "identity": normalized,
            "identity_digest": identity_digest(normalized),
            "diagnostic_alias": diagnostic_alias,
        }
        _validate_origin(receipt, self.epoch)
        _publish_json_no_replace(self._origin_path(normalized), receipt)
        return receipt

    def record_adoption(
        self, identity: object, *, surface: str, diagnostic_alias: str | None = None
    ) -> dict[str, object]:
        normalized = _normalize_identity(identity)
        existing = self._read_existing_origin(normalized)
        if existing is None:
            existing = {
                "schema": ORIGIN_SCHEMA,
                "epoch": self.epoch["epoch"],
                "origin": "adopted",
                "resource_kind": "tmux_pane",
                "surface": str(surface),
                "creator": _actor_identity(),
                "published_at_ns": time.time_ns(),
                "identity": normalized,
                "identity_digest": identity_digest(normalized),
                "diagnostic_alias": diagnostic_alias,
            }
            _validate_origin(existing, self.epoch)
            _publish_json_no_replace(self._origin_path(normalized), existing)
        event = {
            "schema": EVENT_SCHEMA,
            "epoch": self.epoch["epoch"],
            "event": "adoption",
            "resource_kind": "tmux_pane",
            "surface": str(surface),
            "actor": _actor_identity(),
            "published_at_ns": time.time_ns(),
            "identity": normalized,
            "identity_digest": identity_digest(normalized),
            "diagnostic_alias": diagnostic_alias,
        }
        _validate_event(event, self.epoch)
        event_path = self.store.events / f"{time.time_ns()}-{uuid.uuid4().hex}.json"
        _publish_json_no_replace(event_path, event)
        return {"origin": existing, "event": event}

    def snapshot(self) -> dict[str, object]:
        origins = []
        events = []
        for path in sorted(self.store.origins.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                raise ProvenanceError("provenance_unexpected_path", os.fspath(path))
            value = _validate_origin(_read_json(path), self.epoch)
            if path.name != f"{value['identity_digest']}.json":
                raise ProvenanceError("provenance_origin_filename_mismatch", path.name)
            origins.append(value)
        for path in sorted(self.store.events.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                raise ProvenanceError("provenance_unexpected_path", os.fspath(path))
            events.append(_validate_event(_read_json(path), self.epoch))
        return {
            "schema": SNAPSHOT_SCHEMA,
            "epoch": dict(self.epoch),
            "coverage": self.epoch["coverage"],
            "origins": origins,
            "events": events,
        }


class LaunchProvenanceStore:
    def __init__(
        self,
        *,
        assist_home: os.PathLike[str] | str | None = None,
        root: os.PathLike[str] | str | None = None,
        lock_timeout: float = 1.0,
    ) -> None:
        self.assist_home = _assist_home(assist_home)
        self.root = _absolute(root) if root is not None else provenance_root(self.assist_home)
        self.origins = self.root / "origins"
        self.events = self.root / "events"
        self.epoch_path = self.root / "epoch.json"
        self.lock_path = self.root / "registry.lock"
        self.lock_timeout = float(lock_timeout)

    def _validate_layout(self) -> dict[str, object]:
        _checked_stat(self.root, kind="directory", mode=0o700)
        _checked_stat(self.origins, kind="directory", mode=0o700)
        _checked_stat(self.events, kind="directory", mode=0o700)
        _checked_stat(self.lock_path, kind="regular", mode=0o600)
        epoch = _validate_epoch(_read_json(self.epoch_path))
        if Path(str(epoch["assist_home"])) != self.assist_home:
            raise ProvenanceError("provenance_epoch_home_mismatch")
        allowed = {"origins", "events", "epoch.json", "registry.lock"}
        try:
            children = {entry.name for entry in self.root.iterdir()}
        except OSError as exc:
            raise ProvenanceError("provenance_state_unreadable", os.fspath(self.root)) from exc
        if children != allowed:
            raise ProvenanceError("provenance_unexpected_path")
        return epoch

    @contextlib.contextmanager
    def locked(self, *, timeout: float | None = None) -> Iterator[LockedRegistry]:
        self._validate_layout()
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path, flags)
        except OSError as exc:
            raise ProvenanceError("provenance_lock_invalid") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ProvenanceError("provenance_lock_invalid")
            deadline = time.monotonic() + (
                self.lock_timeout if timeout is None else float(timeout)
            )
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ProvenanceError("provenance_lock_timeout")
                    time.sleep(0.01)
            epoch = self._validate_layout()
            yield LockedRegistry(self, epoch)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def snapshot(self, *, timeout: float | None = None) -> dict[str, object]:
        with self.locked(timeout=timeout) as registry:
            return registry.snapshot()

    def record_adoption(
        self,
        identity: object,
        *,
        surface: str,
        diagnostic_alias: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        with self.locked(timeout=timeout) as registry:
            return registry.record_adoption(
                identity,
                surface=surface,
                diagnostic_alias=diagnostic_alias,
            )


def initialize_epoch(
    *,
    assist_home: os.PathLike[str] | str,
    receipt_path: os.PathLike[str] | str,
    expect_empty: bool,
) -> dict[str, object]:
    home = _assist_home(assist_home)
    root = provenance_root(home)
    if not expect_empty:
        raise ProvenanceError("provenance_expect_empty_required")
    if os.path.lexists(root):
        raise ProvenanceError("provenance_already_initialized", os.fspath(root))
    try:
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        _fsync_directory(root)
        _fsync_directory(root.parent)
        for directory in (root / "origins", root / "events"):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
            _fsync_directory(directory)
            _fsync_directory(root)
        lock_fd = os.open(
            root / "registry.lock",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
        finally:
            os.close(lock_fd)
        _fsync_directory(root)
        epoch = {
            "schema": EPOCH_SCHEMA,
            "epoch": uuid.uuid4().hex,
            "coverage": COVERAGE,
            "assist_home": os.fspath(home),
            "initialized_at_ns": time.time_ns(),
            "owner_uid": os.geteuid(),
        }
        _publish_json_no_replace(root / "epoch.json", epoch)
        store = LaunchProvenanceStore(assist_home=home, root=root)
        snapshot = store.snapshot()
        if snapshot["origins"] or snapshot["events"]:
            raise ProvenanceError("provenance_epoch_not_empty")
        receipt = {
            "schema": INITIALIZATION_SCHEMA,
            "assist_home": os.fspath(home),
            "provenance_root": os.fspath(root),
            "coverage": COVERAGE,
            "epoch": epoch,
            "epoch_sha256": hashlib.sha256((root / "epoch.json").read_bytes()).hexdigest(),
            "empty": True,
        }
        _atomic_json_replace(_absolute(receipt_path), receipt)
        return receipt
    except ProvenanceError:
        raise
    except OSError as exc:
        raise ProvenanceError("provenance_initialize_failed", type(exc).__name__) from exc


def initialize_for_startup(
    assist_home: os.PathLike[str] | str | None = None,
) -> bool:
    """Initialize an absent store exactly as the explicit release operation does.

    Existing paths are deliberately left alone.  In particular, startup does
    not repair or replace a corrupt store; normal readers will reject it.
    """
    home = _assist_home(assist_home)
    root = provenance_root(home)
    if os.path.lexists(root):
        return False
    initialize_epoch(
        assist_home=home,
        receipt_path=home / INITIALIZATION_RECEIPT_NAME,
        expect_empty=True,
    )
    return True


def verify_initialization(
    *,
    assist_home: os.PathLike[str] | str,
    receipt_path: os.PathLike[str] | str,
    require_empty: bool,
    require_coverage: str,
) -> dict[str, object]:
    home = _assist_home(assist_home)
    receipt = _read_json(_absolute(receipt_path))
    required = {
        "schema",
        "assist_home",
        "provenance_root",
        "coverage",
        "epoch",
        "epoch_sha256",
        "empty",
    }
    if set(receipt) != required or receipt.get("schema") != INITIALIZATION_SCHEMA:
        raise ProvenanceError("provenance_initialization_receipt_invalid")
    root = provenance_root(home)
    if receipt.get("assist_home") != os.fspath(home):
        raise ProvenanceError("provenance_initialization_home_mismatch")
    if receipt.get("provenance_root") != os.fspath(root):
        raise ProvenanceError("provenance_initialization_root_mismatch")
    if receipt.get("coverage") != require_coverage:
        raise ProvenanceError("provenance_initialization_coverage_mismatch")
    store = LaunchProvenanceStore(assist_home=home, root=root)
    snapshot = store.snapshot()
    if receipt.get("epoch") != snapshot["epoch"]:
        raise ProvenanceError("provenance_initialization_epoch_mismatch")
    digest = hashlib.sha256((root / "epoch.json").read_bytes()).hexdigest()
    if receipt.get("epoch_sha256") != digest:
        raise ProvenanceError("provenance_initialization_epoch_mismatch")
    empty = not snapshot["origins"] and not snapshot["events"]
    if require_empty and not empty:
        raise ProvenanceError("provenance_epoch_not_empty")
    if require_empty and receipt.get("empty") is not True:
        raise ProvenanceError("provenance_initialization_receipt_invalid")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("initialize")
    initialize.add_argument("--assist-home", required=True)
    initialize.add_argument("--expect-empty", action="store_true")
    initialize.add_argument("--receipt", required=True)
    verify = commands.add_parser("verify-initialization")
    verify.add_argument("--assist-home", required=True)
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--require-empty", action="store_true")
    verify.add_argument("--require-coverage", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "initialize":
            initialize_epoch(
                assist_home=arguments.assist_home,
                receipt_path=arguments.receipt,
                expect_empty=arguments.expect_empty,
            )
        else:
            verify_initialization(
                assist_home=arguments.assist_home,
                receipt_path=arguments.receipt,
                require_empty=arguments.require_empty,
                require_coverage=arguments.require_coverage,
            )
    except ProvenanceError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
