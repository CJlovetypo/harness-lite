from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts import harness_train_governance as train_governance
from scripts import harness_workspace as workspace
from scripts.tests import test_harness_train_governance as train_fixture

# The governance adapter supports direct-script execution and therefore owns
# the canonical module instance used for its dataclass identities.
readme_authority = train_governance.harness_readme_authority
reconcile = train_governance.harness_reconcile


def _empty_managed(content: bytes, start: str, end: str) -> bytes:
    pattern = re.compile(
        re.escape(start.encode("utf-8"))
        + rb"(?:\r?\n)[\s\S]*?"
        + re.escape(end.encode("utf-8"))
    )
    matches = tuple(pattern.finditer(content))
    if len(matches) != 1:
        raise AssertionError(f"expected one managed section: {start}")
    newline = b"\r\n" if b"\r\n" in matches[0].group(0) else b"\n"
    return (
        content[: matches[0].start()]
        + start.encode("utf-8")
        + newline
        + end.encode("utf-8")
        + content[matches[0].end() :]
    )


def _bounded_l1(number: str, manual: str, managed: str) -> bytes:
    return (
        b"<!-- managed-by: harness-lite v1 -->\n"
        + f"# Iteration {number}\n\n{manual}\n\n".encode("utf-8")
        + readme_authority.L1_START.encode("utf-8")
        + b"\n"
        + managed.encode("utf-8")
        + b"\n"
        + readme_authority.L1_END.encode("utf-8")
        + b"\n\nhandwritten-tail\n"
    )


