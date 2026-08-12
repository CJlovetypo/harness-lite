from __future__ import annotations

from dataclasses import replace
import unittest

from scripts.harness_candidate import (
    AcceptanceEvidence,
    CandidateInput,
    IdentityRebindInput,
    IntegrationInput,
    build_candidate,
    build_identity_rebinding,
    build_integrated_candidate,
    candidate_evidence_gate,
    candidate_freshness_gate,
    candidate_is_current,
    default_merge_arguments,
    identity_rebind_evidence_gate,
    integrated_evidence_gate,
    main_advance_gate,
)


OID_A = "a" * 40
OID_B = "b" * 40
OID_C = "c" * 40
OID_D = "d" * 40
OID_E = "e" * 40
OID_F = "f" * 40
DIGEST_P = "1" * 64
DIGEST_Q = "2" * 64
STRATEGY_DECLARATION = "3" * 64
GOVERNANCE_EVIDENCE = "4" * 64


def valid_candidate(iteration: str = "001", generation: str = "g1"):
    acceptance_id = f"AC-{iteration}-01"
    return build_candidate(
        CandidateInput(
            iteration=iteration,
            generation=generation,
            base_commit=OID_A,
            candidate_commit=OID_B if iteration == "001" else OID_F,
            candidate_tree=OID_C,
            principle_sha256=DIGEST_P,
            included_paths=("src/app.py", f"harness/iterations/{iteration}/prd-{iteration}.md"),
            acceptance_ids=(acceptance_id,),
            acceptance_evidence=(
                AcceptanceEvidence(
                    acceptance_id=acceptance_id,
                    evidence_ids=(f"evidence:{acceptance_id}:feature",),
                    verification_ids=(f"test:{acceptance_id}:feature",),
                ),
            ),
            verification_ids=(f"test:candidate:{iteration}:{generation}",),
            prd_approved=True,
            spec_approved=True,
            implementation_authorized=True,
            deviations_resolved=True,
            dirty_scope_owned=True,
        )
    )


def valid_rebind(candidate, *, generation: str = "i1", tree: str = OID_E):
    return build_identity_rebinding(
        IdentityRebindInput(
            source_candidate_evidence_digest=candidate.evidence_digest,
            source_candidate_commit=candidate.candidate_commit,
            source_candidate_tree=candidate.candidate_tree,
            integration_generation=generation,
            target_main=OID_A,
            integrated_commit=OID_D,
            integrated_tree=tree,
            principle_sha256=DIGEST_P,
            evidence_ids=(f"evidence:identity-rebound:{candidate.iteration}:{generation}",),
            verification_ids=(f"test:identity-rebound:{candidate.iteration}:{generation}",),
            explicitly_revalidated=True,
        )
    )


def integration_input(
    candidate,
    *,
    strategy: str = "merge-no-ff",
    preserved: bool = True,
    rebindings=(),
    declaration: str | None = None,
):
    return IntegrationInput(
        generation="i1",
        target_main=OID_A,
        integrated_commit=OID_D,
        integrated_tree=OID_E,
        principle_sha256=DIGEST_P,
        candidates=(candidate,),
        merge_strategy=strategy,
        strategy_declaration_digest=declaration,
        dependency_order=(candidate.iteration,),
        preserved_candidate_commits=(candidate.candidate_commit,) if preserved else (),
        identity_rebindings=tuple(rebindings),
        governance_reconciled=True,
        governance_evidence_digest=GOVERNANCE_EVIDENCE,
        cross_prd_verification_ids=("test:integration:i1",),
        integration_evidence_ids=("evidence:integration:i1",),
    )


