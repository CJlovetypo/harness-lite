from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest


TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import harness_integrated_evidence as registry  # noqa: E402
import harness_train as train  # noqa: E402
import harness_coordinator as coordinator  # noqa: E402
import test_harness_train as _train_tests  # noqa: E402


class IntegratedEvidenceRegistryTests(unittest.TestCase):
    """Exercise the public registry against one real merge-train repository."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        # Reuse the established real-Git fixture without importing its TestCase
        # into this module's discovery namespace.
        helper = _train_tests.HarnessTrainTests(
            "test_register_candidate_creates_confirmed_seal_and_atomic_evidence_refs"
        )
        helper.setUp()
        cls.helper = helper
        cls.candidate = helper.register(
            "001",
            "refs/heads/feature/001",
            helper.feature_a,
            generation="registry-g1",
        )
        _prepare_plan, preparation, _notifications = helper.prepare(
            (cls.candidate,),
            generation="registry-i1",
        )
        assert preparation.commit_plan is not None
        cls.commit_token = helper.token(
            "create-integration-commit",
            preparation.commit_plan.commit_plan_digest,
            "REGISTRY-COMMIT",
        )
        cls.integration = train.apply_integration_commit(
            preparation.commit_plan,
            accepted_commit_plan_digest=preparation.commit_plan.commit_plan_digest,
            confirmation_token=cls.commit_token,
        )
        if not cls.integration.evidence_ready:
            raise AssertionError(cls.integration.as_dict())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.helper.tearDown()

    def setUp(self) -> None:
        self.root = self.helper.root
        self.operation = self.integration.operation_id
        self.plan_refs = (
            registry.operation_commit_ref(self.operation),
            registry.operation_evidence_ref(self.operation),
            registry.iteration_evidence_ref(
                self.candidate.iteration,
                self.integration.generation,
            ),
        )
        for reference in self.plan_refs:
            self.helper.git("update-ref", "-d", reference, check=False)
        for reference in (
            "refs/project-harness/v2/iterations/001/integrated",
            "refs/project-harness/v2/iterations/001/final",
            registry.iteration_final_evidence_ref("001"),
        ):
            self.helper.git("update-ref", "-d", reference, check=False)
        # Restore source identities after any preceding drift/tamper case.
        self.helper.git(
            "update-ref",
            self.candidate.candidate_ref,
            self.candidate.candidate_commit,
        )
        self.helper.git(
            "update-ref",
            self.candidate.candidate_evidence_ref,
            self.candidate.candidate_evidence_blob,
        )
        self.helper.git(
            "update-ref",
            self.integration.commit_plan.main_ref,
            self.integration.commit_plan.target_main,
        )
        path = registry.journal_path(self.root, self.operation)
        path.unlink(missing_ok=True)

    def plan(self) -> registry.IntegratedEvidencePlan:
        return registry.plan_register_integrated_evidence(
            self.integration,
            commit_confirmation_token=self.commit_token,
            progress_bindings=(
                (
                    "EV-I001-integration_verified-registry",
                    "refs/project-harness/v2/progress/registry-integration-verified",
                ),
            ),
        )

    def apply(self, plan: registry.IntegratedEvidencePlan, *, failpoint=None):
        return registry.apply_register_integrated_evidence(
            plan,
            accepted_plan_digest=plan.plan_digest,
            commit_confirmation_token=self.commit_token,
            failpoint=failpoint,
        )

    def ref_snapshot(self) -> str:
        return self.helper.git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/project-harness/v2",
        ).stdout

    def test_plan_is_zero_write_and_success_is_publicly_loadable(self) -> None:
        before = self.ref_snapshot()
        journal = registry.journal_path(self.root, self.operation)
        self.assertFalse(journal.exists())

        plan = self.plan()

        self.assertTrue(plan.ready, plan.as_dict())
        self.assertEqual(self.ref_snapshot(), before)
        self.assertFalse(journal.exists())
        receipt = self.apply(plan)
        self.assertFalse(receipt.idempotent)
        self.assertFalse(receipt.pushed)
        self.assertEqual(
            self.helper.oid(plan.commit_ref),
            self.integration.integrated_commit,
        )
        self.assertEqual(self.helper.oid(plan.evidence_ref), plan.metadata_blob)
        self.assertEqual(
            self.helper.oid(plan.iteration_evidence_refs[0].ref_name),
            plan.metadata_blob,
        )

        loaded, blockers = registry.load_registered_integrated_evidence(
            self.root,
            operation_id=self.operation,
        )
        self.assertEqual(blockers, ())
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.metadata, receipt.metadata)
        self.assertEqual(
            registry.registered_integrated_evidence_gate(self.root, loaded),
            (),
        )
        # Operational recovery state is not public authority.
        journal.unlink()
        loaded_without_journal, blockers = registry.load_registered_integrated_evidence(
            self.root,
            operation_id=self.operation,
        )
        self.assertEqual(blockers, ())
        self.assertIsNotNone(loaded_without_journal)

    def test_evidence_blob_ref_tamper_fails_closed(self) -> None:
        plan = self.plan()
        self.apply(plan)
        wrong_blob = self.helper.git(
            "hash-object",
            "-w",
            "--stdin",
            input_text='{"foreign":true}\n',
        ).stdout.strip()
        self.helper.git("update-ref", plan.evidence_ref, wrong_blob, plan.metadata_blob)

        loaded, blockers = registry.load_registered_integrated_evidence(
            self.root,
            operation_id=self.operation,
        )

        self.assertIsNone(loaded)
        self.assertIn("integrated-evidence-metadata-invalid", {item.code for item in blockers})

    def test_commit_ref_object_type_tamper_fails_closed(self) -> None:
        plan = self.plan()
        self.apply(plan)
        self.helper.git(
            "update-ref",
            plan.commit_ref,
            plan.metadata_blob,
            plan.metadata.integrated_commit,
        )

        loaded, blockers = registry.load_registered_integrated_evidence(
            self.root,
            operation_id=self.operation,
        )

        self.assertIsNone(loaded)
        self.assertIn("integrated-evidence-ref-object-type", {item.code for item in blockers})

    def test_candidate_drift_between_plan_and_apply_is_rejected(self) -> None:
        plan = self.plan()
        self.helper.git(
            "update-ref",
            self.candidate.candidate_ref,
            self.integration.commit_plan.target_main,
            self.candidate.candidate_commit,
        )

        with self.assertRaisesRegex(
            registry.IntegratedEvidenceError,
            "source identity is stale",
        ):
            self.apply(plan)

        self.assertFalse(registry.journal_path(self.root, self.operation).exists())
        for reference in self.plan_refs:
            self.assertNotEqual(
                self.helper.git("rev-parse", "--verify", reference, check=False).returncode,
                0,
            )

    def test_old_or_foreign_journal_fails_closed(self) -> None:
        plan = self.plan()
        path = registry.journal_path(self.root, self.operation)
        path.parent.mkdir(parents=True, exist_ok=True)
        foreign = {
            "schema_version": "harness-lite.integrated-evidence-journal/v0",
            "kind": "foreign",
            "operation_id": self.operation,
            "status": "planned",
        }
        foreign["journal_digest"] = registry.digest(foreign)
        path.write_bytes(registry.canonical_json(foreign) + b"\n")

        with self.assertRaisesRegex(
            registry.IntegratedEvidenceError,
            "old or foreign",
        ):
            self.apply(plan)

        for reference in self.plan_refs:
            self.assertNotEqual(
                self.helper.git("rev-parse", "--verify", reference, check=False).returncode,
                0,
            )

    def test_crash_after_atomic_refs_recovers_without_republishing(self) -> None:
        plan = self.plan()

        def crash(stage: str) -> None:
            if stage == "registry-after-refs":
                raise registry.InjectedCrash(stage)

        with self.assertRaises(registry.InjectedCrash):
            self.apply(plan, failpoint=crash)
        expected_refs = {
            plan.commit_ref: plan.metadata.integrated_commit,
            plan.evidence_ref: plan.metadata_blob,
            plan.iteration_evidence_refs[0].ref_name: plan.metadata_blob,
        }
        self.assertEqual(
            {reference: self.helper.oid(reference) for reference in expected_refs},
            expected_refs,
        )
        before = self.ref_snapshot()

        recovered = self.apply(plan)

        self.assertTrue(recovered.idempotent)
        self.assertEqual(self.ref_snapshot(), before)
        journal = json.loads(
            registry.journal_path(self.root, self.operation).read_text(encoding="utf-8")
        )
        self.assertEqual(journal["status"], "complete")
        loaded, blockers = registry.load_registered_integrated_evidence(
            self.root,
            operation_id=self.operation,
        )
        self.assertEqual(blockers, ())
        self.assertIsNotNone(loaded)

    def test_main_advance_requires_public_registration_and_rejects_tamper(self) -> None:
        with self.assertRaisesRegex(train.TrainError, "RegisteredIntegratedEvidence"):
            train.plan_main_advance(self.integration)

        plan = self.plan()
        receipt = self.apply(plan)
        clean = train.plan_main_advance(receipt)
        self.assertNotIn(
            "integrated-evidence-registration-required",
            {item.code for item in clean.blockers},
        )
        wrong_blob = self.helper.git(
            "hash-object",
            "-w",
            "--stdin",
            input_text='{"tampered":true}\n',
        ).stdout.strip()
        self.helper.git("update-ref", plan.evidence_ref, wrong_blob, plan.metadata_blob)

        blocked = train.plan_main_advance(receipt)

        self.assertTrue(
            {
                "integrated-evidence-metadata-invalid",
                "integrated-evidence-ref-drift",
                "integrated-evidence-registration-required",
            }
            & {item.code for item in blocked.blockers}
        )

    def test_main_cas_crash_recovery_binds_public_evidence_and_coordinator_loader(self) -> None:
        receipt = self.apply(self.plan())
        self.helper.bind_primary_for_main_advance()
        advance = train.plan_main_advance(receipt)
        self.assertEqual(advance.blockers, ())
        authorization = "AUTH-ADVANCE-REGISTRY"
        token = train.ConfirmationToken(
            schema_version=train.CONFIRM_TOKEN_SCHEMA,
            action="advance-main",
            subject_digest=advance.plan_digest,
            authorization_id=authorization,
            token_digest=train.confirmation_token_digest(
                "advance-main", advance.plan_digest, authorization
            ),
        )

        def crash(stage: str) -> None:
            if stage == "main-advance-after-refs":
                raise train.InjectedCrash(stage)

        with self.assertRaises(train.InjectedCrash):
            train.apply_main_advance(
                advance,
                accepted_plan_digest=advance.plan_digest,
                accepted_integrated_evidence_digest=receipt.registration_digest,
                confirmation_token=token,
                failpoint=crash,
            )
        recovered = train.apply_main_advance(
            advance,
            accepted_plan_digest=advance.plan_digest,
            accepted_integrated_evidence_digest=receipt.registration_digest,
            confirmation_token=token,
        )
        self.assertTrue(recovered.idempotent)
        for reference in (
            "refs/project-harness/v2/iterations/001/integrated",
            "refs/project-harness/v2/iterations/001/final",
        ):
            self.assertEqual(self.helper.oid(reference), receipt.metadata.integrated_commit)
        self.assertEqual(
            self.helper.oid(registry.iteration_final_evidence_ref("001")),
            receipt.evidence_blob,
        )

        refs = coordinator._refs(self.root)
        integrated, oid, blockers = coordinator._integrated_observation(
            self.root,
            "001",
            refs,
        )
        self.assertTrue(integrated, blockers)
        self.assertEqual(oid, receipt.metadata.integrated_commit)
        self.assertEqual(blockers, ())


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
