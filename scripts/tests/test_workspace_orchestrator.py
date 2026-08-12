from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parents[1] / "harness_workspace.py"


class WorkspaceOrchestratorTests(unittest.TestCase):
    """Black-box safety contracts for the Local/worktree vertical slice."""

    maxDiff = None

    def setUp(self) -> None:
        self.git_executable = shutil.which("git")
        if not self.git_executable:
            self.skipTest("git is required")
        self.temporary = tempfile.TemporaryDirectory(prefix="harness workspace tests ")
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "primary project with spaces"
        self.root.mkdir()
        self.pool = self.sandbox / "linked worktrees with spaces"
        self.pool.mkdir()
        self.git_config = self.sandbox / "isolated gitconfig"
        subprocess.run(
            [self.git_executable, "config", "--file", str(self.git_config), "user.name", "Workspace Tests"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.git_executable, "config", "--file", str(self.git_config), "user.email", "workspace@example.invalid"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": str(self.git_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        self.git("init", "-b", "main")
        (self.root / "app.txt").write_text("committed baseline\n", encoding="utf-8")
        self.git("add", "--", "app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "baseline")
        (self.root / "governance.txt").write_text("committed governance\n", encoding="utf-8")
        self.git("add", "--", "governance.txt")
        self.git("commit", "--no-gpg-sign", "-m", "governance baseline")
        self.base_commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.previous_commit = self.git("rev-parse", "HEAD^").stdout.strip()
        self.base_tree = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        self.allocations: dict[str, dict[str, str]] = {}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(cwd or self.root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )

    def payload(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"CLI did not return one JSON object: {result.stdout!r}, stderr={result.stderr!r}: {exc}")
        self.assertIsInstance(value, dict)
        self.assertEqual(value.get("schema_version"), "harness-lite.workspace-operation/v1")
        self.assertIs(value.get("pushed"), False)
        return value

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        value = self.payload(result)
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}")
        self.assertNotIn(value.get("phase"), {"blocked", "error"})
        return value

    def assert_blocked(self, result: subprocess.CompletedProcess[str], code: str) -> dict[str, Any]:
        value = self.payload(result)
        self.assertNotEqual(result.returncode, 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}")
        codes = {item.get("code") for item in value.get("blocking_reasons", [])}
        self.assertIn(code, codes, value)
        return value

    def snapshot_files(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in sorted(self.sandbox.rglob("*")):
            if path.is_file():
                relative = path.relative_to(self.sandbox).as_posix()
                snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snapshot

    def worktrees(self) -> str:
        return self.git("worktree", "list", "--porcelain").stdout

    def branches(self) -> dict[str, str]:
        result = self.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
        return dict(line.split(" ", 1) for line in result.stdout.splitlines() if line)

    def reserve(self, iteration: str, *, base_commit: str | None = None) -> dict[str, str]:
        commit = base_commit or self.base_commit
        operation = f"OP-{uuid.uuid4().hex}"
        metadata = {
            "schema_version": "harness-lite.allocation-metadata.v1",
            "operation_id": operation,
            "plan_digest": hashlib.sha256(f"plan-{iteration}".encode()).hexdigest(),
            "iteration": iteration,
            "base_commit": commit,
            "base_branch": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.base_commit,
            "governance_tree": self.base_tree,
            "principle_sha256": hashlib.sha256(b"approved principles").hexdigest(),
            "title": f"Workspace fixture {iteration}",
        }
        raw = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        blob = self.git("hash-object", "-w", "--stdin", input_text=raw).stdout.strip()
        allocation_ref = f"refs/project-harness/v2/allocations/{iteration}"
        base_ref = f"refs/project-harness/v2/iterations/{iteration}/base"
        self.git("update-ref", allocation_ref, blob)
        self.git("update-ref", base_ref, commit)
        value = {"base_ref": base_ref, "allocation_ref": allocation_ref, "blob": blob, "commit": commit}
        self.allocations[iteration] = value
        return value

    def plan_activate(
        self,
        iteration: str,
        topology: str,
        branch: str,
        path: Path,
        owner: str,
        *,
        generation: int = 1,
        operation_id: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        arguments = [
            "plan",
            "activate",
            "--project-root",
            str(self.root),
            "--iteration",
            iteration,
            "--execution-topology",
            topology,
            "--base-ref",
            self.allocations[iteration]["base_ref"],
            "--branch-ref",
            branch,
            "--worktree-path",
            str(path),
            "--owner",
            owner,
            "--lease-generation",
            str(generation),
        ]
        if operation_id:
            arguments.extend(["--operation-id", operation_id])
        arguments.append("--json")
        result = self.run_cli(*arguments)
        return result, self.payload(result)

    def apply_activate(
        self,
        plan: dict[str, Any],
        topology: str,
        branch: str,
        path: Path,
        owner: str,
        *,
        generation: int = 1,
        project_root: Path | None = None,
        base_ref: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "activate",
            "--project-root",
            str(project_root or self.root),
            "--iteration",
            str(plan["iteration"]),
            "--execution-topology",
            topology,
            "--base-ref",
            base_ref or self.allocations[str(plan["iteration"])]["base_ref"],
            "--branch-ref",
            branch,
            "--worktree-path",
            str(path),
            "--owner",
            owner,
            "--lease-generation",
            str(generation),
            "--operation-id",
            str(plan["operation_id"]),
            "--accept-plan-digest",
            str(plan["plan_digest"]),
            "--json",
        )

    def activate_local(self, iteration: str = "001", owner: str = "task-a") -> dict[str, Any]:
        self.reserve(iteration)
        result, plan = self.plan_activate(iteration, "local", "refs/heads/main", self.root, owner)
        self.assertEqual(result.returncode, 0, plan)
        applied = self.assert_success(
            self.apply_activate(plan, "local", "refs/heads/main", self.root, owner)
        )
        self.assertEqual(applied["topology"]["phase"], "SINGLE_LOCAL")
        return applied

    def create_b_plan(self) -> tuple[dict[str, Any], Path, str]:
        self.reserve("002")
        target = self.pool / "PRD 002 workspace"
        branch = "refs/heads/prd/002-isolated"
        result, plan = self.plan_activate("002", "worktree", branch, target, "task-b")
        self.assertEqual(result.returncode, 0, plan)
        return plan, target, branch

    def dirty_a(self) -> None:
        (self.root / "app.txt").write_text("A owns this uncommitted edit\n", encoding="utf-8")
        (self.root / "a-staged.txt").write_text("A staged state\n", encoding="utf-8")
        self.git("add", "--", "a-staged.txt")
        (self.root / "a-untracked.txt").write_text("A untracked state\n", encoding="utf-8")

    def plan_bind_local_branch(
        self,
        *,
        new_branch: str = "refs/heads/prd/001-local-a",
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        result = self.run_cli(
            "plan",
            "bind-local-branch",
            "--project-root",
            str(self.root),
            "--iteration",
            "001",
            "--owner",
            "task-a",
            "--lease-generation",
            "1",
            "--worktree-path",
            str(self.root),
            "--base-commit",
            self.base_commit,
            "--new-branch-ref",
            new_branch,
            "--json",
        )
        return result, self.payload(result)

    def apply_bind_local_branch(
        self,
        plan: dict[str, Any],
        *,
        new_branch: str = "refs/heads/prd/001-local-a",
        generation: int = 1,
        base_commit: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "bind-local-branch",
            "--project-root",
            str(self.root),
            "--iteration",
            "001",
            "--owner",
            "task-a",
            "--lease-generation",
            str(generation),
            "--worktree-path",
            str(self.root),
            "--base-commit",
            base_commit or self.base_commit,
            "--new-branch-ref",
            new_branch,
            "--operation-id",
            str(plan["operation_id"]),
            "--accept-plan-digest",
            str(plan["plan_digest"]),
            "--json",
        )

    def test_plan_is_zero_write_and_before_notify_is_complete(self) -> None:
        self.activate_local()
        self.dirty_a()
        self.reserve("002")
        target = self.pool / "PRD 002 workspace"
        before = self.snapshot_files()

        result, plan = self.plan_activate(
            "002",
            "worktree",
            "refs/heads/prd/002-isolated",
            target,
            "task-b",
        )

        self.assertEqual(result.returncode, 0, plan)
        self.assertEqual(self.snapshot_files(), before)
        self.assertEqual(plan["action_level"], "notify")
        self.assertEqual(plan["notification_phase"], "before")
        notify = plan["notification"]
        self.assertEqual(notify["prd"], "PRD-002")
        self.assertEqual(notify["reason_code"], "parallel-prd-lazy-worktree")
        self.assertEqual(notify["base"]["commit"], self.base_commit)
        self.assertEqual(notify["branch"]["ref"], "refs/heads/prd/002-isolated")
        self.assertEqual(Path(notify["worktree"]["path"]).resolve(), target.resolve())
        self.assertIn(str(self.root), notify["effect_on_existing_prds"]["existing_paths"])
        self.assertIs(notify["effect_on_existing_prds"]["moved"], False)
        self.assertIs(notify["effect_on_existing_prds"]["committed"], False)
        self.assertIs(notify["effect_on_existing_prds"]["stashed"], False)
        self.assertEqual(notify["remote"], {"force": False, "involved": False, "pushed": False})
        self.assertRegex(notify["runtime_namespace"], r"^hl-[0-9a-f]{10}-prd-002$")

    def test_real_one_to_two_adds_b_without_moving_or_modifying_dirty_a(self) -> None:
        self.activate_local()
        self.dirty_a()
        plan, target, branch = self.create_b_plan()
        a_status = self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout
        a_cached = self.git("diff", "--cached", "--binary").stdout
        a_files = {
            name: (self.root / name).read_bytes()
            for name in ("app.txt", "a-staged.txt", "a-untracked.txt")
        }
        a_head = self.git("rev-parse", "HEAD").stdout.strip()
        stash_before = self.git("stash", "list").stdout
        worktrees_before = self.worktrees()

        result = self.apply_activate(plan, "worktree", branch, target, "task-b")
        applied = self.assert_success(result)

        self.assertEqual(applied["action_level"], "notify")
        self.assertEqual(applied["notification_phase"], "after")
        self.assertTrue(applied["created_now"])
        self.assertFalse(applied["idempotent_replay"])
        self.assertEqual(applied["topology"]["phase"], "PARALLEL")
        notify = applied["notification"]
        self.assertEqual(Path(notify["actual_path"]).resolve(), target.resolve())
        self.assertEqual(notify["actual_branch_ref"], branch)
        self.assertEqual(notify["actual_head"], self.base_commit)
        self.assertTrue(notify["effect_on_existing_prds"]["source_state_preserved"])
        self.assertFalse(notify["remote"]["involved"])
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "app.txt").read_text(encoding="utf-8"), "committed baseline\n")
        self.assertFalse((target / "a-staged.txt").exists())
        self.assertFalse((target / "a-untracked.txt").exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=target).stdout.strip(), self.base_commit)
        self.assertEqual(self.git("symbolic-ref", "-q", "HEAD", cwd=target).stdout.strip(), branch)
        self.assertEqual(self.git("status", "--porcelain=v1", cwd=target).stdout, "")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), a_head)
        self.assertEqual(self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout, a_status)
        self.assertEqual(self.git("diff", "--cached", "--binary").stdout, a_cached)
        self.assertEqual(self.git("stash", "list").stdout, stash_before)
        for name, content in a_files.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.assertIn(self.root.as_posix(), self.worktrees())
        self.assertIn(target.as_posix(), self.worktrees())
        self.assertIn(self.root.as_posix(), worktrees_before)

    def test_apply_is_idempotent_and_does_not_duplicate_worktree_branch_or_lease(self) -> None:
        self.activate_local()
        plan, target, branch = self.create_b_plan()
        first = self.assert_success(self.apply_activate(plan, "worktree", branch, target, "task-b"))
        self.assertTrue(first["created_now"])
        before_files = self.snapshot_files()
        before_worktrees = self.worktrees()
        before_branches = self.branches()

        replay = self.assert_success(self.apply_activate(plan, "worktree", branch, target, "task-b"))

        self.assertFalse(replay["created_now"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["journal_phase"], "READY")
        self.assertEqual(self.snapshot_files(), before_files)
        self.assertEqual(self.worktrees(), before_worktrees)
        self.assertEqual(self.branches(), before_branches)
        status = self.assert_success(
            self.run_cli("status", "--project-root", str(self.root), "--json")
        )
        self.assertEqual(len(status["writer_leases"]), 2)
        self.assertEqual(len([item for item in status["worktrees"] if Path(item["path"]).resolve() == target.resolve()]), 1)

    def test_wrong_digest_branch_and_generation_block_before_any_workspace_write(self) -> None:
        self.activate_local()
        plan, target, branch = self.create_b_plan()
        before = self.snapshot_files()
        worktrees_before = self.worktrees()
        branches_before = self.branches()

        wrong_digest = dict(plan)
        wrong_digest["plan_digest"] = "0" * 64
        self.assert_blocked(
            self.apply_activate(wrong_digest, "worktree", branch, target, "task-b"),
            "accepted-plan-digest-mismatch",
        )
        self.assert_blocked(
            self.apply_activate(plan, "worktree", "refs/heads/prd/wrong", target, "task-b"),
            "accepted-plan-digest-mismatch",
        )
        self.assert_blocked(
            self.apply_activate(plan, "worktree", branch, target, "task-b", generation=2),
            "accepted-plan-digest-mismatch",
        )

        self.assertEqual(self.snapshot_files(), before)
        self.assertEqual(self.worktrees(), worktrees_before)
        self.assertEqual(self.branches(), branches_before)
        self.assertFalse(target.exists())

    def test_base_drift_blocks_apply_before_branch_lease_or_worktree(self) -> None:
        self.activate_local()
        plan, target, branch = self.create_b_plan()
        self.git("update-ref", self.allocations["002"]["base_ref"], self.previous_commit)
        after_drift = self.snapshot_files()

        blocked = self.assert_blocked(
            self.apply_activate(plan, "worktree", branch, target, "task-b"),
            "workspace-error",
        )

        self.assertIn("disagree", blocked["blocking_reasons"][0]["message"])
        self.assertEqual(self.snapshot_files(), after_drift)
        self.assertNotIn(branch, self.branches())
        self.assertFalse(target.exists())

    def test_wrong_project_root_is_rejected_without_registry_or_git_write(self) -> None:
        self.reserve("001")
        result, plan = self.plan_activate("001", "local", "refs/heads/main", self.root, "task-a")
        self.assertEqual(result.returncode, 0, plan)
        nested = self.root / "nested"
        nested.mkdir()
        before = self.snapshot_files()

        blocked = self.assert_blocked(
            self.apply_activate(
                plan,
                "local",
                "refs/heads/main",
                self.root,
                "task-a",
                project_root=nested,
            ),
            "workspace-error",
        )

        self.assertIn("worktree root exactly", blocked["blocking_reasons"][0]["message"])
        self.assertEqual(self.snapshot_files(), before)

    def test_guard_blocks_wrong_owner_generation_path_branch_base_and_anchor_drift(self) -> None:
        self.activate_local()
        plan, target, branch = self.create_b_plan()
        self.assert_success(self.apply_activate(plan, "worktree", branch, target, "task-b"))

        def guard(*, owner: str = "task-b", generation: int = 1, path: Path = target,
                  branch_ref: str = branch, base: str = self.base_commit) -> subprocess.CompletedProcess[str]:
            return self.run_cli(
                "guard",
                "--project-root",
                str(self.root),
                "--iteration",
                "002",
                "--owner",
                owner,
                "--lease-generation",
                str(generation),
                "--worktree-path",
                str(path),
                "--branch-ref",
                branch_ref,
                "--base-commit",
                base,
                "--json",
            )

        self.assert_success(guard())
        before = self.snapshot_files()
        self.assert_blocked(guard(owner="other-task"), "lease-owner-mismatch")
        self.assert_blocked(guard(generation=9), "lease-generation-mismatch")
        self.assert_blocked(guard(path=self.pool / "wrong path"), "lease-path-mismatch")
        self.assert_blocked(guard(branch_ref="refs/heads/prd/wrong"), "lease-branch-mismatch")
        self.assert_blocked(guard(base=self.previous_commit), "lease-base-mismatch")
        self.assertEqual(self.snapshot_files(), before)

        self.git("update-ref", self.allocations["002"]["base_ref"], self.previous_commit)
        drift_snapshot = self.snapshot_files()
        self.assert_blocked(guard(), "base-anchor-drift")
        self.assertEqual(self.snapshot_files(), drift_snapshot)

    def test_one_writer_lease_blocks_second_owner_and_first_prd_cannot_start_as_worktree(self) -> None:
        self.reserve("001")
        first_target = self.pool / "incorrect first worktree"
        result, first_plan = self.plan_activate(
            "001",
            "worktree",
            "refs/heads/prd/001-wrong",
            first_target,
            "task-a",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "worktree-requires-existing-writer",
            {item["code"] for item in first_plan["blocking_reasons"]},
        )
        self.assertFalse(first_target.exists())
        self.activate_local(owner="task-a")
        before = self.snapshot_files()

        result, conflict = self.plan_activate(
            "001",
            "local",
            "refs/heads/main",
            self.root,
            "task-other",
            operation_id=f"OP-{uuid.uuid4().hex}",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writer-lease-held", {item["code"] for item in conflict["blocking_reasons"]})
        self.assertEqual(self.snapshot_files(), before)

    def test_two_to_one_enters_draining_without_migrating_or_removing_survivor(self) -> None:
        self.activate_local()
        self.dirty_a()
        plan, target, branch = self.create_b_plan()
        self.assert_success(self.apply_activate(plan, "worktree", branch, target, "task-b"))
        worktrees_before = self.worktrees()
        branches_before = self.branches()
        a_status_before = self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout

        release_plan_result = self.run_cli(
            "plan",
            "release",
            "--project-root",
            str(self.root),
            "--iteration",
            "002",
            "--owner",
            "task-b",
            "--lease-generation",
            "1",
            "--worktree-path",
            str(target),
            "--branch-ref",
            branch,
            "--base-commit",
            self.base_commit,
            "--json",
        )
        release_plan = self.assert_success(release_plan_result)
        before_release_plan_files = self.snapshot_files()
        # Re-planning uses a new operation ID but remains read-only.
        second_plan_result = self.run_cli(
            "plan",
            "release",
            "--project-root",
            str(self.root),
            "--iteration",
            "002",
            "--owner",
            "task-b",
            "--lease-generation",
            "1",
            "--worktree-path",
            str(target),
            "--branch-ref",
            branch,
            "--base-commit",
            self.base_commit,
            "--json",
        )
        self.assert_success(second_plan_result)
        self.assertEqual(self.snapshot_files(), before_release_plan_files)

        released = self.assert_success(
            self.run_cli(
                "release",
                "--project-root",
                str(self.root),
                "--iteration",
                "002",
                "--owner",
                "task-b",
                "--lease-generation",
                "1",
                "--worktree-path",
                str(target),
                "--branch-ref",
                branch,
                "--base-commit",
                self.base_commit,
                "--operation-id",
                release_plan["operation_id"],
                "--accept-plan-digest",
                release_plan["plan_digest"],
                "--json",
            )
        )

        self.assertEqual(released["topology"]["phase"], "DRAINING")
        self.assertEqual(released["topology"]["active_count"], 1)
        self.assertEqual(released["notification"]["survivor_policy"], "stay-in-place")
        self.assertFalse(released["notification"]["worktree_removed"])
        self.assertFalse(released["notification"]["branch_deleted"])
        self.assertFalse(released["notification"]["survivor_migrated"])
        self.assertEqual(self.worktrees(), worktrees_before)
        self.assertEqual(self.branches(), branches_before)
        self.assertTrue(target.is_dir())
        self.assertEqual(self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout, a_status_before)
        status = self.assert_success(self.run_cli("status", "--project-root", str(self.root), "--json"))
        self.assertEqual(status["topology"]["phase"], "DRAINING")
        self.assertEqual([item["iteration"] for item in status["writer_leases"]], ["001"])
        replay_snapshot = self.snapshot_files()
        replay = self.assert_success(
            self.run_cli(
                "release",
                "--project-root",
                str(self.root),
                "--iteration",
                "002",
                "--owner",
                "task-b",
                "--lease-generation",
                "1",
                "--worktree-path",
                str(target),
                "--branch-ref",
                branch,
                "--base-commit",
                self.base_commit,
                "--operation-id",
                release_plan["operation_id"],
                "--accept-plan-digest",
                release_plan["plan_digest"],
                "--json",
            )
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertFalse(replay["created_now"])
        self.assertEqual(self.snapshot_files(), replay_snapshot)

    def test_three_active_prds_have_unique_paths_leases_and_runtime_namespaces(self) -> None:
        self.activate_local()
        plan_b, target_b, branch_b = self.create_b_plan()
        self.assert_success(self.apply_activate(plan_b, "worktree", branch_b, target_b, "task-b"))
        self.reserve("003")
        target_c = self.pool / "PRD 003 workspace"
        branch_c = "refs/heads/prd/003-isolated"
        result, plan_c = self.plan_activate("003", "worktree", branch_c, target_c, "task-c")
        self.assertEqual(result.returncode, 0, plan_c)
        self.assert_success(self.apply_activate(plan_c, "worktree", branch_c, target_c, "task-c"))

        status = self.assert_success(self.run_cli("status", "--project-root", str(self.root), "--json"))

        self.assertEqual(status["topology"]["phase"], "PARALLEL")
        self.assertEqual(status["topology"]["active_count"], 3)
        leases = status["writer_leases"]
        self.assertEqual({item["iteration"] for item in leases}, {"001", "002", "003"})
        self.assertEqual(len({item["worktree_path"] for item in leases}), 3)
        self.assertEqual(len({item["runtime_namespace"] for item in leases}), 3)
        self.assertTrue(all(item["guard_valid"] for item in leases))

    def test_b_first_releases_main_by_atomic_in_place_bind_with_complete_notify_and_idempotency(self) -> None:
        self.activate_local()
        self.dirty_a()
        plan_b, target_b, branch_b = self.create_b_plan()
        self.assert_success(self.apply_activate(plan_b, "worktree", branch_b, target_b, "task-b"))
        before_plan_files = self.snapshot_files()

        plan_result, bind_plan = self.plan_bind_local_branch()

        self.assertEqual(plan_result.returncode, 0, bind_plan)
        self.assertEqual(self.snapshot_files(), before_plan_files)
        self.assertEqual(bind_plan["action_level"], "notify")
        self.assertEqual(bind_plan["notification_phase"], "before")
        notify_before = bind_plan["notification"]
        self.assertEqual(notify_before["prd"], "PRD-001")
        self.assertEqual(notify_before["reason_code"], "main-release-for-earlier-integration")
        self.assertEqual(notify_before["base"]["commit"], self.base_commit)
        self.assertEqual(notify_before["branch"]["from_ref"], "refs/heads/main")
        self.assertEqual(notify_before["branch"]["to_ref"], "refs/heads/prd/001-local-a")
        self.assertEqual(Path(notify_before["worktree"]["path"]).resolve(), self.root.resolve())
        self.assertFalse(notify_before["worktree"]["will_move"])
        self.assertFalse(notify_before["effect_on_local_prd"]["commit_will_be_created"])
        self.assertFalse(notify_before["effect_on_local_prd"]["stash_will_be_created"])
        self.assertFalse(notify_before["effect_on_local_prd"]["files_will_move"])
        self.assertFalse(notify_before["main_release"]["main_ref_will_move"])
        self.assertFalse(notify_before["remote"]["involved"])

        source_bytes = {
            name: (self.root / name).read_bytes()
            for name in ("app.txt", "a-staged.txt", "a-untracked.txt")
        }
        raw_index_path = Path(self.git("rev-parse", "--git-path", "index").stdout.strip())
        if not raw_index_path.is_absolute():
            raw_index_path = self.root / raw_index_path
        index_before = raw_index_path.read_bytes()
        index_entries_before = self.git("ls-files", "--stage", "-z").stdout
        status_before = self.git(
            "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching"
        ).stdout
        cached_before = self.git("diff", "--cached", "--binary").stdout
        stash_before = self.git("stash", "list").stdout
        head_before = self.git("rev-parse", "HEAD").stdout.strip()
        main_before = self.git("rev-parse", "refs/heads/main").stdout.strip()
        worktree_paths_before = [
            line.removeprefix("worktree ")
            for line in self.worktrees().splitlines()
            if line.startswith("worktree ")
        ]
        process_cwd_before = Path.cwd()

        applied = self.assert_success(self.apply_bind_local_branch(bind_plan))

        self.assertEqual(Path.cwd(), process_cwd_before)
        self.assertEqual(applied["action_level"], "notify")
        self.assertEqual(applied["notification_phase"], "after")
        self.assertTrue(applied["created_now"])
        self.assertFalse(applied["idempotent_replay"])
        notify_after = applied["notification"]
        self.assertTrue(notify_after["main_released"])
        self.assertFalse(notify_after["main_ref_moved"])
        self.assertEqual(notify_after["actual_branch_ref"], "refs/heads/prd/001-local-a")
        self.assertEqual(notify_after["writer_lease"]["generation_before"], 1)
        self.assertEqual(notify_after["writer_lease"]["generation_after"], 2)
        self.assertTrue(all(notify_after["preservation"].values()), notify_after)
        self.assertFalse(notify_after["effect_on_local_prd"]["committed"])
        self.assertFalse(notify_after["effect_on_local_prd"]["stashed"])
        self.assertFalse(notify_after["effect_on_local_prd"]["files_moved"])
        self.assertFalse(notify_after["effect_on_local_prd"]["worktree_moved"])
        self.assertEqual(self.git("symbolic-ref", "-q", "HEAD").stdout.strip(), "refs/heads/prd/001-local-a")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head_before)
        self.assertEqual(self.git("rev-parse", "refs/heads/main").stdout.strip(), main_before)
        self.assertEqual(self.git("rev-parse", "refs/heads/prd/001-local-a").stdout.strip(), self.base_commit)
        self.assertEqual(raw_index_path.read_bytes(), index_before)
        self.assertEqual(self.git("ls-files", "--stage", "-z").stdout, index_entries_before)
        self.assertEqual(
            self.git("status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching").stdout,
            status_before,
        )
        self.assertEqual(self.git("diff", "--cached", "--binary").stdout, cached_before)
        self.assertEqual(self.git("stash", "list").stdout, stash_before)
        for name, content in source_bytes.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        worktree_paths_after = [
            line.removeprefix("worktree ")
            for line in self.worktrees().splitlines()
            if line.startswith("worktree ")
        ]
        self.assertEqual(worktree_paths_after, worktree_paths_before)
        status = self.assert_success(self.run_cli("status", "--project-root", str(self.root), "--json"))
        leases = {item["iteration"]: item for item in status["writer_leases"]}
        self.assertEqual(leases["001"]["branch_ref"], "refs/heads/prd/001-local-a")
        self.assertEqual(leases["001"]["generation"], 2)
        self.assertEqual(leases["001"]["operation_id"], bind_plan["operation_id"])
        self.assertEqual(status["topology"]["phase"], "PARALLEL")

        replay_snapshot = self.snapshot_files()
        replay_worktrees = self.worktrees()
        replay = self.assert_success(self.apply_bind_local_branch(bind_plan))
        self.assertFalse(replay["created_now"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.snapshot_files(), replay_snapshot)
        self.assertEqual(self.worktrees(), replay_worktrees)

    def test_bind_local_branch_wrong_identity_and_main_drift_block_before_git_or_lease_write(self) -> None:
        self.activate_local()
        self.dirty_a()
        plan_b, target_b, branch_b = self.create_b_plan()
        self.assert_success(self.apply_activate(plan_b, "worktree", branch_b, target_b, "task-b"))
        result, bind_plan = self.plan_bind_local_branch()
        self.assertEqual(result.returncode, 0, bind_plan)
        before = self.snapshot_files()
        branches_before = self.branches()
        worktrees_before = self.worktrees()

        self.assert_blocked(
            self.apply_bind_local_branch(bind_plan, new_branch="refs/heads/prd/wrong-local"),
            "accepted-plan-digest-mismatch",
        )
        self.assert_blocked(
            self.apply_bind_local_branch(bind_plan, generation=9),
            "accepted-plan-digest-mismatch",
        )
        self.assert_blocked(
            self.apply_bind_local_branch(bind_plan, base_commit=self.previous_commit),
            "accepted-plan-digest-mismatch",
        )
        self.assertEqual(self.snapshot_files(), before)
        self.assertEqual(self.branches(), branches_before)
        self.assertEqual(self.worktrees(), worktrees_before)
        self.assertEqual(self.git("symbolic-ref", "-q", "HEAD").stdout.strip(), "refs/heads/main")

        self.git("update-ref", "refs/heads/main", self.previous_commit)
        drift_snapshot = self.snapshot_files()
        drift_branches = self.branches()
        drift_worktrees = self.worktrees()
        blocked = self.payload(self.apply_bind_local_branch(bind_plan))
        self.assertEqual(blocked["phase"], "blocked", blocked)
        self.assertEqual(self.snapshot_files(), drift_snapshot)
        self.assertEqual(self.branches(), drift_branches)
        self.assertEqual(self.worktrees(), drift_worktrees)
        self.assertNotIn("refs/heads/prd/001-local-a", self.branches())

    def test_inside_checkout_path_and_foreign_branch_are_blocked_during_plan_without_write(self) -> None:
        self.activate_local()
        self.reserve("002")
        inside = self.root / "nested worktree"
        before = self.snapshot_files()
        result, blocked = self.plan_activate(
            "002",
            "worktree",
            "refs/heads/prd/002-inside",
            inside,
            "task-b",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe-worktree-path", {item["code"] for item in blocked["blocking_reasons"]})
        self.assertEqual(self.snapshot_files(), before)

        self.git("branch", "prd/foreign", self.base_commit)
        after_branch = self.snapshot_files()
        result, blocked = self.plan_activate(
            "002",
            "worktree",
            "refs/heads/prd/foreign",
            self.pool / "foreign target",
            "task-b",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch-already-exists", {item["code"] for item in blocked["blocking_reasons"]})
        self.assertEqual(self.snapshot_files(), after_branch)


if __name__ == "__main__":
    unittest.main()
