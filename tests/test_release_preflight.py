"""Synthetic fixtures for the combined read-only release preflight.

The fixture estate is wholly temporary.  In particular, these tests do not inspect or
modify the real DAIC or daic-core repositories whose runners do not exist in Phase 1.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.release import release_guard, release_preflight as preflight


class SyntheticClosure:
    def __init__(self, root: Path):
        self.root = root
        self.canonical = root / "canonical-daic"
        self.stage = root / "stage-daic"
        self.receipts = root / "receipts"
        self.dependencies = root / "dependencies"
        self.mask = self.dependencies / "external-dangling-mask"
        self.external_parent = root / "external-parent"
        self.external_target = self.external_parent / "missing" / "target"
        self.receipts.mkdir()
        self.dependencies.mkdir()
        self.mask.mkdir()
        self.mask.chmod(0o555)
        self.external_parent.mkdir()

        self.git("init", "-b", "main", os.fspath(self.canonical), cwd=root)
        self.git("config", "user.name", "Fixture", cwd=self.canonical)
        self.git("config", "user.email", "fixture@example.invalid", cwd=self.canonical)
        self.write(self.canonical, "targets/internal.py", "VALUE = 1\n")
        self.write(self.canonical, "scripts/shared_state.py", "SHARED = True\n")
        self.write(self.canonical, "scripts/session_start.py", "SESSION = True\n")
        self.write(
            self.canonical,
            "scripts/tests/run-isolated-daic.sh",
            "#!/usr/bin/bash\nexit 0\n",
            0o755,
        )
        (self.canonical / "links").mkdir()
        os.symlink(
            os.fspath(self.canonical / "targets/internal.py"),
            self.canonical / "links/internal",
        )
        os.symlink(os.fspath(self.external_target), self.canonical / "links/external")
        self.git("add", "-A", cwd=self.canonical)
        self.git("commit", "-m", "base", cwd=self.canonical)
        self.git(
            "worktree",
            "add",
            "--detach",
            os.fspath(self.stage),
            "HEAD",
            cwd=self.canonical,
        )
        self.runner = self.stage / "scripts/tests/run-isolated-daic.sh"
        self.overlay_path = self.receipts / "daic-user-overlay-v2.json"
        release_guard.capture_overlay(
            os.fspath(self.canonical), os.fspath(self.overlay_path)
        )
        overlay = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        self.overlay_reference = {
            "path": os.fspath(self.overlay_path),
            "sha256": release_guard._receipt_digest(overlay),
            "canonical": os.fspath(self.canonical),
            "head": overlay["head"],
            "common_dir": overlay["common_dir"],
        }
        self.closure_path = self.receipts / "daic-stage-closure-v3.json"
        self.live_path = self.receipts / preflight.LIVE_SOURCE_NAME
        self.write_receipts()

    @staticmethod
    def git(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"},
        )

    @staticmethod
    def write(root: Path, relative: str, content: str, mode: int = 0o644) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        return path

    def write_receipts(self) -> None:
        common = preflight.repo_identity(self.stage, None)["common_dir"]
        closure = {
            "schema": preflight.CLOSURE_SCHEMA,
            "stage_root": os.fspath(self.stage),
            "canonical_root": os.fspath(self.canonical),
            "common_dir": common,
            "runner": preflight.executable_identity(self.runner, "runner"),
            "overlay": {
                "path": os.fspath(self.overlay_path),
                "sha256": self.overlay_reference["sha256"],
            },
            "dependency_root": os.fspath(self.dependencies),
            "mask": {
                "path": os.fspath(self.mask),
                "mount_parent": os.fspath(self.external_parent),
                "identity": release_guard._path_identity(self.mask),
            },
            "namespace_policy": {
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
            },
            "probe_imports": [
                "scripts/shared_state.py",
                "scripts/session_start.py",
            ],
            "links": [
                {
                    "path": "links/internal",
                    "mode": "120000",
                    "link_text": os.fspath(self.canonical / "targets/internal.py"),
                    "state": "internal-resolved",
                    "staged_target": os.fspath(self.stage / "targets/internal.py"),
                    "resolved_identity": preflight._topology_identity(
                        self.stage / "targets/internal.py"
                    ),
                },
                {
                    "path": "links/external",
                    "mode": "120000",
                    "link_text": os.fspath(self.external_target),
                    "state": "external-dangling",
                    "external_target": os.fspath(self.external_target),
                },
            ],
        }
        release_guard._atomic_json(self.closure_path, closure)
        live = {
            "schema": preflight.LIVE_SOURCE_SCHEMA,
            "canonical": os.fspath(self.canonical),
            "forbidden": os.fspath(self.stage),
            "stdlib_only": True,
            "commands_executed": False,
            "entries": [
                {
                    "path": os.fspath(self.root / "project/.claude/statusline-script.py"),
                    "resolved": os.fspath(self.canonical / "scripts/shared_state.py"),
                }
            ],
        }
        release_guard._atomic_json(self.live_path, live)

    def validate(self) -> dict:
        return preflight.validate_closure(
            self.closure_path,
            self.stage,
            self.runner,
            {"internal-resolved": 1, "external-dangling": 1},
            self.mask,
            self.overlay_reference,
        )

    def evidence(self, summary: dict, daic_python: Path) -> dict:
        runner_pid = 4100
        return {
            "schema": preflight.PROBE_SCHEMA,
            "cwd": summary["canonical_root"],
            "interpreter": os.fspath(
                Path(summary["canonical_root"])
                / daic_python.relative_to(summary["stage_root"])
            ),
            "runner_path": summary["runner"]["path"],
            "runner_pid": runner_pid,
            "ancestors": [
                {"pid": 4200, "ppid": runner_pid, "argv": ["python"]},
                {
                    "pid": runner_pid,
                    "ppid": 1,
                    "argv": ["/usr/bin/bash", summary["runner"]["path"]],
                },
            ],
            "environment": {
                "HOME": "/run/effort510/home",
                "XDG_CONFIG_HOME": "/run/effort510/xdg-config",
                "XDG_CACHE_HOME": "/run/effort510/xdg-cache",
                "XDG_STATE_HOME": "/run/effort510/xdg-state",
                "TMPDIR": "/run/effort510/tmp",
                "CLAUDE_PROJECT_DIR": "/run/effort510/project-state",
            },
            "imports": [
                os.fspath(Path(summary["canonical_root"]) / relative)
                for relative in summary["probe_imports"]
            ],
            "external_follow": {path: "absent" for path in summary["external_links"]},
        }


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="effort510-preflight-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self) -> SyntheticClosure:
        return SyntheticClosure(self.root)

    def test_direct_script_entrypoint_can_import_adjacent_release_guard(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "release"
            / "release_preflight.py"
        )
        result = subprocess.run(
            [sys.executable, os.fspath(script), "--help"],
            cwd=script.parent.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def test_absolute_interpreters_are_distinct_and_repo_owned(self):
        assist_root = self.root / "assist"
        daic_root = self.root / "daic"
        core_root = self.root / "core"
        assist_root.mkdir()
        daic_root.mkdir()
        core_root.mkdir()
        assist_python = self._executable(assist_root / "venv/bin/python")
        daic_python = self._executable(daic_root / ".test-venv/bin/python")
        core_python = self._executable(core_root / ".test-venv/bin/python")

        identities = preflight._validate_interpreters(
            assist_root,
            os.fspath(assist_python),
            daic_root,
            os.fspath(daic_python),
            core_root,
            os.fspath(core_python),
        )
        self.assertEqual(identities["daic"]["path"], os.fspath(daic_python))
        with self.assertRaisesRegex(preflight.PreflightError, "absolute path"):
            preflight.executable_identity("python3", "caller interpreter")
        with self.assertRaisesRegex(preflight.PreflightError, "repo-owned"):
            preflight._validate_interpreters(
                assist_root,
                os.fspath(assist_python),
                daic_root,
                os.fspath(assist_python),
                core_root,
                os.fspath(core_python),
            )

    @staticmethod
    def _executable(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/bash\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_public_repo_identity_pins_common_directory_and_attached_release_ref(self):
        fixture = self.fixture()
        public = self.root / "public"
        fixture.git(
            "worktree",
            "add",
            "-b",
            "release/effort-510-v16",
            os.fspath(public),
            "HEAD",
            cwd=fixture.canonical,
        )
        identity = preflight.repo_identity(public, preflight.PUBLIC_REF)
        self.assertEqual(identity["attached_ref"], preflight.PUBLIC_REF)
        self.assertEqual(
            identity["common_dir"], preflight.repo_identity(fixture.canonical, None)["common_dir"]
        )

        fixture.git("switch", "--detach", cwd=public)
        with self.assertRaisesRegex(preflight.PreflightError, "detached or attached"):
            preflight.repo_identity(public, preflight.PUBLIC_REF)

    def test_overlay_is_bound_to_common_directory_head_and_current_state(self):
        fixture = self.fixture()
        identity = preflight.repo_identity(fixture.stage, None)
        reference = preflight._overlay_reference(fixture.overlay_path, identity)
        self.assertEqual(reference["common_dir"], identity["common_dir"])

        fixture.write(fixture.canonical, "CLAUDE.md", "changed overlay\n")
        with self.assertRaisesRegex(preflight.PreflightError, "overlay verification failed"):
            preflight._overlay_reference(fixture.overlay_path, identity)

    def test_closure_pins_live_to_stage_mapping_and_stage_to_live_absence(self):
        fixture = self.fixture()
        summary = fixture.validate()
        self.assertEqual(
            summary["counts"], {"internal-resolved": 1, "external-dangling": 1}
        )
        links = {entry["path"]: entry for entry in summary["links"]}
        self.assertEqual(links["links/external"]["follow_state"], "absent")

        (fixture.stage / "targets/internal.py").unlink()
        with self.assertRaisesRegex(preflight.PreflightError, "target disappeared"):
            fixture.validate()

    def test_closure_rejects_dangling_target_state_transitions_and_barrier_appearance(self):
        fixture = self.fixture()
        summary = fixture.validate()
        daic_python = fixture.stage / ".test-venv/bin/python"
        evidence = fixture.evidence(summary, daic_python)
        self.assertEqual(evidence["external_follow"], {"links/external": "absent"})

        fixture.external_target.parent.mkdir(parents=True)
        fixture.external_target.write_text("appeared after precheck\n", encoding="utf-8")
        with self.assertRaisesRegex(preflight.PreflightError, "changed state"):
            fixture.validate()
        self.assertEqual(
            evidence["external_follow"]["links/external"],
            "absent",
            "the synthetic namespace evidence must prove the mask kept the late target hidden",
        )

    def test_closure_rejects_removed_or_replaced_mask(self):
        fixture = self.fixture()
        fixture.validate()
        fixture.mask.chmod(0o755)
        with self.assertRaisesRegex(preflight.PreflightError, "empty 0555"):
            fixture.validate()

        fixture.mask.rmdir()
        fixture.mask.write_text("replacement\n", encoding="utf-8")
        with self.assertRaisesRegex(preflight.PreflightError, "mask"):
            fixture.validate()

    def test_live_source_receipt_rejects_resolution_into_stage(self):
        fixture = self.fixture()
        valid = preflight.validate_live_sources(
            fixture.live_path, fixture.canonical, fixture.stage
        )
        self.assertEqual(valid["forbidden"], os.fspath(fixture.stage))
        value = json.loads(fixture.live_path.read_text(encoding="utf-8"))
        value["entries"][0]["resolved"] = os.fspath(
            fixture.stage / "scripts/shared_state.py"
        )
        release_guard._atomic_json(fixture.live_path, value)
        with self.assertRaisesRegex(preflight.PreflightError, "resolves into"):
            preflight.validate_live_sources(
                fixture.live_path, fixture.canonical, fixture.stage
            )

    def test_namespace_evidence_pins_runner_ancestry_private_xdg_state_and_imports(self):
        fixture = self.fixture()
        summary = fixture.validate()
        daic_python = fixture.stage / ".test-venv/bin/python"
        evidence = fixture.evidence(summary, daic_python)
        self.assertEqual(
            preflight.validate_namespace_evidence(evidence, summary, daic_python), evidence
        )

        wrong_runner = copy.deepcopy(evidence)
        wrong_runner["runner_pid"] = 9999
        with self.assertRaisesRegex(preflight.PreflightError, "descend"):
            preflight.validate_namespace_evidence(wrong_runner, summary, daic_python)
        live_xdg = copy.deepcopy(evidence)
        live_xdg["environment"]["XDG_STATE_HOME"] = "/home/example/.local/state"
        with self.assertRaisesRegex(preflight.PreflightError, "not private"):
            preflight.validate_namespace_evidence(live_xdg, summary, daic_python)
        live_import = copy.deepcopy(evidence)
        live_import["imports"][0] = os.fspath(
            fixture.stage / "scripts/shared_state.py"
        )
        with self.assertRaisesRegex(preflight.PreflightError, "staged overlay"):
            preflight.validate_namespace_evidence(live_import, summary, daic_python)

    def test_namespace_probe_establishes_private_project_before_transitive_imports(self):
        probe = preflight.NAMESPACE_PROBE
        marker = '(project / ".claude").mkdir(parents=True, exist_ok=True)'
        import_loop = "for index, relative in enumerate(imports):"
        self.assertIn('project = pathlib.Path(os.environ["CLAUDE_PROJECT_DIR"])', probe)
        self.assertIn(marker, probe)
        self.assertLess(probe.index(marker), probe.index(import_loop))
        compile(probe, "<effort510-namespace-probe>", "exec")

    def test_namespace_policy_rejects_transitive_xdg_state_or_import_relaxation(self):
        fixture = self.fixture()
        closure = json.loads(fixture.closure_path.read_text(encoding="utf-8"))
        for key in ("xdg", "state", "imports"):
            changed = copy.deepcopy(closure)
            changed["namespace_policy"][key] = "live-allowed"
            release_guard._atomic_json(fixture.closure_path, changed)
            with self.assertRaisesRegex(preflight.PreflightError, "policy is incomplete"):
                fixture.validate()
            release_guard._atomic_json(fixture.closure_path, closure)

    def test_direct_daic_invocation_is_refused_and_builder_uses_exact_runner(self):
        fixture = self.fixture()
        safe = preflight.daic_runner_command(
            fixture.runner,
            fixture.closure_path,
            [".test-venv/bin/python", "-c", "pass"],
        )
        self.assertEqual(safe[:2], ["/usr/bin/bash", os.fspath(fixture.runner)])
        self.assertIn(os.fspath(fixture.closure_path), safe)
        with self.assertRaisesRegex(preflight.PreflightError, "direct DAIC execution"):
            preflight.validate_daic_launch(
                [os.fspath(fixture.stage / ".test-venv/bin/python"), "-c", "pass"],
                fixture.runner,
                fixture.closure_path,
            )

    def test_before_after_inventory_rejects_content_mode_and_path_changes(self):
        root = self.root / "inventory"
        root.mkdir()
        target = root / "value.txt"
        target.write_text("AAAA", encoding="utf-8")
        before = release_guard.readonly_inventory(root, [])

        target.write_text("BBBB", encoding="utf-8")
        after_hash = release_guard.readonly_inventory(root, [])
        with self.assertRaisesRegex(preflight.PreflightError, "before/after"):
            preflight.compare_snapshots(before, after_hash, "fixture")

        target.write_text("AAAA", encoding="utf-8")
        target.chmod(0o755)
        after_mode = release_guard.readonly_inventory(root, [])
        with self.assertRaisesRegex(preflight.PreflightError, "before/after"):
            preflight.compare_snapshots(before, after_mode, "fixture")

        target.chmod(0o644)
        (root / "new.txt").write_text("new\n", encoding="utf-8")
        after_path = release_guard.readonly_inventory(root, [])
        with self.assertRaisesRegex(preflight.PreflightError, "before/after"):
            preflight.compare_snapshots(before, after_path, "fixture")

    def test_parse_link_state_requires_exact_nonnegative_partition(self):
        self.assertEqual(
            preflight.parse_required_states(
                ["internal-resolved=9", "external-dangling=2"]
            ),
            {"internal-resolved": 9, "external-dangling": 2},
        )
        for invalid in ([], ["missing"], ["x=-1"], ["x=1", "x=2"]):
            with self.assertRaises(preflight.PreflightError):
                preflight.parse_required_states(invalid)


if __name__ == "__main__":
    unittest.main()
