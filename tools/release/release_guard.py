#!/usr/bin/env python3
"""Receipt-backed guard for a prepared maintainer release.

The guard deliberately uses only the Python standard library and Git plumbing.  Receipts
record identities, never working-tree file contents, except for the narrowly scoped
materialisation journal needed to make rollback possible.  Overlay receipts in particular
contain hashes only.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


SCHEMA = "effort510-release-guard-v2"
PUBLIC_REF = "refs/heads/release/effort-510-v16"
HUMAN_APPROVER = "Daniel"
DEFAULT_READONLY_EXCLUDES = (".git", ".venv", "venv")


class GuardError(RuntimeError):
    """A release invariant did not hold."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _receipt_digest(value: Any) -> str:
    return _sha256(_json_bytes(value))


def _atomic_json(path: Path, value: Any) -> None:
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path).absolute()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"invalid receipt {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise GuardError(f"receipt is not an object: {source}")
    return value


def _repo(path: str | Path) -> Path:
    candidate = Path(path).absolute()
    if not candidate.is_dir():
        raise GuardError(f"repository does not exist: {candidate}")
    result = _git(candidate, "rev-parse", "--show-toplevel").stdout.decode().strip()
    resolved = Path(result).absolute()
    if resolved != candidate:
        raise GuardError(f"repository must be its exact top level: {candidate}")
    return candidate


