from __future__ import annotations

from dataclasses import replace
import unittest

from scripts import harness_coordinator as coordinator
from scripts import harness_final_acceptance as final_registry
from scripts import harness_train as train
from scripts.tests.harness_authoritative_fixture import AuthoritativeIntegrationFixture


class FinalAcceptanceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AuthoritativeIntegrationFixture()
        self.root = self.fixture.root
        self.integrated = self.fixture.publish_integrated_evidence()
        self.main_plan = self.fixture.plan_main_advance(self.integrated)
        self.confirmation = self.fixture.advance_token(self.main_plan)

    def tearDown(self) -> None:
        self.fixture.close()

    def plan(self) -> final_registry.FinalAcceptancePlan:
        result = final_registry.plan_final_acceptance(
            self.root,
            main_plan=self.main_plan,
            integrated=self.integrated,
            confirmation=self.confirmation,
        )
        self.assertTrue(result.ready, result.blockers)
        return result

    def test_missing_final_acceptance_fails_closed(self) -> None:
        receipt, blockers = final_registry.load_registered_final_acceptance(
            self.root,
            operation_id=self.integrated.operation_id,
        )
        self.assertIsNone(receipt)
        self.assertIn("final-acceptance-missing", {item.code for item in blockers})

        refs = coordinator._refs(self.root)
        integrated, _oid, observation = coordinator._integrated_observation(
            self.root,
            "001",
            refs,
        )
        self.assertFalse(integrated)
        self.assertEqual(observation, ())

        # Commit-shaped final refs alone are not acceptance authority.
        self.fixture.git(
            "update-ref",
            "refs/project-harness/v2/iterations/001/integrated",
            self.main_plan.integrated_commit,
        )
        self.fixture.git(
            "update-ref",
            "refs/project-harness/v2/iterations/001/final",
            self.main_plan.integrated_commit,
        )
        integrated, _oid, observation = coordinator._integrated_observation(
            self.root,
            "001",
            coordinator._refs(self.root),
        )
        self.assertFalse(integrated)
        self.assertIn("final-acceptance-envelope-missing", observation)

    def test_success_binds_user_integrated_main_candidates_principle_and_refs(self) -> None:
        plan = self.plan()
        metadata = plan.metadata
        self.assertEqual(metadata.confirmation_action, "advance-main")
        self.assertEqual(metadata.confirmation_subject_digest, self.main_plan.plan_digest)
        self.assertEqual(
            metadata.confirmation_authorization_id,
            self.confirmation.authorization_id,
        )
        self.assertEqual(metadata.confirmation_token_digest, self.confirmation.token_digest)
        self.assertEqual(
            metadata.integrated_registration_digest,
            self.integrated.registration_digest,
        )
        self.assertEqual(metadata.main_plan_digest, self.main_plan.plan_digest)
        self.assertEqual(metadata.previous_main, self.main_plan.expected_main)
        self.assertEqual(metadata.accepted_main, self.main_plan.integrated_commit)
        self.assertEqual(metadata.principle_sha256, self.main_plan.principle_sha256)
        self.assertEqual(
            tuple((item.candidate_ref, item.candidate_commit) for item in metadata.accepted_candidates),
            self.main_plan.candidate_refs,
        )

        receipt = final_registry.apply_final_acceptance(
            plan,
            accepted_plan_digest=plan.plan_digest,
            confirmation=self.confirmation,
        )
        self.assertFalse(receipt.idempotent)
        self.assertNotEqual(receipt.evidence_blob, self.integrated.evidence_blob)
        self.assertEqual(self.fixture.oid(self.main_plan.main_ref), self.main_plan.integrated_commit)
        for reference, _old, new in self.main_plan.ref_updates:
            self.assertEqual(self.fixture.oid(reference), new)
        self.assertEqual(self.fixture.oid(receipt.evidence_ref), receipt.evidence_blob)
        for item in receipt.iteration_evidence_refs:
            self.assertEqual(self.fixture.oid(item.ref_name), receipt.evidence_blob)

        # Private journals are recovery aids, not public authority.
        final_registry.journal_path(self.root, receipt.operation_id).unlink()
        loaded, blockers = final_registry.load_registered_final_acceptance(
            self.root,
            operation_id=receipt.operation_id,
        )
        self.assertEqual(blockers, ())
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.registration_digest, receipt.registration_digest)

    def test_wrong_or_tampered_confirmation_is_rejected_before_cas(self) -> None:
        wrong = replace(
            self.confirmation,
            authorization_id="AUTH-ADVANCE-WRONG",
        )
        blocked = final_registry.plan_final_acceptance(
            self.root,
            main_plan=self.main_plan,
            integrated=self.integrated,
            confirmation=wrong,
        )
        self.assertIn("confirmation-token-digest", {item.code for item in blocked.blockers})
        with self.assertRaisesRegex(final_registry.FinalAcceptanceError, "blocked"):
            final_registry.apply_final_acceptance(
                blocked,
                accepted_plan_digest=blocked.plan_digest,
                confirmation=wrong,
            )
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.expected_main)
        self.assertIsNone(
            self.fixture.git(
                "rev-parse",
                "--verify",
                final_registry.operation_final_evidence_ref(self.integrated.operation_id),
                check=False,
            ).stdout.strip()
            or None
        )

    def test_forged_self_hashed_plan_is_rejected_before_any_ref_mutation(self) -> None:
        plan = self.plan()
        forged_update = final_registry.RefUpdate(
            "refs/project-harness/v2/iterations/999/integrated",
            None,
            self.main_plan.integrated_commit,
        )
        forged_metadata = replace(
            plan.metadata,
            main_ref_updates=(*plan.metadata.main_ref_updates, forged_update),
            metadata_digest="0" * 64,
        )
        forged_metadata = replace(
            forged_metadata,
            metadata_digest=final_registry.metadata_digest(forged_metadata),
        )
        raw = final_registry.metadata_bytes(forged_metadata)
        forged_blob = self.fixture.git_bytes(
            "hash-object",
            "--stdin",
            input_bytes=raw,
        ).decode("ascii").strip()
        forged = replace(
            plan,
            metadata=forged_metadata,
            metadata_blob=forged_blob,
            plan_digest="0" * 64,
        )
        forged = replace(
            forged,
            plan_digest=final_registry.final_acceptance_plan_digest(forged),
        )
        with self.assertRaisesRegex(final_registry.FinalAcceptanceError, "authority"):
            final_registry.apply_final_acceptance(
                forged,
                accepted_plan_digest=forged.plan_digest,
                confirmation=self.confirmation,
            )
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.expected_main)
        self.assertNotEqual(
            self.fixture.git(
                "rev-parse",
                "--verify",
                forged_update.ref_name,
                check=False,
            ).returncode,
            0,
        )
        self.assertFalse(
            final_registry.journal_path(self.root, plan.operation_id).exists(),
            "a rejected forged plan must not reserve the canonical recovery journal",
        )

    def test_same_operation_different_plan_cannot_overwrite_recovery_journal(self) -> None:
        first = self.plan()
        second_confirmation = self.fixture._token(
            "advance-main",
            self.main_plan.plan_digest,
            "AUTH-ADVANCE-SECOND",
        )
        second = final_registry.plan_final_acceptance(
            self.root,
            main_plan=self.main_plan,
            integrated=self.integrated,
            confirmation=second_confirmation,
        )
        self.assertTrue(second.ready, second.blockers)
        self.assertNotEqual(first.plan_digest, second.plan_digest)

        def stop_after_journal(stage: str) -> None:
            if stage == "final-acceptance-after-journal":
                raise final_registry.InjectedCrash(stage)

        with self.assertRaises(final_registry.InjectedCrash):
            final_registry.apply_final_acceptance(
                first,
                accepted_plan_digest=first.plan_digest,
                confirmation=self.confirmation,
                failpoint=stop_after_journal,
            )
        with self.assertRaisesRegex(final_registry.FinalAcceptanceError, "old or foreign"):
            final_registry.apply_final_acceptance(
                second,
                accepted_plan_digest=second.plan_digest,
                confirmation=second_confirmation,
            )
        self.assertEqual(
            final_registry.load_final_acceptance_plan(
                self.root,
                operation_id=first.operation_id,
            ),
            first,
        )
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.expected_main)

    def test_direct_apply_rechecks_missing_lease_before_any_ref_mutation(self) -> None:
        plan = self.plan()
        lease = (
            self.fixture.common_dir
            / "project-harness"
            / "train"
            / "v1"
            / "leases"
            / "main-integration.json"
        )
        lease.unlink()
        with self.assertRaisesRegex(final_registry.FinalAcceptanceError, "integration-lease"):
            final_registry.apply_final_acceptance(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation=self.confirmation,
            )
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.expected_main)
        missing = self.fixture.git(
            "rev-parse",
            "--verify",
            final_registry.operation_final_evidence_ref(plan.operation_id),
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(
            final_registry.journal_path(self.root, plan.operation_id).exists(),
            "a stale integration lease must be rejected before journal ownership",
        )

    def test_direct_apply_rechecks_main_checkout_before_any_ref_mutation(self) -> None:
        plan = self.plan()
        checked_out = self.fixture.container / "main checkout"
        self.fixture.git("worktree", "add", str(checked_out), "main")
        with self.assertRaisesRegex(final_registry.FinalAcceptanceError, "main-checked-out"):
            final_registry.apply_final_acceptance(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation=self.confirmation,
            )
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.expected_main)
        missing = self.fixture.git(
            "rev-parse",
            "--verify",
            final_registry.operation_final_evidence_ref(plan.operation_id),
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(
            final_registry.journal_path(self.root, plan.operation_id).exists(),
            "a checked-out main must be rejected before journal ownership",
        )

    def test_public_ref_tamper_blocks_loader_and_coordinator(self) -> None:
        plan = self.plan()
        receipt = final_registry.apply_final_acceptance(
            plan,
            accepted_plan_digest=plan.plan_digest,
            confirmation=self.confirmation,
        )
        self.fixture.git(
            "update-ref",
            receipt.evidence_ref,
            self.integrated.evidence_blob,
            receipt.evidence_blob,
        )
        loaded, blockers = final_registry.load_registered_final_acceptance(
            self.root,
            operation_id=receipt.operation_id,
        )
        self.assertIsNone(loaded)
        self.assertTrue(
            {"final-acceptance-metadata-invalid", "final-acceptance-operation-mismatch"}
            & {item.code for item in blockers},
            blockers,
        )
        accepted, _oid, observation = coordinator._integrated_observation(
            self.root,
            "001",
            coordinator._refs(self.root),
        )
        self.assertFalse(accepted)
        self.assertTrue(any("final-acceptance" in item for item in observation), observation)

    def test_crash_after_refs_is_idempotently_reconstructed(self) -> None:
        plan = self.plan()

        def failpoint(stage: str) -> None:
            if stage == "final-acceptance-after-refs":
                raise final_registry.InjectedCrash(stage)

        with self.assertRaises(final_registry.InjectedCrash):
            final_registry.apply_final_acceptance(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation=self.confirmation,
                failpoint=failpoint,
            )
        durable_plan = final_registry.load_final_acceptance_plan(
            self.root,
            operation_id=plan.operation_id,
        )
        self.assertEqual(durable_plan, plan)
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.integrated_commit)
        recovered = final_registry.apply_final_acceptance(
            plan,
            accepted_plan_digest=plan.plan_digest,
            confirmation=self.confirmation,
        )
        self.assertTrue(recovered.idempotent)
        loaded, blockers = final_registry.load_registered_final_acceptance(
            self.root,
            operation_id=recovered.operation_id,
        )
        self.assertEqual(blockers, ())
        self.assertIsNotNone(loaded)
        assert loaded is not None
        projected = final_registry.main_advance_result_from_final_acceptance(loaded)
        self.assertEqual(projected.current_main, self.main_plan.integrated_commit)
        self.assertEqual(projected.final_acceptance_digest, loaded.registration_digest)
        self.assertIn(loaded.evidence_ref, projected.updated_refs)

    def test_train_pre_cas_crash_leaves_no_wrapper_journal_and_retry_succeeds(self) -> None:
        def failpoint(stage: str) -> None:
            if stage == "main-advance-after-journal":
                raise train.InjectedCrash(stage)

        wrapper_journal = train._journal_path(
            train.open_repository(self.root),
            "advance",
            self.main_plan.operation_id,
        )
        with self.assertRaises(train.InjectedCrash):
            train.apply_main_advance(
                self.main_plan,
                accepted_plan_digest=self.main_plan.plan_digest,
                accepted_integrated_evidence_digest=self.integrated.registration_digest,
                confirmation_token=self.confirmation,
                failpoint=failpoint,
            )
        self.assertFalse(wrapper_journal.exists())
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.expected_main)

        recovered = train.apply_main_advance(
            self.main_plan,
            accepted_plan_digest=self.main_plan.plan_digest,
            accepted_integrated_evidence_digest=self.integrated.registration_digest,
            confirmation_token=self.confirmation,
        )
        self.assertFalse(recovered.idempotent)
        self.assertTrue(wrapper_journal.exists())
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.integrated_commit)

    def test_train_main_advance_publishes_final_envelope_and_coordinator_accepts(self) -> None:
        result = train.apply_main_advance(
            self.main_plan,
            accepted_plan_digest=self.main_plan.plan_digest,
            accepted_integrated_evidence_digest=self.integrated.registration_digest,
            confirmation_token=self.confirmation,
        )
        self.assertIsNotNone(result.final_acceptance_digest)
        self.assertIsNotNone(result.final_acceptance_evidence_blob)
        self.assertIsNotNone(result.final_acceptance_evidence_ref)
        self.assertIn(result.final_acceptance_evidence_ref, result.updated_refs)

        accepted, commit, blockers = coordinator._integrated_observation(
            self.root,
            "001",
            coordinator._refs(self.root),
        )
        self.assertTrue(accepted, blockers)
        self.assertEqual(commit, self.main_plan.integrated_commit)
        self.assertEqual(blockers, ())

        retry = train.apply_main_advance(
            self.main_plan,
            accepted_plan_digest=self.main_plan.plan_digest,
            accepted_integrated_evidence_digest=self.integrated.registration_digest,
            confirmation_token=self.confirmation,
        )
        self.assertTrue(retry.idempotent)
        self.assertEqual(retry.final_acceptance_digest, result.final_acceptance_digest)

    def test_train_crash_after_atomic_refs_recovers_without_second_advance(self) -> None:
        def failpoint(stage: str) -> None:
            if stage == "final-acceptance-after-refs":
                raise train.InjectedCrash(stage)

        with self.assertRaises(train.InjectedCrash):
            train.apply_main_advance(
                self.main_plan,
                accepted_plan_digest=self.main_plan.plan_digest,
                accepted_integrated_evidence_digest=self.integrated.registration_digest,
                confirmation_token=self.confirmation,
                failpoint=failpoint,
            )
        self.assertEqual(self.fixture.oid("refs/heads/main"), self.main_plan.integrated_commit)
        retry = train.apply_main_advance(
            self.main_plan,
            accepted_plan_digest=self.main_plan.plan_digest,
            accepted_integrated_evidence_digest=self.integrated.registration_digest,
            confirmation_token=self.confirmation,
        )
        self.assertTrue(retry.idempotent)


if __name__ == "__main__":
    unittest.main()
