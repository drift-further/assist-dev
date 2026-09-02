"""Synthetic contract tests for the maintainer release guard.

All Git repositories and worktrees in this module live below a TemporaryDirectory.  No
fixture reads or changes either sibling repository from the release plan.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.release import release_guard as guard


class GitFixture:
    def __init__(self, root: Path):
        self.root = root
        self.canonical = root / "canonical"
        self.release = root / "release"
        self.receipts = root / "receipts"
        self.receipts.mkdir()
        self.git("init", "-b", "main", os.fspath(self.canonical), cwd=root)
        self.git("config", "user.name", "Fixture", cwd=self.canonical)
        self.git("config", "user.email", "fixture@example.invalid", cwd=self.canonical)
        self.write(self.canonical, ".gitignore", "alpha\nbeta\ngamma\n")
        self.write(self.canonical, "install.sh", "one\ntwo\nthree\n", 0o755)
        self.write(self.canonical, "CLAUDE.md", "fixture instructions\n")
        self.git("add", "-A", cwd=self.canonical)
        self.git("commit", "-m", "base", cwd=self.canonical)
        self.base = self.git("rev-parse", "HEAD", cwd=self.canonical).stdout.strip()
        self.git(
            "worktree",
            "add",
            "-b",
            "release/effort-510-v16",
            os.fspath(self.release),
            self.base,
            cwd=self.canonical,
        )

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

    def overlay(self) -> Path:
        path = self.receipts / "overlay.json"
        guard.capture_overlay(os.fspath(self.canonical), os.fspath(path))
        return path

    def stage_and_approve(
        self,
        phase: str = "fixture-phase",
        *,
        forbid: tuple[str, ...] = (),
        require: tuple[str, ...] = (),
        tested_execution: str | None = None,
    ) -> tuple[Path, Path]:
        candidate = self.receipts / f"{phase}.candidate.json"
        approved = self.receipts / f"{phase}.approved.json"
        guard.capture_stage(
            os.fspath(self.release),
            phase,
            os.fspath(candidate),
            forbid,
            require,
            tested_execution,
        )
        guard.verify_stage(os.fspath(self.release), os.fspath(candidate), None, None)
        guard.approve_stage(
            os.fspath(self.release),
            os.fspath(candidate),
            "Daniel",
            os.fspath(approved),
        )
        guard.verify_stage(
            os.fspath(self.release), os.fspath(approved), "Daniel", tested_execution
        )
        return candidate, approved


class ReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="effort510-guard-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self) -> GitFixture:
        return GitFixture(self.root)

    def test_overlay_records_hash_only_and_detects_untracked_hash_change(self):
        fixture = self.fixture()
        secret = "value-that-must-never-enter-a-receipt"
        fixture.write(fixture.canonical, ".env.private", secret)
        receipt = fixture.overlay()

        raw = receipt.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        value = json.loads(raw)
        untracked = {item["path"]: item["identity"] for item in value["untracked"]}
        self.assertIn(".env.private", untracked)
        self.assertEqual(untracked[".env.private"]["type"], "file")
        self.assertEqual(len(untracked[".env.private"]["sha256"]), 64)

        fixture.write(fixture.canonical, ".env.private", "x" * len(secret))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = guard.main(
                [
                    "verify-overlay",
                    "--canonical",
                    os.fspath(fixture.canonical),
                    "--receipt",
                    os.fspath(receipt),
                ]
            )
        self.assertEqual(result, 2)
        self.assertNotIn(secret, stderr.getvalue())

    def test_overlay_detects_dirty_same_file_index_and_worktree_changes(self):
        fixture = self.fixture()
        receipt = fixture.overlay()

        fixture.write(fixture.canonical, ".gitignore", "user\nbeta\ngamma\n")
        fixture.git("add", ".gitignore", cwd=fixture.canonical)
        with self.assertRaisesRegex(guard.GuardError, "overlay identity changed"):
            guard.verify_overlay(os.fspath(fixture.canonical), os.fspath(receipt))

        fixture.git("restore", "--staged", ".gitignore", cwd=fixture.canonical)
        fixture.git("restore", ".gitignore", cwd=fixture.canonical)
        fixture.write(fixture.canonical, "install.sh", "one\ntwo\nuser\n", 0o755)
        with self.assertRaisesRegex(guard.GuardError, "overlay identity changed"):
            guard.verify_overlay(os.fspath(fixture.canonical), os.fspath(receipt))

    def test_stage_manifest_pins_paths_blobs_modes_hunks_and_execution_receipt(self):
        fixture = self.fixture()
        fixture.write(fixture.release, ".gitignore", "feature\nbeta\ngamma\n")
        fixture.write(fixture.release, "tests/new_guarded.py", "VALUE = 1\n")
        fixture.git("add", "-A", cwd=fixture.release)
        execution = fixture.receipts / "execution.json"
        execution.write_text('{"closed":true}\n', encoding="utf-8")

        candidate, approved = fixture.stage_and_approve(
            require=(".gitignore:feature",),
            tested_execution=os.fspath(execution),
        )
        value = json.loads(candidate.read_text(encoding="utf-8"))
        self.assertEqual(value["base"], fixture.base)
        self.assertEqual(value["attached_ref"], guard.PUBLIC_REF)
        self.assertEqual(value["paths"], [".gitignore", "tests/new_guarded.py"])
        self.assertTrue(value["normalised_hunks"])
        self.assertEqual(len(value["binary_diff"]["sha256"]), 64)
        self.assertEqual(json.loads(approved.read_text())["approval"]["approver"], "Daniel")

        execution.write_text('{"closed":false}\n', encoding="utf-8")
        with self.assertRaisesRegex(guard.GuardError, "tested-execution"):
            guard.verify_stage(
                os.fspath(fixture.release),
                os.fspath(candidate),
                None,
                os.fspath(execution),
            )

    def test_stage_manifest_refuses_forbidden_and_missing_required_hunks(self):
        fixture = self.fixture()
        fixture.write(fixture.release, "docker/new-file", "no\n")
        fixture.git("add", "-A", cwd=fixture.release)
        with self.assertRaisesRegex(guard.GuardError, "forbidden staged path"):
            guard.capture_stage_data(
                fixture.release, "forbidden", forbid_prefixes=("docker/",)
            )

        fixture.git("restore", "--staged", "docker/new-file", cwd=fixture.release)
        (fixture.release / "docker/new-file").unlink()
        fixture.write(fixture.release, ".gitignore", "feature\nbeta\ngamma\n")
        fixture.git("add", ".gitignore", cwd=fixture.release)
        with self.assertRaisesRegex(guard.GuardError, "required hunk text is absent"):
            guard.capture_stage_data(
                fixture.release,
                "missing-hunk",
                required_hunks=(".gitignore:not-present",),
            )

    def test_verify_stage_rejects_unrelated_staged_dot_claude_path(self):
        fixture = self.fixture()
        fixture.write(fixture.release, "install.sh", "feature\ntwo\nthree\n", 0o755)
        fixture.git("add", "install.sh", cwd=fixture.release)
        candidate = fixture.receipts / "candidate.json"
        guard.capture_stage(
            os.fspath(fixture.release),
            "exact-stage",
            os.fspath(candidate),
            (),
            (),
            None,
        )

        fixture.write(fixture.release, ".claude/unrelated.json", "{}\n")
        fixture.git("add", ".claude/unrelated.json", cwd=fixture.release)
        with self.assertRaisesRegex(guard.GuardError, "identity changed"):
            guard.verify_stage(
                os.fspath(fixture.release), os.fspath(candidate), None, None
            )

    def test_capture_stage_refuses_unstaged_and_untracked_output(self):
        fixture = self.fixture()
        fixture.write(fixture.release, "install.sh", "feature\ntwo\nthree\n", 0o755)
        with self.assertRaisesRegex(guard.GuardError, "unstaged tracked"):
            guard.capture_stage_data(fixture.release, "unstaged")
        fixture.git("restore", "install.sh", cwd=fixture.release)
        fixture.write(fixture.release, "untracked.txt", "output\n")
        with self.assertRaisesRegex(guard.GuardError, "untracked paths"):
            guard.capture_stage_data(fixture.release, "untracked")

    def test_three_way_materialization_preserves_dirty_same_file_overlay_and_index(self):
        fixture = self.fixture()
        fixture.write(fixture.canonical, ".gitignore", "alpha\nbeta\nuser\n")
        fixture.git("add", ".gitignore", cwd=fixture.canonical)
        fixture.write(fixture.canonical, "install.sh", "one\ntwo\nuser\n", 0o755)
        fixture.write(fixture.canonical, "backup.user", "leave me\n")
        overlay = fixture.overlay()
        index_before = fixture.git("diff", "--cached", "--binary", cwd=fixture.canonical).stdout

        fixture.write(fixture.release, ".gitignore", "feature\nbeta\ngamma\n")
        fixture.write(fixture.release, "install.sh", "feature\ntwo\nthree\n", 0o755)
        fixture.write(fixture.release, "tests/landed.py", "LANDED = True\n")
        fixture.git("add", "-A", cwd=fixture.release)
        _, approved = fixture.stage_and_approve()
        receipt = fixture.receipts / "materialization.json"

        guard.materialize(
            os.fspath(fixture.canonical),
            os.fspath(approved),
            os.fspath(overlay),
            [],
            os.fspath(receipt),
        )
        self.assertEqual(
            (fixture.canonical / ".gitignore").read_text(), "feature\nbeta\nuser\n"
        )
        self.assertEqual(
            (fixture.canonical / "install.sh").read_text(), "feature\ntwo\nuser\n"
        )
        self.assertEqual((fixture.canonical / "backup.user").read_text(), "leave me\n")
        self.assertEqual(
            fixture.git("diff", "--cached", "--binary", cwd=fixture.canonical).stdout,
            index_before,
        )
        guard.verify_installed(
            os.fspath(fixture.canonical),
            os.fspath(approved),
            os.fspath(overlay),
            [],
            os.fspath(receipt),
        )

        guard.rollback_materialization(
            os.fspath(fixture.canonical), os.fspath(receipt), os.fspath(overlay)
        )
        guard.verify_overlay(os.fspath(fixture.canonical), os.fspath(overlay))

    def test_ordered_prior_materialization_is_required(self):
        fixture = self.fixture()
        overlay = fixture.overlay()
        fixture.write(fixture.release, ".gitignore", "first\nbeta\ngamma\n")
        fixture.git("add", ".gitignore", cwd=fixture.release)
        _, approved_one = fixture.stage_and_approve("one")
        receipt_one = fixture.receipts / "one.materialization.json"
        guard.materialize(
            os.fspath(fixture.canonical),
            os.fspath(approved_one),
            os.fspath(overlay),
            [],
            os.fspath(receipt_one),
        )
        fixture.git("commit", "-m", "one", cwd=fixture.release)
        fixture.write(fixture.release, "install.sh", "second\ntwo\nthree\n", 0o755)
        fixture.git("add", "install.sh", cwd=fixture.release)
        _, approved_two = fixture.stage_and_approve("two")

        with self.assertRaisesRegex(
            guard.GuardError, "expected state|continue the materialization chain"
        ):
            guard.materialize(
                os.fspath(fixture.canonical),
                os.fspath(approved_two),
                os.fspath(overlay),
                [],
                os.fspath(fixture.receipts / "bad.json"),
            )

        receipt_two = fixture.receipts / "two.materialization.json"
        guard.materialize(
            os.fspath(fixture.canonical),
            os.fspath(approved_two),
            os.fspath(overlay),
            [os.fspath(receipt_one)],
            os.fspath(receipt_two),
        )
        guard.verify_installed(
            os.fspath(fixture.canonical),
            os.fspath(approved_two),
            os.fspath(overlay),
            [os.fspath(receipt_one)],
            os.fspath(receipt_two),
        )

    def _approved_commit(self, fixture: GitFixture) -> tuple[Path, Path]:
        overlay = fixture.overlay()
        fixture.write(fixture.release, "install.sh", "release\ntwo\nthree\n", 0o755)
        fixture.git("add", "install.sh", cwd=fixture.release)
        _, approved = fixture.stage_and_approve("public")
        fixture.git("commit", "-m", "approved public tree", cwd=fixture.release)
        return overlay, approved

    def test_public_commit_rejects_detached_and_wrong_ref_then_records_attached_ref(self):
        fixture = self.fixture()
        overlay, approved = self._approved_commit(fixture)
        receipt = fixture.receipts / "commit.json"
        fixture.git("switch", "--detach", cwd=fixture.release)
        with self.assertRaisesRegex(guard.GuardError, "detached or attached"):
            guard.record_public_commit(
                os.fspath(fixture.release),
                os.fspath(fixture.canonical),
                guard.PUBLIC_REF,
                os.fspath(approved),
                os.fspath(overlay),
                "Daniel",
                os.fspath(receipt),
                None,
                [],
            )
        fixture.git("switch", "-c", "wrong-ref", cwd=fixture.release)
        with self.assertRaisesRegex(guard.GuardError, "wrong ref"):
            guard.record_public_commit(
                os.fspath(fixture.release),
                os.fspath(fixture.canonical),
                guard.PUBLIC_REF,
                os.fspath(approved),
                os.fspath(overlay),
                "Daniel",
                os.fspath(receipt),
                None,
                [],
            )
        fixture.git("switch", "release/effort-510-v16", cwd=fixture.release)
        guard.record_public_commit(
            os.fspath(fixture.release),
            os.fspath(fixture.canonical),
            guard.PUBLIC_REF,
            os.fspath(approved),
            os.fspath(overlay),
            "Daniel",
            os.fspath(receipt),
            None,
            [],
        )
        guard.verify_public_commit(
            os.fspath(fixture.release),
            os.fspath(fixture.canonical),
            guard.PUBLIC_REF,
            os.fspath(receipt),
            os.fspath(overlay),
            "Daniel",
            [],
        )

    def test_public_commit_survives_worktree_removal_but_rejects_moved_ref(self):
        fixture = self.fixture()
        overlay, approved = self._approved_commit(fixture)
        receipt = fixture.receipts / "commit.json"
        guard.record_public_commit(
            os.fspath(fixture.release),
            os.fspath(fixture.canonical),
            guard.PUBLIC_REF,
            os.fspath(approved),
            os.fspath(overlay),
            "Daniel",
            os.fspath(receipt),
            None,
            [],
        )
        fixture.git("worktree", "remove", os.fspath(fixture.release), cwd=fixture.canonical)
        self.assertFalse(fixture.release.exists())
        guard.verify_public_commit(
            os.fspath(fixture.release),
            os.fspath(fixture.canonical),
            guard.PUBLIC_REF,
            os.fspath(receipt),
            os.fspath(overlay),
            "Daniel",
            [],
        )

        fixture.git(
            "worktree",
            "add",
            os.fspath(fixture.release),
            "release/effort-510-v16",
            cwd=fixture.canonical,
        )
        fixture.write(fixture.release, "later.txt", "moves ref\n")
        fixture.git("add", "later.txt", cwd=fixture.release)
        fixture.git("commit", "-m", "move release ref", cwd=fixture.release)
        with self.assertRaisesRegex(guard.GuardError, "release ref moved"):
            guard.verify_public_commit(
                os.fspath(fixture.release),
                os.fspath(fixture.canonical),
                guard.PUBLIC_REF,
                os.fspath(receipt),
                os.fspath(overlay),
                "Daniel",
                [],
            )

    def test_run_readonly_uses_exact_cwd_allows_venv_and_detects_hash_only_change(self):
        root = self.root / "readonly"
        cwd = root / "nested"
        cwd.mkdir(parents=True)
        (root / "data.txt").write_text("AAAA", encoding="utf-8")
        (root / ".venv").mkdir()
        command = [
            sys.executable,
            "-c",
            "import os,pathlib; assert pathlib.Path.cwd() == pathlib.Path(os.environ['EXPECTED']); pathlib.Path('../.venv/cache').write_text('ignored')",
        ]
        with mock.patch.dict(os.environ, {"EXPECTED": os.fspath(cwd)}):
            self.assertEqual(
                guard.run_readonly(os.fspath(root), os.fspath(cwd), [], command), 0
            )

        mutator = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('data.txt').write_text('BBBB')",
        ]
        with self.assertRaisesRegex(guard.GuardError, "mutated read-only root"):
            guard.run_readonly(os.fspath(root), os.fspath(root), [], mutator)


if __name__ == "__main__":
    unittest.main()
