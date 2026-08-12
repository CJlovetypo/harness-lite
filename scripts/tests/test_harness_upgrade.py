from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.harness_upgrade import (
    InjectedUpgradeCrash,
    UpgradeError,
    apply_upgrade_plan,
    build_upgrade_plan,
)
from scripts import project_harness as core


class HarnessUpgradePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="harness-upgrade-test-")
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        (self.root / "harness" / "iterations" / "001").mkdir(parents=True)
        source = Path(__file__).resolve().parents[2]
        for relative in (
            "AGENTS.md",
            "harness/README.md",
            "harness/principle.md",
            "harness/progress.md",
            "harness/iterations/001/README.md",
            "harness/iterations/001/prd-001.md",
            "harness/iterations/001/spec-001.md",
            "harness/iterations/001/deviation-001.md",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((source / relative).read_bytes())
        self.git("add", "AGENTS.md", "harness")
        self.git("commit", "-m", "baseline")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        prd = self.root / "harness" / "iterations" / "001" / "prd-001.md"
        text = prd.read_text(encoding="utf-8-sig")
        text = text.replace("7376803cffb09269bc8a03346901b2e9e224d704", self.base)
        prd.write_text(text, encoding="utf-8")
        self.git("add", "harness/iterations/001/prd-001.md")
        self.git("commit", "-m", "record base")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    def test_completed_legacy_iteration_is_preserved_without_actions(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        final = "refs/project-harness/iterations/001/final"
        self.git("update-ref", base, self.base)
        self.git("update-ref", final, self.head)

        before_refs = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout
        plan = build_upgrade_plan(self.root)
        after_refs = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout

        self.assertEqual(plan.phase, "planned")
        self.assertEqual(plan.iterations[0].lifecycle, "legacy-complete")
        self.assertEqual(plan.iterations[0].disposition, "preserve-legacy-history")
        self.assertEqual(plan.planned_actions, ())
        self.assertEqual(before_refs, after_refs)

    def test_clean_active_legacy_iteration_only_proposes_explicit_adoption(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        self.git("update-ref", base, self.base)

        first = build_upgrade_plan(self.root)
        second = build_upgrade_plan(self.root)

        self.assertEqual(first.phase, "planned")
        self.assertEqual(first.plan_digest, second.plan_digest)
        self.assertEqual(first.iterations[0].lifecycle, "legacy-active-clean")
        self.assertEqual(first.planned_actions[0]["authorization"], "confirm")
        self.assertFalse(first.planned_actions[0]["writes"])

    def test_dirty_active_iteration_fails_closed_without_migration(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        self.git("update-ref", base, self.base)
        (self.root / "implementation.txt").write_text("dirty\n", encoding="utf-8")

        plan = build_upgrade_plan(self.root)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("001:dirty-active-iteration-requires-explicit-adoption", plan.blocking_reasons)
        self.assertEqual(plan.planned_actions, ())

    def test_partial_v2_identity_is_blocked_and_not_repaired(self) -> None:
        self.git("update-ref", "refs/project-harness/v2/iterations/001/base", self.head)
        before = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout

        plan = build_upgrade_plan(self.root)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("001:partial-v2-identity", plan.blocking_reasons)
        self.assertEqual(before, self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout)

    def test_bundle_without_identity_requires_reconcile(self) -> None:
        plan = build_upgrade_plan(self.root)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("001:iteration-bundle-has-no-base-identity", plan.blocking_reasons)

    def test_exact_clean_active_plan_can_atomically_adopt_v2_without_rewriting_legacy(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        self.git("update-ref", base, self.base)
        plan = build_upgrade_plan(self.root)

        result = apply_upgrade_plan(self.root, accepted_plan_digest=plan.plan_digest)

        self.assertEqual(result.adopted_iterations, ("001",))
        self.assertEqual(self.git("rev-parse", base).stdout.strip(), self.base)
        self.assertEqual(
            self.git("rev-parse", "refs/project-harness/v2/iterations/001/base").stdout.strip(),
            self.base,
        )
        allocation = self.git("cat-file", "-t", "refs/project-harness/v2/allocations/001").stdout.strip()
        self.assertEqual(allocation, "blob")
        status = core.build_status_snapshot(self.root, "git", all_worktrees=True)
        self.assertEqual(status.blocking_reasons, ())

        replay = apply_upgrade_plan(self.root, accepted_plan_digest=plan.plan_digest)
        self.assertTrue(replay.idempotent)
        self.assertEqual(replay.adopted_iterations, ("001",))

    def test_crash_after_ref_transaction_recovers_from_durable_pre_state(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        self.git("update-ref", base, self.base)
        plan = build_upgrade_plan(self.root)

        with self.assertRaises(InjectedUpgradeCrash):
            apply_upgrade_plan(
                self.root,
                accepted_plan_digest=plan.plan_digest,
                _failpoint="after-ref-transaction",
            )
        (self.root / "unrelated-after-adoption.txt").write_text("later\n", encoding="utf-8")
        self.git("add", "unrelated-after-adoption.txt")
        self.git("commit", "-m", "advance main after durable ref transaction")

        recovered = apply_upgrade_plan(self.root, accepted_plan_digest=plan.plan_digest)
        self.assertTrue(recovered.idempotent)
        self.assertEqual(recovered.adopted_iterations, ("001",))
        self.assertEqual(core.build_status_snapshot(self.root, "git", all_worktrees=True).blocking_reasons, ())

    def test_replay_before_ref_transaction_revalidates_dirty_worktree(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        allocation_ref = "refs/project-harness/v2/allocations/001"
        v2_base_ref = "refs/project-harness/v2/iterations/001/base"
        self.git("update-ref", base, self.base)
        plan = build_upgrade_plan(self.root)

        with self.assertRaises(InjectedUpgradeCrash):
            apply_upgrade_plan(
                self.root,
                accepted_plan_digest=plan.plan_digest,
                _failpoint="before-ref-transaction",
            )
        (self.root / "appeared-after-plan.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(UpgradeError, "dirty"):
            apply_upgrade_plan(self.root, accepted_plan_digest=plan.plan_digest)

        self.assertNotEqual(self.git("show-ref", "--verify", allocation_ref, check=False).returncode, 0)
        self.assertNotEqual(self.git("show-ref", "--verify", v2_base_ref, check=False).returncode, 0)

    def test_coherently_tampered_metadata_and_journal_block_preserve_v2(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        allocation_ref = "refs/project-harness/v2/allocations/001"
        self.git("update-ref", base, self.base)
        plan = build_upgrade_plan(self.root)
        apply_upgrade_plan(self.root, accepted_plan_digest=plan.plan_digest)
        allocation_object = self.git("rev-parse", allocation_ref).stdout.strip()
        metadata = json.loads(self.git("cat-file", "-p", allocation_object).stdout)
        common_dir = core.resolve_git_common_dir("git", self.root)
        journal_path = core.operation_journal_path(common_dir, metadata["operation_id"])
        journal = json.loads(journal_path.read_text(encoding="utf-8"))

        # Keep both public records internally consistent and schema-valid while
        # substituting a principle hash that does not describe the committed
        # governance object.  Shape-only/journal-vs-ref checks must accept this
        # fixture, while upgrade preservation must reject its false semantics.
        metadata["principle_sha256"] = "0" * 64
        journal["principle_sha256"] = "0" * 64
        journal["manifest"]["governance_snapshot"]["principle_sha256"] = "0" * 64
        tampered_digest = core.schema_digest(journal["manifest"])
        journal["plan_digest"] = tampered_digest
        metadata["plan_digest"] = tampered_digest
        tampered_object = subprocess.run(
            ["git", "-C", str(self.root), "hash-object", "-w", "--stdin"],
            input=json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        journal["allocation_object"] = tampered_object
        journal_path.write_bytes(core.canonical_json_bytes(journal) + b"\n")
        self.git("update-ref", allocation_ref, tampered_object, allocation_object)

        # Demonstrate that the pair is valid-looking to the generic structural
        # status view before exercising upgrade's deeper preservation gate.
        self.assertEqual(
            core.build_status_snapshot(self.root, "git", all_worktrees=True).blocking_reasons,
            (),
        )
        tampered_plan = build_upgrade_plan(self.root)

        self.assertEqual(tampered_plan.phase, "blocked")
        self.assertTrue(
            any("committed Git objects" in reason for reason in tampered_plan.blocking_reasons),
            tampered_plan.blocking_reasons,
        )

    def test_ready_ref_deletion_requires_reconcile_and_is_not_recreated(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        allocation_ref = "refs/project-harness/v2/allocations/001"
        v2_base_ref = "refs/project-harness/v2/iterations/001/base"
        self.git("update-ref", base, self.base)
        plan = build_upgrade_plan(self.root)
        apply_upgrade_plan(self.root, accepted_plan_digest=plan.plan_digest)
        self.git("update-ref", "-d", allocation_ref)
        self.git("update-ref", "-d", v2_base_ref)

        with self.assertRaisesRegex(UpgradeError, "reconcile"):
            apply_upgrade_plan(self.root, accepted_plan_digest=plan.plan_digest)

        self.assertNotEqual(self.git("show-ref", "--verify", allocation_ref, check=False).returncode, 0)
        self.assertNotEqual(self.git("show-ref", "--verify", v2_base_ref, check=False).returncode, 0)
        status = core.build_status_snapshot(self.root, "git", all_worktrees=True)
        self.assertTrue(
            any(reason.code == "orphan-operation-journal" for reason in status.blocking_reasons),
            status.blocking_reasons,
        )

    def test_corrupt_existing_v2_pair_blocks_upgrade(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        self.git("update-ref", base, self.base)
        # hash an empty blob without shell redirection
        written = subprocess.run(
            ["git", "-C", str(self.root), "hash-object", "-w", "--stdin"],
            input="not allocation metadata",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        self.git("update-ref", "refs/project-harness/v2/allocations/001", written)
        self.git("update-ref", "refs/project-harness/v2/iterations/001/base", self.base)

        plan = build_upgrade_plan(self.root)

        self.assertEqual(plan.phase, "blocked")
        self.assertTrue(any("v2" in reason or "metadata" in reason for reason in plan.blocking_reasons))

    def test_wrong_upgrade_digest_is_zero_ref_write(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        self.git("update-ref", base, self.base)
        before = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout

        with self.assertRaisesRegex(UpgradeError, "digest"):
            apply_upgrade_plan(self.root, accepted_plan_digest="0" * 64)

        self.assertEqual(before, self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout)


if __name__ == "__main__":
    unittest.main()
