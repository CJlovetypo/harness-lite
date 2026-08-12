from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.harness_coordinator import plan_route


OWNER = "<!-- managed-by: harness-lite v1 -->\n"


class CoordinatorAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="harness-coordinator-test-")
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        (self.root / "harness" / "iterations" / "001").mkdir(parents=True)
        (self.root / "harness" / "principle.md").write_text(OWNER + "# Principle\n", encoding="utf-8")
        (self.root / "harness" / "iterations" / "001" / "README.md").write_text(OWNER, encoding="utf-8")
        (self.root / "harness" / "iterations" / "001" / "deviation-001.md").write_text(OWNER, encoding="utf-8")
        self.write_authority(approved=True)
        self.git("add", ".")
        self.git("commit", "-m", "governance")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git(
            "update-ref",
            "refs/project-harness/iterations/001/base/refs/heads/main",
            self.head,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    def write_authority(
        self,
        *,
        approved: bool,
        depends_on: str | None = None,
        conflicts_with: str | None = None,
    ) -> None:
        status = "实施中" if approved else "草案"
        approval = "用户明确批准 PRD-001" if approved else "待批准"
        spec_approval = "用户明确批准 SPEC-001" if approved else "待批准"
        auth = "用户明确授权实施" if approved else "未授权"
        dependency = f"- 依赖 PRD：`{depends_on}`\n" if depends_on else ""
        conflict = f"- 冲突 PRD：`{conflicts_with}`\n" if conflicts_with else ""
        prd = (
            OWNER
            + "# PRD-001：Feature\n"
            + f"- 状态：`{status}`\n"
            + f"- 批准依据：{approval}\n"
            + dependency
            + conflict
        )
        spec = (
            OWNER
            + "# SPEC-001：Feature\n"
            + f"- 状态：`{status}`\n"
            + f"- 批准依据：{spec_approval}\n"
            + f"- 实施授权：{auth}\n"
        )
        directory = self.root / "harness" / "iterations" / "001"
        (directory / "prd-001.md").write_text(prd, encoding="utf-8")
        (directory / "spec-001.md").write_text(spec, encoding="utf-8")

    def snapshot(self) -> tuple[str, str, tuple[str, ...]]:
        refs = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout
        status = self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout
        files = tuple(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()))
        return refs, hashlib.sha256(status.encode()).hexdigest(), files

    def test_route_derives_approvals_and_local_topology_without_writing(self) -> None:
        before = self.snapshot()

        plan = plan_route(
            self.root,
            iteration="001",
            read_only=False,
            risk={"localized_impact": True, "straightforward_rollback": True},
            operation_id="OP-" + "1" * 32,
        )

        self.assertEqual(plan.phase, "planned")
        self.assertTrue(plan.authority.prd_approved)
        self.assertTrue(plan.authority.spec_approved)
        self.assertTrue(plan.authority.implementation_authorized)
        self.assertEqual(plan.decision["effective_execution_topology"], "local")
        self.assertEqual(before, self.snapshot())

    def test_unquoted_colon_rich_approval_evidence_is_not_truncated(self) -> None:
        directory = self.root / "harness" / "iterations" / "001"
        spec = (directory / "spec-001.md").read_text(encoding="utf-8")
        spec = spec.replace(
            "- 批准依据：用户明确批准 SPEC-001",
            "- 批准依据：用户于 2026-08-12 明确批准 SPEC-001：包含 merge --no-ff 策略",
        )
        (directory / "spec-001.md").write_text(spec, encoding="utf-8")

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "6" * 32)

        self.assertTrue(plan.authority.spec_approved)
        self.assertNotIn("spec-not-approved", plan.blocking_reasons)

    def test_caller_cannot_override_unapproved_governance_with_booleans(self) -> None:
        self.write_authority(approved=False)

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "2" * 32)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("prd-not-approved", plan.blocking_reasons)
        self.assertIn("spec-not-approved", plan.blocking_reasons)
        self.assertIn("implementation-not-authorized", plan.blocking_reasons)
        self.assertFalse(any(step.get("writes") for step in plan.planned_steps))

    def test_missing_dependency_candidate_blocks_stacked_start(self) -> None:
        dependency = self.root / "harness" / "iterations" / "002"
        dependency.mkdir()
        for name in ("README.md", "deviation-002.md"):
            (dependency / name).write_text(OWNER, encoding="utf-8")
        (dependency / "prd-002.md").write_text(
            OWNER + "# PRD-002：Dependency\n- 状态：`实施中`\n- 批准依据：用户明确批准 PRD-002\n",
            encoding="utf-8",
        )
        (dependency / "spec-002.md").write_text(
            OWNER + "# SPEC-002：Dependency\n- 状态：`实施中`\n- 批准依据：用户明确批准 SPEC-002\n- 实施授权：用户明确授权实施\n",
            encoding="utf-8",
        )
        self.git("add", ".")
        self.git("commit", "-m", "dependency")
        dependency_head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git(
            "update-ref",
            "refs/project-harness/iterations/002/base/refs/heads/main",
            dependency_head,
        )
        self.write_authority(approved=True, depends_on="PRD-002")

        blocked = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "3" * 32)
        self.assertIn("dependency-stable-candidate-missing:002", blocked.blocking_reasons)

        self.git(
            "update-ref",
            "refs/project-harness/v2/iterations/002/candidates/g1",
            dependency_head,
        )
        planned = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "3" * 32)
        self.assertEqual(planned.phase, "planned")
        self.assertEqual(planned.decision["effective_execution_topology"], "stacked-worktree")

    def test_declared_active_conflict_blocks(self) -> None:
        self.write_authority(approved=True, conflicts_with="PRD-002")
        lease = self.root / ".git" / "project-harness" / "workspace" / "v1" / "leases" / "iterations"
        lease.mkdir(parents=True)
        (lease / "002.json").write_text('{"iteration":"002"}', encoding="utf-8")

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "4" * 32)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("declared-conflict-active:002", plan.blocking_reasons)

    def test_dependency_generation_change_marks_dependent_baseline_stale(self) -> None:
        dependency = self.root / "harness" / "iterations" / "002"
        dependency.mkdir()
        for name in ("README.md", "deviation-002.md"):
            (dependency / name).write_text(OWNER, encoding="utf-8")
        (dependency / "prd-002.md").write_text(
            OWNER + "# PRD-002：Dependency\n- 状态：`实施中`\n- 批准依据：用户明确批准 PRD-002\n",
            encoding="utf-8",
        )
        (dependency / "spec-002.md").write_text(
            OWNER + "# SPEC-002：Dependency\n- 状态：`实施中`\n- 批准依据：用户明确批准 SPEC-002\n- 实施授权：用户明确授权实施\n",
            encoding="utf-8",
        )
        self.git("add", ".")
        self.git("commit", "-m", "dependency")
        dependency_head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("update-ref", "refs/project-harness/iterations/002/base/refs/heads/main", dependency_head)
        self.git("update-ref", "refs/project-harness/v2/iterations/002/candidates/g2", dependency_head)
        self.write_authority(approved=True, depends_on="PRD-002")
        lease = self.root / ".git" / "project-harness" / "workspace" / "v1" / "leases" / "iterations"
        lease.mkdir(parents=True)
        (lease / "001.json").write_text(
            '{"iteration":"001","dependency_generations":{"002":"g1"}}',
            encoding="utf-8",
        )

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "5" * 32)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("dependency-candidate-stale:002", plan.blocking_reasons)


if __name__ == "__main__":
    unittest.main()
