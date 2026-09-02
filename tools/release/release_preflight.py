#!/usr/bin/env python3
"""Combined read-only execution preflight for a prepared release.

Phase 1 installs and tests this helper before the DAIC and daic-core runners exist.  Phase 4
uses it once those roots are ready.  The helper writes only its external receipt and a
short-lived namespace export; every repository and dependency inventory must compare equal.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

try:
    from tools.release import release_guard
except ModuleNotFoundError:
    # Direct execution puts tools/release rather than the repository root at
    # sys.path[0], so fall back to the adjacent module.
    import release_guard


SCHEMA = "effort510-preflight-v3"
CLOSURE_SCHEMA = "daic-stage-namespace-v3"
LIVE_SOURCE_SCHEMA = "daic-live-source-isolation-v3"
PROBE_SCHEMA = "daic-namespace-probe-v3"
PUBLIC_REF = "refs/heads/release/effort-510-v16"
OVERLAY_NAMES = {
    "assist": "assist-user-overlay-v2.json",
    "daic": "daic-user-overlay-v2.json",
    "core": "daic-core-user-overlay-v2.json",
}
LIVE_SOURCE_NAME = "daic-live-sources.before.json"
FIXED_TOOLS = (
    os.fspath(Path.home() / ".local" / "bin" / "uv"),
    "/usr/bin/python3.12",
    "/usr/bin/bash",
    "/usr/bin/bwrap",
    "/usr/bin/strace",
)


class PreflightError(RuntimeError):
    """A preflight invariant did not hold."""


def _absolute(path: str | Path, label: str, *, must_exist: bool = True) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise PreflightError(f"{label} must be an absolute path")
    value = value.absolute()
    if must_exist and not value.exists() and not value.is_symlink():
        raise PreflightError(f"{label} does not exist: {value}")
    return value


def _load(path: str | Path) -> dict[str, Any]:
    absolute = _absolute(path, "receipt")
    try:
        value = json.loads(absolute.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"invalid receipt {absolute}: {exc}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"receipt is not an object: {absolute}")
    return value


def _digest(value: Any) -> str:
    return release_guard._receipt_digest(value)


def _identity(path: str | Path) -> dict[str, Any]:
    return release_guard._path_identity(Path(path))


def executable_identity(path: str | Path, label: str) -> dict[str, Any]:
    absolute = _absolute(path, label)
    node = _identity(absolute)
    if node["type"] not in ("file", "symlink") or not os.access(absolute, os.X_OK):
        raise PreflightError(f"{label} is not executable: {absolute}")
    resolved = absolute.resolve(strict=True)
    resolved_identity = _identity(resolved)
    if resolved_identity["type"] != "file":
        raise PreflightError(f"{label} does not resolve to a regular file")
    return {
        "path": os.fspath(absolute),
        "node": node,
        "resolved_path": os.fspath(resolved),
        "resolved_identity": resolved_identity,
    }


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
    )
    if check and result.returncode:
        raise PreflightError(
            f"git {' '.join(arguments)} failed: "
            + result.stderr.decode("utf-8", "replace").strip()
        )
    return result


def repo_identity(root_path: str | Path, required_ref: str | None) -> dict[str, Any]:
    root = _absolute(root_path, "worktree")
    top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.decode().strip()).absolute()
    if top != root:
        raise PreflightError(f"worktree is not its exact Git top level: {root}")
    common = Path(
        _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .stdout.decode()
        .strip()
    ).absolute()
    head = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    symbolic = _git(root, "symbolic-ref", "-q", "HEAD", check=False)
    if symbolic.returncode == 0:
        attached_ref: str | None = symbolic.stdout.decode().strip()
    elif symbolic.returncode == 1:
        attached_ref = None
    else:
        raise PreflightError(f"could not inspect worktree HEAD: {root}")
    if required_ref is not None and attached_ref != required_ref:
        raise PreflightError(f"public worktree is detached or attached to the wrong ref: {root}")
    if required_ref is not None:
        resolved = _git(root, "rev-parse", required_ref).stdout.decode().strip()
        if resolved != head:
            raise PreflightError(f"public ref is not the worktree HEAD: {root}")
    return {
        "root": os.fspath(root),
        "common_dir": os.fspath(common),
        "head": head,
        "attached_ref": attached_ref,
    }


def _under(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).absolute().relative_to(Path(root).absolute())
        return True
    except ValueError:
        return False


def _overlay_reference(path: Path, root_identity: dict[str, Any]) -> dict[str, Any]:
    value = _load(path)
    if value.get("schema") != release_guard.SCHEMA or value.get("kind") != "overlay":
        raise PreflightError(f"not an Effort 510 overlay: {path}")
    if value.get("common_dir") != root_identity["common_dir"]:
        raise PreflightError(f"overlay common directory does not match worktree: {path}")
    if value.get("head") != root_identity["head"]:
        raise PreflightError(f"overlay HEAD does not match worktree: {path}")
    try:
        release_guard.verify_overlay(value["canonical"], os.fspath(path))
    except release_guard.GuardError as exc:
        raise PreflightError(f"overlay verification failed: {path}: {exc}") from exc
    return {
        "path": os.fspath(path.absolute()),
        "sha256": _digest(value),
        "canonical": value["canonical"],
        "head": value["head"],
        "common_dir": value["common_dir"],
    }


def _topology_identity(path: Path) -> dict[str, Any]:
    identity = _identity(path)
    return {key: identity[key] for key in ("type", "mode") if key in identity}


def _map_internal_target(
    stage_root: Path, canonical_root: Path, link_path: str, link_text: str
) -> Path:
    raw = Path(link_text)
    if raw.is_absolute():
        try:
            relative = raw.relative_to(canonical_root)
        except ValueError as exc:
            raise PreflightError(f"internal link escapes canonical root: {link_path}") from exc
        return (stage_root / relative).absolute()
    candidate = (stage_root / PurePosixPath(link_path).parent / raw).absolute()
    if not _under(candidate, stage_root):
        raise PreflightError(f"relative internal link escapes staged root: {link_path}")
    return candidate


def _validate_namespace_policy(closure: Mapping[str, Any]) -> None:
    expected = {
        "canonical_overlay": "required",
        "network": "disabled",
        "home": "private",
        "xdg": "private",
        "state": "private",
        "tmp": "private",
        "imports": "staged-only",
        "file_trace": "required",
        "direct_execution": "forbidden",
        "ancestry": "exact-runner",
    }
    if closure.get("namespace_policy") != expected:
        raise PreflightError("DAIC namespace/XDG/state/import/ancestry policy is incomplete")


def validate_closure(
    closure_path: str | Path,
    stage_root_path: str | Path,
    runner_path: str | Path,
    required_states: Mapping[str, int],
    required_mask_path: str | Path,
    daic_overlay_reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    closure_file = _absolute(closure_path, "DAIC closure receipt")
    closure = _load(closure_file)
    if closure.get("schema") != CLOSURE_SCHEMA:
        raise PreflightError("wrong DAIC closure schema")
    stage_root = _absolute(stage_root_path, "DAIC worktree")
    runner = _absolute(runner_path, "DAIC runner")
    if closure.get("stage_root") != os.fspath(stage_root):
        raise PreflightError("DAIC closure belongs to another staged root")
    canonical_root = _absolute(closure.get("canonical_root", ""), "DAIC canonical root")
    if closure.get("common_dir") != repo_identity(stage_root, None)["common_dir"]:
        raise PreflightError("DAIC closure common directory changed")
    recorded_runner = closure.get("runner")
    actual_runner = executable_identity(runner, "DAIC runner")
    if recorded_runner != actual_runner:
        raise PreflightError("DAIC runner identity changed")
    if daic_overlay_reference is not None:
        expected_overlay = {
            "path": daic_overlay_reference["path"],
            "sha256": daic_overlay_reference["sha256"],
        }
        if closure.get("overlay") != expected_overlay:
            raise PreflightError("DAIC closure is not bound to the canonical overlay")
        if daic_overlay_reference["canonical"] != os.fspath(canonical_root):
            raise PreflightError("DAIC closure canonical root differs from overlay")
    _validate_namespace_policy(closure)

    probe_imports = closure.get("probe_imports")
    if not isinstance(probe_imports, list) or not probe_imports:
        raise PreflightError("DAIC closure has no transitive import probe set")
    for relative in probe_imports:
        if not isinstance(relative, str):
            raise PreflightError("DAIC closure has an invalid import probe path")
        safe = release_guard._safe_relative(relative)
        if not (stage_root / safe).is_file():
            raise PreflightError(f"DAIC import probe is missing: {safe}")

    links = closure.get("links")
    if not isinstance(links, list):
        raise PreflightError("DAIC closure has no link graph")
    counts: dict[str, int] = {}
    link_snapshot: list[dict[str, Any]] = []
    external_targets: list[str] = []
    external_links: list[str] = []
    for entry in links:
        if not isinstance(entry, dict):
            raise PreflightError("invalid DAIC link entry")
        relative = release_guard._safe_relative(entry.get("path", ""))
        node = stage_root / relative
        identity = _identity(node)
        if identity.get("type") != "symlink" or entry.get("mode") != "120000":
            raise PreflightError(f"DAIC link node identity changed: {relative}")
        link_text = os.readlink(node)
        if entry.get("link_text") != link_text:
            raise PreflightError(f"DAIC link text changed: {relative}")
        state = entry.get("state")
        counts[state] = counts.get(state, 0) + 1
        snapshot_entry = {
            "path": relative,
            "mode": identity["mode"],
            "link_text": link_text,
            "state": state,
        }
        if state == "internal-resolved":
            mapped = _map_internal_target(stage_root, canonical_root, relative, link_text)
            if entry.get("staged_target") != os.fspath(mapped):
                raise PreflightError(f"DAIC staged link mapping changed: {relative}")
            actual_target = _topology_identity(mapped)
            if actual_target.get("type") in (None, "missing"):
                raise PreflightError(f"DAIC staged link target disappeared: {relative}")
            if entry.get("resolved_identity") != actual_target:
                raise PreflightError(f"DAIC staged link target identity changed: {relative}")
            snapshot_entry["staged_target"] = os.fspath(mapped)
            snapshot_entry["resolved_identity"] = actual_target
        elif state == "external-dangling":
            target = Path(link_text)
            if not target.is_absolute() or target.exists() or target.is_symlink():
                raise PreflightError(f"external dangling target changed state: {relative}")
            if entry.get("external_target") != os.fspath(target):
                raise PreflightError(f"external target identity changed: {relative}")
            forbidden_keys = {"staged_target", "resolved_identity", "target_identity"}
            if forbidden_keys.intersection(entry):
                raise PreflightError("dangling link receipt invents a target identity")
            external_targets.append(os.fspath(target))
            external_links.append(relative)
            snapshot_entry["external_target"] = os.fspath(target)
            snapshot_entry["follow_state"] = "absent"
        else:
            raise PreflightError(f"unknown DAIC link state: {state}")
        link_snapshot.append(snapshot_entry)
    if counts != dict(required_states):
        raise PreflightError(
            f"DAIC link-state partition mismatch: expected {dict(required_states)}, got {counts}"
        )

    mask_path = _absolute(required_mask_path, "DAIC dangling-target mask")
    mask = closure.get("mask")
    if not isinstance(mask, dict) or mask.get("path") != os.fspath(mask_path):
        raise PreflightError("DAIC dangling-target mask path changed")
    mask_identity = _identity(mask_path)
    if (
        mask_identity.get("type") != "directory"
        or mask_identity.get("mode") != "0555"
        or list(mask_path.iterdir())
    ):
        raise PreflightError("DAIC dangling-target mask is not an empty 0555 directory")
    if mask.get("identity") != mask_identity:
        raise PreflightError("DAIC dangling-target mask identity changed")
    mount_parent = _absolute(mask.get("mount_parent", ""), "DAIC mask mount parent")
    if not all(_under(target, mount_parent) for target in external_targets):
        raise PreflightError("DAIC mask does not cover every external dangling target")

    return {
        "path": os.fspath(closure_file),
        "sha256": _digest(closure),
        "schema": CLOSURE_SCHEMA,
        "stage_root": os.fspath(stage_root),
        "canonical_root": os.fspath(canonical_root),
        "common_dir": closure["common_dir"],
        "runner": actual_runner,
        "overlay": closure.get("overlay"),
        "dependency_root": closure.get("dependency_root"),
        "mask": {
            "path": os.fspath(mask_path),
            "mount_parent": os.fspath(mount_parent),
            "identity": mask_identity,
        },
        "counts": counts,
        "links": sorted(link_snapshot, key=lambda item: item["path"]),
        "external_links": sorted(external_links),
        "external_targets": sorted(external_targets),
        "probe_imports": list(probe_imports),
        "namespace_policy": closure["namespace_policy"],
    }


def validate_live_sources(
    receipt_path: str | Path, canonical_root: str | Path, forbidden_root: str | Path
) -> dict[str, Any]:
    path = _absolute(receipt_path, "live-source receipt")
    value = _load(path)
    canonical = _absolute(canonical_root, "DAIC canonical root")
    forbidden = _absolute(forbidden_root, "DAIC staged root")
    if value.get("schema") != LIVE_SOURCE_SCHEMA:
        raise PreflightError("wrong live-source isolation schema")
    if value.get("canonical") != os.fspath(canonical):
        raise PreflightError("live-source receipt has the wrong canonical root")
    if value.get("forbidden") != os.fspath(forbidden):
        raise PreflightError("live-source receipt has the wrong forbidden staged root")
    if value.get("stdlib_only") is not True or value.get("commands_executed") is not False:
        raise PreflightError("live-source observer was not read-only stdlib inspection")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PreflightError("live-source receipt has no inspected entries")
    for entry in entries:
        if not isinstance(entry, dict) or not Path(entry.get("path", "")).is_absolute():
            raise PreflightError("live-source receipt entry is invalid")
        resolved = entry.get("resolved")
        if resolved is not None:
            if not Path(resolved).is_absolute():
                raise PreflightError("live-source resolution is not absolute")
            if _under(resolved, forbidden):
                raise PreflightError("live source resolves into the staged worktree")
    return {
        "path": os.fspath(path),
        "sha256": _digest(value),
        "canonical": os.fspath(canonical),
        "forbidden": os.fspath(forbidden),
        "entries": entries,
    }


def parse_required_states(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise PreflightError("DAIC link-state requirement must be STATE=COUNT")
        state, raw_count = value.split("=", 1)
        if not state or state in result:
            raise PreflightError("duplicate or empty DAIC link-state requirement")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise PreflightError("DAIC link-state count is not an integer") from exc
        if count < 0:
            raise PreflightError("DAIC link-state count is negative")
        result[state] = count
    if not result:
        raise PreflightError("at least one DAIC link-state requirement is required")
    return result


def _inventory(root: Path, exclude_test_venv: bool) -> dict[str, Any]:
    excludes = [".test-venv"] if exclude_test_venv else []
    try:
        return release_guard.readonly_inventory(root, excludes)
    except release_guard.GuardError as exc:
        raise PreflightError(str(exc)) from exc


def _dependency_inventory(closure: Mapping[str, Any]) -> dict[str, Any]:
    root = _absolute(closure.get("dependency_root", ""), "DAIC dependency root")
    try:
        return release_guard.readonly_inventory(root, [])
    except release_guard.GuardError as exc:
        raise PreflightError(str(exc)) from exc


def compare_snapshots(before: Mapping[str, Any], after: Mapping[str, Any], label: str) -> None:
    if before != after:
        raise PreflightError(f"{label} changed between before/after inventories")


def _run_checked(
    command: Sequence[str], cwd: Path, environment: Mapping[str, str], label: str
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise PreflightError(f"{label} failed: {detail}")
    return result


def daic_runner_command(
    runner: Path,
    closure: Path,
    child: Sequence[str] | None = None,
    export: Path | None = None,
) -> list[str]:
    if not runner.is_absolute() or not closure.is_absolute():
        raise PreflightError("DAIC runner and closure paths must be absolute")
    command = ["/usr/bin/bash", os.fspath(runner)]
    if child is None:
        command.extend(["verify", "--receipt", os.fspath(closure)])
    else:
        command.extend(["--receipt", os.fspath(closure)])
        if export is not None:
            if not export.is_absolute():
                raise PreflightError("DAIC namespace export path must be absolute")
            command.extend(["--export", os.fspath(export)])
        command.append("--")
        command.extend(child)
    validate_daic_launch(command, runner, closure)
    return command


def validate_daic_launch(command: Sequence[str], runner: Path, closure: Path) -> None:
    expected = ["/usr/bin/bash", os.fspath(runner)]
    if list(command[:2]) != expected:
        raise PreflightError("direct DAIC execution is forbidden; use the exact namespace runner")
    arguments = list(command[2:])
    if os.fspath(closure) not in arguments or "--receipt" not in arguments:
        raise PreflightError("DAIC execution is missing the exact namespace receipt")


NAMESPACE_PROBE = r'''
import importlib.util, json, os, pathlib, sys

canonical = pathlib.Path(sys.argv[1])
runner = sys.argv[2]
imports = json.loads(sys.argv[3])
external = json.loads(sys.argv[4])

# The namespace runner creates the private project root itself.  Establish its
# .claude marker before importing DAIC modules so shared_state selects this
# private root for every transitive state write.
project = pathlib.Path(os.environ["CLAUDE_PROJECT_DIR"])
(project / ".claude").mkdir(parents=True, exist_ok=True)

ancestors = []
pid = os.getpid()
seen = set()
while pid > 0 and pid not in seen:
    seen.add(pid)
    status = pathlib.Path(f"/proc/{pid}/status").read_text()
    ppid = 0
    for line in status.splitlines():
        if line.startswith("PPid:"):
            ppid = int(line.split()[1])
            break
    raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    ancestors.append({"pid": pid, "ppid": ppid, "argv": [x.decode("utf-8", "replace") for x in raw if x]})
    pid = ppid

loaded = []
for index, relative in enumerate(imports):
    path = canonical / relative
    name = f"effort510_probe_{index}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    loaded.append(str(path))

external_states = {}
for relative in external:
    try:
        os.stat(canonical / relative)
    except FileNotFoundError:
        external_states[relative] = "absent"
    else:
        external_states[relative] = "present"

evidence = {
    "schema": "daic-namespace-probe-v3",
    "cwd": str(pathlib.Path.cwd()),
    "interpreter": sys.executable,
    "runner_path": os.environ.get("EFFORT510_RUNNER_PATH"),
    "runner_pid": int(os.environ.get("EFFORT510_RUNNER_PID", "0")),
    "ancestors": ancestors,
    "environment": {key: os.environ.get(key) for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "TMPDIR", "CLAUDE_PROJECT_DIR")},
    "imports": loaded,
    "external_follow": external_states,
}
pathlib.Path("/run/effort510/export").write_text(json.dumps(evidence, sort_keys=True) + "\n")
'''


def validate_namespace_evidence(
    evidence: Mapping[str, Any], closure: Mapping[str, Any], daic_python: Path
) -> dict[str, Any]:
    if evidence.get("schema") != PROBE_SCHEMA:
        raise PreflightError("wrong DAIC namespace probe schema")
    if evidence.get("cwd") != closure["canonical_root"]:
        raise PreflightError("DAIC namespace did not chdir to the canonical overlay")
    expected_interpreter = os.fspath(
        Path(closure["canonical_root"]) / daic_python.relative_to(closure["stage_root"])
    )
    if evidence.get("interpreter") != expected_interpreter:
        raise PreflightError("DAIC namespace used the wrong interpreter identity")
    if evidence.get("runner_path") != closure["runner"]["path"]:
        raise PreflightError("DAIC namespace runner path evidence is missing")
    runner_pid = evidence.get("runner_pid")
    ancestors = evidence.get("ancestors")
    if not isinstance(runner_pid, int) or runner_pid <= 0 or not isinstance(ancestors, list):
        raise PreflightError("DAIC namespace runner ancestry evidence is invalid")
    runner_rows = [row for row in ancestors if row.get("pid") == runner_pid]
    if len(runner_rows) != 1 or closure["runner"]["path"] not in runner_rows[0].get("argv", []):
        raise PreflightError("DAIC child PID does not descend from the exact namespace runner")

    environment = evidence.get("environment")
    if not isinstance(environment, dict):
        raise PreflightError("DAIC namespace environment evidence is absent")
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
        "CLAUDE_PROJECT_DIR",
    ):
        value = environment.get(key)
        if not isinstance(value, str) or not value.startswith("/run/effort510/"):
            raise PreflightError(f"DAIC namespace {key} is not private")

    expected_imports = sorted(
        os.fspath(Path(closure["canonical_root"]) / relative)
        for relative in closure["probe_imports"]
    )
    if sorted(evidence.get("imports", [])) != expected_imports:
        raise PreflightError("DAIC transitive imports did not resolve through the staged overlay")
    expected_external = {path: "absent" for path in closure["external_links"]}
    if evidence.get("external_follow") != expected_external:
        raise PreflightError("DAIC namespace resolved an externally dangling target")
    return dict(evidence)


def _private_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "HOME": os.fspath(root / "home"),
            "XDG_CONFIG_HOME": os.fspath(root / "xdg-config"),
            "XDG_CACHE_HOME": os.fspath(root / "xdg-cache"),
            "XDG_STATE_HOME": os.fspath(root / "xdg-state"),
            "TMPDIR": os.fspath(root / "tmp"),
        }
    )
    for name in ("home", "xdg-config", "xdg-cache", "xdg-state", "tmp"):
        (root / name).mkdir(mode=0o700)
    return environment


def _validate_interpreters(
    assist_root: Path,
    assist_python_path: str,
    daic_root: Path,
    daic_python_path: str,
    core_root: Path,
    core_python_path: str,
) -> dict[str, dict[str, Any]]:
    assist = executable_identity(assist_python_path, "Assist interpreter")
    daic = executable_identity(daic_python_path, "DAIC interpreter")
    core = executable_identity(core_python_path, "daic-core interpreter")
    if not _under(daic["path"], daic_root / ".test-venv"):
        raise PreflightError("DAIC interpreter is not in its repo-owned .test-venv")
    if not _under(core["path"], core_root / ".test-venv"):
        raise PreflightError("daic-core interpreter is not in its repo-owned .test-venv")
    if len({assist["path"], daic["path"], core["path"]}) != 3:
        raise PreflightError("each repository must use its own absolute interpreter path")
    return {"assist": assist, "daic": daic, "core": core}


def _tool_identities() -> list[dict[str, Any]]:
    return [executable_identity(path, f"required tool {path}") for path in FIXED_TOOLS]


def _snapshot(
    roots: Mapping[str, Path],
    closure: Mapping[str, Any],
    interpreter_paths: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "inventories": {
            "assist": _inventory(roots["assist"], False),
            "daic": _inventory(roots["daic"], True),
            "core": _inventory(roots["core"], True),
            "daic_dependencies": _dependency_inventory(closure),
        },
        "interpreters": {
            key: executable_identity(value, f"{key} interpreter")
            for key, value in interpreter_paths.items()
        },
        "tools": _tool_identities(),
        "closure_file": _identity(Path(closure["path"])),
        "live_source_file": _identity(Path(closure["live_sources"]["path"])),
        "links": closure["links"],
        "mask": closure["mask"],
    }


def execute_preflight(args: argparse.Namespace) -> None:
    if args.assist_ref != PUBLIC_REF or args.core_ref != PUBLIC_REF:
        raise PreflightError(f"both public refs must be {PUBLIC_REF}")
    roots = {
        "assist": _absolute(args.assist_root, "Assist worktree"),
        "daic": _absolute(args.daic_root, "DAIC worktree"),
        "core": _absolute(args.core_root, "daic-core worktree"),
    }
    repos = {
        "assist": repo_identity(roots["assist"], args.assist_ref),
        "daic": repo_identity(roots["daic"], None),
        "core": repo_identity(roots["core"], args.core_ref),
    }
    output = _absolute(args.out, "preflight output", must_exist=False)
    receipt_dir = output.parent
    overlays = {
        key: _overlay_reference(receipt_dir / name, repos[key])
        for key, name in OVERLAY_NAMES.items()
    }
    states = parse_required_states(args.require_daic_link_state)
    runner = _absolute(args.daic_runner, "DAIC runner")
    closure_path = _absolute(args.daic_closure, "DAIC closure receipt")
    closure = validate_closure(
        closure_path,
        roots["daic"],
        runner,
        states,
        args.require_daic_mask,
        overlays["daic"],
    )
    live_sources = validate_live_sources(
        receipt_dir / LIVE_SOURCE_NAME, closure["canonical_root"], roots["daic"]
    )
    closure["live_sources"] = live_sources
    interpreters = _validate_interpreters(
        roots["assist"],
        args.assist_python,
        roots["daic"],
        args.daic_python,
        roots["core"],
        args.core_python,
    )
    interpreter_paths = {key: value["path"] for key, value in interpreters.items()}
    before = _snapshot(roots, closure, interpreter_paths)

    with tempfile.TemporaryDirectory(prefix="effort510-preflight-") as temporary:
        private_root = Path(temporary)
        environment = _private_environment(private_root)
        _run_checked(
            daic_runner_command(runner, closure_path),
            roots["daic"],
            environment,
            "DAIC closure verification",
        )
        assist_code = (
            "import pathlib,shared,sys; "
            "root=pathlib.Path(sys.argv[1]); "
            "source=pathlib.Path(shared.__file__).resolve(); "
            "assert pathlib.Path.cwd()==root and source.is_relative_to(root)"
        )
        _run_checked(
            [interpreters["assist"]["path"], "-c", assist_code, os.fspath(roots["assist"])],
            roots["assist"],
            environment,
            "Assist isolated-source import",
        )
        core_code = "import pytest; assert pytest.__version__ == '8.3.5'"
        _run_checked(
            [interpreters["core"]["path"], "-c", core_code],
            roots["core"],
            environment,
            "daic-core pytest identity",
        )
        daic_relative_python = Path(interpreters["daic"]["path"]).relative_to(
            roots["daic"]
        ).as_posix()
        _run_checked(
            daic_runner_command(
                runner,
                closure_path,
                [daic_relative_python, "-c", core_code],
            ),
            roots["daic"],
            environment,
            "DAIC pytest identity through namespace runner",
        )
        export = private_root / "namespace-probe.json"
        probe_child = [
            daic_relative_python,
            "-c",
            NAMESPACE_PROBE,
            closure["canonical_root"],
            closure["runner"]["path"],
            json.dumps(closure["probe_imports"], separators=(",", ":")),
            json.dumps(closure["external_links"], separators=(",", ":")),
        ]
        _run_checked(
            daic_runner_command(runner, closure_path, probe_child, export),
            roots["daic"],
            environment,
            "DAIC namespace ancestry/XDG/state/import probe",
        )
        evidence = validate_namespace_evidence(_load(export), closure, Path(args.daic_python))

    for reference in overlays.values():
        try:
            release_guard.verify_overlay(reference["canonical"], reference["path"])
        except release_guard.GuardError as exc:
            raise PreflightError(f"canonical overlay changed during preflight: {exc}") from exc
    closure_after = validate_closure(
        closure_path,
        roots["daic"],
        runner,
        states,
        args.require_daic_mask,
        overlays["daic"],
    )
    closure_after["live_sources"] = validate_live_sources(
        live_sources["path"], closure["canonical_root"], roots["daic"]
    )
    after = _snapshot(roots, closure_after, interpreter_paths)
    compare_snapshots(before, after, "preflight estate")

    receipt = {
        "schema": SCHEMA,
        "kind": "combined-readonly-preflight",
        "repositories": repos,
        "public_ref": PUBLIC_REF,
        "overlays": overlays,
        "interpreters": interpreters,
        "tools": before["tools"],
        "daic_closure": closure,
        "daic_namespace_evidence": evidence,
        "before": before,
        "after": after,
    }
    release_guard._atomic_json(output, receipt)


def verify_receipt(args: argparse.Namespace) -> None:
    receipt_path = _absolute(args.verify_receipt, "preflight receipt")
    value = _load(receipt_path)
    if value.get("schema") != SCHEMA or value.get("kind") != "combined-readonly-preflight":
        raise PreflightError("not an Effort 510 preflight receipt")
    if args.require_public_ref != PUBLIC_REF or value.get("public_ref") != PUBLIC_REF:
        raise PreflightError("preflight receipt has the wrong public release ref")
    states = parse_required_states(args.require_daic_link_state)
    closure_reference = value.get("daic_closure", {})
    required_closure = _absolute(args.require_daic_closure, "required DAIC closure")
    if closure_reference.get("path") != os.fspath(required_closure):
        raise PreflightError("preflight receipt names another DAIC closure")
    closure = _load(required_closure)
    if closure_reference.get("sha256") != _digest(closure):
        raise PreflightError("DAIC closure receipt identity changed")
    if closure_reference.get("counts") != states:
        raise PreflightError("preflight DAIC link-state partition changed")
    required_mask = _absolute(args.require_daic_mask, "required DAIC mask")
    if closure_reference.get("mask", {}).get("path") != os.fspath(required_mask):
        raise PreflightError("preflight receipt names another DAIC mask")

    repositories = value.get("repositories", {})
    for key in ("assist", "daic", "core"):
        recorded = repositories.get(key)
        if not isinstance(recorded, dict):
            raise PreflightError(f"preflight repository receipt is missing: {key}")
        required_ref = PUBLIC_REF if key in ("assist", "core") else None
        if repo_identity(recorded["root"], required_ref) != recorded:
            raise PreflightError(f"preflight repository identity changed: {key}")
    for key, recorded in value.get("interpreters", {}).items():
        if executable_identity(recorded["path"], f"{key} interpreter") != recorded:
            raise PreflightError(f"preflight interpreter identity changed: {key}")
    if _tool_identities() != value.get("tools"):
        raise PreflightError("preflight tool identity changed")
    if args.verify_overlays:
        for reference in value.get("overlays", {}).values():
            try:
                release_guard.verify_overlay(reference["canonical"], reference["path"])
            except release_guard.GuardError as exc:
                raise PreflightError(f"preflight overlay changed: {exc}") from exc
    current_closure = validate_closure(
        required_closure,
        repositories["daic"]["root"],
        closure_reference["runner"]["path"],
        states,
        required_mask,
        value["overlays"]["daic"],
    )
    if current_closure["sha256"] != closure_reference["sha256"]:
        raise PreflightError("preflight closure structure changed")
    validate_namespace_evidence(
        value["daic_namespace_evidence"],
        closure_reference,
        Path(value["interpreters"]["daic"]["path"]),
    )
    current_after = value.get("after")
    if not isinstance(current_after, dict):
        raise PreflightError("preflight after inventory is absent")
    current_closure["live_sources"] = validate_live_sources(
        closure_reference["live_sources"]["path"],
        closure_reference["canonical_root"],
        repositories["daic"]["root"],
    )
    roots = {key: Path(repositories[key]["root"]) for key in repositories}
    paths = {key: value["interpreters"][key]["path"] for key in roots}
    now = _snapshot(roots, current_closure, paths)
    compare_snapshots(current_after, now, "receipted preflight estate")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assist-root")
    parser.add_argument("--assist-ref")
    parser.add_argument("--assist-python")
    parser.add_argument("--daic-root")
    parser.add_argument("--daic-python")
    parser.add_argument("--daic-runner")
    parser.add_argument("--daic-closure")
    parser.add_argument("--core-root")
    parser.add_argument("--core-ref")
    parser.add_argument("--core-python")
    parser.add_argument("--out")
    parser.add_argument("--verify-receipt")
    parser.add_argument("--verify-overlays", action="store_true")
    parser.add_argument("--require-daic-closure")
    parser.add_argument("--require-daic-link-state", action="append", default=[])
    parser.add_argument("--require-daic-mask")
    parser.add_argument("--require-public-ref")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(arguments)
    try:
        if args.verify_receipt:
            required = (
                args.require_daic_closure,
                args.require_daic_mask,
                args.require_public_ref,
            )
            if not all(required):
                raise PreflightError("receipt verification arguments are incomplete")
            verify_receipt(args)
        else:
            required_names = (
                "assist_root",
                "assist_ref",
                "assist_python",
                "daic_root",
                "daic_python",
                "daic_runner",
                "daic_closure",
                "core_root",
                "core_ref",
                "core_python",
                "out",
                "require_daic_mask",
            )
            if any(getattr(args, name) is None for name in required_names):
                raise PreflightError("execution preflight arguments are incomplete")
            execute_preflight(args)
    except (PreflightError, release_guard.GuardError, ValueError) as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
