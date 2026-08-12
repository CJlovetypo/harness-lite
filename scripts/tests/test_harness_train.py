from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from harness_candidate import (  # noqa: E402
    AcceptanceEvidence,
    IdentityRebindInput,
    build_identity_rebinding,
)
import harness_workspace as workspace  # noqa: E402
import harness_governance  # noqa: E402
import harness_principle_audit as principle_audit  # noqa: E402
import harness_train as train  # noqa: E402
import harness_integrated_evidence as integrated_registry  # noqa: E402
from harness_train import (  # noqa: E402
    ConfirmationToken,
    InjectedCrash,
    TrainError,
    VerifyCommand,
    apply_integration_commit,
    apply_cleanup_integration,
    apply_main_advance,
    apply_prepare_integration,
    apply_register_candidate,
    build_governance_receipt,
    confirmation_token_digest,
    finalize_integration_evidence,
    integration_commit_interaction,
    main_advance_interaction,
    plan_main_advance,
    plan_cleanup_integration,
    plan_prepare_integration,
    plan_register_candidate,
    prepare_candidate_registration,
    load_registered_candidate,
    registered_candidate_gate,
)


class HarnessTrainTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.git_executable = shutil.which("git")
        if not self.git_executable:
            self.skipTest("git is required")
        self.temporary = tempfile.TemporaryDirectory(prefix="harness train tests ")
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "primary project with spaces"
        self.root.mkdir()
        self.feature_a = self.sandbox / "feature A with spaces"
        self.feature_b = self.sandbox / "feature B with spaces"
        self.feature_c = self.sandbox / "feature C with spaces"
        self.git_config = self.sandbox / "isolated gitconfig"
        subprocess.run(
            [self.git_executable, "config", "--file", str(self.git_config), "user.name", "Train Tests"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.git_executable, "config", "--file", str(self.git_config), "user.email", "train@example.invalid"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": str(self.git_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Train Tests")
        self.git("config", "user.email", "train@example.invalid")
        self.write("harness/principle.md", "# Principle\n\nApproved global principle.\n")
        self.write(
            "harness/progress.md",
            "<!-- managed-by: harness-lite v1 -->\n# Progress\n\n## 过程事件\n",
        )
        self.write(".gitignore", "ignored.asset\n")
        self.write("app.txt", "baseline\n")
        self.write_authority("001")
        self.write_authority("002")
        self.write_authority("003", depends_on=("001",))
        self.git("add", "--", ".")
        self.git("commit", "--no-gpg-sign", "-m", "baseline authority")
        self.base = self.oid("HEAD")
        self.tree = self.oid("HEAD^{tree}")
        for iteration in ("999", "001", "002", "003"):
            self.reserve_workspace(iteration)
        self.workspace_guards = {}
        self.activate_workspace("999", "local", "refs/heads/main", self.root, "task-bootstrap")
        self.activate_workspace("001", "worktree", "refs/heads/feature/001", self.feature_a, "task-a")
        (self.feature_a / "a.txt").write_text("feature A\n", encoding="utf-8")
        self.git("add", "--", "a.txt", cwd=self.feature_a)
        self.git("commit", "--no-gpg-sign", "-m", "feature A", cwd=self.feature_a)
        self.a_commit = self.oid("refs/heads/feature/001")
        self.activate_workspace("002", "worktree", "refs/heads/feature/002", self.feature_b, "task-b")
        (self.feature_b / "b.txt").write_text("feature B\n", encoding="utf-8")
        self.git("add", "--", "b.txt", cwd=self.feature_b)
        self.git("commit", "--no-gpg-sign", "-m", "feature B", cwd=self.feature_b)
        self.b_commit = self.oid("refs/heads/feature/002")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(cwd or self.root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )

    def oid(self, value: str, *, cwd: Path | None = None) -> str:
        return self.git("rev-parse", value, cwd=cwd).stdout.strip()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_authority(
        self,
        iteration: str,
        *,
        depends_on: tuple[str, ...] = (),
    ) -> None:
        directory = f"harness/iterations/{iteration}"
        principle_sha = hashlib.sha256(
            (self.root / "harness" / "principle.md").read_bytes()
        ).hexdigest()
        self.write(
            f"{directory}/prd-{iteration}.md",
            (
                "<!-- managed-by: harness-lite v1 -->\n"
                f"# PRD-{iteration}: Fixture\n\n"
                "- 状态：`实施中`\n"
                f"- 批准依据：用户已批准 PRD-{iteration} 基线（AUTH-PRD-{iteration}）\n\n"
                f"- principle_base_hash：`{principle_sha}`\n\n"
                + (
                    "- 依赖 PRD："
                    + "、".join(f"PRD-{item}" for item in depends_on)
                    + "\n\n"
                    if depends_on
                    else ""
                )
                + f"## 验收标准\n\n### AC-{iteration}-01\n\nEvidence required.\n"
            ),
        )
        self.write(
            f"{directory}/spec-{iteration}.md",
            (
                "<!-- managed-by: harness-lite v1 -->\n"
                f"# SPEC-{iteration}: Fixture\n\n"
                "- 状态：`实施中`\n"
                f"- 批准依据：用户已批准 SPEC-{iteration} 基线（AUTH-SPEC-{iteration}）\n"
                f"- 实施授权：用户已授权开始实施（AUTH-IMPLEMENT-{iteration}）\n"
            ),
        )
        self.write(
            f"{directory}/deviation-{iteration}.md",
            f"# Deviation {iteration}\n\n当前开放偏差：`0`。\n",
        )

    def reserve_workspace(self, iteration: str) -> None:
        operation = workspace.new_operation_id()
        metadata = {
            "schema_version": "harness-lite.allocation-metadata.v1",
            "operation_id": operation,
            "plan_digest": hashlib.sha256(f"allocation-{iteration}".encode()).hexdigest(),
            "iteration": iteration,
            "base_commit": self.base,
            "base_branch": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.base,
            "governance_tree": self.tree,
            "principle_sha256": hashlib.sha256(
                (self.root / "harness" / "principle.md").read_bytes()
            ).hexdigest(),
            "title": f"Train fixture {iteration}",
        }
        raw = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        blob = self.git("hash-object", "-w", "--stdin", input_text=raw).stdout.strip()
        self.git("update-ref", f"refs/project-harness/v2/allocations/{iteration}", blob)
        self.git("update-ref", f"refs/project-harness/v2/iterations/{iteration}/base", self.base)

    def activate_workspace(
        self,
        iteration: str,
        topology: str,
        branch_ref: str,
        worktree_path: Path,
        owner: str,
        dependency_bindings=(),
    ) -> None:
        operation = workspace.new_operation_id()
        plan = workspace.build_activation_plan(
            self.root,
            iteration=iteration,
            execution_topology=topology,
            base_ref=f"refs/project-harness/v2/iterations/{iteration}/base",
            branch_ref=branch_ref,
            worktree_path=worktree_path,
            owner=owner,
            lease_generation=1,
            dependency_bindings=dependency_bindings,
            operation_id=operation,
        )
        self.assertEqual(plan.blockers, ())
        result = workspace.apply_activation(
            self.root,
            iteration=iteration,
            execution_topology=topology,
            base_ref=f"refs/project-harness/v2/iterations/{iteration}/base",
            branch_ref=branch_ref,
            worktree_path=worktree_path,
            owner=owner,
            lease_generation=1,
            dependency_bindings=dependency_bindings,
            operation_id=operation,
            accepted_plan_digest=plan.digest,
        )
        self.assertEqual(result["blocking_reasons"], [])
        self.workspace_guards[iteration] = {
            "owner": owner,
            "generation": 1,
            "operation_id": operation,
            "plan_digest": plan.digest,
        }

    def dependency_binding(self, candidate) -> dict[str, str]:
        context = workspace.resolve_repository(self.root)
        registry = workspace.dependency_registry_snapshot(context, candidate.iteration)
        return {
            "schema_version": workspace.DEPENDENCY_BINDING_SCHEMA,
            "iteration": candidate.iteration,
            "generation": candidate.generation,
            "candidate_ref": candidate.candidate_ref,
            "candidate_commit": candidate.candidate_commit,
            "candidate_tree": candidate.candidate_tree,
            "candidate_evidence_ref": candidate.candidate_evidence_ref,
            "candidate_evidence_blob": candidate.candidate_evidence_blob,
            "candidate_evidence_digest": candidate.candidate_evidence.evidence_digest,
            "candidate_evidence_metadata_digest": candidate.candidate_evidence_metadata_digest,
            "registration_digest": candidate.registration_digest,
            "registry_digest": str(registry["digest"]),
        }

    def acceptance(self, iteration: str) -> tuple[AcceptanceEvidence, ...]:
        return (
            AcceptanceEvidence(
                acceptance_id=f"AC-{iteration}-01",
                evidence_ids=(f"evidence:{iteration}:feature",),
                verification_ids=(f"test:{iteration}:feature",),
            ),
        )

    def register_plan(
        self,
        iteration: str,
        feature_ref: str,
        worktree: Path,
        *,
        generation: str = "g1",
    ):
        guard = self.workspace_guards[iteration]
        return plan_register_candidate(
            self.root,
            iteration=iteration,
            generation=generation,
            feature_ref=feature_ref,
            feature_worktree=worktree,
            workspace_owner=guard["owner"],
            workspace_generation=guard["generation"],
            workspace_operation_id=guard["operation_id"],
            accepted_workspace_plan_digest=guard["plan_digest"],
            acceptance_evidence=self.acceptance(iteration),
            verify_commands=(
                VerifyCommand(
                    evidence_id=f"test:{iteration}:feature",
                    argv=(sys.executable, "-c", "import sys; sys.exit(0)"),
                ),
            ),
        )

    def register(
        self,
        iteration: str,
        feature_ref: str,
        worktree: Path,
        *,
        generation: str = "g1",
        failpoint=None,
    ):
        plan = self.register_plan(iteration, feature_ref, worktree, generation=generation)
        self.assertEqual(plan.blockers, ())
        seal = prepare_candidate_registration(
            plan,
            accepted_plan_digest=plan.plan_digest,
        )
        self.assertEqual(seal.blockers, ())
        return apply_register_candidate(
            seal,
            accepted_seal_plan_digest=seal.seal_plan_digest,
            confirmation_token=self.token(
                "create-candidate-seal",
                seal.seal_plan_digest,
                f"CANDIDATE-{iteration}-{generation}",
            ),
            failpoint=failpoint,
        )

    def token(self, action: str, subject_digest: str, suffix: str) -> ConfirmationToken:
        authorization_id = f"AUTH-{suffix}"
        return ConfirmationToken(
            schema_version="harness-lite.confirm-token/v1",
            action=action,
            subject_digest=subject_digest,
            authorization_id=authorization_id,
            token_digest=confirmation_token_digest(action, subject_digest, authorization_id),
        )

    def change_main_principle(self) -> str:
        path = self.root / "harness" / "principle.md"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n## P-TRAIN\n\nApproved train-wide constraint.\n",
            encoding="utf-8",
        )
        self.git("add", "--", "harness/principle.md")
        self.git("commit", "--no-gpg-sign", "-m", "change main principle")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def apply_principle_audit(
        self,
        iteration: str,
        disposition: str,
        operation_character: str,
    ):
        decision = principle_audit.PrincipleAuditDecision.create(
            iteration=iteration,
            authority_ref=f"refs/heads/feature/{iteration}",
            disposition=disposition,
            affected_ids=(f"AC-{iteration}-01",),
            evidence_ids=(f"EVIDENCE-{iteration}-{disposition}",),
            authorization_ids=(f"AUTH-{iteration}-{disposition}",),
        )
        plan = principle_audit.plan_principle_impact_audit(
            self.root,
            decision=decision,
            operation_id="OP-" + operation_character * 32,
        )
        self.assertTrue(plan.ready, plan.as_dict())
        return principle_audit.apply_principle_impact_audit(
            plan,
            accept_plan_digest=plan.plan_digest,
        )

    def commit_reapproved_authority(
        self,
        iteration: str,
        current_principle_sha256: str,
    ) -> None:
        directory = self.feature_a / "harness" / "iterations" / iteration
        prd_path = directory / f"prd-{iteration}.md"
        spec_path = directory / f"spec-{iteration}.md"
        prd = prd_path.read_text(encoding="utf-8")
        spec = spec_path.read_text(encoding="utf-8")
        prd = re.sub(
            r"- principle_base_hash：`[0-9a-f]{64}`",
            f"- principle_base_hash：`{current_principle_sha256}`",
            prd,
        ).replace(
            f"AUTH-PRD-{iteration}",
            f"AUTH-PRD-{iteration}-REAPPROVED",
        )
        spec = spec.replace(
            f"AUTH-SPEC-{iteration}",
            f"AUTH-SPEC-{iteration}-REAPPROVED",
        )
        prd_path.write_text(prd, encoding="utf-8")
        spec_path.write_text(spec, encoding="utf-8")
        self.git(
            "add",
            "--",
            f"harness/iterations/{iteration}/prd-{iteration}.md",
            f"harness/iterations/{iteration}/spec-{iteration}.md",
            cwd=self.feature_a,
        )
        self.git(
            "commit",
            "--no-gpg-sign",
            "-m",
            f"reapprove PRD-{iteration} for current principle",
            cwd=self.feature_a,
        )

    def applied_governance(self, context):
        from harness_train import open_repository

        repo = open_repository(context.project_root)
        result = subprocess.run(
            [repo.git, "-C", context.integration_worktree, "write-tree"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="ascii",
            env=self.env,
        )
        return build_governance_receipt(
            context,
            mode="applied",
            result_tree=result.stdout.strip(),
            evidence_ids=(f"governance:{context.operation_id}",),
        )

    def preview_governance(self, context):
        receipt = self.applied_governance(context)
        return build_governance_receipt(
            context,
            mode="preview",
            result_tree=receipt.result_tree,
            evidence_ids=receipt.evidence_ids,
        )

    def prepare_plan(self, candidates, *, generation="i1", strategy="merge-no-ff", declaration=None):
        return plan_prepare_integration(
            self.root,
            generation=generation,
            candidates=tuple(candidates),
            verify_commands=(
                VerifyCommand(
                    evidence_id=f"test:integration:{generation}",
                    argv=(sys.executable, "-c", "import sys; sys.exit(0)"),
                ),
            ),
            merge_strategy=strategy,
            strategy_declaration_digest=declaration,
        )

    def prepare(self, candidates, *, generation="i1", governance=None, strategy="merge-no-ff", declaration=None):
        plan = self.prepare_plan(
            candidates,
            generation=generation,
            strategy=strategy,
            declaration=declaration,
        )
        self.assertEqual(plan.blockers, ())
        notifications = []
        result = apply_prepare_integration(
            plan,
            accepted_plan_digest=plan.plan_digest,
            confirmation_token=self.token("prepare-integration", plan.plan_digest, f"PREPARE-{generation}"),
            notify=notifications.append,
            governance_callback=governance or self.applied_governance,
            governance_conflict_normalizer=self.progress_conflict_normalizer(plan),
        )
        return plan, result, notifications

    def progress_conflict_normalizer(self, train_plan):
        def normalize(observed_plan):
            self.assertEqual(observed_plan.plan_digest, train_plan.plan_digest)
            result = self.git(
                "show",
                f"{train_plan.target_main}:harness/progress.md",
            ).stdout.encode("utf-8")
            for candidate in train_plan.candidates:
                base = self.git(
                    "show",
                    f"{candidate.base_commit}:harness/progress.md",
                ).stdout.encode("utf-8")
                branch = self.git(
                    "show",
                    f"{candidate.candidate_commit}:harness/progress.md",
                ).stdout.encode("utf-8")
                union = harness_governance.plan_progress_union(
                    branch_base=base,
                    latest_main=result,
                    branch_candidate=branch,
                )
                self.assertTrue(union.ready, union.blockers)
                assert union.preview is not None
                result = union.preview
            target = Path(train_plan.worktree_path) / "harness" / "progress.md"
            target.write_bytes(result)
            self.git("add", "--", "harness/progress.md", cwd=Path(train_plan.worktree_path))
            tree = self.git(
                "write-tree",
                cwd=Path(train_plan.worktree_path),
            ).stdout.strip()
            return SimpleNamespace(
                result_tree=tree,
                plan_digest=hashlib.sha256(result).hexdigest(),
                journal_path="test-progress-normalizer",
                journal_sha256=hashlib.sha256(result).hexdigest(),
            )

        return normalize

    def commit(self, preparation):
        self.assertTrue(preparation.ready_for_commit)
        plan = preparation.commit_plan
        assert plan is not None
        return apply_integration_commit(
            plan,
            accepted_commit_plan_digest=plan.commit_plan_digest,
            confirmation_token=self.token(
                "create-integration-commit",
                plan.commit_plan_digest,
                f"COMMIT-{plan.generation}",
            ),
        )

    def register_integration(self, committed):
        plan = integrated_registry.plan_register_integrated_evidence(
            committed,
            commit_confirmation_token=committed.commit_confirmation_token,
        )
        self.assertEqual(plan.blockers, ())
        return integrated_registry.apply_register_integrated_evidence(
            plan,
            accepted_plan_digest=plan.plan_digest,
            commit_confirmation_token=committed.commit_confirmation_token,
        )

    def detach_primary(self) -> None:
        """Simulate an unsafe manual release; the workspace lease intentionally stays stale."""

        self.git("switch", "--detach", self.base)

    def bind_primary_for_main_advance(self) -> None:
        """Release main through the exact governed in-place Local binding workflow."""

        guard = self.workspace_guards["999"]
        operation = workspace.new_operation_id()
        new_branch = "refs/heads/prd/999-local"
        plan = workspace.build_bind_local_branch_plan(
            self.root,
            iteration="999",
            owner=guard["owner"],
            lease_generation=guard["generation"],
            worktree_path=self.root,
            base_commit=self.base,
            new_branch_ref=new_branch,
            operation_id=operation,
        )
        self.assertEqual(plan.blockers, ())
        result = workspace.apply_bind_local_branch(
            self.root,
            iteration="999",
            owner=guard["owner"],
            lease_generation=guard["generation"],
            worktree_path=self.root,
            base_commit=self.base,
            new_branch_ref=new_branch,
            operation_id=operation,
            accepted_plan_digest=plan.digest,
        )
        self.assertEqual(result["blocking_reasons"], [])
        self.workspace_guards["999"] = {
            "owner": guard["owner"],
            "generation": guard["generation"] + 1,
            "operation_id": operation,
            "plan_digest": plan.digest,
        }

    def snapshot_primary(self) -> tuple[str, str, bytes]:
        return (
            self.oid("HEAD"),
            self.git("status", "--porcelain=v1", "--untracked-files=all").stdout,
            (self.root / "app.txt").read_bytes(),
        )

    def test_register_candidate_creates_confirmed_seal_and_atomic_evidence_refs(self) -> None:
        before = self.git("rev-list", "--all", "--count").stdout.strip()

        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)

        self.assertEqual(self.oid(candidate.candidate_ref), candidate.candidate_commit)
        self.assertEqual(self.oid(f"{candidate.candidate_commit}^"), self.a_commit)
        self.assertNotEqual(candidate.candidate_tree, self.oid(f"{self.a_commit}^{{tree}}"))
        self.assertEqual(self.oid(candidate.candidate_evidence_ref), candidate.candidate_evidence_blob)
        self.assertEqual(int(self.git("rev-list", "--all", "--count").stdout.strip()), int(before) + 1)
        self.assertTrue(candidate.verification_receipts)
        self.assertTrue(all(item.exit_code == 0 for item in candidate.verification_receipts))
        event = self.git("show", f"{candidate.candidate_commit}:harness/progress.md").stdout
        self.assertIn(candidate.candidate_ref, event)
        self.assertIn(candidate.candidate_evidence_ref, event)
        self.assertNotIn(candidate.candidate_commit, event)
        loaded, blockers = load_registered_candidate(
            self.root,
            iteration="001",
            generation="g1",
            current_principle_sha256=candidate.principle_sha256,
        )
        self.assertEqual(blockers, ())
        assert loaded is not None
        self.assertEqual(loaded.registration_digest, candidate.registration_digest)
        self.assertFalse(candidate.pushed)

    def test_candidate_principle_binding_records_exact_no_drift_identity(self) -> None:
        candidate = self.register(
            "001",
            "refs/heads/feature/001",
            self.feature_a,
            generation="g-principle-no-drift",
        )

        binding = candidate.principle_gate_binding
        self.assertEqual(binding.mode, "no-drift")
        self.assertFalse(binding.drift)
        self.assertEqual(binding.iteration, "001")
        self.assertEqual(binding.authority_ref, "refs/heads/feature/001")
        self.assertEqual(
            binding.allocation_principle_sha256,
            candidate.principle_sha256,
        )
        self.assertEqual(binding.current_principle_sha256, candidate.principle_sha256)
        self.assertIsNone(binding.audit_generation)
        self.assertIsNone(binding.audit_receipt_digest)
        self.assertIn(
            f"principle-gate:{binding.binding_digest}",
            candidate.candidate_evidence.verification_ids,
        )
        metadata = json.loads(
            self.git("cat-file", "blob", candidate.candidate_evidence_blob).stdout
        )
        journal = json.loads(Path(candidate.journal_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["principle_gate_binding"], binding.as_dict())
        self.assertEqual(journal["principle_gate_binding"], binding.as_dict())
        self.assertEqual(
            registered_candidate_gate(
                self.root,
                candidate,
                current_principle_sha256=candidate.principle_sha256,
            ),
            (),
        )

        # A pre-binding public envelope must not be silently grandfathered.
        metadata.pop("principle_gate_binding")
        metadata["metadata_digest"] = train.digest(
            train._candidate_metadata_payload(metadata)
        )
        repo = train.open_repository(self.root)
        legacy_blob = train._git(
            repo,
            ["hash-object", "-w", "--stdin"],
            input_bytes=train.canonical_json(metadata) + b"\n",
        ).stdout.decode("ascii").strip()
        self.git(
            "update-ref",
            candidate.candidate_evidence_ref,
            legacy_blob,
            candidate.candidate_evidence_blob,
        )
        with self.assertRaisesRegex(TrainError, "principle gate binding is missing"):
            load_registered_candidate(
                self.root,
                iteration="001",
                generation="g-principle-no-drift",
                current_principle_sha256=candidate.principle_sha256,
            )

    def test_candidate_principle_binding_records_exact_no_impact_receipt(self) -> None:
        current_principle = self.change_main_principle()
        audit_result = self.apply_principle_audit(
            "002",
            principle_audit.DISPOSITION_NO_IMPACT,
            "d",
        )

        candidate = self.register(
            "002",
            "refs/heads/feature/002",
            self.feature_b,
            generation="g-principle-no-impact",
        )
        binding = candidate.principle_gate_binding
        self.assertEqual(binding.mode, "audit-receipt")
        self.assertTrue(binding.drift)
        self.assertEqual(binding.current_principle_sha256, current_principle)
        self.assertEqual(binding.disposition, principle_audit.DISPOSITION_NO_IMPACT)
        self.assertEqual(binding.audit_generation, audit_result.receipt.generation)
        self.assertEqual(
            binding.audit_receipt_digest,
            audit_result.receipt.receipt_digest,
        )
        self.assertEqual(binding.audit_supersedes, audit_result.receipt.supersedes)
        self.assertEqual(binding.audit_operation_id, audit_result.receipt.operation_id)
        self.assertEqual(binding.audit_plan_digest, audit_result.receipt.plan_digest)
        self.assertEqual(
            registered_candidate_gate(
                self.root,
                candidate,
                current_principle_sha256=current_principle,
            ),
            (),
        )

    def test_new_principle_audit_generation_stales_old_candidate(self) -> None:
        candidate = self.register(
            "001",
            "refs/heads/feature/001",
            self.feature_a,
            generation="g-before-audit-generation",
        )
        self.assertEqual(candidate.principle_gate_binding.mode, "no-drift")
        current_principle = self.change_main_principle()
        audit_result = self.apply_principle_audit(
            "001",
            principle_audit.DISPOSITION_NO_IMPACT,
            "e",
        )
        self.assertEqual(audit_result.receipt.generation, 1)

        blockers = registered_candidate_gate(
            self.root,
            candidate,
            current_principle_sha256=current_principle,
        )
        codes = {item.code for item in blockers}
        self.assertIn("registered-candidate-principle-binding-stale", codes)
        loaded, load_blockers = load_registered_candidate(
            self.root,
            iteration="001",
            generation="g-before-audit-generation",
            current_principle_sha256=current_principle,
        )
        self.assertIsNone(loaded)
        self.assertIn(
            "registered-candidate-principle-binding-stale",
            {item.code for item in load_blockers},
        )

    def test_candidate_principle_receipt_tamper_and_operational_loss_fail_closed(self) -> None:
        current_principle = self.change_main_principle()
        audit_result = self.apply_principle_audit(
            "002",
            principle_audit.DISPOSITION_NO_IMPACT,
            "f",
        )
        candidate = self.register(
            "002",
            "refs/heads/feature/002",
            self.feature_b,
            generation="g-principle-receipt-loss",
        )
        receipt_path = Path(audit_result.receipt_path)
        receipt_bytes = receipt_path.read_bytes()
        tampered = json.loads(receipt_bytes.decode("utf-8"))
        tampered["next_gate"] = "tampered-next-gate"
        receipt_path.write_text(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

        tamper_codes = {
            item.code
            for item in registered_candidate_gate(
                self.root,
                candidate,
                current_principle_sha256=current_principle,
            )
        }
        self.assertIn("principle-audit-chain-invalid", tamper_codes)

        receipt_path.write_bytes(receipt_bytes)
        self.assertEqual(
            registered_candidate_gate(
                self.root,
                candidate,
                current_principle_sha256=current_principle,
            ),
            (),
        )
        Path(audit_result.journal_path).unlink()
        loss_codes = {
            item.code
            for item in registered_candidate_gate(
                self.root,
                candidate,
                current_principle_sha256=current_principle,
            )
        }
        self.assertIn("principle-audit-chain-invalid", loss_codes)

    def test_principle_audit_gates_candidate_and_integration_generations(self) -> None:
        old_candidate = self.register(
            "001",
            "refs/heads/feature/001",
            self.feature_a,
            generation="g-before-principle",
        )
        current_principle = self.change_main_principle()

        missing = self.register_plan(
            "002",
            "refs/heads/feature/002",
            self.feature_b,
            generation="g-no-audit",
        )
        self.assertIn(
            "principle-impact-audit-required",
            {item.code for item in missing.blockers},
        )
        self.apply_principle_audit(
            "002",
            principle_audit.DISPOSITION_NO_IMPACT,
            "a",
        )
        no_impact = self.register_plan(
            "002",
            "refs/heads/feature/002",
            self.feature_b,
            generation="g-no-impact",
        )
        self.assertNotIn(
            "principle-impact-audit-required",
            {item.code for item in no_impact.blockers},
        )

        self.apply_principle_audit(
            "001",
            principle_audit.DISPOSITION_IMPACT,
            "b",
        )
        impacted = self.prepare_plan((old_candidate,), generation="i-impact")
        impacted_codes = {item.code for item in impacted.blockers}
        self.assertIn("principle-reapproval-required", impacted_codes)
        self.assertIn("registered-candidate-core", impacted_codes)

        self.commit_reapproved_authority("001", current_principle)
        reapproved = self.apply_principle_audit(
            "001",
            principle_audit.DISPOSITION_REAPPROVED,
            "c",
        )
        self.assertEqual(reapproved.receipt.generation, 2)

        stale = self.prepare_plan((old_candidate,), generation="i-stale")
        stale_codes = {item.code for item in stale.blockers}
        self.assertIn("registered-candidate-core", stale_codes)
        self.assertNotIn("principle-reapproval-required", stale_codes)
        self.assertNotIn("principle-impact-audit-required", stale_codes)

        new_candidate = self.register_plan(
            "001",
            "refs/heads/feature/001",
            self.feature_a,
            generation="g-after-reapproval",
        )
        self.assertNotIn(
            "registered-candidate-core",
            {item.code for item in new_candidate.blockers},
        )
        self.assertFalse(
            {item.code for item in new_candidate.blockers}
            & {
                "principle-reapproval-required",
                "principle-impact-audit-required",
                "principle-audit-authority-drift",
            }
        )

    def test_train_mutation_rejects_linked_root_even_with_valid_writer_guard(self) -> None:
        guard = self.workspace_guards["001"]

        with self.assertRaisesRegex(TrainError, "canonical primary coordinator root"):
            plan_register_candidate(
                self.feature_a,
                iteration="001",
                generation="g1",
                feature_ref="refs/heads/feature/001",
                feature_worktree=self.feature_a,
                workspace_owner=guard["owner"],
                workspace_generation=guard["generation"],
                workspace_operation_id=guard["operation_id"],
                accepted_workspace_plan_digest=guard["plan_digest"],
                acceptance_evidence=self.acceptance("001"),
                verification_ids=("test:001:candidate",),
            )

    def test_bare_verification_ids_and_direct_registration_fail_closed(self) -> None:
        guard = self.workspace_guards["001"]
        with self.assertRaisesRegex(TrainError, "canonical string"):
            plan_register_candidate(
                self.root,
                iteration="001",
                generation=1,  # type: ignore[arg-type]
                feature_ref="refs/heads/feature/001",
                feature_worktree=self.feature_a,
                workspace_owner=guard["owner"],
                workspace_generation=guard["generation"],
                workspace_operation_id=guard["operation_id"],
                accepted_workspace_plan_digest=guard["plan_digest"],
                acceptance_evidence=self.acceptance("001"),
                verify_commands=(VerifyCommand("test:001:feature", (sys.executable, "-c", "pass")),),
            )
        legacy = plan_register_candidate(
            self.root,
            iteration="001",
            generation="g-legacy",
            feature_ref="refs/heads/feature/001",
            feature_worktree=self.feature_a,
            workspace_owner=guard["owner"],
            workspace_generation=guard["generation"],
            workspace_operation_id=guard["operation_id"],
            accepted_workspace_plan_digest=guard["plan_digest"],
            acceptance_evidence=self.acceptance("001"),
            verification_ids=("test:001:feature",),
        )

        self.assertIn(
            "candidate-bare-verification-ids-forbidden",
            {item.code for item in legacy.blockers},
        )
        with self.assertRaisesRegex(TrainError, "candidate-seal-preparation-required"):
            apply_register_candidate(legacy, accepted_plan_digest=legacy.plan_digest)
        self.assertNotEqual(
            self.git("show-ref", "--verify", "--quiet", legacy.candidate_ref, check=False).returncode,
            0,
        )
        self.assertNotEqual(
            self.git(
                "show-ref",
                "--verify",
                "--quiet",
                legacy.candidate_evidence_ref,
                check=False,
            ).returncode,
            0,
        )

    def test_candidate_seal_requires_exact_confirmation_and_zero_exit_receipts(self) -> None:
        plan = self.register_plan("001", "refs/heads/feature/001", self.feature_a)
        seal = prepare_candidate_registration(plan, accepted_plan_digest=plan.plan_digest)
        with self.assertRaisesRegex(TrainError, "confirmation-token-missing"):
            apply_register_candidate(
                seal,
                accepted_seal_plan_digest=seal.seal_plan_digest,
            )
        with self.assertRaisesRegex(TrainError, "candidate-seal-plan-not-accepted"):
            apply_register_candidate(
                seal,
                accepted_seal_plan_digest="0" * 64,
                confirmation_token=self.token(
                    "create-candidate-seal",
                    seal.seal_plan_digest,
                    "CANDIDATE-STALE-DIGEST",
                ),
            )

        guard = self.workspace_guards["002"]
        failing = plan_register_candidate(
            self.root,
            iteration="002",
            generation="g-fails",
            feature_ref="refs/heads/feature/002",
            feature_worktree=self.feature_b,
            workspace_owner=guard["owner"],
            workspace_generation=guard["generation"],
            workspace_operation_id=guard["operation_id"],
            accepted_workspace_plan_digest=guard["plan_digest"],
            acceptance_evidence=self.acceptance("002"),
            verify_commands=(
                VerifyCommand(
                    evidence_id="test:002:feature",
                    argv=(sys.executable, "-c", "import sys; sys.exit(7)"),
                ),
            ),
        )
        failing_seal = prepare_candidate_registration(
            failing,
            accepted_plan_digest=failing.plan_digest,
        )
        self.assertIn(
            "candidate-verification-nonzero",
            {item.code for item in failing_seal.blockers},
        )
        with self.assertRaisesRegex(TrainError, "candidate-verification-nonzero"):
            apply_register_candidate(
                failing_seal,
                accepted_seal_plan_digest=failing_seal.seal_plan_digest,
                confirmation_token=self.token(
                    "create-candidate-seal",
                    failing_seal.seal_plan_digest,
                    "CANDIDATE-FAILED-VERIFY",
                ),
            )

    def test_register_rejects_dirty_worktree_stale_main_and_digest(self) -> None:
        (self.feature_a / "untracked.tmp").write_text("dirty", encoding="utf-8")
        dirty = self.register_plan("001", "refs/heads/feature/001", self.feature_a)
        self.assertIn("feature-worktree-dirty", {item.code for item in dirty.blockers})
        (self.feature_a / "untracked.tmp").unlink()
        planned = self.register_plan("001", "refs/heads/feature/001", self.feature_a)
        self.write("main-advance.txt", "drift\n")
        self.git("add", "--", "main-advance.txt")
        self.git("commit", "--no-gpg-sign", "-m", "main drift")
        with self.assertRaisesRegex(TrainError, "candidate-main-drift"):
            prepare_candidate_registration(planned, accepted_plan_digest=planned.plan_digest)
        fresh = self.register_plan("001", "refs/heads/feature/001", self.feature_a)
        with self.assertRaisesRegex(TrainError, "plan-not-accepted"):
            prepare_candidate_registration(fresh, accepted_plan_digest="0" * 64)

    def test_register_crash_after_ref_is_idempotently_reconciled(self) -> None:
        plan = self.register_plan("001", "refs/heads/feature/001", self.feature_a)
        seal = prepare_candidate_registration(plan, accepted_plan_digest=plan.plan_digest)
        token = self.token("create-candidate-seal", seal.seal_plan_digest, "CANDIDATE-CRASH")

        def crash(stage: str) -> None:
            if stage == "candidate-after-refs":
                raise InjectedCrash(stage)

        with self.assertRaises(InjectedCrash):
            apply_register_candidate(
                seal,
                accepted_seal_plan_digest=seal.seal_plan_digest,
                confirmation_token=token,
                failpoint=crash,
            )
        self.assertEqual(self.oid(plan.candidate_ref), seal.seal_commit)
        evidence_before = self.oid(plan.candidate_evidence_ref)

        recovered = apply_register_candidate(
            seal,
            accepted_seal_plan_digest=seal.seal_plan_digest,
            confirmation_token=token,
        )

        self.assertTrue(recovered.idempotent)
        self.assertEqual(self.oid(plan.candidate_evidence_ref), evidence_before)

    def test_prepare_no_ff_dependency_order_notifications_and_primary_unchanged(self) -> None:
        a = self.register("001", "refs/heads/feature/001", self.feature_a)
        b = self.register("002", "refs/heads/feature/002", self.feature_b)
        before = self.snapshot_primary()

        _plan, prepared, notifications = self.prepare((a, b))

        self.assertTrue(prepared.ready_for_commit, prepared)
        commit_plan = prepared.commit_plan
        assert commit_plan is not None
        self.assertEqual(commit_plan.dependency_order, ("001", "002"))
        self.assertEqual(commit_plan.parent_commits, (self.base, a.candidate_commit, b.candidate_commit))
        self.assertEqual([item.phase for item in notifications], ["before", "after"])
        for envelope in notifications:
            self.assertEqual(envelope.schema_version, "harness-lite.interaction/v1")
            self.assertEqual(envelope.action, "create-worktree")
            self.assertEqual(envelope.action_level, "notify")
            self.assertEqual(envelope.facts["project_root"], str(self.root))
            self.assertEqual(envelope.facts["worktree_path"], prepared.worktree_path)
            self.assertEqual(envelope.facts["base_commit"], self.base)
            self.assertFalse(envelope.facts["pushed"])
            self.assertFalse(envelope.facts["force"])
        self.assertEqual(self.oid("HEAD", cwd=Path(prepared.worktree_path)), self.base)
        self.assertEqual(self.snapshot_primary(), before)
        self.assertFalse(prepared.pushed)

    def test_exact_stacked_dependency_bindings_gate_train_and_ancestry(self) -> None:
        dependency_g1 = self.register("001", "refs/heads/feature/001", self.feature_a)
        binding_g1 = self.dependency_binding(dependency_g1)
        self.activate_workspace(
            "003",
            "worktree",
            "refs/heads/feature/003",
            self.feature_c,
            "task-c",
            dependency_bindings=(binding_g1,),
        )
        (self.feature_c / "c.txt").write_text("feature C on B-g1\n", encoding="utf-8")
        self.git("add", "--", "c.txt", cwd=self.feature_c)
        self.git("commit", "--no-gpg-sign", "-m", "feature C on B-g1", cwd=self.feature_c)
        consumer_g1 = self.register("003", "refs/heads/feature/003", self.feature_c)

        self.assertEqual(
            tuple(item.as_dict() for item in consumer_g1.dependency_bindings),
            (binding_g1,),
        )
        self.assertEqual(
            consumer_g1.dependency_bindings_digest,
            workspace.dependency_bindings_digest((binding_g1,)),
        )
        self.assertIn(
            f"dependency-bindings:{consumer_g1.dependency_bindings_digest}",
            consumer_g1.candidate_evidence.verification_ids,
        )
        positive = self.prepare_plan((dependency_g1, consumer_g1), generation="stacked-positive")
        self.assertEqual(positive.blockers, ())

        original_binding = consumer_g1.dependency_bindings[0]
        tampered_binding = replace(original_binding, registry_digest="f" * 64)
        tampered = replace(
            consumer_g1,
            dependency_bindings=(tampered_binding,),
            dependency_bindings_digest=workspace.dependency_bindings_digest(
                (tampered_binding.as_dict(),)
            ),
            registration_digest="0" * 64,
        )
        tampered = replace(
            tampered,
            registration_digest=train.registered_candidate_digest(tampered),
        )
        tampered_plan = self.prepare_plan(
            (dependency_g1, tampered),
            generation="stacked-tamper",
        )
        tampered_codes = {item.code for item in tampered_plan.blockers}
        self.assertTrue(
            {
                "registered-candidate-evidence-dependency-bindings",
                "registered-candidate-dependency-evidence",
                "dependency-baseline-stale",
            }
            & tampered_codes,
            tampered_plan.as_dict(),
        )

        unrelated = replace(
            consumer_g1,
            candidate_commit=self.b_commit,
            candidate_tree=self.oid(f"{self.b_commit}^{{tree}}"),
            registration_digest="0" * 64,
        )
        unrelated = replace(
            unrelated,
            registration_digest=train.registered_candidate_digest(unrelated),
        )
        with mock.patch.object(train, "_registered_candidate_gate", return_value=()):
            non_descendant = self.prepare_plan(
                (dependency_g1, unrelated),
                generation="stacked-non-descendant",
            )
        self.assertIn(
            "integration-dependency-not-ancestor",
            {item.code for item in non_descendant.blockers},
        )

        (self.feature_a / "a-v2.txt").write_text("feature A generation 2\n", encoding="utf-8")
        self.git("add", "--", "a-v2.txt", cwd=self.feature_a)
        self.git("commit", "--no-gpg-sign", "-m", "feature A generation 2", cwd=self.feature_a)
        dependency_g2 = self.register(
            "001",
            "refs/heads/feature/001",
            self.feature_a,
            generation="g2",
        )
        stale = self.prepare_plan(
            (dependency_g2, consumer_g1),
            generation="stacked-stale",
        )
        stale_codes = {item.code for item in stale.blockers}
        self.assertIn("integration-dependency-binding-mismatch", stale_codes)
        self.assertTrue(
            {"dependency-baseline-stale", "integration-dependency-not-ancestor"}
            & stale_codes,
            stale.as_dict(),
        )

    def test_integration_path_blocks_both_ancestor_and_descendant_overlap(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)

        ancestor = plan_prepare_integration(
            self.root,
            generation="i-ancestor",
            candidates=(candidate,),
            verify_commands=(VerifyCommand("test:ancestor", (sys.executable, "-c", "pass")),),
            worktree_path=self.sandbox,
        )
        descendant = plan_prepare_integration(
            self.root,
            generation="i-descendant",
            candidates=(candidate,),
            verify_commands=(VerifyCommand("test:descendant", (sys.executable, "-c", "pass")),),
            worktree_path=self.feature_a / "nested integration",
        )

        self.assertIn("integration-worktree-overlap", {item.code for item in ancestor.blockers})
        self.assertIn("integration-worktree-overlap", {item.code for item in descendant.blockers})

    def test_apply_rechecks_path_overlap_before_lease_or_operation_journal(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        plan = self.prepare_plan((candidate,))
        nested = Path(plan.worktree_path) / "new linked child"
        self.git("worktree", "add", "--detach", str(nested), self.base)

        with self.assertRaisesRegex(TrainError, "integration-worktree-overlap"):
            apply_prepare_integration(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation_token=self.token("prepare-integration", plan.plan_digest, "OVERLAP"),
                notify=lambda _item: None,
                governance_callback=self.applied_governance,
            )

        journal = Path(plan.git_common_dir) / "project-harness" / "train" / "v1" / "journal" / f"integration-{plan.operation_id}.json"
        self.assertFalse(journal.exists())

    def test_notify_window_race_blocks_before_nested_worktree_registration(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        racing_parent = self.sandbox / "racing parent worktree"
        nested = racing_parent / "nested integration"
        plan = plan_prepare_integration(
            self.root,
            generation="i-notify-race",
            candidates=(candidate,),
            verify_commands=(VerifyCommand("test:notify-race", (sys.executable, "-c", "pass")),),
            worktree_path=nested,
        )
        # It is safe at plan time. The callback simulates a non-cooperating
        # direct Git writer claiming the parent after the before notification.
        self.assertEqual(plan.blockers, ())
        notifications = []

        def racing_notify(envelope) -> None:
            notifications.append(envelope)
            if envelope.phase == "before":
                self.git("worktree", "add", "--detach", str(racing_parent), self.base)

        result = apply_prepare_integration(
            plan,
            accepted_plan_digest=plan.plan_digest,
            confirmation_token=self.token("prepare-integration", plan.plan_digest, "NOTIFY-RACE"),
            notify=racing_notify,
            governance_callback=self.applied_governance,
        )

        self.assertFalse(result.ready_for_commit)
        self.assertIn("integration-worktree-overlap", {item.code for item in result.blockers})
        registered = self.git("worktree", "list", "--porcelain").stdout
        self.assertNotIn(str(nested), registered)
        self.assertEqual(
            self.git("status", "--porcelain=v1", "--untracked-files=all", cwd=racing_parent).stdout,
            "",
        )
        self.assertEqual([item.phase for item in notifications], ["before"])

    def test_resume_rejects_extra_merge_parent_and_staged_tree_drift(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        plan = self.prepare_plan((candidate,))
        token = self.token("prepare-integration", plan.plan_digest, "EXACT-RESUME")

        def crash(stage: str) -> None:
            if stage == "integration-after-merge":
                raise InjectedCrash(stage)

        with self.assertRaises(InjectedCrash):
            apply_prepare_integration(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation_token=token,
                notify=lambda _item: None,
                governance_callback=self.applied_governance,
                failpoint=crash,
            )
        worktree = Path(plan.worktree_path)
        raw_merge_path = Path(self.git("rev-parse", "--git-path", "MERGE_HEAD", cwd=worktree).stdout.strip())
        merge_path = raw_merge_path if raw_merge_path.is_absolute() else worktree / raw_merge_path
        original = merge_path.read_text(encoding="ascii")
        merge_path.write_text(original + self.base + "\n", encoding="ascii")
        with self.assertRaisesRegex(TrainError, "MERGE_HEAD changed|extra parent"):
            apply_prepare_integration(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation_token=token,
                notify=lambda _item: None,
                governance_callback=self.applied_governance,
            )
        merge_path.write_text(original, encoding="ascii")
        (worktree / "rogue.txt").write_text("rogue staged change\n", encoding="utf-8")
        self.git("add", "--", "rogue.txt", cwd=worktree)
        with self.assertRaisesRegex(TrainError, "staged tree differs"):
            apply_prepare_integration(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation_token=token,
                notify=lambda _item: None,
                governance_callback=self.applied_governance,
            )

    def test_prepare_derives_dependency_order_from_committed_prd_authority(self) -> None:
        prd = self.feature_b / "harness" / "iterations" / "002" / "prd-002.md"
        prd.write_text(
            prd.read_text(encoding="utf-8") + "\n- 依赖 PRD：PRD-001\n",
            encoding="utf-8",
        )
        self.git("add", "--", "harness/iterations/002/prd-002.md", cwd=self.feature_b)
        self.git("commit", "--no-gpg-sign", "-m", "declare B dependency", cwd=self.feature_b)
        a = self.register("001", "refs/heads/feature/001", self.feature_a)
        b = self.register("002", "refs/heads/feature/002", self.feature_b)

        reversed_plan = self.prepare_plan((b, a))
        ordered_plan = self.prepare_plan((a, b), generation="i2")
        tampered = self.prepare_plan((a, replace(b, depends_on=())), generation="i3")

        self.assertIn(
            "integration-dependency-order-invalid",
            {item.code for item in reversed_plan.blockers},
        )
        self.assertEqual(ordered_plan.blockers, ())
        self.assertIn(
            "registered-candidate-digest",
            {item.code for item in tampered.blockers},
        )

    def test_missing_verify_command_and_preview_governance_block(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        missing = plan_prepare_integration(
            self.root,
            generation="i1",
            candidates=(candidate,),
            verify_commands=(),
        )
        self.assertIn(
            "integration-verification-command-missing",
            {item.code for item in missing.blockers},
        )
        _plan, preview, _notifications = self.prepare(
            (candidate,),
            governance=self.preview_governance,
        )
        self.assertFalse(preview.ready_for_commit)
        self.assertIn("governance-apply-not-connected", {item.code for item in preview.blockers})
        self.assertFalse(preview.governance_apply_connected)

    def test_merge_conflict_fails_without_product_repair_or_primary_change(self) -> None:
        a = self.register("001", "refs/heads/feature/001", self.feature_a)
        self.git("switch", "feature/002", cwd=self.feature_b)
        (self.feature_b / "a.txt").write_text("conflicting B\n", encoding="utf-8")
        self.git("add", "--", "a.txt", cwd=self.feature_b)
        self.git("commit", "--no-gpg-sign", "-m", "conflicting B", cwd=self.feature_b)
        b = self.register("002", "refs/heads/feature/002", self.feature_b)
        before = self.snapshot_primary()

        _plan, result, _notifications = self.prepare((a, b))

        self.assertFalse(result.ready_for_commit)
        self.assertIn("integration-merge-conflict", {item.code for item in result.blockers})
        self.assertEqual(self.snapshot_primary(), before)
        conflict_path = Path(result.worktree_path) / "a.txt"
        self.assertEqual(conflict_path.read_text(encoding="utf-8"), "feature A\n")

    def test_integration_commit_requires_separate_confirmation_and_preserves_ancestry(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        _prepare_plan, prepared, _notifications = self.prepare((candidate,))
        plan = prepared.commit_plan
        assert plan is not None
        with self.assertRaisesRegex(TrainError, "confirmation-token"):
            apply_integration_commit(
                plan,
                accepted_commit_plan_digest=plan.commit_plan_digest,
                confirmation_token=self.token("prepare-integration", plan.commit_plan_digest, "WRONG"),
            )

        result = self.commit(prepared)

        self.assertTrue(result.evidence_ready)
        parents = self.git("rev-list", "--parents", "-n", "1", result.integrated_commit).stdout.split()
        self.assertEqual(parents[1:], [self.base, candidate.candidate_commit])
        self.assertEqual(self.oid(f"{result.integrated_commit}^{{tree}}"), result.integrated_tree)
        self.assertTrue(
            self.git(
                "merge-base",
                "--is-ancestor",
                candidate.candidate_commit,
                result.integrated_commit,
                check=False,
            ).returncode
            == 0
        )

    def test_worktree_and_commit_hooks_cannot_create_unplanned_state(self) -> None:
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for name, marker in (
            ("post-checkout", "post-checkout-fired"),
            ("post-commit", "post-commit-fired"),
        ):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\nprintf fired > {marker}\n", encoding="utf-8", newline="\n")
            hook.chmod(0o755)
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)

        _prepare_plan, prepared, _notifications = self.prepare((candidate,))
        worktree = Path(prepared.worktree_path)
        self.assertFalse((worktree / "post-checkout-fired").exists())

        committed = self.commit(prepared)

        self.assertTrue(committed.evidence_ready)
        self.assertFalse((worktree / "post-commit-fired").exists())
        self.assertEqual(
            self.git("status", "--porcelain=v1", "--untracked-files=all", cwd=worktree).stdout,
            "",
        )

    def test_prepare_and_commit_crashes_resume_without_duplicate_objects(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        plan = self.prepare_plan((candidate,))
        notifications = []

        def crash_after_merge(stage: str) -> None:
            if stage == "integration-after-merge":
                raise InjectedCrash(stage)

        prepare_token = self.token("prepare-integration", plan.plan_digest, "PREPARE-CRASH")
        with self.assertRaises(InjectedCrash):
            apply_prepare_integration(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation_token=prepare_token,
                notify=notifications.append,
                governance_callback=self.applied_governance,
                failpoint=crash_after_merge,
            )
        prepared = apply_prepare_integration(
            plan,
            accepted_plan_digest=plan.plan_digest,
            confirmation_token=prepare_token,
            notify=notifications.append,
            governance_callback=self.applied_governance,
        )
        self.assertTrue(prepared.ready_for_commit, prepared)
        commit_plan = prepared.commit_plan
        assert commit_plan is not None
        commit_token = self.token(
            "create-integration-commit",
            commit_plan.commit_plan_digest,
            "COMMIT-CRASH",
        )

        def crash_after_commit(stage: str) -> None:
            if stage == "integration-after-commit":
                raise InjectedCrash(stage)

        with self.assertRaises(InjectedCrash):
            apply_integration_commit(
                commit_plan,
                accepted_commit_plan_digest=commit_plan.commit_plan_digest,
                confirmation_token=commit_token,
                failpoint=crash_after_commit,
            )
        created = self.oid("HEAD", cwd=Path(prepared.worktree_path))
        recovered = apply_integration_commit(
            commit_plan,
            accepted_commit_plan_digest=commit_plan.commit_plan_digest,
            confirmation_token=commit_token,
        )

        self.assertTrue(recovered.idempotent)
        self.assertEqual(recovered.integrated_commit, created)

    def test_squash_identity_change_requires_new_rebind_then_passes(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        declaration = hashlib.sha256(b"approved squash strategy").hexdigest()
        _prepare_plan, prepared, _notifications = self.prepare(
            (candidate,),
            strategy="squash",
            declaration=declaration,
        )

        committed = self.commit(prepared)

        self.assertFalse(committed.evidence_ready)
        self.assertIn("identity-rebind-required", {item.code for item in committed.blockers})
        unobserved = build_identity_rebinding(
            IdentityRebindInput(
                source_candidate_evidence_digest=candidate.candidate_evidence.evidence_digest,
                source_candidate_commit=candidate.candidate_commit,
                source_candidate_tree=candidate.candidate_tree,
                integration_generation=committed.generation,
                target_main=self.base,
                integrated_commit=committed.integrated_commit,
                integrated_tree=committed.integrated_tree,
                principle_sha256=candidate.principle_sha256,
                evidence_ids=("evidence:not-produced-by-adapter",),
                verification_ids=("test:not-run-by-adapter",),
                explicitly_revalidated=True,
            )
        )
        with self.assertRaisesRegex(TrainError, "identity-rebind-verification-unobserved"):
            finalize_integration_evidence(committed, identity_rebindings=(unobserved,))
        rebind = build_identity_rebinding(
            IdentityRebindInput(
                source_candidate_evidence_digest=candidate.candidate_evidence.evidence_digest,
                source_candidate_commit=candidate.candidate_commit,
                source_candidate_tree=candidate.candidate_tree,
                integration_generation=committed.generation,
                target_main=self.base,
                integrated_commit=committed.integrated_commit,
                integrated_tree=committed.integrated_tree,
                principle_sha256=candidate.principle_sha256,
                evidence_ids=(f"train:{committed.commit_plan.commit_plan_digest}",),
                verification_ids=(committed.commit_plan.verification_receipts[0].evidence_id,),
                explicitly_revalidated=True,
            )
        )

        rebound = finalize_integration_evidence(committed, identity_rebindings=(rebind,))

        self.assertTrue(rebound.evidence_ready)
        self.assertEqual(
            rebound.integrated_candidate.identity_rebind_digests,
            (rebind.evidence_digest,),
        )

    def test_main_advance_requires_exact_evidence_independent_token_and_cas(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        _prepare_plan, prepared, _notifications = self.prepare((candidate,))
        committed = self.commit(prepared)
        self.bind_primary_for_main_advance()
        before_file = (self.root / "app.txt").read_bytes()
        registered = self.register_integration(committed)
        advance = plan_main_advance(registered)
        self.assertEqual(advance.blockers, ())
        evidence = committed.integrated_candidate
        assert evidence is not None
        with self.assertRaisesRegex(TrainError, "final-acceptance"):
            apply_main_advance(
                advance,
                accepted_plan_digest=advance.plan_digest,
                accepted_integrated_evidence_digest="0" * 64,
                confirmation_token=self.token("advance-main", advance.plan_digest, "ADVANCE-001"),
            )
        with self.assertRaisesRegex(TrainError, "confirmation-token"):
            apply_main_advance(
                advance,
                accepted_plan_digest=advance.plan_digest,
                accepted_integrated_evidence_digest=advance.integrated_evidence_digest,
                confirmation_token=self.token("prepare-integration", advance.plan_digest, "WRONG-ADVANCE"),
            )

        result = apply_main_advance(
            advance,
            accepted_plan_digest=advance.plan_digest,
            accepted_integrated_evidence_digest=advance.integrated_evidence_digest,
            confirmation_token=self.token("advance-main", advance.plan_digest, "ADVANCE-001"),
        )

        self.assertEqual(self.oid("refs/heads/main"), committed.integrated_commit)
        self.assertEqual(
            self.oid("refs/project-harness/v2/iterations/001/integrated"),
            committed.integrated_commit,
        )
        self.assertEqual(
            self.oid("refs/project-harness/v2/iterations/001/final"),
            committed.integrated_commit,
        )
        self.assertEqual((self.root / "app.txt").read_bytes(), before_file)
        self.assertEqual(result.cleanup_worktree, "pending-explicit-cleanup")
        self.assertFalse(result.pushed)

        cleanup = plan_cleanup_integration(result)
        self.assertEqual(cleanup.blockers, ())
        cleanup_notifications = []
        cleaned = apply_cleanup_integration(
            cleanup,
            accepted_plan_digest=cleanup.plan_digest,
            notify=cleanup_notifications.append,
        )
        self.assertTrue(cleaned.removed)
        self.assertFalse(Path(cleanup.integration_worktree).exists())
        self.assertEqual([item.phase for item in cleanup_notifications], ["before", "after"])
        self.assertTrue(all(item.action == "remove-clean-worktree" for item in cleanup_notifications))

    def test_commit_main_and_cleanup_cards_disclose_exact_local_git_effects(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        _prepare_plan, prepared, notifications = self.prepare((candidate,), generation="ux-cards")
        commit_plan = prepared.commit_plan
        assert commit_plan is not None
        create_after = notifications[-1]
        self.assertEqual(create_after.facts["affected_prds"], ["001"])
        self.assertTrue(create_after.facts["source_preserved"])
        self.assertFalse(create_after.facts["remote_involved"])
        self.assertEqual(create_after.facts["actual_head"], self.base)

        commit_before = integration_commit_interaction(commit_plan, "before")
        self.assertEqual(commit_before.action, "commit")
        self.assertEqual(commit_before.action_level, "confirm")
        self.assertIn("a.txt", commit_before.facts["paths"])
        self.assertTrue(commit_before.facts["verification_ids"])
        self.assertFalse(commit_before.facts["pushed"])
        committed = self.commit(prepared)
        commit_after = integration_commit_interaction(
            commit_plan,
            "after",
            resulting_commit=committed.integrated_commit,
        )
        self.assertEqual(commit_after.facts["resulting_commit"], committed.integrated_commit)
        self.assertFalse(commit_after.facts["remote_involved"])

        self.bind_primary_for_main_advance()
        registered = self.register_integration(committed)
        advance = plan_main_advance(registered)
        advance_before = main_advance_interaction(advance, "before")
        self.assertEqual(advance_before.action, "main-advance")
        self.assertEqual(advance_before.action_level, "confirm")
        self.assertEqual(advance_before.facts["affected_prds"], ["001"])
        evidence = committed.integrated_candidate
        assert evidence is not None
        result = apply_main_advance(
            advance,
            accepted_plan_digest=advance.plan_digest,
            accepted_integrated_evidence_digest=advance.integrated_evidence_digest,
            confirmation_token=self.token("advance-main", advance.plan_digest, "ADVANCE-UX"),
        )
        advance_after = main_advance_interaction(advance, "after")
        self.assertEqual(advance_after.facts["actual_head"], result.current_main)
        self.assertFalse(advance_after.facts["pushed"])

        cleanup = plan_cleanup_integration(result)
        self.assertEqual(cleanup.affected_prds, ("001",))
        cleanup_notifications = []
        apply_cleanup_integration(
            cleanup,
            accepted_plan_digest=cleanup.plan_digest,
            notify=cleanup_notifications.append,
        )
        for envelope in cleanup_notifications:
            self.assertEqual(envelope.facts["affected_prds"], ["001"])
            self.assertTrue(envelope.facts["source_preserved"])
            self.assertFalse(envelope.facts["remote_involved"])
        replay = apply_cleanup_integration(
            cleanup,
            accepted_plan_digest=cleanup.plan_digest,
            notify=lambda _item: None,
        )
        self.assertTrue(replay.idempotent)

    def test_cleanup_blocks_ignored_asset_and_preserves_worktree(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        _prepare_plan, prepared, _notifications = self.prepare((candidate,))
        committed = self.commit(prepared)
        self.bind_primary_for_main_advance()
        advance = plan_main_advance(self.register_integration(committed))
        evidence = committed.integrated_candidate
        assert evidence is not None
        result = apply_main_advance(
            advance,
            accepted_plan_digest=advance.plan_digest,
            accepted_integrated_evidence_digest=advance.integrated_evidence_digest,
            confirmation_token=self.token("advance-main", advance.plan_digest, "ADVANCE-CLEANUP-IGNORED"),
        )
        worktree = Path(committed.integration_worktree)
        (worktree / "ignored.asset").write_text("must preserve\n", encoding="utf-8")

        cleanup = plan_cleanup_integration(result)

        self.assertIn("cleanup-unowned-assets", {item.code for item in cleanup.blockers})
        self.assertTrue(worktree.exists())

    def test_cleanup_sees_staged_asset_and_recovers_crash_after_remove(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        _prepare_plan, prepared, _notifications = self.prepare((candidate,))
        committed = self.commit(prepared)
        self.bind_primary_for_main_advance()
        advance = plan_main_advance(self.register_integration(committed))
        evidence = committed.integrated_candidate
        assert evidence is not None
        result = apply_main_advance(
            advance,
            accepted_plan_digest=advance.plan_digest,
            accepted_integrated_evidence_digest=advance.integrated_evidence_digest,
            confirmation_token=self.token("advance-main", advance.plan_digest, "ADVANCE-CLEANUP-CRASH"),
        )
        worktree = Path(committed.integration_worktree)
        staged = worktree / "staged-only.txt"
        staged.write_text("must preserve\n", encoding="utf-8")
        self.git("add", "staged-only.txt", cwd=worktree)

        blocked = plan_cleanup_integration(result)

        self.assertIn("cleanup-unowned-assets", {item.code for item in blocked.blockers})
        self.git("rm", "--cached", "staged-only.txt", cwd=worktree)
        staged.unlink()
        cleanup = plan_cleanup_integration(result)
        self.assertEqual(cleanup.blockers, ())
        notifications = []

        def crash(stage: str) -> None:
            if stage == "cleanup-after-remove":
                raise InjectedCrash(stage)

        with self.assertRaises(InjectedCrash):
            apply_cleanup_integration(
                cleanup,
                accepted_plan_digest=cleanup.plan_digest,
                notify=notifications.append,
                failpoint=crash,
            )
        self.assertFalse(worktree.exists())

        recovered_notifications = []
        recovered = apply_cleanup_integration(
            cleanup,
            accepted_plan_digest=cleanup.plan_digest,
            notify=recovered_notifications.append,
        )

        self.assertTrue(recovered.idempotent)
        self.assertEqual([item.phase for item in notifications], ["before"])
        self.assertEqual([item.phase for item in recovered_notifications], ["after"])

    def test_main_advance_blocks_checked_out_main_and_main_drift(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        _prepare_plan, prepared, _notifications = self.prepare((candidate,))
        committed = self.commit(prepared)

        registered = self.register_integration(committed)
        checked_out = plan_main_advance(registered)

        self.assertIn("main-ref-checked-out", {item.code for item in checked_out.blockers})
        self.detach_primary()
        detached = plan_main_advance(registered)
        self.assertIn(
            "main-release-local-holder-active",
            {item.code for item in detached.blockers},
        )
        self.git("switch", "main")
        self.bind_primary_for_main_advance()
        planned = plan_main_advance(registered)
        self.assertEqual(planned.blockers, ())
        self.git("update-ref", "refs/heads/main", self.a_commit, self.base)
        evidence = committed.integrated_candidate
        assert evidence is not None
        with self.assertRaisesRegex(TrainError, "main drifted"):
            apply_main_advance(
                planned,
                accepted_plan_digest=planned.plan_digest,
                accepted_integrated_evidence_digest=planned.integrated_evidence_digest,
                confirmation_token=self.token("advance-main", planned.plan_digest, "ADVANCE-DRIFT"),
            )

    def test_main_advance_crash_after_transaction_recovers_idempotently(self) -> None:
        candidate = self.register("001", "refs/heads/feature/001", self.feature_a)
        _prepare_plan, prepared, _notifications = self.prepare((candidate,))
        committed = self.commit(prepared)
        self.bind_primary_for_main_advance()
        advance = plan_main_advance(self.register_integration(committed))
        evidence = committed.integrated_candidate
        assert evidence is not None
        token = self.token("advance-main", advance.plan_digest, "ADVANCE-CRASH")

        def crash(stage: str) -> None:
            if stage == "main-advance-after-refs":
                raise InjectedCrash(stage)

        with self.assertRaises(InjectedCrash):
            apply_main_advance(
                advance,
                accepted_plan_digest=advance.plan_digest,
                accepted_integrated_evidence_digest=advance.integrated_evidence_digest,
                confirmation_token=token,
                failpoint=crash,
            )
        recovered = apply_main_advance(
            advance,
            accepted_plan_digest=advance.plan_digest,
            accepted_integrated_evidence_digest=advance.integrated_evidence_digest,
            confirmation_token=token,
        )

        self.assertTrue(recovered.idempotent)
        self.assertEqual(self.oid("refs/heads/main"), committed.integrated_commit)


if __name__ == "__main__":
    unittest.main()
