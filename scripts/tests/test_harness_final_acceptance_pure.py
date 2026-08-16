from __future__ import annotations

import unittest

from scripts import harness_final_acceptance as final_registry
from scripts import harness_integrated_evidence as integrated_registry


class FinalAcceptancePureBindingTests(unittest.TestCase):
    def fixture(self):
        # These helpers are pure: only the candidate iteration and progress
        # bindings are observed.  Minimal structural objects keep this test
        # under a second and avoid the real-Git fixture.
        candidates = (
            final_registry.AcceptedCandidate(
                iteration="001",
                generation="g1",
                candidate_ref="refs/candidate/001",
                candidate_commit="1" * 40,
                candidate_evidence_ref="refs/evidence/001",
                candidate_evidence_blob="2" * 40,
                candidate_registration_digest="3" * 64,
                principle_gate_binding_digest="4" * 64,
            ),
            final_registry.AcceptedCandidate(
                iteration="002",
                generation="g1",
                candidate_ref="refs/candidate/002",
                candidate_commit="5" * 40,
                candidate_evidence_ref="refs/evidence/002",
                candidate_evidence_blob="6" * 40,
                candidate_registration_digest="7" * 64,
                principle_gate_binding_digest="8" * 64,
            ),
        )
        bindings = (
            integrated_registry.ProgressBinding(
                schema_version=integrated_registry.PROGRESS_BINDING_SCHEMA,
                event_id="EV-" + "a" * 64,
                ref_name=final_registry.iteration_final_evidence_ref("001"),
            ),
            integrated_registry.ProgressBinding(
                schema_version=integrated_registry.PROGRESS_BINDING_SCHEMA,
                event_id="EV-" + "b" * 64,
                ref_name="refs/project-harness/v2/iterations/001/integrated-evidence/i1",
            ),
            integrated_registry.ProgressBinding(
                schema_version=integrated_registry.PROGRESS_BINDING_SCHEMA,
                event_id="EV-" + "c" * 64,
                ref_name=final_registry.iteration_final_evidence_ref("002"),
            ),
        )
        metadata = type("Metadata", (), {"progress_bindings": bindings})()
        integrated = type("Integrated", (), {"metadata": metadata})()
        return integrated, candidates

    def test_hash_event_ids_are_extracted_by_canonical_final_ref(self) -> None:
        integrated, candidates = self.fixture()
        self.assertEqual(
            final_registry._normalize_event_ids(integrated, candidates, ()),
            ("EV-" + "a" * 64, "EV-" + "c" * 64),
        )
        self.assertEqual(
            final_registry._normalize_event_ids(
                integrated,
                candidates,
                ("EV-" + "a" * 64, "EV-" + "c" * 64),
            ),
            ("EV-" + "a" * 64, "EV-" + "c" * 64),
        )

    def test_missing_wrong_or_duplicate_final_binding_fails_closed(self) -> None:
        integrated, candidates = self.fixture()
        missing_metadata = type(
            "Metadata",
            (),
            {"progress_bindings": integrated.metadata.progress_bindings[:-1]},
        )()
        with self.assertRaisesRegex(final_registry.FinalAcceptanceError, "missing"):
            final_registry._normalize_event_ids(
                type("Integrated", (), {"metadata": missing_metadata})(),
                candidates,
                (),
            )
        with self.assertRaisesRegex(final_registry.FinalAcceptanceError, "differ"):
            final_registry._normalize_event_ids(
                integrated,
                candidates,
                ("EV-" + "c" * 64, "EV-" + "a" * 64),
            )


if __name__ == "__main__":
    unittest.main()
