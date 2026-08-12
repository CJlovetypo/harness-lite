from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "harness_progress.py"
SPEC = importlib.util.spec_from_file_location("harness_progress_tests", SCRIPT)
assert SPEC and SPEC.loader
progress = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = progress
SPEC.loader.exec_module(progress)

GOVERNANCE_SCRIPT = Path(__file__).resolve().parents[1] / "harness_governance.py"
GOVERNANCE_SPEC = importlib.util.spec_from_file_location(
    "harness_progress_governance_tests", GOVERNANCE_SCRIPT
)
assert GOVERNANCE_SPEC and GOVERNANCE_SPEC.loader
governance = importlib.util.module_from_spec(GOVERNANCE_SPEC)
sys.modules[GOVERNANCE_SPEC.name] = governance
GOVERNANCE_SPEC.loader.exec_module(governance)


OWNER = b"<!-- managed-by: harness-lite v1 -->\n"
LEGACY_EVENT = (
    b"## S-20260812-01 / OPEN / 2026-08-12T09:00:00+08:00\n\n"
    b"- fact: baseline opened\n"
)
BASE_PROGRESS = (
    OWNER
    + b"# Progress\n\n"
    + b"<!-- project-harness:progress-index:start -->\n"
    + b"| iteration | status |\n|---|---|\n| 001 | active |\n"
    + b"<!-- project-harness:progress-index:end -->\n\n"
    + LEGACY_EVENT
)


class ProgressAppendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.git_executable = shutil.which("git")
        if not self.git_executable:
            self.skipTest("git is required")
        self.temporary = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "main repo"
        self.root.mkdir()
        self.git_config = self.sandbox / "gitconfig"
        subprocess.run(
            [
                self.git_executable,
                "config",
                "--file",
                str(self.git_config),
                "user.name",
                "Harness Progress Tests",
            ],
            check=True,
        )
        subprocess.run(
            [
                self.git_executable,
                "config",
                "--file",
                str(self.git_config),
                "user.email",
                "progress@example.invalid",
            ],
            check=True,
        )
        self.environment = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(self.git_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=False,
        )
        self.environment.start()
        self.git(self.root, "init", "-b", "main")
        target = self.root / "harness" / "progress.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(BASE_PROGRESS)
        self.git(self.root, "add", "--", "harness/progress.md")
        self.git(self.root, "commit", "--no-gpg-sign", "-m", "progress baseline")
        self.head = self.git(self.root, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def git(
        self,
        root: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def operation(label: str) -> str:
        return "OP-" + hashlib.sha256(label.encode("utf-8")).hexdigest()[:32]

    def event(
        self,
        label: str,
        *,
        source_ref: str = "refs/heads/main",
        summary: str | None = None,
        causal_parent: str | None = "S-20260812-01/OPEN",
        corrects: str | None = None,
    ):
        return progress.workspace_event(
            workspace_state=label,
            session_id="S-20260812-02",
            iteration="001",
            occurred_at="2026-08-12T10:00:00+08:00",
            source_ref=source_ref,
            source_commit=self.head,
            operation_id=self.operation(label),
            causal_parent=causal_parent,
            evidence_refs=(f"evidence:{label}",),
            summary=summary or f"workspace {label}",
            corrects=corrects,
        )

    @staticmethod
    def file_snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def event_count(self, root: Path, event_id: str) -> int:
        parsed = governance.parse_progress_events(
            (root / "harness" / "progress.md").read_bytes(), source=str(root)
        )
        self.assertFalse(parsed.blockers)
        return sum(item.identity == event_id for item in parsed.events)

    def test_plan_is_zero_write_and_output_is_compatible_with_ev_parser(self) -> None:
        event = self.event("local-ready")
        before = self.file_snapshot(self.root)

        plan = progress.plan_progress_append(project_root=self.root, event=event)

        self.assertEqual(before, self.file_snapshot(self.root))
        self.assertEqual(plan.action, "APPEND")
        common = progress.resolve_git_common_dir(self.root)
        self.assertFalse(
            progress.journal_path(common, event.operation_id, event.event_id).exists()
        )

        result = progress.apply_progress_append(
            plan,
            accept_plan_digest=plan.plan_digest,
        )
        parsed = governance.parse_progress_events(
            (self.root / "harness" / "progress.md").read_bytes(), source="result"
        )
        self.assertFalse(parsed.blockers)
        appended = next(item for item in parsed.events if item.identity == event.event_id)
        self.assertEqual(appended.event_type, "CHECKPOINT")
        self.assertEqual(appended.causal_parent, "S-20260812-01/OPEN")
        self.assertTrue(result.appended)
        self.assertIn(f"- iteration: `{event.iteration}`".encode(), appended.exact_bytes)
        self.assertIn(f"- operation_id: `{event.operation_id}`".encode(), appended.exact_bytes)
        self.assertIn(f"- source_commit: `{self.head}`".encode(), appended.exact_bytes)

    def test_operational_journal_uses_a_bounded_hashed_event_locator(self) -> None:
        event = self.event("bounded-windows-locator")
        common = progress.resolve_git_common_dir(self.root)

        path = progress.journal_path(common, event.operation_id, event.event_id)

        self.assertEqual(path.parent.name, event.operation_id)
        self.assertRegex(path.name, r"^event-[0-9a-f]{64}\.json$")
        self.assertNotIn(event.event_id, path.name)
        plan = progress.plan_progress_append(project_root=self.root, event=event)
        result = progress.apply_progress_append(plan, accept_plan_digest=plan.plan_digest)
        self.assertEqual(Path(result.journal_path), path)
        recovered = progress.load_progress_append_plan(
            common,
            event.operation_id,
            event.event_id,
        )
        self.assertEqual(recovered.plan_digest, plan.plan_digest)

    def test_same_operation_retry_reuses_journal_and_never_duplicates_event(self) -> None:
        event = self.event("retry")
        plan = progress.plan_progress_append(project_root=self.root, event=event)
        first = progress.apply_progress_append(plan, accept_plan_digest=plan.plan_digest)
        second = progress.apply_progress_append(plan, accept_plan_digest=plan.plan_digest)

        replanned = progress.plan_progress_append(project_root=self.root, event=event)
        third = progress.apply_progress_append(
            replanned,
            accept_plan_digest=replanned.plan_digest,
        )

        self.assertTrue(first.appended)
        self.assertFalse(second.appended)
        self.assertFalse(third.appended)
        self.assertTrue(second.resumed)
        self.assertEqual(replanned.plan_digest, plan.plan_digest)
        self.assertEqual(self.event_count(self.root, event.event_id), 1)

    def test_same_event_id_with_different_bytes_is_a_hard_blocker(self) -> None:
        original = self.event("collision", summary="first fact")
        plan = progress.plan_progress_append(project_root=self.root, event=original)
        progress.apply_progress_append(plan, accept_plan_digest=plan.plan_digest)
        conflicting = self.event("collision", summary="different fact")
        self.assertEqual(original.event_id, conflicting.event_id)

        with self.assertRaisesRegex(progress.ProgressError, "different exact bytes"):
            progress.plan_progress_append(project_root=self.root, event=conflicting)

        self.assertEqual(self.event_count(self.root, original.event_id), 1)

    def test_write_free_exact_append_is_idempotent_and_rejects_same_id_tamper(self) -> None:
        original = self.event("snapshot-event", summary="exact train snapshot fact")

        appended, changed = progress.append_progress_event_exact(BASE_PROGRESS, original)
        repeated, repeated_changed = progress.append_progress_event_exact(appended, original)

        self.assertTrue(changed)
        self.assertFalse(repeated_changed)
        self.assertEqual(repeated, appended)
        conflicting = self.event("snapshot-event", summary="tampered train snapshot fact")
        self.assertEqual(conflicting.event_id, original.event_id)
        with self.assertRaisesRegex(progress.ProgressError, "different exact bytes"):
            progress.append_progress_event_exact(appended, conflicting)

    def test_crash_after_atomic_replace_resumes_without_duplicate(self) -> None:
        event = self.event("crash-recovery")
        plan = progress.plan_progress_append(project_root=self.root, event=event)

        def crash(stage: str, _path: str) -> None:
            if stage == "after_replace_before_journal":
                raise progress.SimulatedCrash("power loss")

        with self.assertRaises(progress.SimulatedCrash):
            progress.apply_progress_append(
                plan,
                accept_plan_digest=plan.plan_digest,
                fault_injector=crash,
            )
        self.assertEqual(self.event_count(self.root, event.event_id), 1)

        common = progress.resolve_git_common_dir(self.root)
        recovered = progress.load_progress_append_plan(
            common, event.operation_id, event.event_id
        )
        result = progress.apply_progress_append(
            recovered,
            accept_plan_digest=recovered.plan_digest,
        )

        self.assertFalse(result.appended)
        self.assertTrue(result.resumed)
        self.assertEqual(result.phase, "APPLIED")
        self.assertEqual(self.event_count(self.root, event.event_id), 1)
        journal = json.loads(Path(result.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(journal["phase"], "APPLIED")

    def test_rewriting_observed_base_history_blocks_before_append(self) -> None:
        event = self.event("base-drift")
        plan = progress.plan_progress_append(project_root=self.root, event=event)
        target = self.root / "harness" / "progress.md"
        target.write_bytes(target.read_bytes().replace(b"baseline opened", b"history rewritten"))

        with self.assertRaisesRegex(progress.ProgressError, "changed after planning"):
            progress.apply_progress_append(plan, accept_plan_digest=plan.plan_digest)

        self.assertNotIn(event.event_id.encode(), target.read_bytes())

    def test_autocrlf_true_accepts_only_git_clean_proven_crlf_and_binds_actual_bytes(self) -> None:
        self.git(self.root, "config", "core.autocrlf", "true")
        target = self.root / "harness" / "progress.md"
        target.unlink()
        self.git(self.root, "checkout-index", "--force", "--", "harness/progress.md")
        current = target.read_bytes()
        self.assertIn(b"\r\n", current)
        self.assertNotIn(b"\n", current.replace(b"\r\n", b""))
        event = self.event("autocrlf-crlf")

        plan = progress.plan_progress_append(project_root=self.root, event=event)

        self.assertEqual(plan.before_sha256, hashlib.sha256(current).hexdigest())
        self.assertEqual(plan.manifest["newline"], "CRLF")
        self.assertIn("crlf", plan.manifest["allowed_source_variants"])
        self.assertEqual(
            plan.manifest["checkout_policy"]["source_commit"],
            self.head,
        )
        result = progress.apply_progress_append(plan, accept_plan_digest=plan.plan_digest)
        self.assertTrue(result.appended)
        after = target.read_bytes()
        self.assertEqual(hashlib.sha256(after).hexdigest(), plan.after_sha256)
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""))
        replay = progress.apply_progress_append(plan, accept_plan_digest=plan.plan_digest)
        self.assertFalse(replay.appended)
        self.assertEqual(self.event_count(self.root, event.event_id), 1)

    def test_mixed_line_endings_are_never_treated_as_checkout_equivalent(self) -> None:
        self.git(self.root, "config", "core.autocrlf", "true")
        target = self.root / "harness" / "progress.md"
        target.write_bytes(BASE_PROGRESS.replace(b"# Progress\n", b"# Progress\r\n"))

        with self.assertRaisesRegex(progress.ProgressError, "mixed LF/CRLF"):
            progress.plan_progress_append(
                project_root=self.root,
                event=self.event("mixed-eol"),
            )

    def test_custom_filter_and_working_tree_encoding_in_pinned_source_fail_closed(self) -> None:
        for attribute in ("filter=evil", "working-tree-encoding=UTF-16"):
            with self.subTest(attribute=attribute):
                with tempfile.TemporaryDirectory(dir=self.sandbox, prefix="attrs-") as raw:
                    clone = Path(raw) / "repo"
                    self.git(self.root, "clone", "--local", str(self.root), str(clone))
                    self.git(clone, "config", "user.name", "Harness Progress Tests")
                    self.git(clone, "config", "user.email", "progress@example.invalid")
                    (clone / ".gitattributes").write_text(
                        f"harness/progress.md {attribute}\n",
                        encoding="utf-8",
                    )
                    self.git(clone, "add", "--", ".gitattributes")
                    self.git(clone, "commit", "--no-gpg-sign", "-m", "unsafe attrs")
                    head = self.git(clone, "rev-parse", "HEAD").stdout.strip()
                    event = progress.workspace_event(
                        workspace_state="unsafe-attrs",
                        session_id="S-20260812-02",
                        iteration="001",
                        occurred_at="2026-08-12T10:00:00+08:00",
                        source_ref="refs/heads/main",
                        source_commit=head,
                        operation_id=self.operation(attribute),
                        causal_parent="S-20260812-01/OPEN",
                        evidence_refs=(f"attribute:{attribute}",),
                        summary="unsafe attributes must block",
                    )
                    with self.assertRaisesRegex(
                        progress.ProgressError,
                        "unsafe|custom Git filter|working-tree-encoding|filters/encoding",
                    ):
                        progress.plan_progress_append(project_root=clone, event=event)

    def test_dirty_caller_attributes_cannot_override_pinned_source_semantics(self) -> None:
        (self.root / ".gitattributes").write_text(
            "harness/progress.md filter=caller-evil\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(progress.ProgressError, "caller attributes"):
            progress.plan_progress_append(
                project_root=self.root,
                event=self.event("caller-attrs"),
            )

    def test_attribute_drift_after_plan_blocks_before_append(self) -> None:
        event = self.event("attrs-drift")
        plan = progress.plan_progress_append(project_root=self.root, event=event)
        target = self.root / "harness" / "progress.md"
        before = target.read_bytes()
        (self.root / ".gitattributes").write_text(
            "harness/progress.md text eol=crlf\n",
            encoding="utf-8",
        )
        self.git(self.root, "add", "--", ".gitattributes")
        self.git(self.root, "commit", "--no-gpg-sign", "-m", "attributes drift")

        with self.assertRaisesRegex(progress.ProgressError, "attributes differ|policy changed"):
            progress.apply_progress_append(plan, accept_plan_digest=plan.plan_digest)

        self.assertEqual(target.read_bytes(), before)
        self.assertNotIn(event.event_id.encode(), target.read_bytes())

    def test_missing_check_attr_source_support_fails_closed_without_write(self) -> None:
        event = self.event("missing-check-attr-source")
        before = self.file_snapshot(self.root)
        real_git = progress._git

        def unsupported(root, *arguments, **kwargs):
            if arguments and arguments[0] == "check-attr" and any(
                str(item).startswith("--source=") for item in arguments
            ):
                raise progress.ProgressError("git check-attr --source is unavailable")
            return real_git(root, *arguments, **kwargs)

        with mock.patch.object(progress, "_git", side_effect=unsupported):
            with self.assertRaisesRegex(progress.ProgressError, "--source is unavailable"):
                progress.plan_progress_append(project_root=self.root, event=event)

        self.assertEqual(self.file_snapshot(self.root), before)
        common = progress.resolve_git_common_dir(self.root)
        self.assertFalse(
            progress.journal_path(common, event.operation_id, event.event_id).exists()
        )

    def test_two_linked_worktrees_append_concurrently_and_union_semantically(self) -> None:
        worktree_a = self.sandbox / "prd a"
        worktree_b = self.sandbox / "prd b"
        self.git(self.root, "worktree", "add", "-b", "prd-a", str(worktree_a), self.head)
        self.git(self.root, "worktree", "add", "-b", "prd-b", str(worktree_b), self.head)
        event_a = self.event("parallel-a", source_ref="refs/heads/prd-a")
        event_b = self.event("parallel-b", source_ref="refs/heads/prd-b")
        plan_a = progress.plan_progress_append(project_root=worktree_a, event=event_a)
        plan_b = progress.plan_progress_append(project_root=worktree_b, event=event_b)

        def apply(plan):
            return progress.apply_progress_append(
                plan,
                accept_plan_digest=plan.plan_digest,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(apply, (plan_a, plan_b)))

        self.assertTrue(all(item.appended for item in results))
        self.assertEqual(self.event_count(worktree_a, event_a.event_id), 1)
        self.assertEqual(self.event_count(worktree_b, event_b.event_id), 1)
        self.assertNotIn(event_b.event_id.encode(), (worktree_a / "harness/progress.md").read_bytes())
        self.assertNotIn(event_a.event_id.encode(), (worktree_b / "harness/progress.md").read_bytes())

        union = governance.plan_progress_union(
            branch_base=BASE_PROGRESS,
            latest_main=(worktree_a / "harness/progress.md").read_bytes(),
            branch_candidate=(worktree_b / "harness/progress.md").read_bytes(),
        )
        self.assertTrue(union.ready, union.blockers)
        self.assertEqual(union.appended_event_identities, (event_b.event_id,))
        parsed = governance.parse_progress_events(union.preview, source="union")
        self.assertFalse(parsed.blockers)
        self.assertEqual(
            [item.identity for item in parsed.events],
            ["S-20260812-01/OPEN", event_a.event_id, event_b.event_id],
        )
        self.assertIn(f"- operation_id: `{event_a.operation_id}`".encode(), union.preview)
        self.assertIn(f"- operation_id: `{event_b.operation_id}`".encode(), union.preview)

    def test_correction_is_a_new_event_with_explicit_corrects_identity(self) -> None:
        original = self.event("fact-to-correct")
        first = progress.plan_progress_append(project_root=self.root, event=original)
        progress.apply_progress_append(first, accept_plan_digest=first.plan_digest)
        correction = self.event(
            "fact-correction",
            causal_parent=original.event_id,
            corrects=original.event_id,
            summary="corrected conclusion",
        )
        second = progress.plan_progress_append(project_root=self.root, event=correction)
        progress.apply_progress_append(second, accept_plan_digest=second.plan_digest)

        raw = (self.root / "harness/progress.md").read_bytes()
        self.assertIn(f"- corrects: `{original.event_id}`".encode(), raw)
        self.assertEqual(self.event_count(self.root, original.event_id), 1)
        self.assertEqual(self.event_count(self.root, correction.event_id), 1)

    def test_open_workspace_candidate_and_integration_helpers_have_distinct_stable_ids(self) -> None:
        common = {
            "session_id": "S-20260812-02",
            "iteration": "001",
            "occurred_at": "2026-08-12T10:00:00+08:00",
            "source_ref": "refs/heads/main",
            "source_commit": self.head,
            "operation_id": self.operation("helper-operation"),
            "causal_parent": "S-20260812-01/OPEN",
            "evidence_refs": ("evidence:helper",),
            "summary": "helper fact",
        }
        opened = progress.open_event(open_key="iteration-created", **common)
        workspace = progress.workspace_event(workspace_state="ready", **common)
        candidate = progress.candidate_event(
            generation=1, candidate_state="validated", **common
        )
        integration = progress.integration_event(
            integration_state="integrated-candidate", **common
        )

        self.assertEqual(opened.scope, "lifecycle")
        self.assertEqual(opened.event_type, "OPEN")
        self.assertEqual(workspace.scope, "workspace")
        self.assertEqual(candidate.scope, "candidate")
        self.assertEqual(integration.scope, "integration")
        self.assertEqual(integration.event_type, "MERGE")
        self.assertEqual(
            len({opened.event_id, workspace.event_id, candidate.event_id, integration.event_id}),
            4,
        )
        repeated_open = progress.open_event(open_key="iteration-created", **common)
        self.assertEqual(repeated_open.event_id, opened.event_id)
        repeated = progress.workspace_event(workspace_state="ready", **common)
        self.assertEqual(repeated.event_id, workspace.event_id)


if __name__ == "__main__":
    unittest.main()
