from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "project_harness.py"
SPEC = importlib.util.spec_from_file_location("project_harness_validator_views", SCRIPT)
assert SPEC and SPEC.loader
project_harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = project_harness
SPEC.loader.exec_module(project_harness)


class ValidatorViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.git_executable = shutil.which("git")
        if not self.git_executable:
            self.skipTest("git is required")
        self.temporary = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "project"
        self.root.mkdir()
        self.git_config = self.sandbox / "gitconfig"
        subprocess.run(
            [self.git_executable, "config", "--file", str(self.git_config), "user.name", "Harness Tests"],
            check=True,
        )
        subprocess.run(
            [self.git_executable, "config", "--file", str(self.git_config), "user.email", "harness@example.invalid"],
            check=True,
        )
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(self.git_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=False,
        )
        self.env_patch.start()
        self.git("init", "-b", "main")
        source = self.root / "src" / "app.txt"
        source.parent.mkdir()
        source.write_text("baseline\n", encoding="utf-8")
        self.git("add", "--", "src/app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "baseline")
        initialized = self.cli("init", "--project-root", str(self.root), "--project-name", "View Test")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.git("add", "--", "AGENTS.md", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "governance baseline")
        self.base_commit = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temporary.cleanup()

    def git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(self.root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )

    def create_v2_bundle(self) -> str:
        git = project_harness.require_git()
        operation_id = "OP-" + uuid.uuid4().hex
        plan = project_harness.build_reserve_iteration_plan(
            self.root,
            git,
            title="V2 validator fixture",
            operation_id=operation_id,
            base_ref="refs/heads/main",
            governance_ref="refs/heads/main",
        )
        self.assertFalse(plan.blocking_reasons)
        journal, _ = project_harness.reserve_iteration(plan, git, self.root)
        self.assertEqual(journal.iteration, "001")
        number, operations = project_harness.build_new_iteration_operations(
            self.root,
            "V2 validator fixture",
            datetime.now().astimezone(),
            self.base_commit,
            "refs/heads/main",
        )
        self.assertEqual(number, "001")
        project_harness.apply_operations(self.root, operations)
        return number

    def ref_snapshot(self) -> str:
        return self.git(
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)",
        ).stdout

    def test_v2_bundle_validates_in_live_and_committed_views_without_writes(self) -> None:
        number = self.create_v2_bundle()
        live = project_harness.collect_validation(self.root)
        self.assertEqual(live.errors, [])

        self.git("add", "--", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "committed v2 bundle")
        commit = self.git("rev-parse", "HEAD").stdout.strip()
        entries = project_harness.read_committed_governance_entries(
            project_harness.require_git(), self.root, commit
        )
        before = {
            "head": commit,
            "refs": self.ref_snapshot(),
            "status": self.git("status", "--porcelain=v1", "-uall").stdout,
            "worktrees": self.git("worktree", "list", "--porcelain").stdout,
            "index": hashlib.sha256((self.root / ".git" / "index").read_bytes()).hexdigest(),
        }
        calls: list[tuple[str, ...]] = []
        original_run_git = project_harness.run_git

        def recording_run_git(git: str, root: Path, arguments: list[str] | tuple[str, ...], **kwargs):
            calls.append(tuple(arguments))
            return original_run_git(git, root, arguments, **kwargs)

        with mock.patch.object(project_harness, "run_git", side_effect=recording_run_git), mock.patch.object(
            project_harness.tempfile,
            "TemporaryDirectory",
            side_effect=AssertionError("committed-tree validation must not create a temporary checkout"),
        ):
            committed = project_harness.collect_committed_governance_validation(
                project_harness.require_git(), self.root, commit, entries
            )

        self.assertEqual(committed.errors, [])
        self.assertFalse(any(call and call[0] == "worktree" for call in calls), calls)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before["head"])
        self.assertEqual(self.ref_snapshot(), before["refs"])
        self.assertEqual(self.git("status", "--porcelain=v1", "-uall").stdout, before["status"])
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout, before["worktrees"])
        self.assertEqual(
            hashlib.sha256((self.root / ".git" / "index").read_bytes()).hexdigest(),
            before["index"],
        )
        evidence = project_harness.read_iteration_base_compat(
            project_harness.require_git(), self.root, number
        )
        self.assertEqual(evidence and evidence["format"], "v2")
        self.assertEqual(evidence and evidence["commit"], self.base_commit)
        self.assertEqual(evidence and evidence["branch"], "refs/heads/main")

    def test_v2_committed_view_rejects_prd_metadata_drift_against_allocation_metadata(self) -> None:
        number = self.create_v2_bundle()
        prd = self.root / "harness" / "iterations" / number / f"prd-{number}.md"
        text = prd.read_text(encoding="utf-8")
        text = text.replace(
            f"- Git 基线：`{self.base_commit}`",
            f"- Git 基线：`{'0' * len(self.base_commit)}`",
            1,
        ).replace(
            "- Git 分支：`refs/heads/main`",
            f"- Git 分支：`refs/project-harness/v2/iterations/{number}/final`",
            1,
        )
        prd.write_text(text, encoding="utf-8")
        self.git("add", "--", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "invalid committed metadata fixture")
        commit = self.git("rev-parse", "HEAD").stdout.strip()
        git = project_harness.require_git()
        entries = project_harness.read_committed_governance_entries(git, self.root, commit)

        report = project_harness.collect_committed_governance_validation(
            git, self.root, commit, entries
        )

        codes = {issue.code for issue in report.errors}
        self.assertIn("iteration-base-anchor-drift", codes)
        self.assertIn("iteration-branch-anchor-drift", codes)

    def test_v2_anchor_requires_matching_allocation_metadata(self) -> None:
        number = self.create_v2_bundle()
        allocation_ref = project_harness.v2_allocation_ref(number)
        allocation_object = self.git("rev-parse", allocation_ref).stdout.strip()
        metadata = json.loads(self.git("cat-file", "-p", allocation_object).stdout)
        metadata["base_commit"] = "0" * len(self.base_commit)
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        written = subprocess.run(
            [self.git_executable, "-C", str(self.root), "hash-object", "-w", "--stdin"],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=encoded,
        ).stdout.strip()
        self.git("update-ref", allocation_ref, written, allocation_object)

        report = project_harness.collect_validation(self.root)

        anchor_issues = [issue for issue in report.errors if issue.code == "iteration-base-anchor"]
        self.assertTrue(anchor_issues)
        self.assertIn("metadata base differs", anchor_issues[0].message)

    def test_legacy_anchor_remains_compatible_with_shared_validator(self) -> None:
        result = self.cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Legacy compatibility",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = project_harness.read_iteration_base_compat(
            project_harness.require_git(), self.root, "001"
        )
        self.assertEqual(evidence and evidence["format"], "legacy")
        self.assertEqual(evidence and evidence["commit"], self.base_commit)
        self.assertEqual(evidence and evidence["branch"], "refs/heads/main")
        self.assertEqual(project_harness.collect_validation(self.root).errors, [])


if __name__ == "__main__":
    unittest.main()