class FeatureCandidateTests(unittest.TestCase):
    def test_candidate_requires_vertical_governance_ac_evidence_and_tests(self) -> None:
        candidate = build_candidate(
            CandidateInput(
                iteration="001",
                generation="g1",
                base_commit=OID_A,
                candidate_commit=OID_B,
                candidate_tree=OID_C,
                principle_sha256=DIGEST_P,
                included_paths=(),
                acceptance_ids=("AC-001-01",),
                acceptance_evidence=(),
                verification_ids=(),
                prd_approved=False,
                spec_approved=False,
                implementation_authorized=False,
                deviations_resolved=False,
                dirty_scope_owned=False,
            )
        )

        self.assertFalse(candidate.verified)
        self.assertIn("prd-not-approved", candidate.blockers)
        self.assertIn("included-paths-missing", candidate.blockers)
        self.assertIn("candidate-verification-missing", candidate.blockers)
        self.assertIn("acceptance-evidence-missing:AC-001-01", candidate.blockers)
        self.assertIn("acceptance-verification-missing:AC-001-01", candidate.blockers)

    def test_each_ac_requires_its_own_evidence_and_verification(self) -> None:
        candidate = build_candidate(
            CandidateInput(
                iteration="001",
                generation="g1",
                base_commit=OID_A,
                candidate_commit=OID_B,
                candidate_tree=OID_C,
                principle_sha256=DIGEST_P,
                included_paths=("src/app.py",),
                acceptance_ids=("AC-001-01", "AC-001-02"),
                acceptance_evidence=(
                    AcceptanceEvidence("AC-001-01", ("evidence:1",), ("test:1",)),
                    AcceptanceEvidence("AC-001-02", (), ()),
                ),
                verification_ids=("test:candidate",),
                prd_approved=True,
                spec_approved=True,
                implementation_authorized=True,
                deviations_resolved=True,
                dirty_scope_owned=True,
            )
        )

        self.assertFalse(candidate.verified)
        self.assertNotIn("acceptance-evidence-missing:AC-001-01", candidate.blockers)
        self.assertIn("acceptance-evidence-missing:AC-001-02", candidate.blockers)
        self.assertIn("acceptance-verification-missing:AC-001-02", candidate.blockers)

    def test_unknown_and_duplicate_ac_evidence_are_blocked(self) -> None:
        duplicate = AcceptanceEvidence("AC-001-01", ("evidence:1",), ("test:1",))
        candidate = build_candidate(
            CandidateInput(
                iteration="001",
                generation="g1",
                base_commit=OID_A,
                candidate_commit=OID_B,
                candidate_tree=OID_C,
                principle_sha256=DIGEST_P,
                included_paths=("src/app.py",),
                acceptance_ids=("AC-001-01",),
                acceptance_evidence=(
                    duplicate,
                    duplicate,
                    AcceptanceEvidence("AC-001-99", ("evidence:99",), ("test:99",)),
                ),
                verification_ids=("test:candidate",),
                prd_approved=True,
                spec_approved=True,
                implementation_authorized=True,
                deviations_resolved=True,
                dirty_scope_owned=True,
            )
        )

        self.assertIn("acceptance-evidence-duplicate:AC-001-01", candidate.blockers)
        self.assertIn("acceptance-evidence-unknown:AC-001-99", candidate.blockers)

    def test_candidate_digest_is_deterministic_and_tamper_evident(self) -> None:
        first = valid_candidate()
        second = valid_candidate()

        self.assertTrue(first.verified)
        self.assertRegex(first.evidence_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(first.evidence_digest, second.evidence_digest)
        self.assertTrue(candidate_evidence_gate(first).allowed)

        tampered = replace(first, candidate_tree=OID_D)
        gate = candidate_evidence_gate(tampered)
        self.assertFalse(gate.allowed)
        self.assertIn("candidate-evidence-digest-mismatch", gate.blockers)

    def test_candidate_freshness_reports_base_commit_tree_and_principle_staleness(self) -> None:
        candidate = valid_candidate()

        fresh = candidate_freshness_gate(
            candidate,
            current_base_commit=OID_A,
            current_candidate_commit=OID_B,
            current_candidate_tree=OID_C,
            current_principle_sha256=DIGEST_P,
        )
        stale = candidate_freshness_gate(
            candidate,
            current_base_commit=OID_F,
            current_candidate_commit=OID_D,
            current_candidate_tree=OID_E,
            current_principle_sha256=DIGEST_Q,
        )

        self.assertTrue(fresh.allowed)
        self.assertFalse(stale.allowed)
        self.assertEqual(
            set(stale.blockers),
            {
                "candidate-base-stale",
                "candidate-commit-stale",
                "candidate-tree-stale",
                "candidate-principle-stale",
            },
        )
        self.assertTrue(
            candidate_is_current(
                candidate,
                candidate_commit=OID_B,
                candidate_tree=OID_C,
                principle_sha256=DIGEST_P,
            )
        )


class IntegrationCandidateTests(unittest.TestCase):
    def test_default_merge_strategy_is_no_ff_and_no_commit(self) -> None:
        self.assertEqual(
            default_merge_arguments("refs/project-harness/v2/iterations/001/candidates/g1"),
            (
                "merge",
                "--no-ff",
                "--no-commit",
                "refs/project-harness/v2/iterations/001/candidates/g1",
            ),
        )

    def test_default_no_ff_with_preserved_candidate_identity_is_verified(self) -> None:
        candidate = valid_candidate()

        integrated = build_integrated_candidate(integration_input(candidate))

        self.assertTrue(integrated.verified)
        self.assertEqual(integrated.merge_strategy, "merge-no-ff")
        self.assertEqual(integrated.preserved_candidate_commits, (candidate.candidate_commit,))
        self.assertEqual(integrated.identity_rebind_digests, ())
        self.assertTrue(integrated_evidence_gate(integrated).allowed)

    def test_integration_requires_dependency_governance_cross_tests_and_evidence(self) -> None:
        candidate = valid_candidate()
        incomplete = IntegrationInput(
            generation="i1",
            target_main=OID_A,
            integrated_commit=OID_D,
            integrated_tree=OID_E,
            principle_sha256=DIGEST_P,
            candidates=(candidate,),
            preserved_candidate_commits=(candidate.candidate_commit,),
        )

        integrated = build_integrated_candidate(incomplete)

        self.assertFalse(integrated.verified)
        self.assertIn("dependency-order-missing", integrated.blockers)
        self.assertIn("governance-not-reconciled", integrated.blockers)
        self.assertIn("integration-verification-missing", integrated.blockers)
        self.assertIn("integration-evidence-missing", integrated.blockers)

    def test_candidate_principle_drift_blocks_integration(self) -> None:
        candidate = valid_candidate()
        value = replace(integration_input(candidate), principle_sha256=DIGEST_Q)

        integrated = build_integrated_candidate(value)

        self.assertFalse(integrated.verified)
        self.assertIn("principle-drift:001/g1", integrated.blockers)

    def test_changed_identity_without_rebinding_is_blocked_for_any_strategy(self) -> None:
        candidate = valid_candidate()

        integrated = build_integrated_candidate(
            integration_input(candidate, preserved=False)
        )

        self.assertFalse(integrated.verified)
        self.assertIn("candidate-identity-changed-rebind-required:001/g1", integrated.blockers)

    def test_non_default_strategy_must_be_explicitly_declared(self) -> None:
        candidate = valid_candidate()
        rebind = valid_rebind(candidate)

        integrated = build_integrated_candidate(
            integration_input(
                candidate,
                strategy="squash",
                preserved=False,
                rebindings=(rebind,),
                declaration=None,
            )
        )

        self.assertFalse(integrated.verified)
        self.assertIn("merge-strategy-not-explicitly-declared", integrated.blockers)
        self.assertNotIn("candidate-identity-changed-rebind-required:001/g1", integrated.blockers)

    def test_declared_squash_with_new_integrated_identity_revalidation_and_evidence_passes(self) -> None:
        candidate = valid_candidate()
        rebind = valid_rebind(candidate)

        integrated = build_integrated_candidate(
            integration_input(
                candidate,
                strategy="squash",
                preserved=False,
                rebindings=(rebind,),
                declaration=STRATEGY_DECLARATION,
            )
        )

        self.assertTrue(rebind.verified)
        self.assertTrue(identity_rebind_evidence_gate(rebind).allowed)
        self.assertTrue(integrated.verified)
        self.assertEqual(integrated.merge_strategy, "squash")
        self.assertEqual(integrated.identity_rebind_digests, (rebind.evidence_digest,))

    def test_alternative_strategy_can_preserve_identity_without_unnecessary_rebind(self) -> None:
        candidate = valid_candidate()

        integrated = build_integrated_candidate(
            integration_input(
                candidate,
                strategy="cherry-pick",
                preserved=True,
                declaration=STRATEGY_DECLARATION,
            )
        )

        self.assertTrue(integrated.verified)
        self.assertEqual(integrated.identity_rebind_digests, ())

    def test_rebind_without_explicit_revalidation_or_new_evidence_is_rejected(self) -> None:
        candidate = valid_candidate()
        invalid = build_identity_rebinding(
            IdentityRebindInput(
                source_candidate_evidence_digest=candidate.evidence_digest,
                source_candidate_commit=candidate.candidate_commit,
                source_candidate_tree=candidate.candidate_tree,
                integration_generation="i1",
                target_main=OID_A,
                integrated_commit=OID_D,
                integrated_tree=OID_E,
                principle_sha256=DIGEST_P,
                evidence_ids=(),
                verification_ids=(),
                explicitly_revalidated=False,
            )
        )

        integrated = build_integrated_candidate(
            integration_input(
                candidate,
                strategy="squash",
                preserved=False,
                rebindings=(invalid,),
                declaration=STRATEGY_DECLARATION,
            )
        )

        self.assertFalse(invalid.verified)
        self.assertIn("identity-revalidation-not-explicit", invalid.blockers)
        self.assertIn("identity-rebound-evidence-missing", invalid.blockers)
        self.assertIn("identity-revalidation-missing", invalid.blockers)
        self.assertFalse(integrated.verified)
        self.assertIn("identity-rebind-invalid:001/g1", integrated.blockers)

    def test_reusing_feature_evidence_or_tests_is_not_a_rebind(self) -> None:
        candidate = valid_candidate()
        acceptance = candidate.acceptance_evidence[0]
        reused = build_identity_rebinding(
            IdentityRebindInput(
                source_candidate_evidence_digest=candidate.evidence_digest,
                source_candidate_commit=candidate.candidate_commit,
                source_candidate_tree=candidate.candidate_tree,
                integration_generation="i1",
                target_main=OID_A,
                integrated_commit=OID_D,
                integrated_tree=OID_E,
                principle_sha256=DIGEST_P,
                evidence_ids=acceptance.evidence_ids,
                verification_ids=acceptance.verification_ids,
                explicitly_revalidated=True,
            )
        )

        integrated = build_integrated_candidate(
            integration_input(
                candidate,
                strategy="squash",
                preserved=False,
                rebindings=(reused,),
                declaration=STRATEGY_DECLARATION,
            )
        )

        self.assertFalse(integrated.verified)
        self.assertIn("identity-evidence-not-rebound:001/g1", integrated.blockers)
        self.assertIn("identity-revalidation-not-new:001/g1", integrated.blockers)

    def test_rebind_is_bound_to_exact_new_integrated_tree(self) -> None:
        candidate = valid_candidate()
        stale_rebind = valid_rebind(candidate, tree=OID_F)

        integrated = build_integrated_candidate(
            integration_input(
                candidate,
                strategy="squash",
                preserved=False,
                rebindings=(stale_rebind,),
                declaration=STRATEGY_DECLARATION,
            )
        )

        self.assertFalse(integrated.verified)
        self.assertIn("identity-rebind-tree-mismatch:001/g1", integrated.blockers)

    def test_identity_rebind_evidence_digest_detects_tampering(self) -> None:
        rebind = valid_rebind(valid_candidate())
        tampered = replace(rebind, integrated_tree=OID_F)

        gate = identity_rebind_evidence_gate(tampered)

        self.assertFalse(gate.allowed)
        self.assertIn("identity-rebind-evidence-digest-mismatch", gate.blockers)

    def test_integrated_evidence_digest_detects_tree_tampering(self) -> None:
        integrated = build_integrated_candidate(integration_input(valid_candidate()))
        tampered = replace(integrated, integrated_tree=OID_F)

        gate = integrated_evidence_gate(tampered)

        self.assertFalse(gate.allowed)
        self.assertIn("integrated-evidence-digest-mismatch", gate.blockers)


class MainAdvanceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = valid_candidate()
        self.integrated = build_integrated_candidate(integration_input(self.candidate))
        assert self.integrated.verified

    def gate(self, **overrides):
        values = {
            "current_main": OID_A,
            "current_integrated_commit": OID_D,
            "current_integrated_tree": OID_E,
            "current_principle_sha256": DIGEST_P,
            "current_candidate_digests": self.integrated.candidate_digests,
            "current_identity_rebind_digests": self.integrated.identity_rebind_digests,
            "user_accepted_evidence_digest": self.integrated.evidence_digest,
        }
        values.update(overrides)
        return main_advance_gate(self.integrated, **values)

    def test_exact_fresh_integrated_result_and_acceptance_allow_main_advance(self) -> None:
        decision = self.gate()

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.blockers, ())

    def test_exact_accepted_identity_rebound_result_can_advance(self) -> None:
        rebind = valid_rebind(self.candidate)
        integrated = build_integrated_candidate(
            integration_input(
                self.candidate,
                strategy="squash",
                preserved=False,
                rebindings=(rebind,),
                declaration=STRATEGY_DECLARATION,
            )
        )
        self.assertTrue(integrated.verified)

        decision = main_advance_gate(
            integrated,
            current_main=OID_A,
            current_integrated_commit=OID_D,
            current_integrated_tree=OID_E,
            current_principle_sha256=DIGEST_P,
            current_candidate_digests=integrated.candidate_digests,
            current_identity_rebind_digests=integrated.identity_rebind_digests,
            user_accepted_evidence_digest=integrated.evidence_digest,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.blockers, ())

    def test_main_tree_principle_candidate_and_acceptance_staleness_all_block(self) -> None:
        decision = self.gate(
            current_main=OID_B,
            current_integrated_commit=OID_F,
            current_integrated_tree=OID_C,
            current_principle_sha256=DIGEST_Q,
            current_candidate_digests=("9" * 64,),
            current_identity_rebind_digests=("8" * 64,),
            user_accepted_evidence_digest=None,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            set(decision.blockers),
            {
                "main-drift",
                "integrated-commit-drift",
                "integrated-tree-drift",
                "integrated-principle-drift",
                "integrated-candidate-set-stale",
                "integrated-rebind-evidence-stale",
                "final-acceptance-missing-or-stale",
            },
        )

    def test_acceptance_of_previous_integrated_digest_does_not_authorize_rebuilt_result(self) -> None:
        accepted_old_digest = self.integrated.evidence_digest
        rebuilt = build_integrated_candidate(
            replace(
                integration_input(self.candidate),
                generation="i2",
                integrated_commit=OID_F,
                cross_prd_verification_ids=("test:integration:i2",),
                integration_evidence_ids=("evidence:integration:i2",),
            )
        )
        self.assertTrue(rebuilt.verified)

        decision = main_advance_gate(
            rebuilt,
            current_main=OID_A,
            current_integrated_commit=OID_F,
            current_integrated_tree=OID_E,
            current_principle_sha256=DIGEST_P,
            current_candidate_digests=rebuilt.candidate_digests,
            current_identity_rebind_digests=rebuilt.identity_rebind_digests,
            user_accepted_evidence_digest=accepted_old_digest,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("final-acceptance-missing-or-stale", decision.blockers)

    def test_tampered_integrated_evidence_cannot_be_advanced_even_with_matching_stale_digest(self) -> None:
        tampered = replace(self.integrated, integrated_tree=OID_F)

        decision = main_advance_gate(
            tampered,
            current_main=OID_A,
            current_integrated_commit=OID_D,
            current_integrated_tree=OID_F,
            current_principle_sha256=DIGEST_P,
            current_candidate_digests=tampered.candidate_digests,
            current_identity_rebind_digests=tampered.identity_rebind_digests,
            user_accepted_evidence_digest=tampered.evidence_digest,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("integrated-evidence-digest-mismatch", decision.blockers)


if __name__ == "__main__":
    unittest.main()
