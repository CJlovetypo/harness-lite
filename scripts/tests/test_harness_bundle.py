from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts.harness_bundle import BundleError, _common, _journal, apply_bundle, plan_bundle
from scripts.harness_governance import parse_progress_events
from scripts.project_harness import (
    apply_operations,
    build_init_operations,
    build_new_iteration_operations,
    build_reserve_iteration_plan,
    operation_journal_path,
    reserve_iteration,
)


class SimulatedCrash(BaseException):
    pass


class V2BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="harness-v2-bundle-")
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        bootstrap_time = datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc)
        apply_operations(self.root, build_init_operations(self.root, "Bundle Test", bootstrap_time))
        # Exercise exact byte preservation on the two renderer-updated files.
        for relative in ("harness/README.md", "harness/progress.md"):
            path = self.root / relative
            raw = path.read_bytes()
            path.write_bytes(b"\xef\xbb\xbf" + raw)
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.reserve_operation = "OP-" + "a" * 32
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
        plan = build_reserve_iteration_plan(
            self.root,
            "git",
            title="Parallel feature",
            operation_id=self.reserve_operation,
            base_ref="refs/heads/main",
            governance_ref="refs/heads/main",
        )
        self.assertFalse(plan.blocking_reasons)
        journal, created = reserve_iteration(plan, "git", self.root)
        self.assertTrue(created)
        self.assertEqual(journal.phase, "READY")
        self.assertEqual(journal.iteration, number)

    def bundle_journal(self, operation: str) -> Path:
        return _journal(_common(self.root), operation)

    def test_plan_is_zero_write_and_apply_closes_reservation_bundle_gap(self) -> None:
        operation = "OP-" + "b" * 32
        before_refs = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout
        before_status = self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout

        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)
        repeated_plan = plan_bundle(
            self.root,
            iteration="001",
            operation_id=operation,
            planned_at=self.now,
        )

        self.assertEqual(plan.phase, "planned")
        self.assertEqual(repeated_plan.plan_digest, plan.plan_digest)
        self.assertEqual(repeated_plan.files, plan.files)
        self.assertEqual(plan.reservation_operation_id, self.reserve_operation)
        self.assertEqual(before_refs, self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout)
        self.assertEqual(before_status, self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout)
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())

        planned_progress = next(item for item in plan.files if item.path == "harness/progress.md")
        progress_after = base64.b64decode(planned_progress.after_base64, validate=True)
        parsed_plan = parse_progress_events(progress_after, source="planned v2 bundle")
        self.assertFalse(parsed_plan.blockers)
        opened = parsed_plan.events[-1]
        self.assertRegex(opened.event_id, r"^EV-I001-lifecycle-[0-9a-f]{64}$")
        self.assertEqual(opened.identity, opened.event_id)
        self.assertEqual(opened.event_type, "OPEN")
        self.assertEqual(opened.causal_parent, "S-20260812-01/CLOSE")
        self.assertIn(b"- session_id: `S-20260812-02`", opened.exact_bytes)
        self.assertNotEqual(opened.event_id, "S-20260812-02")
        self.assertIn(f"- operation_id: `{operation}`".encode(), opened.exact_bytes)
        self.assertIn(b"- iteration: `001`", opened.exact_bytes)
        self.assertIn(b"- source_ref: `refs/heads/main`", opened.exact_bytes)
        self.assertIn(f"- source_commit: `{self.head}`".encode(), opened.exact_bytes)
        for evidence in (
            "allocation-ref:refs/project-harness/v2/allocations/001",
            f"allocation-object:{plan.allocation_object}",
            "base-ref:refs/project-harness/v2/iterations/001/base",
            f"base-commit:{self.head}",
            "governance-ref:refs/heads/main",
            f"governance-commit:{self.head}",
        ):
            self.assertIn(evidence.encode(), opened.exact_bytes)
        planned_l1 = next(
            item for item in plan.files if item.path == "harness/iterations/001/README.md"
        )
        self.assertIn(opened.event_id.encode(), base64.b64decode(planned_l1.after_base64, validate=True))

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
        self.assertTrue((self.root / "harness" / "README.md").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue((self.root / "harness" / "progress.md").read_bytes().startswith(b"\xef\xbb\xbf"))
        parsed_result = parse_progress_events(
            (self.root / "harness" / "progress.md").read_bytes(), source="applied v2 bundle"
        )
        self.assertFalse(parsed_result.blockers)
        self.assertEqual(sum(event.identity == opened.event_id for event in parsed_result.events), 1)

    def test_legacy_renderer_keeps_session_scoped_open_event(self) -> None:
        number, operations = build_new_iteration_operations(
            self.root,
            "Legacy renderer fixture",
            self.now,
            self.head,
            "refs/heads/main",
        )

        self.assertEqual(number, "001")
        progress_after = next(
            operation.new_raw
            for operation in operations
            if operation.path == self.root / "harness" / "progress.md"
        )
        parsed = parse_progress_events(progress_after, source="legacy renderer")
        self.assertFalse(parsed.blockers)
        opened = parsed.events[-1]
        self.assertEqual(opened.event_id, "S-20260812-02")
        self.assertEqual(opened.identity, "S-20260812-02/OPEN")
        self.assertEqual(opened.event_type, "OPEN")
        self.assertNotIn(b"schema_version", opened.exact_bytes)
        self.assertIn(b"PRD-001 / SPEC-001", opened.exact_bytes)
        self.assertFalse(any(event.event_id.startswith("EV-") for event in parsed.events))

    def test_apply_replay_is_idempotent(self) -> None:
        operation = "OP-" + "c" * 32
        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)
        planned_progress = next(item for item in plan.files if item.path == "harness/progress.md")
        parsed_plan = parse_progress_events(
            base64.b64decode(planned_progress.after_base64, validate=True), source="retry plan"
        )
        self.assertFalse(parsed_plan.blockers)
        event_id = parsed_plan.events[-1].event_id
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
        parsed_result = parse_progress_events(
            (self.root / "harness" / "progress.md").read_bytes(), source="retry result"
        )
        self.assertFalse(parsed_result.blockers)
        self.assertEqual(sum(event.identity == event_id for event in parsed_result.events), 1)

    def test_drift_after_plan_blocks_before_write(self) -> None:
        operation = "OP-" + "d" * 32
        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)
        readme = self.root / "harness" / "README.md"
        readme.write_bytes(readme.read_bytes() + b"\nuser edit\n")

        with self.assertRaisesRegex(Exception, "governance|plan changed|drifted"):
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

    def test_crash_after_one_replace_resumes_from_mixed_before_after_state(self) -> None:
        operation = "OP-" + "1" * 32
        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)

        def crash_after_first(_: Path, operations: list[object]) -> None:
            first = operations[0]
            first.path.parent.mkdir(parents=True, exist_ok=True)
            first.path.write_bytes(first.new_raw)
            raise SimulatedCrash("power loss")

        with mock.patch("scripts.harness_bundle.core.apply_operations", side_effect=crash_after_first):
            with self.assertRaises(SimulatedCrash):
                apply_bundle(
                    self.root,
                    iteration="001",
                    operation_id=operation,
                    accepted_plan_digest=plan.plan_digest,
                    planned_at=self.now,
                )
        journal = json.loads(self.bundle_journal(operation).read_text(encoding="utf-8"))
        self.assertEqual(journal["phase"], "PLANNED")

        result = apply_bundle(
            self.root,
            iteration="001",
            operation_id=operation,
            accepted_plan_digest=plan.plan_digest,
            planned_at=self.now,
        )

        self.assertEqual(result["journal_phase"], "READY")

    def test_ordinary_partial_write_failure_rolls_back_exact_bytes_and_is_retriable(self) -> None:
        operation = "OP-" + "2" * 32
        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)
        readme_before = (self.root / "harness" / "README.md").read_bytes()
        progress_before = (self.root / "harness" / "progress.md").read_bytes()

        def fail_after_first(_: Path, operations: list[object]) -> None:
            first = operations[0]
            first.path.parent.mkdir(parents=True, exist_ok=True)
            first.path.write_bytes(first.new_raw)
            raise RuntimeError("replace failed")

        with mock.patch("scripts.harness_bundle.core.apply_operations", side_effect=fail_after_first):
            with self.assertRaisesRegex(RuntimeError, "replace failed"):
                apply_bundle(
                    self.root,
                    iteration="001",
                    operation_id=operation,
                    accepted_plan_digest=plan.plan_digest,
                    planned_at=self.now,
                )
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())
        self.assertEqual((self.root / "harness" / "README.md").read_bytes(), readme_before)
        self.assertEqual((self.root / "harness" / "progress.md").read_bytes(), progress_before)
        self.assertEqual(
            json.loads(self.bundle_journal(operation).read_text(encoding="utf-8"))["phase"],
            "PLANNED",
        )

        result = apply_bundle(
            self.root,
            iteration="001",
            operation_id=operation,
            accepted_plan_digest=plan.plan_digest,
            planned_at=self.now,
        )
        self.assertEqual(result["journal_phase"], "READY")

    def test_tampered_journal_payload_is_rejected_before_write(self) -> None:
        operation = "OP-" + "3" * 32
        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)
        with mock.patch("scripts.harness_bundle.core.apply_operations", side_effect=SimulatedCrash("stop")):
            with self.assertRaises(SimulatedCrash):
                apply_bundle(
                    self.root,
                    iteration="001",
                    operation_id=operation,
                    accepted_plan_digest=plan.plan_digest,
                    planned_at=self.now,
                )
        path = self.bundle_journal(operation)
        journal = json.loads(path.read_text(encoding="utf-8"))
        journal["plan"]["files"][0]["after_base64"] = "QQ=="
        path.write_text(json.dumps(journal), encoding="utf-8")

        with self.assertRaisesRegex(BundleError, "digest"):
            apply_bundle(
                self.root,
                iteration="001",
                operation_id=operation,
                accepted_plan_digest=plan.plan_digest,
                planned_at=self.now,
            )

    def test_existing_journal_binds_planned_at_exactly(self) -> None:
        operation = "OP-" + "4" * 32
        plan = plan_bundle(self.root, iteration="001", operation_id=operation, planned_at=self.now)
        with mock.patch("scripts.harness_bundle.core.apply_operations", side_effect=SimulatedCrash("stop")):
            with self.assertRaises(SimulatedCrash):
                apply_bundle(
                    self.root,
                    iteration="001",
                    operation_id=operation,
                    accepted_plan_digest=plan.plan_digest,
                    planned_at=self.now,
                )

        with self.assertRaisesRegex(BundleError, "planned_at"):
            apply_bundle(
                self.root,
                iteration="001",
                operation_id=operation,
                accepted_plan_digest=plan.plan_digest,
                planned_at=self.now + timedelta(seconds=1),
            )

    def test_invalid_operation_id_cannot_escape_registry(self) -> None:
        plan = plan_bundle(self.root, iteration="001", operation_id="OP-" + "5" * 32, planned_at=self.now)
        with self.assertRaisesRegex(BundleError, "operation ID"):
            apply_bundle(
                self.root,
                iteration="001",
                operation_id="..\\..\\..\\escape",
                accepted_plan_digest=plan.plan_digest,
                planned_at=self.now,
            )

    def test_orphan_allocation_without_reserve_owner_journal_is_rejected(self) -> None:
        owner_journal = operation_journal_path(_common(self.root), self.reserve_operation)
        owner_journal.unlink()

        with self.assertRaisesRegex(BundleError, "owner journal"):
            plan_bundle(
                self.root,
                iteration="001",
                operation_id="OP-" + "6" * 32,
                planned_at=self.now,
            )

    def test_second_preplanned_operation_cannot_overwrite_first_bundle(self) -> None:
        first_op = "OP-" + "7" * 32
        second_op = "OP-" + "8" * 32
        first = plan_bundle(self.root, iteration="001", operation_id=first_op, planned_at=self.now)
        second = plan_bundle(self.root, iteration="001", operation_id=second_op, planned_at=self.now)
        apply_bundle(
            self.root,
            iteration="001",
            operation_id=first_op,
            accepted_plan_digest=first.plan_digest,
            planned_at=self.now,
        )

        with self.assertRaisesRegex(BundleError, "changed|blocked"):
            apply_bundle(
                self.root,
                iteration="001",
                operation_id=second_op,
                accepted_plan_digest=second.plan_digest,
                planned_at=self.now,
            )


if __name__ == "__main__":
    unittest.main()