def _git(
    repo: Path,
    *arguments: str,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GuardError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def _decode_path(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuardError("repository path is not valid UTF-8") from exc


def _safe_relative(path: str) -> str:
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise GuardError(f"unsafe repository path: {path!r}")
    return candidate.as_posix()


def _common_dir(repo: Path) -> str:
    raw = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout
    return os.fspath(Path(raw.decode().strip()).absolute())


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def _symbolic_head(repo: Path) -> str | None:
    result = _git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise GuardError("could not inspect symbolic HEAD")
    return result.stdout.decode().strip()


def _mode_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character"
    if stat.S_ISBLK(mode):
        return "block"
    return "unknown"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _path_identity(path: Path) -> dict[str, Any]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"type": "missing"}
    kind = _mode_kind(before.st_mode)
    identity: dict[str, Any] = {
        "type": kind,
        "mode": format(stat.S_IMODE(before.st_mode), "04o"),
    }
    if kind == "file":
        identity["sha256"] = _hash_file(path)
        identity["size"] = before.st_size
    elif kind == "symlink":
        target = os.readlink(path)
        identity["sha256"] = _sha256(os.fsencode(target))
        identity["target"] = target
    elif kind == "directory":
        identity["sha256"] = _sha256(b"directory")
    else:
        identity["sha256"] = _sha256(
            f"{kind}:{before.st_rdev}:{before.st_ino}".encode()
        )
    after = path.lstat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise GuardError(f"path changed while it was hashed: {path}")
    return identity


def _split_z(raw: bytes) -> list[bytes]:
    values = raw.split(b"\0")
    if values and values[-1] == b"":
        values.pop()
    return values


def _index_entries(repo: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for record in _split_z(_git(repo, "ls-files", "--stage", "-z").stdout):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split()
        except ValueError as exc:
            raise GuardError("could not parse Git index") from exc
        entries.append(
            {
                "path": _safe_relative(_decode_path(raw_path)),
                "mode": mode,
                "oid": object_id,
                "stage": stage,
            }
        )
    return entries


def _git_paths(repo: Path, *arguments: str) -> list[str]:
    return [
        _safe_relative(_decode_path(value))
        for value in _split_z(_git(repo, *arguments, "-z").stdout)
    ]


def capture_overlay_data(canonical: str | Path) -> dict[str, Any]:
    repo = _repo(canonical)
    index_entries = _index_entries(repo)
    tracked_paths = sorted({entry["path"] for entry in index_entries})
    untracked_paths = sorted(
        _git_paths(repo, "ls-files", "--others", "--exclude-standard")
    )
    tracked = [
        {"path": path, "identity": _path_identity(repo / path)}
        for path in tracked_paths
    ]
    untracked = [
        {"path": path, "identity": _path_identity(repo / path)}
        for path in untracked_paths
    ]
    index_diff = _git(
        repo,
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-renames",
        "HEAD",
        "--",
    ).stdout
    worktree_diff = _git(
        repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-renames",
        "--",
    ).stdout
    status = _git(
        repo,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
    ).stdout
    return {
        "schema": SCHEMA,
        "kind": "overlay",
        "canonical": os.fspath(repo),
        "common_dir": _common_dir(repo),
        "head": _head(repo),
        "symbolic_head": _symbolic_head(repo),
        "index_stages": index_entries,
        "tracked_worktree": tracked,
        "untracked": untracked,
        "index_diff": {"sha256": _sha256(index_diff), "size": len(index_diff)},
        "worktree_diff": {
            "sha256": _sha256(worktree_diff),
            "size": len(worktree_diff),
        },
        "status": {"sha256": _sha256(status), "size": len(status)},
    }


def _require_overlay(value: dict[str, Any], canonical: Path) -> None:
    if value.get("schema") != SCHEMA or value.get("kind") != "overlay":
        raise GuardError("not an Effort 510 overlay receipt")
    if value.get("canonical") != os.fspath(canonical):
        raise GuardError("overlay belongs to a different canonical checkout")


def capture_overlay(canonical: str, output: str) -> None:
    receipt = capture_overlay_data(canonical)
    _atomic_json(Path(output), receipt)


def verify_overlay(canonical: str, receipt_path: str) -> None:
    repo = _repo(canonical)
    expected = _load_json(receipt_path)
    _require_overlay(expected, repo)
    actual = capture_overlay_data(repo)
    if actual != expected:
        raise GuardError("canonical overlay identity changed")


def _tree_entry(repo: Path, tree: str, path: str) -> dict[str, str] | None:
    raw = _git(repo, "ls-tree", "-z", tree, "--", path).stdout
    records = _split_z(raw)
    if not records:
        return None
    if len(records) != 1:
        raise GuardError(f"ambiguous tree entry: {path}")
    metadata, raw_path = records[0].split(b"\t", 1)
    mode, object_type, object_id = metadata.decode("ascii").split()
    if _decode_path(raw_path) != path:
        raise GuardError(f"tree returned a different path for {path}")
    return {"mode": mode, "type": object_type, "oid": object_id}


def _stage_zero_entry(repo: Path, path: str) -> dict[str, str] | None:
    raw = _git(repo, "ls-files", "--stage", "-z", "--", path).stdout
    records = _split_z(raw)
    if not records:
        return None
    parsed: list[dict[str, str]] = []
    for record in records:
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split()
        if _decode_path(raw_path) == path:
            parsed.append({"mode": mode, "oid": object_id, "stage": stage})
    if len(parsed) != 1 or parsed[0]["stage"] != "0":
        raise GuardError(f"path has unresolved or ambiguous index stages: {path}")
    return {"mode": parsed[0]["mode"], "type": "blob", "oid": parsed[0]["oid"]}


def _diff_bytes(repo: Path, base: str, tree: str | None = None) -> bytes:
    arguments = [
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-renames",
    ]
    if tree is None:
        arguments.extend(["--cached", base, "--"])
    else:
        arguments.extend([base, tree, "--"])
    return _git(repo, *arguments).stdout


def _text_diff(repo: Path, base: str, path: str, tree: str | None = None) -> bytes:
    arguments = [
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
    ]
    if tree is None:
        arguments.extend(["--cached", base, "--", path])
    else:
        arguments.extend([base, tree, "--", path])
    return _git(repo, *arguments).stdout


HUNK_HEADER = re.compile(rb"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(.*)$")


def _normalised_hunks(diff: bytes) -> list[str]:
    hunks: list[bytes] = []
    current: list[bytes] | None = None
    for line in diff.splitlines():
        match = HUNK_HEADER.match(line)
        if match:
            if current is not None:
                hunks.append(b"\n".join(current) + b"\n")
            current = [b"@@ @@" + match.group(1)]
        elif current is not None:
            if line.startswith(b"diff --git "):
                hunks.append(b"\n".join(current) + b"\n")
                current = None
            else:
                current.append(line)
    if current is not None:
        hunks.append(b"\n".join(current) + b"\n")
    return [_sha256(hunk) for hunk in hunks]


def _changed_paths(repo: Path, base: str, tree: str | None = None) -> list[str]:
    arguments = ["diff", "--name-only", "--no-renames", "-z"]
    if tree is None:
        arguments.extend(["--cached", base, "--"])
    else:
        arguments.extend([base, tree, "--"])
    return sorted(
        _safe_relative(_decode_path(item))
        for item in _split_z(_git(repo, *arguments).stdout)
    )


def _entries_for_diff(
    repo: Path, base: str, paths: Iterable[str], tree: str | None = None
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in paths:
        old = _tree_entry(repo, base, path)
        new = _stage_zero_entry(repo, path) if tree is None else _tree_entry(repo, tree, path)
        entries.append({"path": path, "old": old, "new": new})
    return entries


def _parse_requirement(requirement: str) -> tuple[str, str]:
    if ":" not in requirement:
        raise GuardError("required hunk must be PATH:TEXT")
    path, needle = requirement.split(":", 1)
    return _safe_relative(path), needle


def _check_constraints(
    repo: Path,
    base: str,
    paths: Sequence[str],
    forbid_prefixes: Sequence[str],
    required_hunks: Sequence[str],
    tree: str | None = None,
) -> dict[str, list[str]]:
    for prefix in forbid_prefixes:
        clean = _safe_relative(prefix.rstrip("/"))
        for path in paths:
            if path == clean or path.startswith(clean + "/"):
                raise GuardError(f"forbidden staged path: {path}")
    hunks: dict[str, list[str]] = {}
    diffs: dict[str, bytes] = {}
    for path in paths:
        diff = _text_diff(repo, base, path, tree)
        diffs[path] = diff
        hunks[path] = _normalised_hunks(diff)
    for requirement in required_hunks:
        path, needle = _parse_requirement(requirement)
        if path not in diffs:
            raise GuardError(f"required hunk path is not staged: {path}")
        changed_lines = [
            line[1:]
            for line in diffs[path].splitlines()
            if (line.startswith(b"+") and not line.startswith(b"+++"))
            or (line.startswith(b"-") and not line.startswith(b"---"))
        ]
        if not any(needle.encode() in line for line in changed_lines):
            raise GuardError(f"required hunk text is absent: {requirement}")
    return hunks


def _execution_identity(path: str) -> dict[str, Any]:
    absolute = Path(path).absolute()
    identity = _path_identity(absolute)
    if identity["type"] != "file":
        raise GuardError(f"tested-execution receipt is not a regular file: {absolute}")
    return {"path": os.fspath(absolute), "identity": identity}


def capture_stage_data(
    repo_path: str | Path,
    phase: str,
    forbid_prefixes: Sequence[str] = (),
    required_hunks: Sequence[str] = (),
    tested_execution: str | None = None,
) -> dict[str, Any]:
    repo = _repo(repo_path)
    if not phase.strip():
        raise GuardError("phase name must not be empty")
    unstaged = _git(repo, "diff", "--quiet", "--no-ext-diff", "--", check=False)
    if unstaged.returncode not in (0, 1):
        raise GuardError("could not inspect unstaged changes")
    if unstaged.returncode == 1:
        raise GuardError("unstaged tracked changes are not permitted")
    untracked = _git_paths(repo, "ls-files", "--others", "--exclude-standard")
    if untracked:
        raise GuardError(f"untracked paths are not permitted: {untracked[0]}")
    if any(entry["stage"] != "0" for entry in _index_entries(repo)):
        raise GuardError("unmerged index stages are not permitted")
    base = _head(repo)
    paths = _changed_paths(repo, base)
    if not paths:
        raise GuardError("stage is empty")
    tree = _git(repo, "write-tree").stdout.decode().strip()
    binary_diff = _diff_bytes(repo, base)
    hunks = _check_constraints(
        repo, base, paths, forbid_prefixes, required_hunks, tree=None
    )
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "stage-candidate",
        "phase": phase,
        "repo": os.fspath(repo),
        "common_dir": _common_dir(repo),
        "attached_ref": _symbolic_head(repo),
        "base": base,
        "index_tree": tree,
        "binary_diff": {"sha256": _sha256(binary_diff), "size": len(binary_diff)},
        "paths": paths,
        "entries": _entries_for_diff(repo, base, paths),
        "normalised_hunks": [
            {"path": path, "sha256": digest}
            for path in paths
            for digest in hunks[path]
        ],
        "forbid_prefixes": list(forbid_prefixes),
        "required_hunks": list(required_hunks),
        "tested_execution": (
            _execution_identity(tested_execution) if tested_execution else None
        ),
    }
    return result


def _candidate_from_manifest(value: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(value)
    if candidate.get("kind") == "stage-approved":
        candidate.pop("approval", None)
        candidate["kind"] = "stage-candidate"
    if candidate.get("schema") != SCHEMA or candidate.get("kind") != "stage-candidate":
        raise GuardError("not an Effort 510 stage manifest")
    return candidate


def _require_approval(value: dict[str, Any], approver: str = HUMAN_APPROVER) -> None:
    if value.get("schema") != SCHEMA or value.get("kind") != "stage-approved":
        raise GuardError("stage has not been human-approved")
    approval = value.get("approval")
    if not isinstance(approval, dict) or approval.get("approver") != approver:
        raise GuardError(f"stage approval is not from {approver}")
    candidate = _candidate_from_manifest(value)
    if approval.get("candidate_sha256") != _receipt_digest(candidate):
        raise GuardError("approved stage candidate identity changed")


def capture_stage(
    repo: str,
    phase: str,
    output: str,
    forbid_prefixes: Sequence[str],
    required_hunks: Sequence[str],
    tested_execution: str | None,
) -> None:
    value = capture_stage_data(
        repo, phase, forbid_prefixes, required_hunks, tested_execution
    )
    _atomic_json(Path(output), value)


def verify_stage(
    repo: str,
    manifest_path: str,
    human_approval: str | None,
    tested_execution: str | None,
) -> None:
    manifest = _load_json(manifest_path)
    if human_approval is not None:
        _require_approval(manifest, human_approval)
    candidate = _candidate_from_manifest(manifest)
    if candidate.get("repo") != os.fspath(_repo(repo)):
        raise GuardError("stage manifest belongs to another worktree")
    tested = candidate.get("tested_execution")
    if tested_execution is not None:
        expected = _execution_identity(tested_execution)
        if tested != expected:
            raise GuardError("tested-execution receipt identity changed")
    elif tested is not None:
        expected = _execution_identity(tested["path"])
        if tested != expected:
            raise GuardError("tested-execution receipt identity changed")
    fresh = capture_stage_data(
        repo,
        candidate["phase"],
        candidate.get("forbid_prefixes", []),
        candidate.get("required_hunks", []),
        tested["path"] if tested else None,
    )
    if fresh != candidate:
        raise GuardError("staged tree, path, mode, blob, or hunk identity changed")


def approve_stage(repo: str, candidate_path: str, approver: str, output: str) -> None:
    if approver != HUMAN_APPROVER:
        raise GuardError(f"approver must be {HUMAN_APPROVER}")
    verify_stage(repo, candidate_path, None, None)
    candidate = _candidate_from_manifest(_load_json(candidate_path))
    approved = copy.deepcopy(candidate)
    approved["kind"] = "stage-approved"
    approved["approval"] = {
        "approver": approver,
        "candidate_sha256": _receipt_digest(candidate),
    }
    _atomic_json(Path(output), approved)


def _tree_manifest(repo: Path, approved: dict[str, Any]) -> dict[str, Any]:
    base = approved["base"]
    tree = approved["index_tree"]
    for object_name in (f"{base}^{{commit}}", f"{tree}^{{tree}}"):
        if _git(repo, "cat-file", "-e", object_name, check=False).returncode:
            raise GuardError(f"approved Git object is unavailable: {object_name}")
    paths = _changed_paths(repo, base, tree)
    diff = _diff_bytes(repo, base, tree)
    hunks = _check_constraints(
        repo,
        base,
        paths,
        approved.get("forbid_prefixes", []),
        approved.get("required_hunks", []),
        tree,
    )
    return {
        "binary_diff": {"sha256": _sha256(diff), "size": len(diff)},
        "paths": paths,
        "entries": _entries_for_diff(repo, base, paths, tree),
        "normalised_hunks": [
            {"path": path, "sha256": digest}
            for path in paths
            for digest in hunks[path]
        ],
    }


def _validate_approved_objects(repo: Path, approved: dict[str, Any]) -> None:
    _require_approval(approved)
    if approved.get("common_dir") != _common_dir(repo):
        raise GuardError("approved stage belongs to a different Git repository")
    actual = _tree_manifest(repo, approved)
    for key in ("binary_diff", "paths", "entries", "normalised_hunks"):
        if actual[key] != approved.get(key):
            raise GuardError(f"approved {key} identity changed")


def _validate_prior_chain(
    canonical: Path,
    overlay: dict[str, Any],
    prior_paths: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    expected = overlay
    references: list[dict[str, str]] = []
    previous_digest: str | None = None
    for raw_path in prior_paths:
        path = Path(raw_path).absolute()
        receipt = _load_json(path)
        if receipt.get("schema") != SCHEMA or receipt.get("kind") != "materialization":
            raise GuardError(f"not a materialization receipt: {path}")
        if receipt.get("canonical") != os.fspath(canonical):
            raise GuardError("prior materialization belongs to another canonical checkout")
        if receipt.get("overlay_sha256") != _receipt_digest(overlay):
            raise GuardError("prior materialization uses another overlay")
        if receipt.get("previous_receipt_sha256") != previous_digest:
            raise GuardError("materialization receipts are not in their recorded order")
        if receipt.get("before_snapshot") != expected:
            raise GuardError("prior materialization does not continue the expected state")
        expected = receipt.get("after_snapshot")
        if not isinstance(expected, dict):
            raise GuardError("prior materialization has no after snapshot")
        digest = _receipt_digest(receipt)
        references.append({"path": os.fspath(path), "sha256": digest})
        previous_digest = digest
    return expected, references


def _assert_current_snapshot(canonical: Path, expected: dict[str, Any]) -> None:
    actual = capture_overlay_data(canonical)
    if actual != expected:
        raise GuardError("canonical checkout differs from the receipted expected state")


def _git_mode_kind(mode: str | None) -> str:
    if mode is None:
        return "missing"
    if mode in ("100644", "100755"):
        return "file"
    if mode == "120000":
        return "symlink"
    raise GuardError(f"unsupported approved Git mode: {mode}")


def _blob(repo: Path, entry: dict[str, str] | None) -> bytes | None:
    if entry is None:
        return None
    if entry.get("type") != "blob":
        raise GuardError("only blob-backed paths can be materialized")
    return _git(repo, "cat-file", "blob", entry["oid"]).stdout


def _read_state(path: Path) -> dict[str, Any]:
    identity = _path_identity(path)
    kind = identity["type"]
    if kind == "missing":
        return {"type": "missing"}
    if kind == "file":
        return {
            "type": "file",
            "mode": identity["mode"],
            "content_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }
    if kind == "symlink":
        return {
            "type": "symlink",
            "mode": identity["mode"],
            "target": os.readlink(path),
        }
    raise GuardError(f"materialization target has unsupported type: {path}")


def _state_bytes(state: dict[str, Any]) -> bytes | None:
    if state["type"] == "missing":
        return None
    if state["type"] == "file":
        return base64.b64decode(state["content_b64"], validate=True)
    if state["type"] == "symlink":
        return os.fsencode(state["target"])
    raise GuardError("invalid materialization state")


def _state_git_mode(state: dict[str, Any]) -> str | None:
    if state["type"] == "missing":
        return None
    if state["type"] == "symlink":
        return "120000"
    if state["type"] == "file":
        mode = int(state["mode"], 8)
        return "100755" if mode & 0o111 else "100644"
    raise GuardError("invalid materialization state")


def _merged_file(current: bytes, base: bytes, target: bytes) -> bytes:
    if current == base:
        return target
    if current == target or target == base:
        return current
    if b"\0" in current or b"\0" in base or b"\0" in target:
        raise GuardError("binary same-file materialization conflict")
    with tempfile.TemporaryDirectory(prefix="effort510-merge-") as temporary:
        root = Path(temporary)
        current_path = root / "current"
        base_path = root / "base"
        target_path = root / "approved"
        current_path.write_bytes(current)
        base_path.write_bytes(base)
        target_path.write_bytes(target)
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                os.fspath(current_path),
                os.fspath(base_path),
                os.fspath(target_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            raise GuardError("same-file three-way materialization conflict")
        return result.stdout


def _planned_state(
    current: dict[str, Any],
    old_entry: dict[str, str] | None,
    new_entry: dict[str, str] | None,
    old_bytes: bytes | None,
    new_bytes: bytes | None,
) -> dict[str, Any]:
    old_kind = _git_mode_kind(old_entry["mode"] if old_entry else None)
    new_kind = _git_mode_kind(new_entry["mode"] if new_entry else None)
    current_kind = current["type"]
    current_bytes = _state_bytes(current)
    current_git_mode = _state_git_mode(current)
    old_mode = old_entry["mode"] if old_entry else None
    new_mode = new_entry["mode"] if new_entry else None

    if current_kind == new_kind and current_bytes == new_bytes and current_git_mode == new_mode:
        return copy.deepcopy(current)
    if old_kind == "missing":
        if current_kind != "missing":
            raise GuardError("approved addition collides with an existing overlay path")
        result_bytes = new_bytes
    elif new_kind == "missing":
        if current_kind != old_kind or current_bytes != old_bytes:
            raise GuardError("approved deletion conflicts with the user overlay")
        return {"type": "missing"}
    elif old_kind == new_kind == current_kind == "file":
        assert current_bytes is not None and old_bytes is not None and new_bytes is not None
        result_bytes = _merged_file(current_bytes, old_bytes, new_bytes)
    else:
        if current_kind != old_kind or current_bytes != old_bytes:
            raise GuardError("approved type change conflicts with the user overlay")
        result_bytes = new_bytes

    if new_kind == "file":
        if current_kind == "file":
            actual_mode = int(current["mode"], 8)
        else:
            actual_mode = 0o644
        if old_mode != new_mode:
            if current_git_mode not in (old_mode, new_mode):
                raise GuardError("approved mode change conflicts with the user overlay")
            if new_mode == "100755":
                actual_mode |= 0o111
            else:
                actual_mode &= ~0o111
        return {
            "type": "file",
            "mode": format(actual_mode, "04o"),
            "content_b64": base64.b64encode(result_bytes or b"").decode("ascii"),
        }
    if new_kind == "symlink":
        return {
            "type": "symlink",
            "mode": "0777",
            "target": os.fsdecode(result_bytes or b""),
        }
    raise GuardError("invalid planned materialization state")


def _safe_target(repo: Path, relative: str) -> Path:
    relative = _safe_relative(relative)
    target = repo / relative
    cursor = repo
    for part in PurePosixPath(relative).parts[:-1]:
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise GuardError(f"materialization parent is not a real directory: {cursor}")
    return target


def _write_state(path: Path, state: dict[str, Any], created_dirs: list[str], root: Path) -> None:
    missing: list[Path] = []
    cursor = path.parent
    while cursor != root and not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o755)
        created_dirs.append(directory.relative_to(root).as_posix())
    kind = state["type"]
    if kind == "missing":
        try:
            if path.is_dir() and not path.is_symlink():
                raise GuardError(f"refusing to unlink directory: {path}")
            path.unlink()
        except FileNotFoundError:
            pass
        return
    temporary = path.parent / f".{path.name}.effort510-{os.getpid()}"
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    if kind == "file":
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, int(state["mode"], 8))
        try:
            content = base64.b64decode(state["content_b64"], validate=True)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, int(state["mode"], 8), follow_symlinks=False)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
    elif kind == "symlink":
        os.symlink(state["target"], temporary)
    else:
        raise GuardError("invalid materialization write state")
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def materialize(
    canonical_path: str,
    approved_path: str,
    overlay_path: str,
    prior_paths: Sequence[str],
    receipt_path: str,
) -> None:
    canonical = _repo(canonical_path)
    overlay = _load_json(overlay_path)
    _require_overlay(overlay, canonical)
    expected, prior_references = _validate_prior_chain(canonical, overlay, prior_paths)
    _assert_current_snapshot(canonical, expected)
    approved = _load_json(approved_path)
    _validate_approved_objects(canonical, approved)
    if any(PurePosixPath(path).name.startswith(".env") for path in approved["paths"]):
        raise GuardError("materialization may not target .env files")
    if prior_paths:
        expected_base_tree = _load_json(prior_paths[-1])["approved_tree"]
        actual_base_tree = _git(
            canonical, "rev-parse", f"{approved['base']}^{{tree}}"
        ).stdout.decode().strip()
        if actual_base_tree != expected_base_tree:
            raise GuardError("approved stage does not continue the materialization chain")
    elif approved["base"] != overlay["head"]:
        raise GuardError("approved stage does not continue the materialization chain")

    operations: list[dict[str, Any]] = []
    for entry in approved["entries"]:
        target = _safe_target(canonical, entry["path"])
        before = _read_state(target)
        after = _planned_state(
            before,
            entry["old"],
            entry["new"],
            _blob(canonical, entry["old"]),
            _blob(canonical, entry["new"]),
        )
        operations.append({"path": entry["path"], "before": before, "after": after})

    _assert_current_snapshot(canonical, expected)
    created_dirs: list[str] = []
    applied: list[dict[str, Any]] = []
    try:
        for operation in operations:
            target = _safe_target(canonical, operation["path"])
            if _read_state(target) != operation["before"]:
                raise GuardError(f"materialization race at {operation['path']}")
            _write_state(target, operation["after"], created_dirs, canonical)
            applied.append(operation)
    except BaseException:
        for operation in reversed(applied):
            _write_state(
                _safe_target(canonical, operation["path"]),
                operation["before"],
                [],
                canonical,
            )
        for relative in reversed(created_dirs):
            try:
                (canonical / relative).rmdir()
            except OSError:
                pass
        raise

    after_snapshot = capture_overlay_data(canonical)
    receipt = {
        "schema": SCHEMA,
        "kind": "materialization",
        "canonical": os.fspath(canonical),
        "overlay_path": os.fspath(Path(overlay_path).absolute()),
        "overlay_sha256": _receipt_digest(overlay),
        "approved_path": os.fspath(Path(approved_path).absolute()),
        "approved_sha256": _receipt_digest(approved),
        "approved_base": approved["base"],
        "approved_tree": approved["index_tree"],
        "prior_receipts": prior_references,
        "previous_receipt_sha256": (
            prior_references[-1]["sha256"] if prior_references else None
        ),
        "before_snapshot": expected,
        "after_snapshot": after_snapshot,
        "operations": operations,
        "created_dirs": created_dirs,
    }
    try:
        _atomic_json(Path(receipt_path), receipt)
    except BaseException:
        for operation in reversed(operations):
            _write_state(
                _safe_target(canonical, operation["path"]),
                operation["before"],
                [],
                canonical,
            )
        for relative in reversed(created_dirs):
            try:
                (canonical / relative).rmdir()
            except OSError:
                pass
        raise


def verify_installed(
    canonical_path: str,
    approved_path: str,
    overlay_path: str,
    prior_paths: Sequence[str],
    receipt_path: str,
) -> None:
    canonical = _repo(canonical_path)
    overlay = _load_json(overlay_path)
    _require_overlay(overlay, canonical)
    expected_before, prior_references = _validate_prior_chain(
        canonical, overlay, prior_paths
    )
    approved = _load_json(approved_path)
    _validate_approved_objects(canonical, approved)
    receipt = _load_json(receipt_path)
    if receipt.get("schema") != SCHEMA or receipt.get("kind") != "materialization":
        raise GuardError("not a materialization receipt")
    checks = {
        "canonical": os.fspath(canonical),
        "overlay_sha256": _receipt_digest(overlay),
        "approved_sha256": _receipt_digest(approved),
        "approved_base": approved["base"],
        "approved_tree": approved["index_tree"],
        "prior_receipts": prior_references,
        "before_snapshot": expected_before,
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise GuardError(f"materialization receipt {key} mismatch")
    _assert_current_snapshot(canonical, receipt["after_snapshot"])
    for operation in receipt.get("operations", []):
        target = _safe_target(canonical, operation["path"])
        if _read_state(target) != operation["after"]:
            raise GuardError(f"installed identity changed: {operation['path']}")


def rollback_materialization(
    canonical_path: str, receipt_path: str, overlay_path: str
) -> None:
    canonical = _repo(canonical_path)
    overlay = _load_json(overlay_path)
    _require_overlay(overlay, canonical)
    receipt = _load_json(receipt_path)
    if receipt.get("schema") != SCHEMA or receipt.get("kind") != "materialization":
        raise GuardError("not a materialization receipt")
    if receipt.get("canonical") != os.fspath(canonical):
        raise GuardError("materialization receipt belongs to another checkout")
    if receipt.get("overlay_sha256") != _receipt_digest(overlay):
        raise GuardError("materialization receipt uses another overlay")
    _assert_current_snapshot(canonical, receipt["after_snapshot"])
    for operation in receipt.get("operations", []):
        if _read_state(_safe_target(canonical, operation["path"])) != operation["after"]:
            raise GuardError(f"rollback after-state mismatch: {operation['path']}")
    for operation in reversed(receipt.get("operations", [])):
        _write_state(
            _safe_target(canonical, operation["path"]),
            operation["before"],
            [],
            canonical,
        )
    for relative in reversed(receipt.get("created_dirs", [])):
        try:
            (canonical / _safe_relative(relative)).rmdir()
        except OSError as exc:
            raise GuardError(f"could not remove materialized directory: {relative}") from exc
    _assert_current_snapshot(canonical, receipt["before_snapshot"])


def _commit_receipt_identity(path: str) -> tuple[dict[str, Any], dict[str, str]]:
    absolute = Path(path).absolute()
    value = _load_json(absolute)
    if value.get("schema") != SCHEMA or value.get("kind") != "public-commit":
        raise GuardError("prior commit receipt is invalid")
    return value, {"path": os.fspath(absolute), "sha256": _receipt_digest(value)}


def record_public_commit(
    repo_path: str,
    canonical_path: str,
    ref: str,
    approved_path: str,
    overlay_path: str,
    approver: str,
    output: str,
    prior_commit_path: str | None,
    prior_materialization_paths: Sequence[str],
) -> None:
    if approver != HUMAN_APPROVER:
        raise GuardError(f"approver must be {HUMAN_APPROVER}")
    repo = _repo(repo_path)
    canonical = _repo(canonical_path)
    if ref != PUBLIC_REF:
        raise GuardError(f"public ref must be {PUBLIC_REF}")
    if _common_dir(repo) != _common_dir(canonical):
        raise GuardError("release worktree and canonical checkout do not share Git metadata")
    if _symbolic_head(repo) != ref:
        raise GuardError("release worktree is detached or attached to the wrong ref")
    commit = _head(repo)
    resolved_ref = _git(canonical, "rev-parse", ref).stdout.decode().strip()
    if resolved_ref != commit:
        raise GuardError("release ref is not the committed worktree HEAD")
    approved = _load_json(approved_path)
    _validate_approved_objects(canonical, approved)
    tree = _git(canonical, "rev-parse", f"{commit}^{{tree}}").stdout.decode().strip()
    if tree != approved["index_tree"]:
        raise GuardError("commit tree is not the approved index tree")
    parents = _git(canonical, "show", "-s", "--format=%P", commit).stdout.decode().split()
    if parents != [approved["base"]]:
        raise GuardError("public commit does not have the exact approved base")

    prior_commit_reference = None
    if prior_commit_path:
        prior_commit, prior_commit_reference = _commit_receipt_identity(prior_commit_path)
        if prior_commit.get("canonical") != os.fspath(canonical):
            raise GuardError("prior commit receipt belongs to another canonical checkout")
        if prior_commit.get("ref") != ref or prior_commit.get("commit") != approved["base"]:
            raise GuardError("public commit does not continue the prior commit receipt")
        if _git(
            canonical,
            "merge-base",
            "--is-ancestor",
            prior_commit["commit"],
            ref,
            check=False,
        ).returncode:
            raise GuardError("prior public commit is no longer reachable from the release ref")
    elif approved["base"] != _head(canonical):
        raise GuardError("initial public commit base is not canonical HEAD")

    overlay = _load_json(overlay_path)
    _require_overlay(overlay, canonical)
    expected, prior_materializations = _validate_prior_chain(
        canonical, overlay, prior_materialization_paths
    )
    _assert_current_snapshot(canonical, expected)
    receipt = {
        "schema": SCHEMA,
        "kind": "public-commit",
        "repo": os.fspath(repo),
        "canonical": os.fspath(canonical),
        "common_dir": _common_dir(canonical),
        "ref": ref,
        "approver": approver,
        "approved_path": os.fspath(Path(approved_path).absolute()),
        "approved_sha256": _receipt_digest(approved),
        "overlay_path": os.fspath(Path(overlay_path).absolute()),
        "overlay_sha256": _receipt_digest(overlay),
        "prior_commit": prior_commit_reference,
        "prior_materializations": prior_materializations,
        "base": approved["base"],
        "commit": commit,
        "tree": tree,
        "canonical_snapshot_at_record": expected,
    }
    _atomic_json(Path(output), receipt)


def verify_public_commit(
    repo_path: str,
    canonical_path: str,
    ref: str,
    receipt_path: str,
    overlay_path: str,
    required_approver: str,
    prior_materialization_paths: Sequence[str],
) -> None:
    canonical = _repo(canonical_path)
    receipt = _load_json(receipt_path)
    if receipt.get("schema") != SCHEMA or receipt.get("kind") != "public-commit":
        raise GuardError("not a public commit receipt")
    if required_approver != HUMAN_APPROVER or receipt.get("approver") != required_approver:
        raise GuardError(f"public commit was not approved by {HUMAN_APPROVER}")
    if ref != PUBLIC_REF or receipt.get("ref") != ref:
        raise GuardError("public commit receipt has the wrong release ref")
    if receipt.get("canonical") != os.fspath(canonical):
        raise GuardError("public commit receipt belongs to another canonical checkout")
    if receipt.get("common_dir") != _common_dir(canonical):
        raise GuardError("public commit common Git directory changed")
    repo = Path(repo_path).absolute()
    if repo.exists():
        checked_repo = _repo(repo)
        if _common_dir(checked_repo) != _common_dir(canonical):
            raise GuardError("release worktree now belongs to another repository")
        if _head(checked_repo) == receipt["commit"] and _symbolic_head(checked_repo) != ref:
            raise GuardError("release worktree is detached or attached to the wrong ref")
    resolved_ref = _git(canonical, "rev-parse", ref, check=False)
    if resolved_ref.returncode or resolved_ref.stdout.decode().strip() != receipt.get("commit"):
        raise GuardError("release ref moved away from the receipted commit")
    if _git(
        canonical,
        "merge-base",
        "--is-ancestor",
        receipt["commit"],
        ref,
        check=False,
    ).returncode:
        raise GuardError("receipted commit is not reachable from the release ref")
    tree = _git(canonical, "rev-parse", f"{receipt['commit']}^{{tree}}").stdout.decode().strip()
    if tree != receipt.get("tree"):
        raise GuardError("receipted commit tree changed")
    approved = _load_json(receipt["approved_path"])
    _validate_approved_objects(canonical, approved)
    if _receipt_digest(approved) != receipt.get("approved_sha256"):
        raise GuardError("approved receipt identity changed")
    if tree != approved["index_tree"] or receipt.get("base") != approved["base"]:
        raise GuardError("public commit no longer matches the approved base/tree")
    parents = _git(
        canonical, "show", "-s", "--format=%P", receipt["commit"]
    ).stdout.decode().split()
    if parents != [approved["base"]]:
        raise GuardError("public commit parent changed")
    overlay = _load_json(overlay_path)
    _require_overlay(overlay, canonical)
    if _receipt_digest(overlay) != receipt.get("overlay_sha256"):
        raise GuardError("overlay receipt identity changed")
    expected, prior_materializations = _validate_prior_chain(
        canonical, overlay, prior_materialization_paths
    )
    if receipt.get("prior_materializations") != prior_materializations:
        raise GuardError("public commit prior materializations changed")
    if receipt.get("canonical_snapshot_at_record") != expected:
        raise GuardError("public commit did not record the expected canonical overlay")


def _normalise_excludes(excludes: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    for item in [*DEFAULT_READONLY_EXCLUDES, *excludes]:
        clean = _safe_relative(item.rstrip("/"))
        if clean not in values:
            values.append(clean)
    return tuple(values)


def _excluded(relative: str, excludes: Sequence[str]) -> bool:
    return any(relative == item or relative.startswith(item + "/") for item in excludes)


def readonly_inventory(repo_path: str | Path, excludes: Sequence[str]) -> dict[str, Any]:
    repo = Path(repo_path).absolute()
    if not repo.is_dir():
        raise GuardError(f"read-only root does not exist: {repo}")
    normalised = _normalise_excludes(excludes)
    result: dict[str, Any] = {}

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise GuardError(f"could not inventory {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(repo).as_posix()
            if _excluded(relative, normalised):
                continue
            identity = _path_identity(path)
            result[relative] = identity
            if identity["type"] == "directory":
                visit(path)

    visit(repo)
    return {
        "root": os.fspath(repo),
        "excludes": list(normalised),
        "paths": result,
    }


def run_readonly(
    repo_path: str,
    cwd_path: str | None,
    excludes: Sequence[str],
    command: Sequence[str],
) -> int:
    repo = Path(repo_path).absolute()
    cwd = Path(cwd_path).absolute() if cwd_path else repo
    if not command:
        raise GuardError("run-readonly requires a child command")
    if not cwd.is_dir():
        raise GuardError(f"child cwd does not exist: {cwd}")
    before = readonly_inventory(repo, excludes)
    result = subprocess.run(list(command), cwd=cwd, check=False)
    after = readonly_inventory(repo, excludes)
    if after != before:
        before_paths = before["paths"]
        after_paths = after["paths"]
        changed = sorted(
            path
            for path in set(before_paths) | set(after_paths)
            if before_paths.get(path) != after_paths.get(path)
        )
        first = changed[0] if changed else "<inventory metadata>"
        raise GuardError(f"child command mutated read-only root at {first}")
    if result.returncode:
        raise GuardError(f"child command exited {result.returncode}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    capture = subparsers.add_parser("capture-overlay")
    capture.add_argument("--canonical", required=True)
    capture.add_argument("--out", required=True)

    verify = subparsers.add_parser("verify-overlay")
    verify.add_argument("--canonical", required=True)
    verify.add_argument("--receipt", required=True)

    stage = subparsers.add_parser("capture-stage")
    stage.add_argument("--repo", required=True)
    stage.add_argument("--phase", required=True)
    stage.add_argument("--out", required=True)
    stage.add_argument("--forbid-prefix", action="append", default=[])
    stage.add_argument("--require-hunk", action="append", default=[])
    stage.add_argument("--tested-execution")

    stage_verify = subparsers.add_parser("verify-stage")
    stage_verify.add_argument("--repo", required=True)
    stage_verify.add_argument("--manifest", required=True)
    stage_verify.add_argument("--require-human-approval")
    stage_verify.add_argument("--require-tested-execution")

    approve = subparsers.add_parser("approve-stage")
    approve.add_argument("--repo", required=True)
    approve.add_argument("--candidate", required=True)
    approve.add_argument("--approver", required=True)
    approve.add_argument("--out", required=True)

    materialise = subparsers.add_parser("materialize")
    materialise.add_argument("--canonical", required=True)
    materialise.add_argument("--approved", required=True)
    materialise.add_argument("--overlay", required=True)
    materialise.add_argument("--prior", action="append", default=[])
    materialise.add_argument("--receipt", required=True)

    installed = subparsers.add_parser("verify-installed")
    installed.add_argument("--canonical", required=True)
    installed.add_argument("--approved", required=True)
    installed.add_argument("--overlay", required=True)
    installed.add_argument("--prior", action="append", default=[])
    installed.add_argument("--receipt", required=True)

    rollback = subparsers.add_parser("rollback-materialization")
    rollback.add_argument("--canonical", required=True)
    rollback.add_argument("--receipt", required=True)
    rollback.add_argument("--overlay", required=True)

    record = subparsers.add_parser("record-public-commit")
    record.add_argument("--repo", required=True)
    record.add_argument("--canonical", required=True)
    record.add_argument("--ref", required=True)
    record.add_argument("--approved", required=True)
    record.add_argument("--prior-commit")
    record.add_argument("--overlay", required=True)
    record.add_argument("--prior-materialization", action="append", default=[])
    record.add_argument("--approver", required=True)
    record.add_argument("--out", required=True)

    commit_verify = subparsers.add_parser("verify-public-commit")
    commit_verify.add_argument("--repo", required=True)
    commit_verify.add_argument("--canonical", required=True)
    commit_verify.add_argument("--ref", required=True)
    commit_verify.add_argument("--receipt", required=True)
    commit_verify.add_argument("--overlay", required=True)
    commit_verify.add_argument("--prior-materialization", action="append", default=[])
    commit_verify.add_argument("--require-human-approval", required=True)

    readonly = subparsers.add_parser("run-readonly")
    readonly.add_argument("--repo", required=True)
    readonly.add_argument("--cwd")
    readonly.add_argument("--exclude", action="append", default=[])
    readonly.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(arguments)
    try:
        if args.operation == "capture-overlay":
            capture_overlay(args.canonical, args.out)
        elif args.operation == "verify-overlay":
            verify_overlay(args.canonical, args.receipt)
        elif args.operation == "capture-stage":
            capture_stage(
                args.repo,
                args.phase,
                args.out,
                args.forbid_prefix,
                args.require_hunk,
                args.tested_execution,
            )
        elif args.operation == "verify-stage":
            verify_stage(
                args.repo,
                args.manifest,
                args.require_human_approval,
                args.require_tested_execution,
            )
        elif args.operation == "approve-stage":
            approve_stage(args.repo, args.candidate, args.approver, args.out)
        elif args.operation == "materialize":
            materialize(
                args.canonical,
                args.approved,
                args.overlay,
                args.prior,
                args.receipt,
            )
        elif args.operation == "verify-installed":
            verify_installed(
                args.canonical,
                args.approved,
                args.overlay,
                args.prior,
                args.receipt,
            )
        elif args.operation == "rollback-materialization":
            rollback_materialization(args.canonical, args.receipt, args.overlay)
        elif args.operation == "record-public-commit":
            record_public_commit(
                args.repo,
                args.canonical,
                args.ref,
                args.approved,
                args.overlay,
                args.approver,
                args.out,
                args.prior_commit,
                args.prior_materialization,
            )
        elif args.operation == "verify-public-commit":
            verify_public_commit(
                args.repo,
                args.canonical,
                args.ref,
                args.receipt,
                args.overlay,
                args.require_human_approval,
                args.prior_materialization,
            )
        elif args.operation == "run-readonly":
            command = args.command
            if command and command[0] == "--":
                command = command[1:]
            return run_readonly(args.repo, args.cwd, args.exclude, command)
        else:  # pragma: no cover - argparse owns this branch
            parser.error("unknown operation")
    except GuardError as exc:
        print(f"release-guard: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
