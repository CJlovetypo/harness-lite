from __future__ import annotations

import unittest

from scripts import harness_coordinator as coordinator
from scripts import harness_integrated_evidence as registry
from scripts import harness_train as train
from scripts.tests.harness_authoritative_fixture import AuthoritativeIntegrationFixture


class FastIntegratedEvidenceFlowTests(unittest.TestCase):
    """Focused public-registry/main-CAS coverage without the full workspace flow."""

    def setUp(self) -> None:
        self.fixture = AuthoritativeIntegrationFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_main_cas_success_materializes_coordinator_integrated_authority(self) -> None:
        receipt = self.fixture.publish_integrated_evidence()
        plan = self.fixture.plan_main_advance(receipt)

        result = train.apply_main_advance(
            plan,
            accepted_plan_digest=plan.plan_digest,
            accepted_integrated_evidence_digest=receipt.registration_digest,
            confirmation_token=self.fixture.advance_token(plan),
        )

        self.assertFalse(result.idempotent)
        self.assertEqual(
            self.fixture.oid("refs/heads/main"),
            receipt.metadata.integrated_commit,
        )
        self.assertEqual(
            self.fixture.oid("refs/project-harness/v2/iterations/001/integrated"),
            receipt.metadata.integrated_commit,
        )
        self.assertEqual(
            self.fixture.oid("refs/project-harness/v2/iterations/001/final"),
            receipt.metadata.integrated_commit,
        )
        self.assertEqual(
            self.fixture.oid(registry.iteration_final_evidence_ref("001")),
            result.final_acceptance_evidence_blob,
        )
        self.fixture.write_iteration_bundle()
        authority = coordinator.derive_iteration_authority(self.fixture.root, "001")
        self.assertTrue(authority.integrated, authority.blockers)
        self.assertEqual(authority.integrated_object, receipt.metadata.integrated_commit)

    def test_crash_after_ref_transaction_recovers_without_duplicate_refs(self) -> None:
        receipt = self.fixture.publish_integrated_evidence()
        plan = self.fixture.plan_main_advance(receipt)
        token = self.fixture.advance_token(plan)

        def crash(stage: str) -> None:
            if stage == "main-advance-after-refs":
                raise train.InjectedCrash(stage)

        with self.assertRaises(train.InjectedCrash):
            train.apply_main_advance(
                plan,
                accepted_plan_digest=plan.plan_digest,
                accepted_integrated_evidence_digest=receipt.registration_digest,
                confirmation_token=token,
                failpoint=crash,
            )
        before = self.fixture.git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/project-harness/v2",
        ).stdout

        recovered = train.apply_main_advance(
            plan,
            accepted_plan_digest=plan.plan_digest,
            accepted_integrated_evidence_digest=receipt.registration_digest,
            confirmation_token=token,
        )

        self.assertTrue(recovered.idempotent)
        self.assertEqual(
            self.fixture.git(
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/project-harness/v2",
            ).stdout,
            before,
        )

    def test_registry_tamper_blocks_main_plan(self) -> None:
        receipt = self.fixture.publish_integrated_evidence()
        wrong_blob = self.fixture.git(
            "hash-object",
            "-w",
            "--stdin",
            input_text='{"tampered":true}\n',
        ).stdout.strip()
        self.fixture.git(
            "update-ref",
            receipt.evidence_ref,
            wrong_blob,
            receipt.evidence_blob,
        )

        plan = train.plan_main_advance(receipt)

        self.assertTrue(plan.blockers)
        self.assertTrue(
            {
                "integrated-evidence-metadata-invalid",
                "integrated-evidence-ref-drift",
                "integrated-evidence-registration-required",
            }
            & {item.code for item in plan.blockers}
        )


if __name__ == "__main__":
    unittest.main()
