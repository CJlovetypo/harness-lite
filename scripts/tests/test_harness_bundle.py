from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.harness_bundle import apply_bundle, plan_bundle
from scripts.project_harness import (
    ALLOCATION_METADATA_SCHEMA_V1,
    apply_operations,
    build_init_operations,
    canonical_json_bytes,
)


class V2BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="harness-v2-bundle-")
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        bootstrap_time = datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc)
        apply_operations(self.root, build_init_operations(self.root, "Bundle Test", bootstrap_time))
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.reserve("001")
        self.now = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)

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

    def reserve(self, number: str) -> None:
        metadata = {
            "schema_version": ALLOCATION_METADATA_SCHEMA_V1,
            "operation_id": "OP-" + "a" * 32,
            "plan_digest": "1" * 64,
            "iteration": number,
            "base_commit": self.head,
            "base_branch": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.head,
            "governance_tree": self.git("rev-parse", "HEAD^{tree}").stdout.strip(),
            "principle_sha256": "2" * 64,
            "title": "Parallel feature",
        }
        blob = subprocess.run(
            ["git", "-C", str(self.root), "hash-object", "-w", "--stdin"],
            input=canonical_json_bytes(metadata),
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode().strip()
        self.git("update-ref", f"refs/project-harness/v2/allocations/{number}", blob)
        self.git("update-ref", f"refs/project-harness/v2/iterations/{number}/base", self.head)

    def test_plan_is_zero_write_and_apply_closes_reservation_bundle_gap(self) -> None:
        operation = "OP-" + "b" * 32
        before_refs = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout
        before_status = self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout

        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)

        self.assertEqual(plan.phase, "planned")
        self.assertEqual(before_refs, self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout)
        self.assertEqual(before_status, self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout)
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())

        result = apply_bundle(
            self.root,
            iteration="001",
            operation_id=operation,
            accepted_plan_digest=plan.plan_digest,
            planned_at=self.now,
        )

        self.assertEqual(result["phase"], "succeeded")
        self.assertEqual(len(result["paths"]), 6)
        self.assertTrue((self.root / "harness" / "iterations" / "001" / "prd-001.md").is_file())
        prd = (self.root / "harness" / "iterations" / "001" / "prd-001.md").read_text(encoding="utf-8")
        self.assertIn(self.head, prd)
        self.assertEqual(before_refs, self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout)

    def test_apply_replay_is_idempotent(self) -> None:
        operation = "OP-" + "c" * 32
        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)
        first = apply_bundle(
            self.root,
            iteration="001",
            operation_id=operation,
            accepted_plan_digest=plan.plan_digest,
            planned_at=self.now,
        )
        snapshot = {path: (self.root / path).read_bytes() for path in first["paths"]}

        second = apply_bundle(
            self.root,
            iteration="001",
            operation_id=operation,
            accepted_plan_digest=plan.plan_digest,
            planned_at=self.now,
        )

        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(snapshot, {path: (self.root / path).read_bytes() for path in first["paths"]})

    def test_drift_after_plan_blocks_before_write(self) -> None:
        operation = "OP-" + "d" * 32
        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)
        readme = self.root / "harness" / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nuser edit\n", encoding="utf-8")

        with self.assertRaisesRegex(Exception, "plan changed|drifted"):
            apply_bundle(
                self.root,
                iteration="001",
                operation_id=operation,
                accepted_plan_digest=plan.plan_digest,
                planned_at=self.now,
            )
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())

    def test_existing_bundle_is_not_overwritten(self) -> None:
        target = self.root / "harness" / "iterations" / "001"
        target.mkdir()
        (target / "user.md").write_text("mine\n", encoding="utf-8")

        plan = plan_bundle(self.root, iteration="001", operation_id="OP-" + "e" * 32, planned_at=self.now)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("iteration-bundle-already-present", plan.blocking_reasons)
        self.assertEqual((target / "user.md").read_text(encoding="utf-8"), "mine\n")


if __name__ == "__main__":
    unittest.main()
