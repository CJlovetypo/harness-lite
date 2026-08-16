from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import harness_bundle as bundle
from scripts import harness_lifecycle as lifecycle
from scripts import harness_principle_audit as principle_audit
from scripts import harness_progress as progress
from scripts import harness_train as train
from scripts import harness_workspace as workspace
from scripts import project_harness as core
from scripts.tests.harness_authoritative_fixture import AuthoritativeIntegrationFixture


class LifecycleFacadeTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        # The v2 progress recovery key intentionally includes a full EV digest;
        # keep this real-repository fixture below legacy Windows MAX_PATH while
        # retaining the separate explicit space-path coverage in other suites.
        self.temporary = tempfile.TemporaryDirectory(prefix="hl-")
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "p"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Lifecycle Tests")
        self.git("config", "user.email", "lifecycle@example.invalid")
        # Progress v2 intentionally binds exact committed event bytes.  Keep
        # fixture checkouts canonical instead of inheriting a machine-global
        # autocrlf policy that would be a deliberate fail-closed drift input.
        self.git("config", "core.autocrlf", "false")
        core.apply_operations(
            self.root,
            core.build_init_operations(
                self.root,
                "Lifecycle Test",
                datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),
            ),
        )
        (self.root / "app.txt").write_text("baseline\n", encoding="utf-8")
        self.git("add", "--", "AGENTS.md", "harness", "app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "canonical baseline")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            ["git", "-C", str(cwd or self.root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    @staticmethod
    def operation(character: str) -> str:
        return "OP-" + character * 32

    def request(self, *, iteration: str | None = None, **extra: object) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": lifecycle.REQUEST_SCHEMA,
            "summary": "Implement an isolated product change",
            "risk": {"localized_impact": True, "straightforward_rollback": True},
        }
        if iteration is not None:
            value["iteration"] = iteration
        value.update(extra)
        return value

    def dependency_binding_fixture(self, iteration: str = "002") -> dict[str, str]:
        generation = "g1"
        return {
            "schema_version": workspace.DEPENDENCY_BINDING_SCHEMA,
            "iteration": iteration,
            "generation": generation,
            "candidate_ref": f"refs/project-harness/v2/iterations/{iteration}/candidates/{generation}",
            "candidate_commit": "1" * 40,
            "candidate_tree": "2" * 40,
            "candidate_evidence_ref": f"refs/project-harness/v2/iterations/{iteration}/candidate-evidence/{generation}",
            "candidate_evidence_blob": "3" * 40,
            "candidate_evidence_digest": "4" * 64,
            "candidate_evidence_metadata_digest": "5" * 64,
            "registration_digest": "6" * 64,
            "registry_digest": "7" * 64,
        }

    def test_stacked_route_extracts_only_coordinator_bound_dependency_identity(self) -> None:
        binding = self.dependency_binding_fixture()
        route = {
            "coordinator": {
                "planned_steps": [
                    {"step": "authority-preflight", "writes": False},
                    {
                        "step": "workspace-plan",
                        "implementation_ref": binding["candidate_ref"],
                        "implementation_commit": binding["candidate_commit"],
                        "dependency_bindings": [binding],
                        "dependency_bindings_digest": workspace.dependency_bindings_digest((binding,)),
                    },
                ]
            }
        }

        self.assertEqual(lifecycle._route_dependency_bindings(route), (binding,))
        changed = json.loads(json.dumps(route))
        changed["coordinator"]["planned_steps"][1]["implementation_commit"] = "8" * 40
        with self.assertRaisesRegex(lifecycle.LifecycleError, "implementation start"):
            lifecycle._route_dependency_bindings(changed)
        missing = json.loads(json.dumps(route))
        missing["coordinator"]["planned_steps"][1]["dependency_bindings"] = []
        with self.assertRaisesRegex(lifecycle.LifecycleError, "stable dependency"):
            lifecycle._route_dependency_bindings(missing)

    def test_final_acceptance_stage_is_the_only_main_mutation_stage(self) -> None:
        self.assertEqual(
            lifecycle.STAGE_NEXT_GATE["final-acceptance-register"],
            "plan-cleanup",
        )
        self.assertNotIn("main-advance", lifecycle.STAGE_ORDER)
        with self.assertRaisesRegex(lifecycle.LifecycleError, "second main mutation"):
            lifecycle.plan_main_advance_stage(
                object(),
                lifecycle_operation_id=self.operation("f"),
            )

    def test_ordered_integration_is_the_only_public_prepare_entrypoint(self) -> None:
        self.assertNotIn("integration-prepare", lifecycle.GENERIC_STAGE_ORDER)
        self.assertNotIn("plan_integration_preparation_stage", lifecycle.__all__)
        self.assertNotIn("apply_integration_preparation_stage", lifecycle.__all__)
        self.assertFalse(hasattr(lifecycle, "plan_integration_preparation_stage"))
        self.assertFalse(hasattr(lifecycle, "apply_integration_preparation_stage"))
        with self.assertRaisesRegex(lifecycle.LifecycleError, "unsupported lifecycle stage"):
            lifecycle._stage_artifact_type("integration-prepare")

    def test_candidate_seal_stage_uses_nested_registration_operation_identity(self) -> None:
        registration = mock.Mock(
            operation_id=self.operation("c"),
            project_root=str(self.root),
            candidate_ref="refs/project-harness/v2/iterations/001/candidates/g1",
            candidate_evidence_ref=(
                "refs/project-harness/v2/iterations/001/candidate-evidence/g1"
            ),
        )
        seal = mock.Mock(spec=train.CandidateSealPlan)
        seal.registration_plan = registration
        sentinel = object()
        with mock.patch.object(
            lifecycle,
            "_build_stage_plan",
            return_value=sentinel,
        ) as build:
            result = lifecycle.plan_candidate_registration_stage(
                seal,
                lifecycle_operation_id=self.operation("d"),
            )

        self.assertIs(result, sentinel)
        self.assertEqual(
            build.call_args.kwargs["child_operation_id"],
            registration.operation_id,
        )

    def test_final_cas_crash_recovers_from_durable_child_without_second_cas(self) -> None:
        fixture = AuthoritativeIntegrationFixture()
        try:
            integrated = fixture.publish_integrated_evidence()
            main_plan = fixture.plan_main_advance(integrated)
            token = fixture.advance_token(main_plan)
            lifecycle_operation = self.operation("a")
            plan = lifecycle.plan_final_acceptance_stage(
                integrated,
                lifecycle_operation_id=lifecycle_operation,
                main_confirmation_token=token,
            )

            def crash(stage: str) -> None:
                if stage == "final-acceptance-after-refs":
                    raise RuntimeError("crash after exact final CAS")

            with self.assertRaisesRegex(RuntimeError, "after exact final CAS"):
                lifecycle.apply_final_acceptance_stage(
                    plan,
                    integrated,
                    accepted_plan_digest=plan.plan_digest,
                    main_confirmation_token=token,
                    failpoint=crash,
                )
            refs_after_cas = fixture.git(
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/project-harness/v2",
            ).stdout
            recovered = lifecycle.apply_final_acceptance_stage(
                plan,
                integrated,
                accepted_plan_digest=plan.plan_digest,
                main_confirmation_token=token,
            )

            self.assertIsInstance(recovered.child_result, train.MainAdvanceResult)
            self.assertTrue(recovered.child_result.idempotent)
            self.assertEqual(
                fixture.git(
                    "for-each-ref",
                    "--format=%(refname) %(objectname)",
                    "refs/project-harness/v2",
                ).stdout,
                refs_after_cas,
            )
            self.assertEqual(recovered.notification_receipts, ())
        finally:
            fixture.close()

    def test_stage_plan_is_zero_write_digest_bound_and_requires_exact_acceptance(self) -> None:
        @dataclass(frozen=True)
        class FakePlan:
            schema_version: str
            operation_id: str
            project_root: str
            iteration: str
            plan_digest: str
            blockers: tuple[object, ...] = ()

            def as_dict(self):
                return {
                    **self.__dict__,
                    "blockers": [],
                }

        child = FakePlan(
            "fake/v1",
            self.operation("a"),
            str(self.root),
            "001",
            "1" * 64,
        )
        before = list((self.root / ".git").rglob("*"))
        plan = lifecycle._build_stage_plan(
            project_root=self.root,
            lifecycle_operation_id=self.operation("b"),
            stage="candidate-preverify",
            subject=child,
            child=child,
            child_digest_attribute="plan_digest",
            action_level="silent",
            confirmation_action=None,
        )

        self.assertEqual(before, list((self.root / ".git").rglob("*")))
        lifecycle._validate_stage_plan(plan)
        with self.assertRaisesRegex(lifecycle.LifecycleError, "accepted lifecycle stage digest"):
            lifecycle._apply_stage_transaction(
                plan,
                accepted_plan_digest="2" * 64,
                subject=child,
                replanned=plan,
                execute=lambda _notify: child,
            )
        self.assertFalse(lifecycle._stage_journal_path(lifecycle._context(self.root), plan.operation_id).exists())
        with self.assertRaisesRegex(lifecycle.LifecycleError, "plan digest changed"):
            lifecycle._validate_stage_plan(replace(plan, plan_digest="0" * 64))

    def test_stage_journal_recovers_after_child_and_notification_without_duplicate(self) -> None:
        @dataclass(frozen=True)
        class Fake:
            schema_version: str
            operation_id: str
            project_root: str
            iteration: str
            plan_digest: str

            def as_dict(self):
                return self.__dict__

        @dataclass(frozen=True)
        class FakeResult:
            schema_version: str
            evidence: str
            idempotent: bool

            def as_dict(self):
                return self.__dict__

        subject = Fake("fake/v1", self.operation("c"), str(self.root), "001", "3" * 64)
        plan = lifecycle._build_stage_plan(
            project_root=self.root,
            lifecycle_operation_id=self.operation("d"),
            stage="integration-cleanup",
            subject=subject,
            child=subject,
            child_digest_attribute="plan_digest",
            action_level="notify",
            confirmation_action=None,
        )
        notifications: list[dict[str, object]] = []
        execute_calls = 0

        def execute(proxy):
            nonlocal execute_calls
            execute_calls += 1
            proxy({"schema_version": "fake-notification/v1", "phase": "before"})
            return FakeResult(
                "fake-result/v1",
                "stable",
                # Runtime replay flags are deliberately not authority identity.
                execute_calls >= 3,
            )

        def crash(stage):
            if stage == "after-child-before-stage-journal":
                raise RuntimeError("crash")

        with self.assertRaisesRegex(RuntimeError, "crash"):
            lifecycle._apply_stage_transaction(
                plan,
                accepted_plan_digest=plan.plan_digest,
                subject=subject,
                replanned=plan,
                execute=execute,
                notify=lambda item: notifications.append(item),
                failpoint=crash,
            )
        resumed = lifecycle._apply_stage_transaction(
            plan,
            accepted_plan_digest=plan.plan_digest,
            subject=subject,
            replanned=plan,
            execute=execute,
            notify=lambda item: notifications.append(item),
        )
        replay = lifecycle._apply_stage_transaction(
            plan,
            accepted_plan_digest=plan.plan_digest,
            subject=subject,
            replanned=plan,
            execute=execute,
            notify=lambda item: notifications.append(item),
        )

        self.assertEqual(len(notifications), 1)
        self.assertEqual(len(resumed.notification_receipts), 1)
        self.assertTrue(replay.idempotent_replay)
        self.assertIsInstance(replay.child_result, FakeResult)
        self.assertFalse(replay.child_result.idempotent)
        status = lifecycle.lifecycle_stage_status(self.root)
        self.assertEqual(status["next_gate"], "iteration-close-or-next-candidate")

    def test_stage_cli_help_and_dispatch_do_not_print_token(self) -> None:
        parser = lifecycle.build_parser()
        planned = parser.parse_args(
            [
                "plan-stage",
                "--project-root",
                str(self.root),
                "--stage",
                "candidate-preverify",
                "--operation-id",
                self.operation("e"),
                "--artifact",
                "artifact.json",
                "--json",
            ]
        )
        applied = parser.parse_args(
            [
                "apply-stage",
                "--plan",
                "plan.json",
                "--artifact",
                "artifact.json",
                "--accept-plan-digest",
                "4" * 64,
                "--token",
                "secret-token.json",
                "--json",
            ]
        )
        self.assertEqual(planned.command, "plan-stage")
        self.assertEqual(applied.command, "apply-stage")
        with mock.patch("sys.stdout") as stdout:
            with self.assertRaises(SystemExit):
                parser.parse_args(["plan-stage", "--help"])
        rendered = "".join(str(call) for call in stdout.write.call_args_list)
        self.assertIn("--artifact", rendered)
        self.assertNotIn("secret-token.json", rendered)

    def test_completed_stage_replay_cannot_clear_later_active_stage(self) -> None:
        @dataclass(frozen=True)
        class Fake:
            schema_version: str
            operation_id: str
            project_root: str
            iteration: str
            plan_digest: str

            def as_dict(self):
                return self.__dict__

        lifecycle_operation = self.operation("b")
        first = Fake("fake/v1", self.operation("1"), str(self.root), "001", "1" * 64)
        second = Fake("fake/v1", self.operation("2"), str(self.root), "001", "2" * 64)
        first_plan = lifecycle._build_stage_plan(
            project_root=self.root,
            lifecycle_operation_id=lifecycle_operation,
            stage="candidate-preverify",
            subject=first,
            child=first,
            child_digest_attribute="plan_digest",
            action_level="silent",
            confirmation_action=None,
        )
        second_plan = lifecycle._build_stage_plan(
            project_root=self.root,
            lifecycle_operation_id=lifecycle_operation,
            stage="candidate-register",
            subject=second,
            child=second,
            child_digest_attribute="plan_digest",
            action_level="confirm",
            confirmation_action="create-candidate-seal",
        )
        lifecycle._apply_stage_transaction(
            first_plan,
            accepted_plan_digest=first_plan.plan_digest,
            subject=first,
            replanned=first_plan,
            execute=lambda _proxy: first,
        )

        def crash(stage: str) -> None:
            if stage == "after-child-before-stage-journal":
                raise RuntimeError("later stage crash")

        with self.assertRaisesRegex(RuntimeError, "later stage crash"):
            lifecycle._apply_stage_transaction(
                second_plan,
                accepted_plan_digest=second_plan.plan_digest,
                subject=second,
                replanned=second_plan,
                execute=lambda _proxy: second,
                failpoint=crash,
            )
        context = workspace.resolve_repository(self.root)
        before = lifecycle._load_stage_journal(context, lifecycle_operation)
        assert before is not None
        self.assertEqual(
            lifecycle.digest(before["active_plan"]),
            lifecycle.digest(second_plan.as_dict()),
        )

        with self.assertRaisesRegex(lifecycle.LifecycleError, "another stage is active"):
            lifecycle._apply_stage_transaction(
                first_plan,
                accepted_plan_digest=first_plan.plan_digest,
                subject=first,
                replanned=first_plan,
                execute=lambda _proxy: first,
            )
        after = lifecycle._load_stage_journal(context, lifecycle_operation)
        self.assertEqual(after, before)

        drift_child = replace(second, plan_digest="3" * 64)
        drift_plan = lifecycle._build_stage_plan(
            project_root=self.root,
            lifecycle_operation_id=lifecycle_operation,
            stage="candidate-register",
            subject=second,
            child=drift_child,
            child_digest_attribute="plan_digest",
            action_level="confirm",
            confirmation_action="create-candidate-seal",
        )
        with self.assertRaisesRegex(lifecycle.LifecycleError, "child plan changed"):
            lifecycle._apply_stage_transaction(
                second_plan,
                accepted_plan_digest=second_plan.plan_digest,
                subject=second,
                replanned=drift_plan,
                execute=lambda _proxy: second,
            )
        self.assertEqual(lifecycle._load_stage_journal(context, lifecycle_operation), before)

        recovered = lifecycle._apply_stage_transaction(
            second_plan,
            accepted_plan_digest=second_plan.plan_digest,
            subject=second,
            replanned=second_plan,
            execute=lambda _proxy: second,
        )
        self.assertFalse(recovered.idempotent_replay)
        final = lifecycle._load_stage_journal(context, lifecycle_operation)
        assert final is not None
        self.assertIsNone(final["active_plan"])
        self.assertEqual(
            [item["stage"] for item in final["completed_stages"]],
            ["candidate-preverify", "candidate-register"],
        )

    def refs(self) -> str:
        return self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout

    def status(self) -> str:
        return self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout

    def worktrees(self) -> str:
        return self.git("worktree", "list", "--porcelain", "-z").stdout

    def lifecycle_registry(self) -> Path:
        return self.root / ".git" / "project-harness" / "lifecycle" / "v1"

    def progress_event_blocks(self, root: Path, event_id: str) -> list[bytes]:
        parsed = progress.parse_progress_events(
            (root / "harness" / "progress.md").read_bytes(),
            source=str(root / "harness" / "progress.md"),
        )
        self.assertFalse(parsed.blockers, parsed.blockers)
        return [item.exact_bytes for item in parsed.events if item.identity == event_id]

    def reserve_underlying_local_writer(self) -> str:
        reserve_operation = self.operation("9")
        reserve = core.build_reserve_iteration_plan(
            self.root,
            "git",
            title="Existing local writer",
            operation_id=reserve_operation,
            base_ref="refs/heads/main",
            governance_ref="refs/heads/main",
        )
        journal, _ = core.reserve_iteration(reserve, "git", self.root)
        number = str(journal.iteration)
        base_ref = f"refs/project-harness/v2/iterations/{number}/base"
        activation = workspace.build_activation_plan(
            self.root,
            iteration=number,
            execution_topology="local",
            base_ref=base_ref,
            branch_ref="refs/heads/main",
            worktree_path=self.root,
            owner="existing-a",
            lease_generation=1,
            operation_id=self.operation("8"),
        )
        self.assertFalse(activation.blockers)
        result = workspace.apply_activation(
            self.root,
            iteration=number,
            execution_topology="local",
            base_ref=base_ref,
            branch_ref="refs/heads/main",
            worktree_path=self.root,
            owner="existing-a",
            lease_generation=1,
            operation_id=activation.operation_id,
            accepted_plan_digest=activation.digest,
        )
        self.assertEqual(result["phase"], "succeeded")
        base_time = datetime.fromisoformat(
            self.git("show", "-s", "--format=%cI", str(journal.base_commit)).stdout.strip()
        )
        bundle_operation = self.operation("7")
        bundle_plan = bundle.plan_bundle(
            self.root,
            iteration=number,
            operation_id=bundle_operation,
            planned_at=base_time,
        )
        bundle_result = bundle.apply_bundle(
            self.root,
            iteration=number,
            operation_id=bundle_operation,
            accepted_plan_digest=bundle_plan.plan_digest,
            planned_at=base_time,
        )
        self.assertEqual(bundle_result["phase"], "succeeded")
        self.git("add", "--", "harness")
        self.git("commit", "--no-gpg-sign", "-m", f"draft PRD-{number}")
        return number

    def apply_one(self, request: dict[str, object], title: str, operation: str) -> dict[str, object]:
        plan = lifecycle.plan_start(self.root, request, title=title, operation_id=operation)
        self.assertEqual(plan["phase"], "planned", plan)
        return lifecycle.start_lifecycle(
            self.root,
            request,
            title=title,
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
        )

    def reserve_and_bundle(self, title: str, operation: str) -> str:
        request = self.request()
        reserved = self.apply_one(request, title, operation)
        number = str(reserved["iteration"])
        bundled = self.apply_one(request, title, operation)
        self.assertEqual(bundled["child_result"]["phase"], "succeeded")
        return number

    def approve_and_commit(self, number: str) -> None:
        directory = self.root / "harness" / "iterations" / number
        prd_path = directory / f"prd-{number}.md"
        spec_path = directory / f"spec-{number}.md"
        l1_path = directory / "README.md"
        prd = prd_path.read_text(encoding="utf-8-sig")
        spec = spec_path.read_text(encoding="utf-8-sig")
        prd = prd.replace("- 状态：`草案`", "- 状态：`实施中`")
        for current in ("- 批准依据：待用户明确批准。", "- 批准依据：尚无；当前仅建立草案。"):
            prd = prd.replace(current, f"- 批准依据：用户明确批准 PRD-{number} 产品基线。")
        for placeholder in core.PRD_TEMPLATE_PLACEHOLDERS:
            prd = prd.replace(placeholder, "本轮产品范围已明确，结果必须可观察并且可依据 AC 完成验证。")
        prd = prd.replace(f"### R-{number}-01：待定义", f"### R-{number}-01：交付经批准的隔离实现")
        spec = spec.replace("- 状态：`受 PRD 阻塞`", "- 状态：`实施中`")
        spec = spec.replace(
            f"- 当前批准基线：尚无；等待 PRD-{number} 批准。",
            f"- 当前批准基线：用户已批准 PRD-{number} 产品基线。",
        )
        for current in ("- 批准依据：待用户明确批准。", "- 批准依据：尚无；当前仅建立规格草案。"):
            spec = spec.replace(current, f"- 批准依据：用户明确批准 SPEC-{number} 实施基线。")
        for current in ("- 实施授权：未授权。", "- 实施授权：尚无。"):
            spec = spec.replace(current, "- 实施授权：用户明确授权开始实施。")
        for placeholder in core.SPEC_TEMPLATE_PLACEHOLDERS:
            spec = spec.replace(placeholder, "实现遵循已批准 PRD，保持可回滚，并以自动化验证证明对应验收标准。")
        spec = spec.replace("| 待定义 | 待定义 |", "| lifecycle facade | automated test evidence |")
        prd_path.write_bytes(prd.encode("utf-8"))
        spec_path.write_bytes(spec.encode("utf-8"))

        l1 = l1_path.read_text(encoding="utf-8-sig")
        l1 = l1.replace("- PRD 状态：`草案`", "- PRD 状态：`实施中`")
        l1 = l1.replace("- SPEC 状态：`受 PRD 阻塞`", "- SPEC 状态：`实施中`")
        l1_path.write_bytes(l1.encode("utf-8"))
        for relative in ("harness/README.md", "harness/progress.md"):
            path = self.root / relative
            text = path.read_text(encoding="utf-8-sig")
            lines: list[str] = []
            for line in text.splitlines(keepends=True):
                if f"[{number}]" in line and line.lstrip().startswith("|"):
                    line = line.replace("| 草案 | 受 PRD 阻塞 |", "| 实施中 | 实施中 |")
                lines.append(line)
            path.write_bytes("".join(lines).encode("utf-8"))
        self.git("add", "--", f"harness/iterations/{number}", "harness/README.md", "harness/progress.md")
        self.git("commit", "--no-gpg-sign", "-m", f"approve PRD-{number}")

    def change_principle(self) -> None:
        principle = self.root / "harness" / "principle.md"
        principle.write_text(
            principle.read_text(encoding="utf-8-sig")
            + "\n## P-TEST\n\nUser-approved durable test principle.\n",
            encoding="utf-8",
        )
        # The audit authority is committed Git data; include the newly created
        # bundle together with the principle change in this fixture commit.
        self.git("add", "--", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "change canonical principle")

    def apply_principle_audit(
        self,
        number: str,
        disposition: str,
        operation: str,
    ) -> principle_audit.PrincipleAuditApplyResult:
        decision = principle_audit.PrincipleAuditDecision.create(
            iteration=number,
            authority_ref="refs/heads/main",
            disposition=disposition,
            affected_ids=("P-TEST",),
            evidence_ids=(f"EVIDENCE-{number}-{disposition}",),
            authorization_ids=(f"AUTH-{number}-{disposition}",),
        )
        plan = principle_audit.plan_principle_impact_audit(
            self.root,
            decision=decision,
            operation_id=operation,
        )
        self.assertTrue(plan.ready, plan.as_dict())
        return principle_audit.apply_principle_impact_audit(
            plan,
            accept_plan_digest=plan.plan_digest,
        )

    def start_workspace(
        self,
        number: str,
        title: str,
        operation: str,
        *,
        notify: list[dict[str, object]] | None = None,
        planned: list[dict[str, object]] | None = None,
        request_extra: dict[str, object] | None = None,
        failpoint=None,
    ) -> dict[str, object]:
        request = self.request(**(request_extra or {}))
        plan = lifecycle.plan_start(self.root, request, title=title, operation_id=operation)
        self.assertEqual(plan["phase"], "planned", plan)
        if planned is not None:
            planned.append(plan)
        return lifecycle.start_lifecycle(
            self.root,
            request,
            title=title,
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
            notify=notify.append if notify is not None else None,
            failpoint=failpoint,
        )

    def test_route_has_three_independent_axes_and_never_infers_authority(self) -> None:
        before = (self.refs(), self.status(), self.worktrees())
        route = lifecycle.route_request(self.root, self.request())

        self.assertEqual(route["axes"]["governance_path"], "co-draft")
        self.assertEqual(route["axes"]["execution_topology"], "local")
        self.assertEqual(route["axes"]["authorization_gate"], "approve-prd")
        self.assertFalse(route["inferred_authorization"])
        grilled = lifecycle.route_request(
            self.root,
            self.request(ambiguities=["Who owns the permission decision?"]),
        )
        self.assertEqual(grilled["axes"]["governance_path"], "grill")
        self.assertEqual(grilled["axes"]["execution_topology"], "local")
        self.assertEqual(grilled["axes"]["authorization_gate"], "approve-prd")
        prd_first = lifecycle.route_request(
            self.root,
            self.request(risk={"public_contract": True}),
        )
        self.assertEqual(prd_first["axes"]["governance_path"], "prd-first")
        self.assertEqual(prd_first["axes"]["execution_topology"], "local")
        self.assertEqual(prd_first["axes"]["authorization_gate"], "approve-prd")
        self.assertEqual(before, (self.refs(), self.status(), self.worktrees()))

    def test_plan_start_is_zero_write_and_bad_digest_writes_no_journal(self) -> None:
        request = self.request()
        before = (self.refs(), self.status(), self.worktrees())
        plan = lifecycle.plan_start(
            self.root,
            request,
            title="First feature",
            operation_id=self.operation("a"),
        )

        self.assertEqual(plan["phase"], "planned")
        self.assertEqual(plan["accepted_child"]["action"], "reserve-iteration")
        self.assertEqual(before, (self.refs(), self.status(), self.worktrees()))
        self.assertFalse(self.lifecycle_registry().exists())

        with self.assertRaisesRegex(lifecycle.LifecycleError, "digest"):
            lifecycle.start_lifecycle(
                self.root,
                request,
                title="First feature",
                operation_id=self.operation("a"),
                accepted_plan_digest="0" * 64,
            )
        self.assertEqual(before, (self.refs(), self.status(), self.worktrees()))
        journals = self.lifecycle_registry() / "journal"
        self.assertFalse(journals.exists() and any(journals.iterdir()))

    def test_uncommitted_approval_cannot_activate_implementation(self) -> None:
        operation = self.operation("b")
        number = self.reserve_and_bundle("Approval boundary", operation)
        self.approve_and_commit(number)
        prd = self.root / "harness" / "iterations" / number / f"prd-{number}.md"
        prd.write_text(prd.read_text(encoding="utf-8") + "\nUncommitted approval mutation.\n", encoding="utf-8")

        plan = lifecycle.plan_start(
            self.root,
            self.request(),
            title="Approval boundary",
            operation_id=operation,
        )

        self.assertEqual(plan["phase"], "blocked")
        self.assertIn(
            f"authority-live-commit-mismatch:harness/iterations/{number}/prd-{number}.md",
            plan["blocking_reasons"],
        )
        self.assertEqual(plan["next_gate"], "commit-or-reconcile-authority")
        journal_path = self.lifecycle_registry() / "journal" / f"{operation}.json"
        before_journal = journal_path.read_bytes()
        result = lifecycle.start_lifecycle(
            self.root,
            self.request(),
            title="Approval boundary",
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
        )
        self.assertEqual(result["phase"], "blocked")
        self.assertEqual(result["next_gate"], "commit-or-reconcile-authority")
        self.assertEqual(journal_path.read_bytes(), before_journal)
        self.assertFalse((self.root / ".git" / "project-harness" / "workspace" / "v1" / "leases").exists())

    def test_reservation_crash_retries_without_duplicate_identity_or_child_write(self) -> None:
        operation = self.operation("f")
        request = self.request()
        plan = lifecycle.plan_start(self.root, request, title="Crash-safe reserve", operation_id=operation)

        def crash(stage: str) -> None:
            if stage == "after-child-before-journal":
                raise BaseException("simulated process loss")

        with self.assertRaisesRegex(BaseException, "process loss"):
            lifecycle.start_lifecycle(
                self.root,
                request,
                title="Crash-safe reserve",
                operation_id=operation,
                accepted_plan_digest=str(plan["plan_digest"]),
                failpoint=crash,
            )
        refs_after_crash = self.refs()
        self.assertIn("refs/project-harness/v2/allocations/001", refs_after_crash)

        recovered = lifecycle.start_lifecycle(
            self.root,
            request,
            title="Crash-safe reserve",
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
        )
        self.assertEqual(recovered["iteration"], "001")
        self.assertTrue(recovered["child_result"]["reservation"]["idempotent_replay"])
        self.assertEqual(self.refs(), refs_after_crash)

        replay = lifecycle.start_lifecycle(
            self.root,
            request,
            title="Crash-safe reserve",
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.refs(), refs_after_crash)

    def test_zero_to_one_uses_exact_latest_main_locally_without_branch_worktree_commit_or_stash(self) -> None:
        self.git("config", "core.autocrlf", "true")
        operation = self.operation("c")
        title = "Only active feature"
        number = self.reserve_and_bundle(title, operation)
        self.approve_and_commit(number)
        head = self.git("rev-parse", "HEAD").stdout.strip()
        branches = self.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").stdout
        worktrees = self.worktrees()

        plan = lifecycle.plan_start(self.root, self.request(), title=title, operation_id=operation)
        self.assertEqual(plan["phase"], "planned", plan)
        progress_binding = plan["accepted_child"]["parameters"]["activation_progress"]
        self.assertEqual(progress_binding["topology"], "local")
        committed_progress = lifecycle._committed_progress_snapshot(
            workspace.resolve_repository(self.root),
            head,
        )
        self.assertEqual(
            progress_binding["expected_before_sha256"],
            hashlib.sha256(committed_progress).hexdigest(),
        )
        self.assertNotEqual(
            progress_binding["event"]["event_id"],
            progress_binding["event"]["session_id"],
        )
        result = lifecycle.start_lifecycle(
            self.root,
            self.request(),
            title=title,
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
        )

        self.assertEqual(result["child_result"]["phase"], "succeeded")
        self.assertEqual(result["child_result"]["topology"]["phase"], "SINGLE_LOCAL")
        self.assertEqual(result["interaction_events"], [])
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").stdout, branches)
        self.assertEqual(self.worktrees(), worktrees)
        self.assertFalse((self.root / ".git" / "refs" / "stash").exists())
        progress_result = result["child_result"]["progress_child"]
        self.assertEqual(progress_result["phase"], "APPLIED")
        self.assertEqual(progress_result["topology"], "local")
        self.assertFalse(progress_result["commit_created"])
        self.assertFalse(progress_result["pushed"])
        self.assertEqual(
            hashlib.sha256((self.root / "harness" / "progress.md").read_bytes()).hexdigest(),
            progress_binding["expected_after_sha256"],
        )
        event_id = progress_binding["event"]["event_id"]
        self.assertEqual(len(self.progress_event_blocks(self.root, event_id)), 1)
        event = progress_binding["event"]
        self.assertEqual(event["iteration"], number)
        self.assertEqual(event["scope"], "workspace")
        self.assertEqual(event["operation_id"], plan["accepted_child"]["operation_id"])
        self.assertEqual(event["source_ref"], "refs/heads/main")
        self.assertEqual(event["source_commit"], head)
        self.assertIn("topology:local", event["evidence_refs"])
        self.assertIn(
            f"allocation-base:refs/project-harness/v2/iterations/{number}/base@{plan['accepted_child']['parameters']['base_commit']}",
            event["evidence_refs"],
        )

    def test_one_to_two_notifies_before_and_after_and_preserves_dirty_a(self) -> None:
        self.git("config", "core.autocrlf", "true")
        a = self.reserve_underlying_local_writer()
        op_b = self.operation("e")
        title_b = "Parallel B"
        b = self.reserve_and_bundle(title_b, op_b)
        self.approve_and_commit(b)
        (self.root / "app.txt").write_text("A dirty work\n", encoding="utf-8")
        (self.root / "a-staged.txt").write_text("A index\n", encoding="utf-8")
        self.git("add", "--", "a-staged.txt")
        (self.root / "a-untracked.txt").write_text("A untracked\n", encoding="utf-8")
        status_a = self.status()
        head_a = self.git("rev-parse", "HEAD").stdout.strip()

        target = self.root.parent / f"{self.root.name}.prd-{b}"
        events: list[dict[str, object]] = []
        plans: list[dict[str, object]] = []
        result = self.start_workspace(
            b,
            title_b,
            op_b,
            notify=events,
            planned=plans,
        )

        self.assertEqual(plans[0]["action_level"], "notify")
        self.assertEqual(plans[0]["actions"][0]["notification"], events[0])
        self.assertEqual([item["phase"] for item in events], ["before", "after"])
        notification_ids = [str(item["notification_id"]) for item in events]
        self.assertEqual(len(set(notification_ids)), 2)
        self.assertTrue(all(lifecycle.NOTIFICATION_ID_RE.fullmatch(item) for item in notification_ids))
        before = events[0]["facts"]
        self.assertEqual(before["iteration"], b)
        self.assertEqual(before["worktree"]["path"], str(target))
        self.assertEqual(before["effect_on_existing_prds"]["strategy"], "add-only")
        self.assertFalse(before["remote"]["involved"])
        self.assertEqual(before["implementation_start"]["ref"], "refs/heads/main")
        self.assertEqual(before["implementation_start"]["commit"], head_a)
        self.assertEqual(result["action_level"], "notify")
        self.assertEqual(self.status(), status_a)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head_a)
        self.assertTrue(target.is_dir())
        base_b = self.git("rev-parse", f"refs/project-harness/v2/iterations/{b}/base").stdout.strip()
        self.assertEqual(before["base"]["commit"], base_b)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=target).stdout.strip(), head_a)
        self.assertEqual((target / "app.txt").read_text(encoding="utf-8"), "baseline\n")
        self.assertFalse((target / "a-untracked.txt").exists())
        progress_binding = plans[0]["accepted_child"]["parameters"]["activation_progress"]
        event_id = progress_binding["event"]["event_id"]
        self.assertEqual(progress_binding["target_project_root"], str(target))
        self.assertEqual(progress_binding["topology"], "worktree")
        self.assertEqual(progress_binding["source"]["ref"], f"refs/heads/harness/prd-{b}")
        self.assertEqual(progress_binding["source"]["commit"], head_a)
        actual_progress_sha = hashlib.sha256(
            (target / "harness" / "progress.md").read_bytes()
        ).hexdigest()
        self.assertIn(
            actual_progress_sha,
            {
                item["after_sha256"]
                for item in progress_binding["allowed_variants"].values()
            },
        )
        self.assertEqual(len(self.progress_event_blocks(target, event_id)), 1)
        progress_result = result["child_result"]["progress_child"]
        self.assertEqual(progress_result["phase"], "APPLIED")
        self.assertEqual(progress_result["topology"], "worktree")
        self.assertFalse(progress_result["commit_created"])
        self.assertFalse(progress_result["pushed"])
        self.assertFalse(events[1]["facts"]["git_effects"]["commit_created"])
        self.assertFalse(events[1]["facts"]["git_effects"]["push_performed"])
        receipts = result["notification_receipts"]
        self.assertEqual([item["notification_id"] for item in receipts], notification_ids)
        self.assertEqual([item["callback_state"] for item in receipts], ["returned", "returned"])
        self.assertEqual(result["notification_recovery"]["suppressed_recorded_ids"], [])
        self.assertFalse(result["notification_recovery"]["callback_grants_authority"])
        journal = lifecycle._load_lifecycle_journal(workspace.resolve_repository(self.root), op_b)
        self.assertIsNotNone(journal)
        self.assertEqual(journal["notification_receipts"], receipts)

    def test_local_main_release_stage_records_progress_once_and_recovers_after_progress(self) -> None:
        a = self.reserve_underlying_local_writer()
        operation_b = self.operation("6")
        title_b = "Earlier integration B"
        b = self.reserve_and_bundle(title_b, operation_b)
        self.approve_and_commit(b)
        self.start_workspace(b, title_b, operation_b)

        (self.root / "app.txt").write_text("A dirty implementation\n", encoding="utf-8")
        (self.root / "a-index.txt").write_text("A staged bytes\n", encoding="utf-8")
        self.git("add", "--", "a-index.txt")
        (self.root / "a-untracked.txt").write_text("A untracked bytes\n", encoding="utf-8")
        index_before = (self.root / ".git" / "index").read_bytes()
        feature_before = (self.root / "app.txt").read_bytes()
        untracked_before = (self.root / "a-untracked.txt").read_bytes()
        head_before = self.git("rev-parse", "HEAD").stdout.strip()
        main_before = self.git("rev-parse", "refs/heads/main").stdout.strip()

        context = workspace.resolve_repository(self.root)
        lease = workspace.load_lease(context, a)
        assert lease is not None
        bind = workspace.build_bind_local_branch_plan(
            self.root,
            iteration=a,
            owner=str(lease["owner"]),
            lease_generation=int(lease["generation"]),
            worktree_path=self.root,
            base_commit=self.git(
                "rev-parse", f"refs/project-harness/v2/iterations/{a}/base"
            ).stdout.strip(),
            new_branch_ref=f"refs/heads/harness/prd-{a}",
            operation_id=self.operation("5"),
        )
        self.assertEqual(bind.blockers, (), bind.blockers)
        parsed = progress.parse_progress_events(
            (self.root / "harness" / "progress.md").read_bytes(),
            source="local-main-release-before",
        )
        self.assertFalse(parsed.blockers, parsed.blockers)
        parent = parsed.events[-1].identity if parsed.events else None
        lifecycle_operation = self.operation("4")
        stage_plan = lifecycle.plan_local_main_release_stage(
            bind,
            lifecycle_operation_id=lifecycle_operation,
            session_id="S-20260812-09",
            occurred_at="2026-08-12T09:00:00+08:00",
            causal_parent=parent,
        )
        self.assertTrue(stage_plan.ready, stage_plan.blockers)
        notifications: list[object] = []

        def crash(stage: str) -> None:
            if stage == "local-main-release-after-progress":
                raise BaseException("power loss after progress")

        with self.assertRaisesRegex(BaseException, "after progress"):
            lifecycle.apply_local_main_release_stage(
                stage_plan,
                bind,
                accepted_plan_digest=stage_plan.plan_digest,
                session_id="S-20260812-09",
                occurred_at="2026-08-12T09:00:00+08:00",
                causal_parent=parent,
                notify=notifications.append,
                failpoint=crash,
            )
        self.assertEqual(len(notifications), 1)
        self.assertEqual(self.git("symbolic-ref", "HEAD").stdout.strip(), f"refs/heads/harness/prd-{a}")
        self.assertEqual(self.git("rev-parse", "refs/heads/main").stdout.strip(), main_before)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head_before)
        self.assertEqual((self.root / ".git" / "index").read_bytes(), index_before)
        self.assertEqual((self.root / "app.txt").read_bytes(), feature_before)
        self.assertEqual((self.root / "a-untracked.txt").read_bytes(), untracked_before)
        planned_event_id = next(
            ref for ref in stage_plan.evidence_refs if ref.startswith("EV-")
        )
        self.assertEqual(len(self.progress_event_blocks(self.root, planned_event_id)), 1)

        recovered = lifecycle.apply_local_main_release_stage(
            stage_plan,
            bind,
            accepted_plan_digest=stage_plan.plan_digest,
            session_id="S-20260812-09",
            occurred_at="2026-08-12T09:00:00+08:00",
            causal_parent=parent,
            notify=notifications.append,
        )
        self.assertEqual(recovered.child_result["phase"], "succeeded")
        event_id = recovered.child_result["progress"]["event_id"]
        self.assertEqual(len(self.progress_event_blocks(self.root, event_id)), 1)
        self.assertEqual([item["phase"] for item in notifications], ["before", "after"])
        self.assertEqual((self.root / ".git" / "index").read_bytes(), index_before)
        self.assertEqual((self.root / "app.txt").read_bytes(), feature_before)
        self.assertEqual((self.root / "a-untracked.txt").read_bytes(), untracked_before)

        replay = lifecycle.apply_local_main_release_stage(
            stage_plan,
            bind,
            accepted_plan_digest=stage_plan.plan_digest,
            session_id="S-20260812-09",
            occurred_at="2026-08-12T09:00:00+08:00",
            causal_parent=parent,
            notify=notifications.append,
        )
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(len(notifications), 2)
        self.assertEqual(len(self.progress_event_blocks(self.root, event_id)), 1)
        self.assertEqual(replay.child_result_digest, recovered.child_result_digest)

    def test_crash_after_workspace_child_retries_idempotently_without_duplicate_worktree(self) -> None:
        self.reserve_underlying_local_writer()
        op_b = self.operation("2")
        title_b = "Crash B"
        b = self.reserve_and_bundle(title_b, op_b)
        self.approve_and_commit(b)
        target = self.root.parent / f"{self.root.name}.prd-{b}"
        request = self.request()
        plan = lifecycle.plan_start(self.root, request, title=title_b, operation_id=op_b)

        def crash(stage: str) -> None:
            if stage == "after-child-before-journal":
                raise BaseException("simulated power loss")

        first_events: list[dict[str, object]] = []
        with self.assertRaisesRegex(BaseException, "power loss"):
            lifecycle.start_lifecycle(
                self.root,
                request,
                title=title_b,
                operation_id=op_b,
                accepted_plan_digest=str(plan["plan_digest"]),
                notify=first_events.append,
                failpoint=crash,
            )
        self.assertTrue(target.is_dir())
        self.assertEqual([item["phase"] for item in first_events], ["before", "after"])
        first_notification_ids = [str(item["notification_id"]) for item in first_events]
        context = workspace.resolve_repository(self.root)
        count_before = sum(
            workspace.same_path(str(item["path"]), target)
            for item in workspace.list_worktrees(context, include_status=False)
        )

        replay_events: list[dict[str, object]] = []
        result = lifecycle.start_lifecycle(
            self.root,
            request,
            title=title_b,
            operation_id=op_b,
            accepted_plan_digest=str(plan["plan_digest"]),
            notify=replay_events.append,
        )

        self.assertEqual(count_before, 1)
        self.assertEqual(
            sum(
                workspace.same_path(str(item["path"]), target)
                for item in workspace.list_worktrees(context, include_status=False)
            ),
            1,
        )
        self.assertTrue(result["child_result"]["idempotent_replay"])
        self.assertEqual(replay_events, [])
        self.assertEqual(
            result["notification_recovery"]["suppressed_recorded_ids"],
            first_notification_ids,
        )
        self.assertEqual(
            [item["notification_id"] for item in result["notification_receipts"]],
            first_notification_ids,
        )
        event_id = plan["accepted_child"]["parameters"]["activation_progress"]["event"]["event_id"]
        self.assertEqual(len(self.progress_event_blocks(target, event_id)), 1)

    def test_notify_callback_error_is_durable_explainable_and_not_repeated(self) -> None:
        self.reserve_underlying_local_writer()
        operation = self.operation("b")
        title = "Callback recovery B"
        number = self.reserve_and_bundle(title, operation)
        self.approve_and_commit(number)
        target = self.root.parent / f"{self.root.name}.prd-{number}"
        request = self.request()
        plan = lifecycle.plan_start(self.root, request, title=title, operation_id=operation)
        callback_events: list[dict[str, object]] = []

        def fail_before(payload: dict[str, object]) -> None:
            callback_events.append(payload)
            if payload["phase"] == "before":
                raise RuntimeError("notification renderer unavailable")

        with self.assertRaisesRegex(RuntimeError, "renderer unavailable"):
            lifecycle.start_lifecycle(
                self.root,
                request,
                title=title,
                operation_id=operation,
                accepted_plan_digest=str(plan["plan_digest"]),
                notify=fail_before,
            )

        self.assertEqual([item["phase"] for item in callback_events], ["before"])
        self.assertFalse(target.exists())
        context = workspace.resolve_repository(self.root)
        failed_journal = lifecycle._load_lifecycle_journal(context, operation)
        self.assertIsNotNone(failed_journal)
        failed_receipts = failed_journal["notification_receipts"]
        self.assertEqual(len(failed_receipts), 1)
        self.assertEqual(failed_receipts[0]["callback_state"], "raised")
        self.assertEqual(failed_receipts[0]["callback_error"]["type"], "RuntimeError")
        before_id = str(failed_receipts[0]["notification_id"])

        replay_events: list[dict[str, object]] = []
        recovered = lifecycle.start_lifecycle(
            self.root,
            request,
            title=title,
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
            notify=replay_events.append,
        )

        self.assertTrue(target.is_dir())
        self.assertEqual([item["phase"] for item in replay_events], ["after"])
        self.assertNotEqual(replay_events[0]["notification_id"], before_id)
        self.assertEqual(recovered["interaction_events"], replay_events)
        receipts = recovered["notification_receipts"]
        self.assertEqual([item["phase"] for item in receipts], ["before", "after"])
        self.assertEqual([item["callback_state"] for item in receipts], ["raised", "returned"])
        self.assertEqual(recovered["notification_recovery"]["suppressed_recorded_ids"], [before_id])
        self.assertEqual(recovered["notification_recovery"]["callback_failed_ids"], [before_id])
        self.assertFalse(recovered["notification_recovery"]["callback_grants_authority"])

    def test_progress_crash_after_append_retries_without_duplicate_activation_event(self) -> None:
        self.reserve_underlying_local_writer()
        operation = self.operation("3")
        title = "Progress crash B"
        number = self.reserve_and_bundle(title, operation)
        self.approve_and_commit(number)
        request = self.request()
        plan = lifecycle.plan_start(self.root, request, title=title, operation_id=operation)
        target = Path(plan["accepted_child"]["parameters"]["worktree_path"])
        event_id = plan["accepted_child"]["parameters"]["activation_progress"]["event"]["event_id"]

        def crash(stage: str) -> None:
            if stage == "progress:after_replace_before_journal":
                raise BaseException("simulated progress process loss")

        with self.assertRaisesRegex(BaseException, "progress process loss"):
            lifecycle.start_lifecycle(
                self.root,
                request,
                title=title,
                operation_id=operation,
                accepted_plan_digest=str(plan["plan_digest"]),
                failpoint=crash,
            )
        self.assertTrue(target.is_dir())
        self.assertEqual(len(self.progress_event_blocks(target, event_id)), 1)

        recovered = lifecycle.start_lifecycle(
            self.root,
            request,
            title=title,
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
        )
        self.assertEqual(recovered["phase"], "progressed")
        self.assertEqual(recovered["child_result"]["progress_child"]["phase"], "APPLIED")
        self.assertTrue(recovered["child_result"]["progress_child"]["resumed"])
        self.assertEqual(len(self.progress_event_blocks(target, event_id)), 1)

    def test_wrong_prebound_progress_digest_blocks_before_workspace_activation(self) -> None:
        operation = self.operation("4")
        title = "Wrong digest local"
        number = self.reserve_and_bundle(title, operation)
        self.approve_and_commit(number)
        request = self.request()
        plan = lifecycle.plan_start(self.root, request, title=title, operation_id=operation)
        tampered = json.loads(json.dumps(plan))
        binding = tampered["accepted_child"]["parameters"]["activation_progress"]
        binding["expected_before_sha256"] = "0" * 64
        tampered_body = dict(tampered)
        tampered_body.pop("plan_digest")
        tampered["plan_digest"] = lifecycle.digest(tampered_body)
        journal = lifecycle._initial_journal(tampered)
        lifecycle._atomic_json(
            workspace.resolve_repository(self.root),
            self.lifecycle_registry() / "journal" / f"{operation}.json",
            journal,
        )
        before = (self.refs(), self.status(), self.worktrees())

        with self.assertRaisesRegex(lifecycle.LifecycleError, "progress binding changed"):
            lifecycle.start_lifecycle(
                self.root,
                request,
                title=title,
                operation_id=operation,
                accepted_plan_digest=str(tampered["plan_digest"]),
            )

        self.assertEqual(before, (self.refs(), self.status(), self.worktrees()))
        self.assertFalse((self.root / ".git" / "project-harness" / "workspace" / "v1" / "leases").exists())

    def test_dirty_progress_after_workspace_activation_blocks_append_without_replacing_bytes(self) -> None:
        self.reserve_underlying_local_writer()
        operation = self.operation("5")
        title = "Dirty progress B"
        number = self.reserve_and_bundle(title, operation)
        self.approve_and_commit(number)
        request = self.request()
        plan = lifecycle.plan_start(self.root, request, title=title, operation_id=operation)
        target = Path(plan["accepted_child"]["parameters"]["worktree_path"])
        expected_before = plan["accepted_child"]["parameters"]["activation_progress"]["expected_before_sha256"]

        def dirty(stage: str) -> None:
            if stage == "after-workspace-before-progress":
                path = target / "harness" / "progress.md"
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected_before)
                path.write_bytes(path.read_bytes() + b"\nUNOWNED DIRTY PROGRESS\n")

        with self.assertRaisesRegex(lifecycle.LifecycleError, "progress bytes differ"):
            lifecycle.start_lifecycle(
                self.root,
                request,
                title=title,
                operation_id=operation,
                accepted_plan_digest=str(plan["plan_digest"]),
                failpoint=dirty,
            )
        dirty_bytes = (target / "harness" / "progress.md").read_bytes()
        self.assertTrue(dirty_bytes.endswith(b"UNOWNED DIRTY PROGRESS\n"))
        status = lifecycle.lifecycle_status(self.root)
        child = next(
            item
            for item in status["progress"]["children"]
            if item["operation_id"] == plan["accepted_child"]["operation_id"]
        )
        self.assertEqual(child["phase"], "BLOCKED")
        self.assertTrue(child["blocking"])
        self.assertEqual(status["progress"]["next_gate"], "reconcile-progress")
        self.assertEqual(status["next_gate"], "reconcile-progress")

    def test_status_is_versioned_read_only_aggregate(self) -> None:
        before = (self.refs(), self.status(), self.worktrees())
        value = lifecycle.lifecycle_status(self.root)

        self.assertEqual(value["schema_version"], lifecycle.STATUS_SCHEMA)
        self.assertIn("governance", value)
        self.assertIn("workspace", value)
        self.assertIn("train", value)
        self.assertIn("routes", value)
        self.assertFalse(value["pushed"])
        self.assertEqual(before, (self.refs(), self.status(), self.worktrees()))

    def test_status_and_route_block_candidate_integration_on_principle_drift(self) -> None:
        operation = self.operation("6")
        number = self.reserve_and_bundle("Principle drift", operation)
        self.change_principle()

        value = lifecycle.lifecycle_status(self.root)

        self.assertEqual(value["phase"], "blocked")
        self.assertTrue(value["principle_drift"])
        self.assertEqual(value["next_gate"], "principle-impact-audit")
        gate = next(item for item in value["governance"]["iteration_gates"] if item["iteration"] == number)
        self.assertTrue(gate["principle_drift"])
        self.assertFalse(gate["candidate_integration_allowed"])
        self.assertEqual(gate["next_gate"], "plan-principle-impact-audit")
        self.assertIsNone(gate["audit_generation"])
        self.assertIsNone(gate["audit_causal_tip"])
        self.assertNotEqual(
            gate["allocation_principle_sha256"],
            gate["canonical_main_principle_sha256"],
        )
        route = lifecycle.route_request(
            self.root,
            self.request(iteration=number, read_only=True),
        )
        self.assertEqual(route["phase"], "blocked")
        self.assertEqual(route["next_gate"], "plan-principle-impact-audit")
        self.assertIn("principle:principle-impact-audit-required", route["blocking_reasons"])

        impact = self.apply_principle_audit(
            number,
            principle_audit.DISPOSITION_IMPACT,
            self.operation("5"),
        )
        blocked = lifecycle.lifecycle_status(self.root)
        impact_gate = next(
            item
            for item in blocked["governance"]["iteration_gates"]
            if item["iteration"] == number
        )
        self.assertEqual(impact_gate["audit_generation"], 1)
        self.assertEqual(impact_gate["audit_causal_tip"], impact.receipt.receipt_digest)
        self.assertFalse(impact_gate["candidate_integration_allowed"])
        self.assertIn(
            "principle-reapproval-required",
            impact_gate["principle_gate"]["blockers"],
        )

    def test_no_impact_tip_survives_same_principle_fast_forward_and_tamper_fails_closed(self) -> None:
        number = self.reserve_and_bundle("No impact audit", self.operation("4"))
        self.change_principle()
        applied = self.apply_principle_audit(
            number,
            principle_audit.DISPOSITION_NO_IMPACT,
            self.operation("3"),
        )

        current = lifecycle.lifecycle_status(self.root)
        gate = next(
            item
            for item in current["governance"]["iteration_gates"]
            if item["iteration"] == number
        )
        self.assertTrue(gate["principle_drift"])
        self.assertTrue(gate["principle_gate"]["allowed"])
        self.assertEqual(gate["audit_generation"], 1)
        self.assertEqual(gate["audit_causal_tip"], applied.receipt.receipt_digest)

        (self.root / "app.txt").write_text("same-principle fast-forward\n", encoding="utf-8")
        self.git("add", "--", "app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "unrelated main fast-forward")
        advanced = lifecycle.lifecycle_status(self.root)
        advanced_gate = next(
            item
            for item in advanced["governance"]["iteration_gates"]
            if item["iteration"] == number
        )
        self.assertTrue(advanced_gate["principle_gate"]["allowed"])
        self.assertEqual(advanced_gate["audit_causal_tip"], applied.receipt.receipt_digest)

        receipt_path = Path(applied.receipt_path)
        original_receipt = receipt_path.read_text(encoding="utf-8")
        tampered = json.loads(original_receipt)
        tampered["receipt_digest"] = "f" * 64
        receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
        corrupt = lifecycle.lifecycle_status(self.root)
        corrupt_gate = next(
            item
            for item in corrupt["governance"]["iteration_gates"]
            if item["iteration"] == number
        )
        self.assertFalse(corrupt_gate["principle_gate"]["allowed"])
        self.assertEqual(
            corrupt_gate["principle_gate"]["next_gate"],
            "reconcile-principle-audit-chain",
        )

        receipt_path.write_text(original_receipt, encoding="utf-8")
        journal_path = Path(applied.journal_path)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["phase"] = "APPLYING"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        corrupt_journal = lifecycle.lifecycle_status(self.root)
        journal_gate = next(
            item
            for item in corrupt_journal["governance"]["iteration_gates"]
            if item["iteration"] == number
        )
        self.assertFalse(journal_gate["principle_gate"]["allowed"])
        self.assertEqual(
            journal_gate["principle_gate"]["next_gate"],
            "reconcile-principle-audit-chain",
        )

    def test_request_rejects_non_boolean_risk_and_unknown_authority_claim(self) -> None:
        with self.assertRaisesRegex(lifecycle.LifecycleError, "risk.user_visible"):
            lifecycle.validate_request(
                self.request(risk={"user_visible": "yes"})
            )
        with self.assertRaisesRegex(lifecycle.LifecycleError, "unknown request fields"):
            lifecycle.validate_request(
                {**self.request(), "implementation_authorized": True}
            )


if __name__ == "__main__":
    unittest.main()
