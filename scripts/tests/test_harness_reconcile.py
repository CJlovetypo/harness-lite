from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "harness_reconcile.py"
SPEC = importlib.util.spec_from_file_location("harness_reconcile_tests", SCRIPT)
assert SPEC and SPEC.loader
reconcile = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reconcile
SPEC.loader.exec_module(reconcile)


OWNER = b"<!-- managed-by: harness-lite v1 -->\n"
FOCUS_START = b"<!-- project-harness:focus:start -->\n"
FOCUS_END = b"<!-- project-harness:focus:end -->\n"
ITERATIONS_START = b"<!-- project-harness:iterations:start -->\n"
ITERATIONS_END = b"<!-- project-harness:iterations:end -->\n"


def legacy_event(body: str = "base") -> bytes:
    return (
        b"## S-20260812-01 / OPEN / 2026-08-12T10:00:00+08:00\n\n"
        + f"- fact: {body}\n".encode("utf-8")
    )


def v2_event(identity: str, body: str) -> bytes:
    return (
        f"## {identity} / CHECKPOINT / 2026-08-12T11:00:00+08:00\n\n"
        "- causal_parent: S-20260812-01/OPEN\n"
        f"- fact: {body}\n"
    ).encode("utf-8")


def progress(*events: bytes) -> bytes:
    document = (
        OWNER
        + b"# Progress\n\n"
        + b"<!-- project-harness:progress-index:start -->\n"
        + b"| iteration | status |\n|---|---|\n| 001 | active |\n"
        + b"<!-- project-harness:progress-index:end -->\n"
    )
    for event in events:
        document = document.rstrip(b"\n") + b"\n\n" + event
    return document


def l0(focus: str, registry: str, *, manual: str = "manual-main") -> bytes:
    return (
        OWNER
        + b"# Harness\n\n"
        + f"manual: {manual}\n\n".encode("utf-8")
        + FOCUS_START
        + f"- {focus}\n".encode("utf-8")
        + FOCUS_END
        + b"\n"
        + ITERATIONS_START
        + f"| {registry} |\n".encode("utf-8")
        + ITERATIONS_END
    )


def l1(result: str) -> bytes:
    return OWNER + b"# Iteration 001\n\n## Current result\n\n" + result.encode("utf-8") + b"\n"


class GovernanceReconcileApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.git_executable = shutil.which("git")
        if not self.git_executable:
            self.skipTest("git is required")
        self.temporary = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "main repo"
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
        self.environment = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(self.git_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=False,
        )
        self.environment.start()
        self.git("init", "-b", "main")
        self.base_principle = OWNER + b"# Principles\n\n- stable\n"
        self.base_progress = progress(legacy_event())
        self.base_l0 = l0("base focus", "001 | base")
        self.base_l1 = l1("base result")
        self.write_governance(
            self.root,
            principle=self.base_principle,
            progress_bytes=self.base_progress,
            root_readme=self.base_l0,
            iteration_readme=self.base_l1,
        )
        self.git("add", "--", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "governance baseline")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.base_root = self.sandbox / "base snapshot"
        self.candidate_root = self.sandbox / "candidate snapshot"
        self.copy_governance(self.root, self.base_root)
        self.copy_governance(self.root, self.candidate_root)
        self.branch_event = v2_event("EV-branch-01", "branch fact")
        self.write_governance(
            self.candidate_root,
            principle=self.base_principle,
            progress_bytes=progress(legacy_event(), self.branch_event),
            root_readme=l0("candidate focus", "001 | candidate"),
            iteration_readme=l1("candidate result"),
        )

    def tearDown(self) -> None:
        self.environment.stop()
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

    @staticmethod
    def write_governance(
        root: Path,
        *,
        principle: bytes,
        progress_bytes: bytes,
        root_readme: bytes,
        iteration_readme: bytes,
    ) -> None:
        iteration = root / "harness" / "iterations" / "001"
        iteration.mkdir(parents=True, exist_ok=True)
        (root / "harness" / "principle.md").write_bytes(principle)
        (root / "harness" / "progress.md").write_bytes(progress_bytes)
        (root / "harness" / "README.md").write_bytes(root_readme)
        (iteration / "README.md").write_bytes(iteration_readme)

    @staticmethod
    def copy_governance(source: Path, destination: Path) -> None:
        destination.mkdir()
        shutil.copytree(source / "harness", destination / "harness")

    def operation_id(self) -> str:
        return "OP-" + uuid.uuid4().hex

    def plan(self, operation_id: str | None = None, **kwargs):
        return reconcile.plan_reconciliation_from_roots(
            operation_id=operation_id or self.operation_id(),
            branch_base_root=self.base_root,
            latest_main_root=self.root,
            branch_candidate_root=self.candidate_root,
            **kwargs,
        )

    def journal(self, operation_id: str) -> dict[str, object]:
        path = reconcile.journal_path(reconcile.resolve_git_common_dir(self.root), operation_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def refs(self) -> str:
        return self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout

    def test_real_repo_apply_binds_digest_and_changes_only_governance_files(self) -> None:
        operation_id = self.operation_id()
        plan = self.plan(operation_id)
        self.assertTrue(plan.ready, plan.blockers)
        self.assertEqual(
            [item.path for item in plan.previews],
            [
                "harness/progress.md",
                "harness/README.md",
                "harness/iterations/001/README.md",
            ],
        )
        before_refs = self.refs()

        result = reconcile.apply_reconciliation(plan, accept_plan_digest=plan.plan_digest)

        self.assertEqual(result.phase, "APPLIED")
        self.assertFalse(result.resumed)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.head)
        self.assertEqual(self.refs(), before_refs)
        self.assertEqual((self.root / "harness/progress.md").read_bytes().count(self.branch_event), 1)
        self.assertIn(b"candidate focus", (self.root / "harness/README.md").read_bytes())
        self.assertIn(b"candidate result", (self.root / "harness/iterations/001/README.md").read_bytes())
        self.assertEqual(self.journal(operation_id)["phase"], "APPLIED")
        self.assertEqual(result.as_dict()["pushed"], False)
        self.assertIn("no commit", result.as_dict()["exclusions"])

    def test_target_drift_fails_before_any_planned_file_write(self) -> None:
        operation_id = self.operation_id()
        plan = self.plan(operation_id)
        self.assertTrue(plan.ready, plan.blockers)
        original_l0 = (self.root / "harness/README.md").read_bytes()
        (self.root / "harness/progress.md").write_bytes(self.base_progress + b"\nexternal drift\n")

        with self.assertRaisesRegex(reconcile.ReconcileError, "target drift"):
            reconcile.apply_reconciliation(plan, accept_plan_digest=plan.plan_digest)

        journal = self.journal(operation_id)
        self.assertEqual(journal["phase"], "FAILED_NEEDS_RECONCILE")
        self.assertEqual((self.root / "harness/README.md").read_bytes(), original_l0)
        with self.assertRaisesRegex(reconcile.ReconcileError, "manual reconcile"):
            reconcile.apply_reconciliation(plan, accept_plan_digest=plan.plan_digest)

    def test_unmodified_governance_observation_drift_also_blocks_all_writes(self) -> None:
        operation_id = self.operation_id()
        plan = self.plan(operation_id)
        original_progress = (self.root / "harness/progress.md").read_bytes()
        (self.root / "harness/principle.md").write_bytes(self.base_principle + b"\nexternal\n")

        with self.assertRaisesRegex(reconcile.ReconcileError, "target observation drift"):
            reconcile.apply_reconciliation(plan, accept_plan_digest=plan.plan_digest)

        self.assertEqual((self.root / "harness/progress.md").read_bytes(), original_progress)
        self.assertEqual(self.journal(operation_id)["phase"], "FAILED_NEEDS_RECONCILE")

    def test_crash_after_replace_before_journal_resumes_idempotently(self) -> None:
        operation_id = self.operation_id()
        plan = self.plan(operation_id)
        crashed = False

        def injector(stage: str, path: str | None) -> None:
            nonlocal crashed
            if not crashed and stage == "after_replace_before_journal":
                crashed = True
                raise reconcile.SimulatedCrash(path)

        with self.assertRaises(reconcile.SimulatedCrash):
            reconcile.apply_reconciliation(
                plan,
                accept_plan_digest=plan.plan_digest,
                fault_injector=injector,
            )
        self.assertEqual(self.journal(operation_id)["phase"], "APPLYING")
        self.assertEqual((self.root / "harness/progress.md").read_bytes().count(self.branch_event), 1)

        reloaded = reconcile.load_reconciliation_plan(
            reconcile.resolve_git_common_dir(self.root), operation_id
        )
        resumed = reconcile.apply_reconciliation(reloaded, accept_plan_digest=plan.plan_digest)
        repeated = reconcile.apply_reconciliation(reloaded, accept_plan_digest=plan.plan_digest)

        self.assertTrue(resumed.resumed)
        self.assertTrue(repeated.resumed)
        self.assertEqual(self.journal(operation_id)["phase"], "APPLIED")
        self.assertEqual((self.root / "harness/progress.md").read_bytes().count(self.branch_event), 1)
        self.assertIn(b"candidate focus", (self.root / "harness/README.md").read_bytes())
        self.assertIn(b"candidate result", (self.root / "harness/iterations/001/README.md").read_bytes())

    def test_same_event_id_with_different_bytes_blocks_without_journal(self) -> None:
        collision_main = v2_event("EV-collision-01", "main bytes")
        collision_candidate = v2_event("EV-collision-01", "candidate bytes")
        (self.root / "harness/progress.md").write_bytes(progress(legacy_event(), collision_main))
        (self.candidate_root / "harness/progress.md").write_bytes(
            progress(legacy_event(), collision_candidate)
        )
        operation_id = self.operation_id()
        plan = self.plan(operation_id)

        self.assertFalse(plan.ready)
        self.assertIn("progress-same-id-different-bytes", {item.code for item in plan.blockers})
        with self.assertRaisesRegex(reconcile.ReconcileError, "blocked"):
            reconcile.apply_reconciliation(plan, accept_plan_digest=plan.plan_digest)
        self.assertFalse(
            reconcile.journal_path(reconcile.resolve_git_common_dir(self.root), operation_id).exists()
        )

    def test_principle_change_requires_exact_approval_and_installed_global_lease(self) -> None:
        changed = OWNER + b"# Principles\n\n- stable\n- approved addition\n"
        (self.candidate_root / "harness/principle.md").write_bytes(changed)
        operation_id = self.operation_id()
        approval = reconcile.PrincipleApproval(
            change_id="PC-001",
            evidence_ref="EV-principle-approval",
            exact_before=self.base_principle,
            exact_after=changed,
        )
        blocked = self.plan(operation_id, principle_approval=approval)
        self.assertIn("global-principle-lease-required", {item.code for item in blocked.blockers})

        lease = reconcile.GlobalPrincipleLease(
            lease_id="PL-test-001",
            operation_id=operation_id,
            holder="integration-lane",
            generation=1,
            before_sha256=reconcile.sha256_bytes(self.base_principle),
            after_sha256=reconcile.sha256_bytes(changed),
            approval_change_id="PC-001",
        )
        common = reconcile.resolve_git_common_dir(self.root)
        self.assertTrue(reconcile.acquire_global_principle_lease(common, lease))
        self.assertFalse(reconcile.acquire_global_principle_lease(common, lease))
        plan = self.plan(operation_id, principle_approval=approval, principle_lease=lease)
        self.assertTrue(plan.ready, plan.blockers)

        reconcile.apply_reconciliation(plan, accept_plan_digest=plan.plan_digest)

        self.assertEqual((self.root / "harness/principle.md").read_bytes(), changed)
        journal = self.journal(operation_id)
        self.assertEqual(journal["manifest"]["principle"]["evidence_ref"], "EV-principle-approval")
        self.assertEqual(journal["manifest"]["principle_lease"]["lease_id"], "PL-test-001")
        self.assertTrue(reconcile.principle_lease_path(common).is_file())
        self.assertEqual(
            json.loads(reconcile.principle_lease_path(common).read_text(encoding="utf-8"))["lease_id"],
            "PL-test-001",
        )

    def test_l1_without_harness_owner_marker_is_blocked(self) -> None:
        candidate_l1 = self.candidate_root / "harness" / "iterations" / "001" / "README.md"
        candidate_l1.write_bytes(b"# Foreign iteration summary\n")

        plan = self.plan()

        self.assertFalse(plan.ready)
        self.assertIn("l1-owner-marker-missing", {item.code for item in plan.blockers})

    def test_explicit_common_dir_must_belong_to_target_project(self) -> None:
        foreign = self.sandbox / "foreign common"
        foreign.mkdir()
        with self.assertRaisesRegex(reconcile.ReconcileError, "does not belong"):
            reconcile.plan_reconciliation(
                project_root=self.root,
                git_common_dir=foreign,
                operation_id=self.operation_id(),
                branch_base=reconcile.read_snapshot_from_root(self.base_root, source_id="base"),
                latest_main=reconcile.read_snapshot_from_root(self.root, source_id="main"),
                branch_candidate=reconcile.read_snapshot_from_root(
                    self.candidate_root, source_id="candidate"
                ),
            )

    def test_wrong_accepted_digest_is_zero_write(self) -> None:
        operation_id = self.operation_id()
        plan = self.plan(operation_id)
        before = {
            item.path: (self.root / Path(item.path)).read_bytes()
            for item in plan.previews
        }

        with self.assertRaisesRegex(reconcile.ReconcileError, "digest"):
            reconcile.apply_reconciliation(plan, accept_plan_digest="0" * 64)

        self.assertEqual(
            before,
            {item.path: (self.root / Path(item.path)).read_bytes() for item in plan.previews},
        )
        self.assertFalse(
            reconcile.journal_path(reconcile.resolve_git_common_dir(self.root), operation_id).exists()
        )


if __name__ == "__main__":
    unittest.main()
