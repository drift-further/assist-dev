#!/usr/bin/env python3
"""Create once, then read-only verify, the immutable Assist Docker tree.

The manifest stores identities only: relative path, object type, executable
mode bits, regular-file SHA-256, and symlink target. Normal execution never
opens the manifest or Docker tree for writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


SCHEMA = "container-boundary-v16-docker-tree-v1"
BOUNDARY = "CONTAINER-BOUNDARY-V16"
REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_ROOT = REPO_ROOT / "docker"
BASELINE = REPO_ROOT / "tools" / "release" / "container-boundary-v16-docker-tree.json"


class BoundaryError(RuntimeError):
    pass


def _mode(value: int) -> str:
    return f"{stat.S_IMODE(value) & 0o111:04o}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _identity(path: Path, relative: str) -> dict[str, str]:
    metadata = path.lstat()
    entry = {"path": relative, "mode": _mode(metadata.st_mode)}
    if stat.S_ISREG(metadata.st_mode):
        entry.update({"type": "file", "sha256": _sha256(path)})
    elif stat.S_ISDIR(metadata.st_mode):
        entry["type"] = "directory"
    elif stat.S_ISLNK(metadata.st_mode):
        entry.update({"type": "symlink", "target": os.readlink(path)})
    elif stat.S_ISFIFO(metadata.st_mode):
        entry["type"] = "fifo"
    elif stat.S_ISSOCK(metadata.st_mode):
        entry["type"] = "socket"
    elif stat.S_ISCHR(metadata.st_mode):
        entry["type"] = "character-device"
    elif stat.S_ISBLK(metadata.st_mode):
        entry["type"] = "block-device"
    else:
        entry["type"] = "unknown"
    return entry


def inventory(root: Path = DOCKER_ROOT) -> list[dict[str, str]]:
    if not root.is_dir() or root.is_symlink():
        raise BoundaryError(f"Docker tree root is not a directory: {root}")

    entries = [_identity(root, ".")]

    def visit(directory: Path) -> None:
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda item: item.name)
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            identity = _identity(path, relative)
            entries.append(identity)
            if identity["type"] == "directory":
                visit(path)

    visit(root)
    return entries


def payload() -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "boundary": BOUNDARY,
        "tree": "docker",
        "entries": inventory(),
    }


def write_baseline() -> None:
    serialized = (
        json.dumps(payload(), sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            BASELINE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
    except FileExistsError as exc:
        raise BoundaryError(
            f"baseline already exists and will not be updated: {BASELINE}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(serialized)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        # The exclusive create is intentionally not retried or replaced.  A
        # partial file is retained for inspection rather than adopted.
        raise
    print(f"{BOUNDARY}: baseline created ({len(payload()['entries'])} objects)")


def verify() -> None:
    try:
        expected = json.loads(BASELINE.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BoundaryError(f"baseline does not exist: {BASELINE}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"invalid baseline {BASELINE}: {exc}") from exc
    actual = payload()
    if expected != actual:
        expected_entries = {
            entry.get("path"): entry for entry in expected.get("entries", [])
        }
        actual_entries = {entry["path"]: entry for entry in actual["entries"]}
        changed = sorted(
            path
            for path in set(expected_entries) | set(actual_entries)
            if expected_entries.get(path) != actual_entries.get(path)
        )
        detail = ", ".join(changed[:20])
        if len(changed) > 20:
            detail += f", ... ({len(changed)} total)"
        raise BoundaryError(f"Docker tree identity mismatch: {detail}")
    print(f"{BOUNDARY}: PASS ({len(actual['entries'])} objects)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.write_baseline:
            write_baseline()
        else:
            verify()
    except BoundaryError as exc:
        print(f"{BOUNDARY}: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
