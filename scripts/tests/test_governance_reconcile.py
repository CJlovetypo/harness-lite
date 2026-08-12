from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "harness_governance.py"
SPEC = importlib.util.spec_from_file_location("harness_governance", SCRIPT)
assert SPEC and SPEC.loader
harness_governance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness_governance
SPEC.loader.exec_module(harness_governance)


Blocker = harness_governance.Blocker
IterationRoutingState = harness_governance.IterationRoutingState
ManagedSection = harness_governance.ManagedSection
PrincipleApproval = harness_governance.PrincipleApproval
RootRoutingAuthority = harness_governance.RootRoutingAuthority
parse_progress_events = harness_governance.parse_progress_events
plan_principle_reconciliation = harness_governance.plan_principle_reconciliation
plan_progress_union = harness_governance.plan_progress_union
preview_managed_markdown = harness_governance.preview_managed_markdown
preview_root_readme = harness_governance.preview_root_readme


def blocker_codes(plan: object) -> set[str]:
    return {blocker.code for blocker in plan.blockers}


def legacy_event(
    session: str,
    event_type: str,
    fact: str,
    *,
    newline: str = "\n",
) -> bytes:
    return (
        f"## {session} / {event_type} / 2026-08-12T10:00:00+08:00{newline}"
        f"{newline}"
        f"- 事实：{fact}{newline}"
    ).encode("utf-8")


def v2_event(
    event_id: str,
    fact: str,
    *,
    parent: str | None = None,
    newline: str = "\n",
) -> bytes:
    parent_value = parent if parent is not None else "none"
    return (
        f"## {event_id} / CHECKPOINT / 2026-08-12T10:00:00+08:00{newline}"
        f"{newline}"
        f"- causal_parent: {parent_value}{newline}"
        f"- fact: {fact}{newline}"
    ).encode("utf-8")


def progress_document(*events: bytes, newline: bytes = b"\n", index: bytes = b"base-index") -> bytes:
    preamble = (
        b"# Progress"
        + newline
        + newline
        + b"<!-- project-harness:progress-index:start -->"
        + newline
        + index
        + newline
        + b"<!-- project-harness:progress-index:end -->"
        + newline
        + newline
        + b"## Events"
        + newline
    )
    result = preamble
    for event in events:
        if not result.endswith(newline + newline):
            result += newline
        result += event
    return result


