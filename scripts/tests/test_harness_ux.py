from __future__ import annotations

import unittest

from scripts.harness_ux import ActionFacts, InteractionError, interaction, is_exact_reply_to


OID_A = "a" * 40
OID_B = "b" * 40


class HarnessInteractionTests(unittest.TestCase):
    def test_worktree_before_and_after_are_notify_and_show_required_facts(self) -> None:
        common = dict(
            action="create-worktree",
            iteration="002",
            operation_id="OP-" + "1" * 32,
            project_root="D:/workspace/project-main",
            base_commit=OID_A,
            branch_ref="refs/heads/prd/002",
            worktree_path="D:/workspace/worktrees/prd-002",
            reason="second writable PRD requires isolation",
            affected_prds=("001", "002"),
            runtime_namespace="prd-002",
            effect_on_existing_prds=("PRD-001 stays in place",),
            remote_involved=False,
            source_preserved=True,
            next_gate="implement-prd-002",
        )
        before = interaction(ActionFacts(phase="before", **common))
        after = interaction(ActionFacts(phase="after", actual_head=OID_A, **common))

        self.assertEqual(before.action_level, "notify")
        self.assertFalse(before.requires_user_response)
        self.assertEqual(before.facts["base_commit"], OID_A)
        self.assertEqual(after.phase, "after")
        self.assertFalse(after.facts["pushed"])
        self.assertEqual(after.facts["affected_prds"], ["001", "002"])
        self.assertEqual(after.facts["runtime_namespace"], "prd-002")
        self.assertEqual(after.facts["actual_head"], OID_A)
        self.assertTrue(after.facts["source_preserved"])
        self.assertFalse(after.facts["remote_involved"])

    def test_worktree_notification_missing_path_fails_closed(self) -> None:
        with self.assertRaisesRegex(InteractionError, "worktree_path"):
            interaction(
                ActionFacts(
                    action="create-worktree",
                    phase="before",
                    iteration="002",
                    operation_id="OP-" + "1" * 32,
                    project_root="D:/workspace/project-main",
                    base_commit=OID_A,
                    branch_ref="refs/heads/prd/002",
                    reason="parallel isolation",
                )
            )

    def test_commit_is_confirm_and_never_implies_push(self) -> None:
        before = interaction(
            ActionFacts(
                action="commit",
                phase="before",
                iteration="001",
                paths=("scripts/tool.py", "scripts/tests/test_tool.py"),
                message="checkpoint: add tool",
                verification_ids=("test:tool",),
                excluded_paths=("scripts/__pycache__/",),
                pushed=False,
            )
        )
        after = interaction(
            ActionFacts(
                action="commit",
                phase="after",
                iteration="001",
                paths=("scripts/tool.py", "scripts/tests/test_tool.py"),
                message="checkpoint: add tool",
                verification_ids=("test:tool",),
                excluded_paths=("scripts/__pycache__/",),
                resulting_commit=OID_B,
                pushed=False,
            )
        )

        self.assertEqual(before.action_level, "confirm")
        self.assertTrue(before.requires_user_response)
        self.assertTrue(is_exact_reply_to(before, before.facts_digest))
        self.assertFalse(is_exact_reply_to(before, "0" * 64))
        self.assertEqual(after.facts["resulting_commit"], OID_B)
        self.assertFalse(after.facts["pushed"])

    def test_commit_cannot_claim_push(self) -> None:
        with self.assertRaisesRegex(InteractionError, "cannot claim a push"):
            interaction(
                ActionFacts(
                    action="commit",
                    phase="after",
                    paths=("a",),
                    message="commit",
                    verification_ids=("test:a",),
                    resulting_commit=OID_B,
                    pushed=True,
                )
            )

    def test_push_is_separate_confirm_card_with_force_false(self) -> None:
        push = interaction(
            ActionFacts(
                action="push",
                phase="before",
                remote="origin",
                source_ref="refs/heads/prd/001",
                target_ref="refs/heads/prd/001",
                commit_range="main..prd/001",
                force=False,
                pushed=False,
            )
        )

        self.assertEqual(push.action_level, "confirm")
        self.assertNotEqual(push.action, "commit")
        self.assertFalse(push.facts["force"])

    def test_force_push_has_no_normal_interaction_path(self) -> None:
        with self.assertRaisesRegex(InteractionError, "force-push"):
            interaction(
                ActionFacts(
                    action="push",
                    phase="before",
                    remote="origin",
                    source_ref="refs/heads/prd/001",
                    target_ref="refs/heads/prd/001",
                    commit_range="main..prd/001",
                    force=True,
                )
            )

    def test_unknown_mutation_fails_closed_to_confirm(self) -> None:
        result = interaction(ActionFacts(action="future-git-write", phase="before"))

        self.assertEqual(result.action_level, "confirm")
        self.assertTrue(result.requires_user_response)


if __name__ == "__main__":
    unittest.main()
