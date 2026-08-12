from __future__ import annotations

import unittest

from scripts.harness_decision import (
    AuthorizationState,
    DecisionInput,
    RiskVector,
    action_level,
    classify,
)


class HarnessDecisionTests(unittest.TestCase):
    def test_small_clear_co_drafts_but_does_not_authorize(self) -> None:
        result = classify(
            DecisionInput(
                read_only=False,
                risk=RiskVector(localized_impact=True, straightforward_rollback=True),
            )
        )
        self.assertEqual(result.governance_path, "co-draft")
        self.assertEqual(result.execution_topology, "local")
        self.assertEqual(result.authorization_gate, "approve-prd")
        self.assertFalse(result.inferred_authorization)

    def test_ambiguity_grills_even_when_change_looks_small(self) -> None:
        result = classify(
            DecisionInput(
                read_only=False,
                ambiguities=("export format", "PII visibility"),
                risk=RiskVector(localized_impact=True, straightforward_rollback=True),
            )
        )
        self.assertEqual(result.governance_path, "grill")
        self.assertIn("ambiguity:PII visibility", result.blocking_reasons)

    def test_unknown_risk_fails_upward_to_grill(self) -> None:
        result = classify(
            DecisionInput(
                read_only=False,
                risk=RiskVector(
                    localized_impact=True,
                    straightforward_rollback=True,
                    unknowns=("data retention impact",),
                ),
            )
        )
        self.assertEqual(result.governance_path, "grill")
        self.assertIn("risk-unknown:data retention impact", result.blocking_reasons)

    def test_clear_public_contract_change_is_prd_first(self) -> None:
        result = classify(
            DecisionInput(
                read_only=False,
                risk=RiskVector(
                    public_contract=True,
                    compatibility=True,
                    cross_system_coordination=True,
                    straightforward_rollback=True,
                ),
            )
        )
        self.assertEqual(result.governance_path, "prd-first")
        self.assertIn("risk:public_contract", result.reason_codes)
        self.assertNotEqual(result.governance_path, "grill")

    def test_topology_is_independent_of_governance_path(self) -> None:
        result = classify(
            DecisionInput(
                read_only=False,
                ambiguities=("failure behavior",),
                risk=RiskVector(localized_impact=True, straightforward_rollback=True),
                active_writers=2,
            )
        )
        self.assertEqual(result.governance_path, "grill")
        self.assertEqual(result.execution_topology, "independent-worktree")

    def test_first_writer_is_local_and_additional_writer_is_worktree(self) -> None:
        first = classify(DecisionInput(read_only=False, active_writers=0))
        second = classify(DecisionInput(read_only=False, active_writers=1))
        third = classify(DecisionInput(read_only=False, active_writers=2))
        self.assertEqual(first.execution_topology, "local")
        self.assertEqual(second.execution_topology, "independent-worktree")
        self.assertEqual(third.execution_topology, "independent-worktree")

    def test_dependency_and_global_barriers_override_writer_count(self) -> None:
        stacked = classify(
            DecisionInput(read_only=False, active_writers=0, depends_on=("PRD-002",))
        )
        serialized = classify(
            DecisionInput(read_only=False, active_writers=0, principle_change=True)
        )
        self.assertEqual(stacked.execution_topology, "stacked-worktree")
        self.assertEqual(serialized.execution_topology, "serialize")
        self.assertIn("global-principle-barrier", serialized.blocking_reasons)

    def test_authorization_gates_advance_without_inference(self) -> None:
        cases = (
            (AuthorizationState(), "approve-prd"),
            (AuthorizationState(prd_approved=True), "approve-spec"),
            (
                AuthorizationState(prd_approved=True, spec_approved=True),
                "authorize-implementation",
            ),
            (
                AuthorizationState(
                    prd_approved=True,
                    spec_approved=True,
                    implementation_authorized=True,
                ),
                "candidate-verification",
            ),
        )
        for state, expected in cases:
            with self.subTest(expected=expected):
                result = classify(DecisionInput(read_only=False, authorization=state))
                self.assertEqual(result.authorization_gate, expected)
                self.assertFalse(result.inferred_authorization)

    def test_action_levels_are_fail_closed(self) -> None:
        self.assertEqual(action_level("read-status"), "silent")
        self.assertEqual(action_level("create-worktree"), "notify")
        self.assertEqual(action_level("commit"), "confirm")
        self.assertEqual(action_level("push"), "confirm")
        self.assertEqual(action_level("unknown-new-mutation"), "confirm")


if __name__ == "__main__":
    unittest.main()
