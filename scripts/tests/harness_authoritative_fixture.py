"""Small real-Git authority fixture for integrated-evidence/main-CAS tests.

The fixture deliberately skips workspace activation and candidate preparation,
but it does not mock or bypass a production gate.  It builds the exact public
candidate metadata blob/refs plus the candidate recovery journal that the
public loader authenticates, then constructs a real no-ff integration commit
and lets the production integrated-evidence registry publish its own refs.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from scripts import harness_integrated_evidence as registry
from scripts import harness_train as train
from scripts import harness_workspace as workspace
from scripts import project_harness as core
from scripts.harness_candidate import (
    AcceptanceEvidence,
    CandidateInput,
    IntegrationInput,
    build_candidate,
    build_integrated_candidate,
)


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class AuthoritativeIntegrationFixture:
    """Create one valid PRD-001 candidate and integrated evidence registry."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-authority-fast-")
        self.container = Path(self.temporary.name).resolve()
        self.root = self.container / "project with spaces"
        self.root.mkdir()
        self.git_executable = shutil.which("git")
        if self.git_executable is None:
            raise RuntimeError("git is required")
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Authority Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        core.apply_operations(
            self.root,
            core.build_init_operations(
                self.root,
                "Authority Fixture",
                datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc),
            ),
        )
        self.git("add", "--", ".")
        self.git("commit", "--no-gpg-sign", "--no-verify", "-m", "governance baseline")
        self.base_commit = self.oid("refs/heads/main")
        self.base_tree = self.oid(f"{self.base_commit}^{{tree}}")
        self.principle_sha256 = hashlib.sha256(
            (self.root / "harness" / "principle.md").read_bytes()
        ).hexdigest()
        self._reserve_iteration()
        self.registered_candidate = self._publish_candidate()
        self.integration_result = self._build_integration_result()

    def close(self) -> None:
        self.temporary.cleanup()

    def git(
        self,
        *arguments: str,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(self.root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment,
        )

    def git_bytes(self, *arguments: str, input_bytes: bytes) -> bytes:
        return subprocess.run(
            [self.git_executable, "-C", str(self.root), *arguments],
            check=True,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment,
        ).stdout

    def oid(self, revision: str) -> str:
        return self.git("rev-parse", "--verify", revision).stdout.strip()

    @property
    def common_dir(self) -> Path:
        raw = Path(self.git("rev-parse", "--git-common-dir").stdout.strip())
        return (raw if raw.is_absolute() else self.root / raw).resolve()

    @staticmethod
    def _token(action: str, subject_digest: str, authorization_id: str) -> train.ConfirmationToken:
        return train.ConfirmationToken(
            schema_version=train.CONFIRM_TOKEN_SCHEMA,
            action=action,
            subject_digest=subject_digest,
            authorization_id=authorization_id,
            token_digest=train.confirmation_token_digest(
                action,
                subject_digest,
                authorization_id,
            ),
        )

    def _reserve_iteration(self) -> None:
        plan = core.build_reserve_iteration_plan(
            self.root,
            self.git_executable,
            title="Fast integrated evidence fixture",
            operation_id="OP-" + "1" * 32,
            base_ref="refs/heads/main",
            governance_ref="refs/heads/main",
        )
        if plan.blocking_reasons:
            raise AssertionError(plan.blocking_reasons)
        journal, _created = core.reserve_iteration(plan, self.git_executable, self.root)
        if journal.phase != "READY" or journal.iteration != "001":
            raise AssertionError(journal)

    def _publish_candidate(self) -> train.RegisteredCandidate:
        self.git("switch", "-c", "fixture/001")
        (self.root / "feature.txt").write_text("authoritative fixture\n", encoding="utf-8")
        self.git("add", "--", "feature.txt")
        self.git("commit", "--no-gpg-sign", "--no-verify", "-m", "fixture implementation")
        pre_seal_commit = self.oid("HEAD")
        pre_seal_tree = self.oid("HEAD^{tree}")
        self.git(
            "commit",
            "--allow-empty",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "fixture candidate seal",
        )
        seal_commit = self.oid("HEAD")
        seal_tree = self.oid("HEAD^{tree}")

        provisional_binding = train.PrincipleGateBinding(
            schema_version=train.PRINCIPLE_GATE_BINDING_SCHEMA,
            iteration="001",
            authority_ref="refs/heads/fixture/001",
            mode="no-drift",
            allocation_principle_sha256=self.principle_sha256,
            current_principle_sha256=self.principle_sha256,
            drift=False,
            disposition=None,
            audit_generation=None,
            audit_receipt_digest=None,
            audit_supersedes=None,
            audit_operation_id=None,
            audit_plan_digest=None,
            binding_digest="0" * 64,
        )
        principle_binding = replace(
            provisional_binding,
            binding_digest=train.principle_gate_binding_digest(provisional_binding),
        )
        provisional_verification = train.CandidateVerificationReceipt(
            schema_version=train.CANDIDATE_VERIFICATION_RECEIPT_SCHEMA,
            phase="seal",
            evidence_id="verify-001-fast",
            candidate_commit=seal_commit,
            candidate_tree=seal_tree,
            argv=("fixture-verify",),
            exit_code=0,
            stdout_sha256=EMPTY_SHA256,
            stderr_sha256=EMPTY_SHA256,
            receipt_digest="0" * 64,
        )
        verification = replace(
            provisional_verification,
            receipt_digest=train.candidate_verification_receipt_digest(
                provisional_verification
            ),
        )
        dependency_bindings_digest = workspace.dependency_bindings_digest(())
        sealed_verification_id = f"candidate-verification:{verification.receipt_digest}"
        candidate_evidence = build_candidate(
            CandidateInput(
                iteration="001",
                generation="g1",
                base_commit=self.base_commit,
                candidate_commit=seal_commit,
                candidate_tree=seal_tree,
                principle_sha256=self.principle_sha256,
                included_paths=tuple(
                    line
                    for line in self.git(
                        "diff",
                        "--name-only",
                        self.base_commit,
                        seal_commit,
                        "--",
                    ).stdout.splitlines()
                    if line
                ),
                acceptance_ids=("AC-001-01",),
                acceptance_evidence=(
                    AcceptanceEvidence(
                        acceptance_id="AC-001-01",
                        evidence_ids=("EV-001-fast-candidate",),
                        verification_ids=(sealed_verification_id,),
                    ),
                ),
                verification_ids=(
                    sealed_verification_id,
                    f"principle-gate:{principle_binding.binding_digest}",
                    f"dependency-bindings:{dependency_bindings_digest}",
                ),
                prd_approved=True,
                spec_approved=True,
                implementation_authorized=True,
                deviations_resolved=True,
                dirty_scope_owned=True,
            )
        )
        operation_id = "OP-" + "2" * 32
        candidate_ref = "refs/project-harness/v2/iterations/001/candidates/g1"
        evidence_ref = "refs/project-harness/v2/iterations/001/candidate-evidence/g1"
        authority_digest = train.digest({"fixture": "authority-001"})
        workspace_guard = train.WorkspaceGuardReceipt(
            schema_version=train.WORKSPACE_GUARD_SCHEMA,
            iteration="001",
            owner="fixture-owner-001",
            generation=1,
            operation_id=operation_id,
            accepted_plan_digest=train.digest({"fixture": "workspace-plan-001"}),
            worktree_path=str(self.root),
            branch_ref="refs/heads/main",
            base_commit=self.base_commit,
            implementation_ref="refs/heads/main",
            implementation_commit=self.base_commit,
            reconciliation_ref="refs/heads/main",
            reconciliation_commit=self.base_commit,
            dependency_refresh_generation=0,
            dependency_bindings=(),
            dependency_bindings_digest=dependency_bindings_digest,
            lease_digest=train.digest({"fixture": "workspace-lease-001"}),
            guard_digest="0" * 64,
        )
        workspace_guard = replace(
            workspace_guard,
            guard_digest=train.workspace_guard_digest(workspace_guard),
        )
        guard_digest = workspace_guard.guard_digest
        registration_plan_digest = train.digest({"fixture": "candidate-registration"})
        seal_plan_digest = train.digest({"fixture": "candidate-seal"})
        metadata: dict[str, object] = {
            "schema_version": train.CANDIDATE_EVIDENCE_METADATA_SCHEMA,
            "operation_id": operation_id,
            "iteration": "001",
            "generation": "g1",
            "candidate_ref": candidate_ref,
            "candidate_evidence_ref": evidence_ref,
            "base_ref": "refs/project-harness/v2/iterations/001/base",
            "pre_seal_commit": pre_seal_commit,
            "pre_seal_tree": pre_seal_tree,
            "seal_commit": seal_commit,
            "seal_tree": seal_tree,
            "principle_gate_binding": principle_binding.as_dict(),
            "authority_evidence_digest": authority_digest,
            "workspace_guard": workspace_guard.as_dict(),
            "workspace_guard_digest": guard_digest,
            "implementation_commit": self.base_commit,
            "depends_on": [],
            "dependency_bindings": [],
            "dependency_bindings_digest": dependency_bindings_digest,
            "candidate_evidence": candidate_evidence.as_dict(),
            "seal_verification_receipts": [verification.as_dict()],
            "seal_authorization_id": "AUTH-CANDIDATE-001-G1",
            "registration_plan_digest": registration_plan_digest,
            "seal_plan_digest": seal_plan_digest,
            "metadata_digest": "0" * 64,
        }
        payload = dict(metadata)
        payload.pop("metadata_digest")
        metadata["metadata_digest"] = train.digest(payload)
        evidence_raw = train.canonical_json(metadata) + b"\n"
        evidence_blob = self.git_bytes(
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=evidence_raw,
        ).decode("ascii").strip()
        self.git("update-ref", candidate_ref, seal_commit)
        self.git("update-ref", evidence_ref, evidence_blob)

        candidate_journal = {
            "schema_version": train.JOURNAL_SCHEMA,
            "kind": "candidate-register",
            "status": "complete",
            "registration_plan_digest": registration_plan_digest,
            "seal_plan_digest": seal_plan_digest,
            "candidate_ref": candidate_ref,
            "candidate_evidence_ref": evidence_ref,
            "pre_seal_commit": pre_seal_commit,
            "pre_seal_tree": pre_seal_tree,
            "seal_commit": seal_commit,
            "seal_tree": seal_tree,
            "implementation_commit": self.base_commit,
            "candidate_evidence": candidate_evidence.as_dict(),
            "candidate_evidence_blob": evidence_blob,
            "candidate_evidence_metadata_digest": metadata["metadata_digest"],
            "seal_authorization_id": "AUTH-CANDIDATE-001-G1",
            "verification_receipts": [verification.as_dict()],
            "authority_receipt": {
                "evidence_digest": authority_digest,
                "depends_on": [],
            },
            "workspace_guard": workspace_guard.as_dict(),
            "dependency_bindings": [],
            "dependency_bindings_digest": dependency_bindings_digest,
            "principle_gate_binding": principle_binding.as_dict(),
        }
        journal_path = (
            self.common_dir
            / "project-harness"
            / "train"
            / "v1"
            / "journal"
            / f"candidate-{operation_id}.json"
        )
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(train.canonical_json(candidate_journal) + b"\n")
        candidate, blockers = train.load_registered_candidate(
            self.root,
            iteration="001",
            generation="g1",
            current_principle_sha256=self.principle_sha256,
        )
        if candidate is None or blockers:
            raise AssertionError(blockers)
        return candidate

    def _build_integration_result(self) -> train.IntegrationCommitResult:
        candidate = self.registered_candidate
        message = "fixture no-ff integration"
        integrated_commit = self.git(
            "commit-tree",
            candidate.candidate_tree,
            "-p",
            self.base_commit,
            "-p",
            candidate.candidate_commit,
            "-m",
            message,
        ).stdout.strip()
        operation_id = "OP-" + "3" * 32
        verification = train.VerificationReceipt(
            schema_version=train.VERIFICATION_RECEIPT_SCHEMA,
            evidence_id="verify-integration-fast",
            argv=("fixture-integration-verify",),
            exit_code=0,
            stdout_sha256=EMPTY_SHA256,
            stderr_sha256=EMPTY_SHA256,
        )
        context = train.GovernanceContext(
            schema_version=train.GOVERNANCE_RECEIPT_SCHEMA,
            operation_id=operation_id,
            project_root=str(self.root),
            integration_worktree=str(self.root),
            target_main=self.base_commit,
            principle_sha256=self.principle_sha256,
            candidate_digests=(candidate.candidate_evidence.evidence_digest,),
            pre_governance_tree=candidate.candidate_tree,
        )
        governance = train.build_governance_receipt(
            context,
            mode="applied",
            result_tree=candidate.candidate_tree,
            evidence_ids=("EV-governance-fast",),
        )
        provisional_plan = train.IntegrationCommitPlan(
            schema_version=train.COMMIT_PLAN_SCHEMA,
            operation_id=operation_id,
            project_root=str(self.root),
            integration_worktree=str(self.root),
            generation="i1",
            main_ref="refs/heads/main",
            target_main=self.base_commit,
            integrated_tree=candidate.candidate_tree,
            parent_commits=(self.base_commit, candidate.candidate_commit),
            candidates=(candidate,),
            dependency_order=("001",),
            principle_sha256=self.principle_sha256,
            merge_strategy=train.DEFAULT_MERGE_STRATEGY,
            strategy_declaration_digest=None,
            governance_receipt=governance,
            verification_receipts=(verification,),
            commit_message=message,
            prepare_plan_digest=train.digest({"fixture": "integration-prepare"}),
            commit_plan_digest="0" * 64,
        )
        commit_plan = replace(
            provisional_plan,
            commit_plan_digest=train.integration_commit_plan_digest(provisional_plan),
        )
        confirmation = self._token(
            "create-integration-commit",
            commit_plan.commit_plan_digest,
            "AUTH-INTEGRATION-FAST",
        )
        integrated_candidate = build_integrated_candidate(
            IntegrationInput(
                generation="i1",
                target_main=self.base_commit,
                integrated_commit=integrated_commit,
                integrated_tree=candidate.candidate_tree,
                principle_sha256=self.principle_sha256,
                candidates=(candidate.candidate_evidence,),
                merge_strategy=train.DEFAULT_MERGE_STRATEGY,
                dependency_order=("001",),
                preserved_candidate_commits=(candidate.candidate_commit,),
                governance_reconciled=True,
                governance_evidence_digest=governance.evidence_digest,
                cross_prd_verification_ids=(verification.evidence_id,),
                integration_evidence_ids=(
                    *governance.evidence_ids,
                    f"train:{commit_plan.commit_plan_digest}",
                ),
            )
        )
        return train.IntegrationCommitResult(
            schema_version=train.COMMIT_RESULT_SCHEMA,
            operation_id=operation_id,
            project_root=str(self.root),
            integration_worktree=str(self.root),
            generation="i1",
            integrated_commit=integrated_commit,
            integrated_tree=candidate.candidate_tree,
            commit_plan=commit_plan,
            commit_confirmation_token=confirmation,
            integrated_candidate=integrated_candidate,
            blockers=(),
            journal_path=str(
                self.common_dir
                / "project-harness"
                / "train"
                / "v1"
                / "journal"
                / f"integration-{operation_id}.json"
            ),
            idempotent=False,
        )

    def publish_integrated_evidence(self) -> registry.RegisteredIntegratedEvidence:
        token = self.integration_result.commit_confirmation_token
        plan = registry.plan_register_integrated_evidence(
            self.integration_result,
            commit_confirmation_token=token,
            progress_bindings=(
                (
                    "EV-" + "a" * 64,
                    registry.iteration_final_evidence_ref("001"),
                ),
            ),
        )
        if plan.blockers:
            raise AssertionError(plan.blockers)
        return registry.apply_register_integrated_evidence(
            plan,
            accepted_plan_digest=plan.plan_digest,
            commit_confirmation_token=token,
        )

    def plan_main_advance(
        self,
        receipt: registry.RegisteredIntegratedEvidence,
    ) -> train.MainAdvancePlan:
        lease_path = (
            self.common_dir
            / "project-harness"
            / "train"
            / "v1"
            / "leases"
            / "main-integration.json"
        )
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease = {
            "schema_version": train.LEASE_SCHEMA,
            "operation_id": receipt.operation_id,
            "plan_digest": self.integration_result.commit_plan.prepare_plan_digest,
            "scope": "refs/heads/main",
            "generation": self.integration_result.generation,
            "expected_main": self.base_commit,
            "worktree_path": str(self.root),
            "status": "active",
        }
        lease_path.write_bytes(train.canonical_json(lease) + b"\n")
        plan = train.plan_main_advance(receipt)
        if plan.blockers:
            raise AssertionError(plan.blockers)
        return plan

    def advance_token(self, plan: train.MainAdvancePlan) -> train.ConfirmationToken:
        return self._token(
            "advance-main",
            plan.plan_digest,
            "AUTH-ADVANCE-FAST",
        )

    def write_iteration_bundle(self) -> None:
        directory = self.root / "harness" / "iterations" / "001"
        directory.mkdir(parents=True, exist_ok=True)
        owner = "<!-- managed-by: harness-lite v1 -->\n"
        (directory / "README.md").write_text(owner + "# Iteration 001\n", encoding="utf-8")
        (directory / "prd-001.md").write_text(
            owner
            + "# PRD-001：Fast authority fixture\n"
            + "- 状态：`实施中`\n"
            + "- 批准依据：用户明确批准 PRD-001\n\n"
            + "## 验收标准\n\n### AC-001-01\n\nEvidence required.\n",
            encoding="utf-8",
        )
        (directory / "spec-001.md").write_text(
            owner
            + "# SPEC-001：Fast authority fixture\n"
            + "- 状态：`实施中`\n"
            + "- 批准依据：用户明确批准 SPEC-001\n"
            + "- 实施授权：用户明确授权开始实施\n",
            encoding="utf-8",
        )
        (directory / "deviation-001.md").write_text(
            owner + "# PRD-001 / SPEC-001 deviations\n\n当前开放偏差：`0`。\n",
            encoding="utf-8",
        )
