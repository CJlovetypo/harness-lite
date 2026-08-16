from __future__ import annotations

from dataclasses import replace
import unittest

from scripts import harness_merge_train as ordering
from scripts.tests.harness_authoritative_fixture import AuthoritativeIntegrationFixture


class MergeTrainOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AuthoritativeIntegrationFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_shuffled_candidates_are_stably_topologically_sorted(self) -> None:
        candidate = self.fixture.registered_candidate
        # The authoritative fixture has one real candidate.  Duplicate its
        # immutable public identity under two independent logical iterations
        # only for the pure ordering seam; the public gate deliberately blocks
        # those synthetic receipts, but ordering remains deterministic.
        b = replace(candidate, iteration="002", depends_on=())
        c = replace(candidate, iteration="003", depends_on=("002",))
        a = replace(candidate, iteration="001", depends_on=())
        queue = {
            "001": {"priority": 5, "queued_identity": "Q-002"},
            "002": {"priority": 5, "queued_identity": "Q-001"},
            "003": {"priority": 100, "queued_identity": "Q-000"},
        }
        first = ordering.plan_merge_train_order(
            self.fixture.root,
            candidates=(c, a, b),
            current_principle_sha256=self.fixture.principle_sha256,
            queue_metadata=queue,
        )
        second = ordering.plan_merge_train_order(
            self.fixture.root,
            candidates=(b, c, a),
            current_principle_sha256=self.fixture.principle_sha256,
            queue_metadata=queue,
        )
        self.assertEqual(first.ordered_iterations, ("002", "003", "001"))
        self.assertEqual(second.ordered_iterations, first.ordered_iterations)
        self.assertEqual(first.dependency_edges, (("002", "003"),))
        # Synthetic receipts are not promoted: the real public gate remains
        # part of the plan and records blockers for the altered identities.
        self.assertTrue(first.blockers)

    def test_real_candidate_is_publicly_gated_and_tamper_blocks(self) -> None:
        plan = ordering.plan_merge_train_order(
            self.fixture.root,
            candidates=(self.fixture.registered_candidate,),
            current_principle_sha256=self.fixture.principle_sha256,
        )
        self.assertTrue(plan.ready, plan.blockers)
        self.assertEqual(plan.ordered_iterations, ("001",))
        self.assertEqual(ordering.merge_train_order_gate(plan), ())
        tampered = replace(plan, ordered_iterations=("999",))
        codes = {item.code for item in ordering.merge_train_order_gate(tampered)}
        self.assertIn("merge-train-plan-digest", codes)
        forged = replace(tampered, plan_digest="0" * 64)
        forged = replace(
            forged,
            plan_digest=ordering.merge_train_order_plan_digest(forged),
        )
        forged_codes = {item.code for item in ordering.merge_train_order_gate(forged)}
        self.assertIn("merge-train-order-recomputed-drift", forged_codes)


if __name__ == "__main__":
    unittest.main()