class PrincipleReconciliationTests(unittest.TestCase):
    def test_no_branch_diff_adopts_latest_main_as_sole_authority(self) -> None:
        base = b"# Principles\nP-001\n"
        latest_main = b"# Principles\nP-001\nP-002\n"

        plan = plan_principle_reconciliation(
            branch_base=base,
            latest_main=latest_main,
            branch_candidate=base,
        )

        self.assertTrue(plan.ready)
        self.assertEqual(plan.action, "ADOPT_LATEST_MAIN")
        self.assertEqual(plan.preview, latest_main)
        self.assertEqual(plan.result_sha256, plan.latest_main_sha256)
        self.assertEqual(plan.blockers, ())

    def test_exact_approved_change_is_previewed_when_main_has_not_drifted(self) -> None:
        base = b"# Principles\nP-001\n"
        candidate = b"# Principles\nP-001\nP-002\n"
        approval = PrincipleApproval(
            change_id="PC-001",
            evidence_ref="EV-global-01",
            exact_before=base,
            exact_after=candidate,
        )

        plan = plan_principle_reconciliation(
            branch_base=base,
            latest_main=base,
            branch_candidate=candidate,
            approval=approval,
        )

        self.assertTrue(plan.ready)
        self.assertEqual(plan.action, "APPLY_APPROVED_EXACT_CHANGE")
        self.assertEqual(plan.preview, candidate)
        self.assertEqual(plan.change_id, "PC-001")
        self.assertEqual(plan.evidence_ref, "EV-global-01")

    def test_unapproved_branch_change_is_blocked(self) -> None:
        plan = plan_principle_reconciliation(
            branch_base=b"before\n",
            latest_main=b"before\n",
            branch_candidate=b"after\n",
        )

        self.assertFalse(plan.ready)
        self.assertIsNone(plan.preview)
        self.assertIn("principle-approval-required", blocker_codes(plan))

    def test_approval_must_match_exact_before_and_after_bytes(self) -> None:
        approval = PrincipleApproval(
            change_id="PC-001",
            evidence_ref="EV-global-01",
            exact_before=b"before\r\n",
            exact_after=b"different-after\n",
        )

        plan = plan_principle_reconciliation(
            branch_base=b"before\n",
            latest_main=b"before\n",
            branch_candidate=b"after\n",
            approval=approval,
        )

        self.assertEqual(
            blocker_codes(plan),
            {"principle-approved-before-mismatch", "principle-approved-after-mismatch"},
        )
        self.assertIsNone(plan.preview)

    def test_main_drift_blocks_even_an_exact_approved_change(self) -> None:
        base = b"P-001\n"
        candidate = b"P-001\nP-branch\n"
        approval = PrincipleApproval(
            change_id="PC-001",
            evidence_ref="EV-global-01",
            exact_before=base,
            exact_after=candidate,
        )

        plan = plan_principle_reconciliation(
            branch_base=base,
            latest_main=b"P-001\nP-main\n",
            branch_candidate=candidate,
            approval=approval,
        )

        self.assertFalse(plan.ready)
        self.assertIn("principle-main-drift", blocker_codes(plan))
        self.assertIsNone(plan.result_sha256)
        self.assertIsNone(plan.preview)

    def test_invalid_utf8_fails_closed(self) -> None:
        invalid = b"principle\xff"

        plan = plan_principle_reconciliation(
            branch_base=invalid,
            latest_main=invalid,
            branch_candidate=invalid,
        )

        self.assertFalse(plan.ready)
        self.assertIn("principle-input-not-utf8", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_oversized_input_fails_closed_without_truncation(self) -> None:
        oversized = b"valid utf8\n" * 2
        with mock.patch.object(harness_governance, "MAX_PRINCIPLE_BYTES", len(oversized) - 1):
            plan = plan_principle_reconciliation(
                branch_base=oversized,
                latest_main=oversized,
                branch_candidate=oversized,
            )

        self.assertFalse(plan.ready)
        self.assertIn("principle-input-too-large", blocker_codes(plan))
        self.assertIsNone(plan.preview)


class ProgressUnionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_event = legacy_event("S-20260812-01", "OPEN", "base")
        self.base = progress_document(self.base_event)

    def test_two_parallel_appends_are_unioned_after_latest_main(self) -> None:
        main_event = v2_event("EV-main-01", "main", parent="S-20260812-01/OPEN")
        branch_event = v2_event("EV-branch-01", "branch", parent="S-20260812-01/OPEN")
        latest_main = progress_document(self.base_event, main_event, index=b"main-index")
        candidate = progress_document(self.base_event, branch_event, index=b"branch-preview-index")

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=latest_main,
            branch_candidate=candidate,
        )

        self.assertTrue(plan.ready)
        self.assertEqual(plan.appended_event_identities, ("EV-branch-01",))
        self.assertEqual(plan.deduplicated_event_identities, ())
        assert plan.preview is not None
        self.assertTrue(plan.preview.startswith(latest_main))
        self.assertLess(plan.preview.index(b"EV-main-01"), plan.preview.index(b"EV-branch-01"))
        self.assertIn(b"main-index", plan.preview)
        self.assertNotIn(b"branch-preview-index", plan.preview)

    def test_same_id_same_exact_bytes_is_idempotently_deduplicated(self) -> None:
        shared = v2_event("EV-shared-01", "same bytes", parent="S-20260812-01/OPEN")
        latest_main = progress_document(self.base_event, shared)
        candidate = progress_document(self.base_event, shared)

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=latest_main,
            branch_candidate=candidate,
        )

        self.assertTrue(plan.ready)
        self.assertEqual(plan.preview, latest_main)
        self.assertEqual(plan.appended_event_identities, ())
        self.assertEqual(plan.deduplicated_event_identities, ("EV-shared-01",))
        self.assertEqual(plan.preview.count(b"EV-shared-01"), 1)

    def test_same_id_different_bytes_hard_blocks(self) -> None:
        main_event = v2_event("EV-collision-01", "main version", parent="S-20260812-01/OPEN")
        branch_event = v2_event("EV-collision-01", "branch version", parent="S-20260812-01/OPEN")

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=progress_document(self.base_event, main_event),
            branch_candidate=progress_document(self.base_event, branch_event),
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-same-id-different-bytes", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_candidate_cannot_rewrite_base_event_bytes(self) -> None:
        rewritten = legacy_event("S-20260812-01", "OPEN", "rewritten")

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=progress_document(rewritten),
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-candidate-rewrites-base-event", blocker_codes(plan))

    def test_latest_main_cannot_rewrite_base_event_bytes(self) -> None:
        rewritten = legacy_event("S-20260812-01", "OPEN", "main rewrote history")

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=progress_document(rewritten),
            branch_candidate=self.base,
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-main-rewrites-base-event", blocker_codes(plan))

    def test_candidate_cannot_insert_before_or_reorder_base_history(self) -> None:
        second = legacy_event("S-20260812-02", "CHECKPOINT", "second")
        base = progress_document(self.base_event, second)
        inserted = v2_event("EV-inserted-01", "inserted")
        candidate = progress_document(self.base_event, inserted, second)

        plan = plan_progress_union(
            branch_base=base,
            latest_main=base,
            branch_candidate=candidate,
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-candidate-reorders-base-history", blocker_codes(plan))

    def test_causal_parent_must_precede_child(self) -> None:
        child = v2_event("EV-child-01", "child", parent="EV-parent-01")
        parent = v2_event("EV-parent-01", "parent", parent="S-20260812-01/OPEN")

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=progress_document(self.base_event, child, parent),
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-causal-order-invalid", blocker_codes(plan))

    def test_missing_causal_parent_blocks(self) -> None:
        orphan = v2_event("EV-orphan-01", "orphan", parent="EV-missing-01")

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=progress_document(self.base_event, orphan),
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-missing-causal-parent", blocker_codes(plan))

    def test_new_event_must_declare_even_a_root_causal_parent(self) -> None:
        event = (
            b"## EV-root-01 / CHECKPOINT / 2026-08-12T10:00:00+08:00\n\n"
            b"- fact: undeclared root\n"
        )

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=progress_document(self.base_event, event),
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-causal-parent-missing", blocker_codes(plan))

    def test_valid_causal_chain_preserves_branch_order(self) -> None:
        parent = v2_event("EV-parent-01", "parent", parent="S-20260812-01/OPEN")
        child = v2_event("EV-child-01", "child", parent="EV-parent-01")

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=progress_document(self.base_event, parent, child),
        )

        self.assertTrue(plan.ready)
        self.assertEqual(plan.appended_event_identities, ("EV-parent-01", "EV-child-01"))
        assert plan.preview is not None
        self.assertLess(plan.preview.index(b"EV-parent-01"), plan.preview.index(b"EV-child-01"))

    def test_causal_dependency_cycle_has_an_explicit_blocker(self) -> None:
        first = v2_event("EV-cycle-a", "a", parent="EV-cycle-b")
        second = v2_event("EV-cycle-b", "b", parent="EV-cycle-a")

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=progress_document(self.base_event, first, second),
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-causal-cycle", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_legacy_open_and_close_share_session_without_identity_collision(self) -> None:
        opened = legacy_event("S-20260812-03", "OPEN", "opened")
        closed = legacy_event("S-20260812-03", "CLOSE", "closed")
        parsed = parse_progress_events(progress_document(opened, closed), source="legacy")

        self.assertEqual(parsed.blockers, ())
        self.assertEqual(
            tuple(event.identity for event in parsed.events),
            ("S-20260812-03/OPEN", "S-20260812-03/CLOSE"),
        )

    def test_malformed_event_like_heading_blocks_instead_of_being_ignored(self) -> None:
        malformed = b"## EV-bad-01 / UNKNOWN / now\n\n- fact: bad\n"
        candidate = self.base + b"\n" + malformed

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=candidate,
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-malformed-event-heading", blocker_codes(plan))

    def test_separator_blank_lines_do_not_rewrite_event_block_bytes(self) -> None:
        candidate_event = v2_event("EV-new-01", "new", parent="S-20260812-01/OPEN")
        candidate = self.base.rstrip(b"\n") + b"\n\n\n" + candidate_event

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=candidate,
        )

        self.assertTrue(plan.ready)
        self.assertEqual(plan.appended_event_identities, ("EV-new-01",))

    def test_crlf_event_bytes_remain_exact_in_preview(self) -> None:
        newline = b"\r\n"
        base_event = legacy_event("S-20260812-01", "OPEN", "base", newline="\r\n")
        base = progress_document(base_event, newline=newline)
        appended = v2_event(
            "EV-crlf-01",
            "crlf",
            parent="S-20260812-01/OPEN",
            newline="\r\n",
        )
        candidate = progress_document(base_event, appended, newline=newline)

        plan = plan_progress_union(
            branch_base=base,
            latest_main=base,
            branch_candidate=candidate,
        )

        self.assertTrue(plan.ready)
        assert plan.preview is not None
        parsed = parse_progress_events(plan.preview, source="preview")
        preview_event = next(event for event in parsed.events if event.identity == "EV-crlf-01")
        self.assertEqual(preview_event.exact_bytes, appended)

    def test_invalid_utf8_progress_input_fails_closed(self) -> None:
        candidate = self.base + b"\n\xff\n"

        plan = plan_progress_union(
            branch_base=self.base,
            latest_main=self.base,
            branch_candidate=candidate,
        )

        self.assertFalse(plan.ready)
        self.assertIn("progress-input-not-utf8", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_oversized_progress_input_fails_closed(self) -> None:
        with mock.patch.object(harness_governance, "MAX_PROGRESS_BYTES", len(self.base) - 1):
            plan = plan_progress_union(
                branch_base=self.base,
                latest_main=self.base,
                branch_candidate=self.base,
            )

        self.assertFalse(plan.ready)
        self.assertIn("progress-input-too-large", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_progress_event_count_limit_fails_closed(self) -> None:
        extra = v2_event("EV-second-01", "second", parent="S-20260812-01/OPEN")
        with mock.patch.object(harness_governance, "MAX_PROGRESS_EVENTS", 1):
            plan = plan_progress_union(
                branch_base=self.base,
                latest_main=self.base,
                branch_candidate=progress_document(self.base_event, extra),
            )

        self.assertFalse(plan.ready)
        self.assertIn("progress-event-count-exceeded", blocker_codes(plan))
        self.assertIsNone(plan.preview)


def route(
    number: str,
    title: str,
    *,
    workspace: str = "Local",
    depends_on: tuple[str, ...] = (),
) -> IterationRoutingState:
    return IterationRoutingState(
        number=number,
        title=title,
        prd_status="实施中",
        spec_status="实施中",
        open_deviations=0,
        workspace=workspace,
        governance_gate="candidate verification",
        candidate_state="not-created",
        integration_state="not-created",
        result="implementing",
        next_step="run verification",
        depends_on=depends_on,
    )


def root_readme(*, newline: bytes = b"\n") -> bytes:
    return newline.join(
        (
            b"# Harness Router",
            b"",
            b"User authored before.",
            b"",
            b"<!-- project-harness:focus:start -->",
            b"old focus",
            b"<!-- project-harness:focus:end -->",
            b"",
            b"| iteration | title | PRD | SPEC | deviations | result | next | link |",
            b"|---|---|---|---|---:|---|---|---|",
            b"<!-- project-harness:iterations:start -->",
            b"old rows",
            b"<!-- project-harness:iterations:end -->",
            b"",
            b"User authored after.",
            b"",
        )
    )


class ReadmePreviewTests(unittest.TestCase):
    def test_root_preview_is_sorted_deterministic_and_preserves_handwritten_bytes(self) -> None:
        original = root_readme()
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration="002",
            global_gate="SPEC approval",
            next_step="approve SPEC-002",
            iterations=(
                route("010", "Later", workspace="worktree"),
                route("002", "Now", workspace="Local"),
            ),
        )

        first = preview_root_readme(original, authority=authority)
        second = preview_root_readme(original, authority=authority)

        self.assertTrue(first.ready)
        self.assertEqual(first.preview, second.preview)
        self.assertEqual(first.result_sha256, second.result_sha256)
        self.assertTrue(first.changed)
        assert first.preview is not None
        self.assertTrue(first.preview.startswith(b"# Harness Router\n\nUser authored before.\n"))
        self.assertTrue(first.preview.endswith(b"User authored after.\n"))
        self.assertLess(first.preview.index(b"[002]"), first.preview.index(b"[010]"))
        self.assertIn("执行位置：Local".encode(), first.preview)
        self.assertIn("门禁：candidate verification".encode(), first.preview)
        self.assertIn("依赖：无".encode(), first.preview)

    def test_rebuilding_preview_again_is_byte_idempotent(self) -> None:
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration="001",
            global_gate="verification",
            next_step="verify",
            iterations=(route("001", "One"),),
        )
        first = preview_root_readme(root_readme(), authority=authority)
        assert first.preview is not None

        second = preview_root_readme(first.preview, authority=authority)

        self.assertTrue(second.ready)
        self.assertFalse(second.changed)
        self.assertEqual(second.preview, first.preview)

    def test_table_values_are_one_line_and_escape_pipe(self) -> None:
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration="001",
            global_gate="gate | exact",
            next_step="line one\nline two",
            iterations=(route("001", "One | Two"),),
        )

        plan = preview_root_readme(root_readme(), authority=authority)

        self.assertTrue(plan.ready)
        assert plan.preview is not None
        self.assertIn(b"One \\| Two", plan.preview)
        self.assertIn(b"line one line two", plan.preview)
        self.assertNotIn(b"line one\nline two", plan.preview)

    def test_crlf_document_keeps_crlf_in_managed_preview(self) -> None:
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration=None,
            global_gate="idle",
            next_step="wait",
            iterations=(),
        )

        plan = preview_root_readme(root_readme(newline=b"\r\n"), authority=authority)

        self.assertTrue(plan.ready)
        assert plan.preview is not None
        self.assertNotIn(b"\n", plan.preview.replace(b"\r\n", b""))
        self.assertIn(b"\r\n<!-- project-harness:focus:end -->", plan.preview)

    def test_missing_or_duplicate_managed_marker_blocks_without_preview(self) -> None:
        duplicate = root_readme() + b"<!-- project-harness:focus:start -->\n"
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration=None,
            global_gate="idle",
            next_step="wait",
            iterations=(),
        )

        plan = preview_root_readme(duplicate, authority=authority)

        self.assertFalse(plan.ready)
        self.assertIn("readme-managed-marker-missing-or-duplicate", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_missing_managed_marker_blocks_without_preview(self) -> None:
        missing = root_readme().replace(b"<!-- project-harness:focus:end -->\n", b"")
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration=None,
            global_gate="idle",
            next_step="wait",
            iterations=(),
        )

        plan = preview_root_readme(missing, authority=authority)

        self.assertFalse(plan.ready)
        self.assertIn("readme-managed-marker-missing-or-duplicate", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_invalid_authority_state_blocks_before_rendering(self) -> None:
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration="003",
            global_gate="gate",
            next_step="next",
            iterations=(route("001", "One"), route("0001", "Duplicate decimal")),
        )

        plan = preview_root_readme(root_readme(), authority=authority)

        self.assertFalse(plan.ready)
        self.assertIn("readme-iteration-number-duplicate", blocker_codes(plan))
        self.assertIn("readme-current-iteration-missing", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_missing_iteration_dependency_blocks(self) -> None:
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration="001",
            global_gate="gate",
            next_step="next",
            iterations=(route("001", "One", depends_on=("999",)),),
        )

        plan = preview_root_readme(root_readme(), authority=authority)

        self.assertFalse(plan.ready)
        self.assertIn("readme-dependency-missing", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_iteration_dependency_cycle_blocks(self) -> None:
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration="001",
            global_gate="gate",
            next_step="next",
            iterations=(
                route("001", "One", depends_on=("002",)),
                route("002", "Two", depends_on=("001",)),
            ),
        )

        plan = preview_root_readme(root_readme(), authority=authority)

        self.assertFalse(plan.ready)
        self.assertIn("readme-dependency-cycle", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_invalid_utf8_readme_fails_closed(self) -> None:
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration=None,
            global_gate="idle",
            next_step="wait",
            iterations=(),
        )

        plan = preview_root_readme(root_readme() + b"\xff", authority=authority)

        self.assertFalse(plan.ready)
        self.assertIn("readme-input-not-utf8", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_oversized_readme_fails_closed(self) -> None:
        original = root_readme()
        authority = RootRoutingAuthority(
            authority_id="main-tree:abc123",
            current_iteration=None,
            global_gate="idle",
            next_step="wait",
            iterations=(),
        )
        with mock.patch.object(harness_governance, "MAX_README_BYTES", len(original) - 1):
            plan = preview_root_readme(original, authority=authority)

        self.assertFalse(plan.ready)
        self.assertIn("readme-input-too-large", blocker_codes(plan))
        self.assertIsNone(plan.preview)

    def test_generic_managed_preview_replaces_sections_in_document_order(self) -> None:
        document = (
            b"prefix\n<!-- b:start -->\nold b\n<!-- b:end -->\n"
            b"middle\n<!-- a:start -->\nold a\n<!-- a:end -->\nsuffix\n"
        )
        plan = preview_managed_markdown(
            document,
            authority_id="authority-1",
            sections=(
                ManagedSection("a", "<!-- a:start -->", "<!-- a:end -->", b"new a"),
                ManagedSection("b", "<!-- b:start -->", "<!-- b:end -->", b"new b"),
            ),
        )

        self.assertTrue(plan.ready)
        self.assertEqual(tuple(change.name for change in plan.sections), ("b", "a"))
        self.assertEqual(
            plan.preview,
            (
                b"prefix\n<!-- b:start -->\nnew b\n<!-- b:end -->\n"
                b"middle\n<!-- a:start -->\nnew a\n<!-- a:end -->\nsuffix\n"
            ),
        )


if __name__ == "__main__":
    unittest.main()