class ReadmeOuterAuthorityPureTests(unittest.TestCase):
    @staticmethod
    def projection() -> object:
        return readme_authority.IterationProjection(
            number="001",
            title="Product 001",
            prd_status="approved",
            spec_status="approved",
            open_deviations=0,
            depends_on=(),
            workspace="Local",
            governance_gate="candidate evidence",
            candidate_state="none",
            integration_state="not-integrated",
            result="implementation active",
            next_step="verify",
            recent_events=(),
            source_commit="1" * 40,
        )

    def test_candidate_handwritten_outer_bytes_survive_with_current_managed_body(self) -> None:
        path = "harness/iterations/001/README.md"
        base = _bounded_l1("001", "manual-base", "managed-base")
        current = _bounded_l1("001", "manual-base", "managed-from-progress")
        candidate = _bounded_l1("001", "manual-feature-note", "stale-candidate")

        result = train_governance._merge_readme_outer_document(
            path,
            base=base,
            current=current,
            candidate=candidate,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn(b"manual-feature-note", result)
        self.assertIn(b"handwritten-tail", result)
        self.assertIn(b"managed-from-progress", result)
        self.assertNotIn(b"stale-candidate", result)

    def test_conflicting_handwritten_outer_edits_block_instead_of_choosing_main(self) -> None:
        path = "harness/iterations/001/README.md"
        base = _bounded_l1("001", "manual-base", "managed-base")
        main = _bounded_l1("001", "manual-main", "managed-main")
        candidate = _bounded_l1("001", "manual-candidate", "managed-candidate")

        with self.assertRaisesRegex(
            train_governance.GovernanceAdapterError,
            "changed README outer bytes differently",
        ):
            train_governance._merge_readme_outer_document(
                path,
                base=base,
                current=main,
                candidate=candidate,
            )

    def test_markerless_l1_with_user_notes_requires_explicit_migration(self) -> None:
        source = (
            b"<!-- managed-by: harness-lite v1 -->\n# Iteration 001\n\n"
            b"## Current result\nlegacy\n\n## User Notes\nkeep-this-byte-exact\n"
        )
        with self.assertRaisesRegex(
            readme_authority.ReadmeAuthorityError,
            "explicit byte-preserving README migration",
        ):
            readme_authority._render_l1(source, self.projection(), "a" * 64)
        self.assertIn(b"keep-this-byte-exact", source)

    def test_current_integrated_and_candidate_selection_never_use_name_sorting(self) -> None:
        current = SimpleNamespace(
            metadata=SimpleNamespace(
                main_ref="refs/heads/main",
                target_main="1" * 40,
                principle_sha256="2" * 64,
            )
        )
        stale = SimpleNamespace(
            metadata=SimpleNamespace(
                main_ref="refs/heads/main",
                target_main="3" * 40,
                principle_sha256="2" * 64,
            )
        )
        arguments = {
            "current_main_ref": "refs/heads/main",
            "current_main": "1" * 40,
            "principle_sha256": "2" * 64,
        }
        self.assertTrue(readme_authority._integrated_receipt_is_current(current, **arguments))
        self.assertFalse(readme_authority._integrated_receipt_is_current(stale, **arguments))
        candidates = (
            SimpleNamespace(candidate_ref="refs/candidates/release-z", candidate_commit="4" * 40),
            SimpleNamespace(candidate_ref="refs/candidates/g2", candidate_commit="5" * 40),
        )
        self.assertIsNone(readme_authority._single_verified_candidate(candidates))

        operation = "OP-" + "a" * 32
        receipt = SimpleNamespace(
            metadata=SimpleNamespace(
                main_ref="refs/heads/main",
                target_main="3" * 40,
                principle_sha256="2" * 64,
                candidate_bindings=(SimpleNamespace(iteration="001"),),
                generation="i-old",
                integrated_commit="6" * 40,
                integrated_tree="7" * 40,
            ),
            iteration_evidence_refs=(),
            registration_digest="8" * 64,
            evidence_blob="9" * 40,
        )
        refs = {
            f"refs/project-harness/v2/integrations/{operation.lower()}/commit": "6" * 40,
            f"refs/project-harness/v2/integrations/{operation.lower()}/evidence": "9" * 40,
        }
        with mock.patch.object(
            readme_authority.integrated_registry,
            "load_registered_integrated_evidence",
            return_value=(receipt, ()),
        ):
            history, current_by_iteration, _final = (
                readme_authority._validate_public_integration_registry(
                    Path.cwd(),
                    refs,
                    current_main_ref="refs/heads/main",
                    current_main="1" * 40,
                    principle_sha256="2" * 64,
                )
            )
        self.assertEqual(current_by_iteration, {})
        self.assertFalse(history[0]["current"])


class ReadmeAuthorityProductTests(unittest.TestCase):
    """AC-001-04 proof over real refs, commits and three Git worktrees."""

    maxDiff = None

    def setUp(self) -> None:
        # Reuse the lightweight public-candidate fixture.  It writes genuine
        # candidate evidence refs/blobs and recovery journals that production
        # loaders authenticate; no loader or approval gate is mocked.
        self.fixture = train_fixture.MergeTrainGovernanceAdapterTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _add_third_committed_bundle(self) -> None:
        fixture = self.fixture
        fixture.write_bytes(
            "harness/iterations/003/README.md", train_fixture.l1("003", "baseline")
        )
        fixture.write_authority("003")
        fixture.git("add", "--", "harness/iterations/003")
        fixture.git("commit", "--no-gpg-sign", "-m", "add authoritative fixture 003")
        fixture.reserve_workspace("003")

    def _write_lease(
        self,
        *,
        number: str,
        checkout: Path,
        branch_ref: str,
        topology: str,
    ) -> None:
        fixture = self.fixture
        common_raw = Path(fixture.git("rev-parse", "--git-common-dir").stdout.strip())
        common = (
            common_raw if common_raw.is_absolute() else fixture.root / common_raw
        ).resolve()
        lease_dir = (
            common
            / "project-harness"
            / "workspace"
            / "v1"
            / "leases"
            / "iterations"
        )
        lease_dir.mkdir(parents=True, exist_ok=True)
        operation = "OP-" + hashlib.sha256(f"readme-lease-{number}".encode()).hexdigest()[:32]
        value = {
            "schema_version": workspace.LEASE_SCHEMA,
            "scope": "iteration-writer",
            "state": "active",
            "iteration": number,
            "operation_id": operation,
            "owner": f"task-{number}",
            "generation": 1,
            "execution_topology": topology,
            "expected_root": str(fixture.root),
            "worktree_path": str(checkout),
            "branch_ref": branch_ref,
            "base_ref": f"refs/project-harness/v2/iterations/{number}/base",
            "base_commit": fixture.base,
            "implementation_ref": "refs/heads/main",
            "implementation_commit": fixture.base,
            "reconciliation_ref": "refs/heads/main",
            "reconciliation_commit": fixture.base,
            "dependency_bindings": [],
            "dependency_bindings_digest": workspace.dependency_bindings_digest(()),
            "dependency_refresh_generation": 0,
            "principle_sha256": fixture.principle_sha256,
            "runtime_namespace": f"fixture-prd-{number}",
            "acquired_at": "2026-08-12T12:00:00+08:00",
            "heartbeat": "2026-08-12T12:00:00+08:00",
        }
        (lease_dir / f"{number}.json").write_bytes(workspace.canonical_json(value) + b"\n")

    def test_three_workspace_integrated_view_rebuilds_deleted_managed_bytes(self) -> None:
        fixture = self.fixture
        self._add_third_committed_bundle()
        _event_a, _event_b, plan, context = fixture.union_case("readme-authority")

        self._write_lease(
            number="001",
            checkout=fixture.feature_a,
            branch_ref="refs/heads/feature/001",
            topology="worktree",
        )
        self._write_lease(
            number="002",
            checkout=fixture.feature_b,
            branch_ref="refs/heads/feature/002",
            topology="worktree",
        )
        self._write_lease(
            number="003",
            checkout=fixture.root,
            branch_ref="refs/heads/main",
            topology="local",
        )

        # This old object contains arbitrary final L1 bytes.  The adapter may
        # parse it for source compatibility but cannot use those bytes as
        # authority; output must come from the independent product derivation.
        arbitrary = fixture.readme_authority()
        adapter = train_governance.build_governance_callback(
            plan, readme_authority=arbitrary
        )
        preview = adapter.preview(context)
        self.assertTrue(preview.ready, preview.blockers)
        authority = preview.readme_authority
        self.assertIsNotNone(authority)
        assert authority is not None
        readme_authority.validate_derived_readme_authority(authority)

        self.assertEqual(authority.topology_phase, "PARALLEL")
        self.assertEqual(
            [(item.iteration, item.topology) for item in authority.workspace_projections],
            [("001", "worktree"), ("002", "worktree"), ("003", "local")],
        )
        self.assertEqual(
            [(item.iteration, item.generation) for item in authority.candidate_bindings],
            [("001", "g1"), ("002", "g1")],
        )
        self.assertEqual(
            {item.number for item in authority.iteration_projections},
            {"001", "002", "003"},
        )
        self.assertTrue(
            all(
                item.integration_state.startswith("integrated-candidate:")
                for item in authority.iteration_projections
                if item.number in {"001", "002"}
            )
        )
        original_documents = readme_authority.documents_by_path(authority)
        self.assertNotIn(b"authority rebuilt A", original_documents["harness/iterations/001/README.md"])
        self.assertNotIn(b"authority rebuilt B", original_documents["harness/iterations/002/README.md"])

        readme_index = next(
            index
            for index, label in enumerate(preview.reconciliation_labels)
            if label.startswith("readme-")
        )
        semantic_before_readme = preview.reconciliation_snapshots[readme_index - 1]
        stripped = semantic_before_readme.as_mapping()
        stripped["harness/README.md"] = _empty_managed(
            _empty_managed(
                original_documents["harness/README.md"],
                "<!-- project-harness:focus:start -->",
                "<!-- project-harness:focus:end -->",
            ),
            "<!-- project-harness:iterations:start -->",
            "<!-- project-harness:iterations:end -->",
        )
        for number in ("001", "002", "003"):
            path = f"harness/iterations/{number}/README.md"
            stripped[path] = _empty_managed(
                original_documents[path],
                readme_authority.L1_START,
                readme_authority.L1_END,
            )
        deleted_managed_snapshot = reconcile.GovernanceSnapshot.from_files(
            "managed-regions-deleted", stripped
        )
        rebuilt = readme_authority.derive_train_readme_authority(
            plan,
            semantic_snapshot=deleted_managed_snapshot,
            governance_context=context,
        )
        self.assertEqual(
            readme_authority.documents_by_path(rebuilt),
            original_documents,
        )
        # Inputs changed (the managed bodies were deleted), so the authority
        # identity changes even though deterministic output bytes do not.
        self.assertNotEqual(rebuilt.input_digest, authority.input_digest)
        self.assertNotEqual(rebuilt.authority_digest, authority.authority_digest)

        first = authority.documents[0]
        tampered_document = replace(first, content=first.content + b"tamper")
        tampered = replace(
            authority,
            documents=(tampered_document, *authority.documents[1:]),
        )
        with self.assertRaises(readme_authority.ReadmeAuthorityError):
            readme_authority.validate_derived_readme_authority(tampered)

    def test_orphan_candidate_and_unbound_integrated_refs_never_render_verified(self) -> None:
        fixture = self.fixture
        _event_a, _event_b, plan, context = fixture.union_case("unverified-public-refs")
        candidate = plan.candidates[0]
        orphan = (
            "refs/project-harness/v2/iterations/001/candidates/g2"
        )
        fixture.git("update-ref", orphan, candidate.candidate_commit)

        orphan_preview = train_governance.build_governance_callback(plan).preview(context)
        self.assertFalse(orphan_preview.ready)
        self.assertTrue(
            any(
                item.code == "readme-authority-derivation-blocked"
                and "public candidate authority is invalid" in item.message
                and (
                    "orphan-public-ref" in item.message
                    or "registered-candidate-partial-refs" in item.message
                )
                for item in orphan_preview.blockers
            ),
            orphan_preview.blockers,
        )

        fixture.git("update-ref", "-d", orphan)
        raw_integrated = "refs/project-harness/v2/iterations/001/integrated"
        fixture.git("update-ref", raw_integrated, candidate.candidate_commit)
        integrated_preview = train_governance.build_governance_callback(plan).preview(context)
        self.assertFalse(integrated_preview.ready)
        self.assertTrue(
            any(
                item.code == "readme-authority-derivation-blocked"
                and "lacks authenticated public evidence" in item.message
                for item in integrated_preview.blockers
            ),
            integrated_preview.blockers,
        )


if __name__ == "__main__":
    unittest.main()
