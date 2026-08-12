from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from harness_candidate import AcceptanceEvidence, CandidateInput, build_candidate  # noqa: E402
from harness_governance import (  # noqa: E402
    IterationRoutingState,
    PrincipleApproval,
    RootRoutingAuthority,
    parse_progress_events,
)
import harness_train  # noqa: E402
import harness_progress  # noqa: E402
import harness_train_governance as governance_adapter  # noqa: E402
from harness_train import (  # noqa: E402
    CandidateVerificationReceipt,
    ConfirmationToken,
    GovernanceContext,
    RegisteredCandidate,
    VerifyCommand,
    governance_receipt_gate,
    confirmation_token_digest,
    integration_prepare_plan_digest,
    plan_prepare_integration,
    apply_prepare_integration,
    candidate_verification_receipt_digest,
    registered_candidate_digest,
)
from harness_train_governance import (  # noqa: E402
    DerivedReadme,
    GovernanceAdapterError,
    InjectedGovernanceCrash,
    apply_premerge_normalization,
    build_governance_callback,
    build_conflict_normalizer,
    build_readme_rebuild_authority,
    inspect_governance_resume,
    materialize_train_progress_events,
    plan_premerge_normalization,
    ProgressEvidenceResolution,
    resume_governance_callback,
)


OWNER = b"<!-- managed-by: harness-lite v1 -->\n"
FOCUS_START = b"<!-- project-harness:focus:start -->\n"
FOCUS_END = b"<!-- project-harness:focus:end -->\n"
ITERATIONS_START = b"<!-- project-harness:iterations:start -->\n"
ITERATIONS_END = b"<!-- project-harness:iterations:end -->\n"


def event(identity: str, fact: str, *, parent: str = "S-20260812-01/OPEN") -> bytes:
    return (
        f"## {identity} / CHECKPOINT / 2026-08-12T11:00:00+08:00\n\n"
        f"- causal_parent: {parent}\n"
        f"- fact: {fact}\n"
    ).encode("utf-8")


def legacy_event() -> bytes:
    return b"## S-20260812-01 / OPEN / 2026-08-12T10:00:00+08:00\n\n- fact: baseline\n"


def progress(*events: bytes) -> bytes:
    result = (
        OWNER
        + b"# Progress\n\n"
        + b"<!-- project-harness:progress-index:start -->\n"
        + b"| iteration | status |\n|---|---|\n| 001 | active |\n| 002 | active |\n"
        + b"<!-- project-harness:progress-index:end -->\n"
    )
    for value in events:
        result = result.rstrip(b"\n") + b"\n\n" + value
    return result


def root_readme(focus: str, row: str) -> bytes:
    return (
        OWNER
        + b"# Harness\n\nmanual: must-survive\n\n"
        + FOCUS_START
        + f"- {focus}\n".encode()
        + FOCUS_END
        + b"\n"
        + ITERATIONS_START
        + f"| {row} |\n".encode()
        + ITERATIONS_END
    )


def l1(number: str, result: str) -> bytes:
    return OWNER + f"# Iteration {number}\n\n## Current result\n\n{result}\n".encode()


class MergeTrainGovernanceAdapterTests(unittest.TestCase):
    maxDiff = None

    def test_conditional_train_progress_materializes_only_from_exact_public_binding(self) -> None:
        operation_id = "OP-" + "a" * 32
        event = harness_progress.integration_event(
            integration_state="verified:i-test",
            session_id="S-20260812-02",
            iteration="001",
            occurred_at="2026-08-12T12:00:00+08:00",
            source_ref="refs/heads/main",
            source_commit="1" * 40,
            operation_id=operation_id,
            causal_parent="S-20260812-01/OPEN",
            evidence_refs=(
                "refs/project-harness/v2/iterations/001/integrated-evidence/i-test",
            ),
            summary="Conditional integration verification proposal",
        )
        raw = event.render(b"\n")
        provisional = governance_adapter.TrainProgressEventSpec(
            schema_version=governance_adapter.TRAIN_PROGRESS_SPEC_SCHEMA,
            transition="integration_verified",
            iteration="001",
            generation="i-test",
            event=event,
            event_bytes_b64=governance_adapter.base64.b64encode(raw).decode("ascii"),
            event_sha256=hashlib.sha256(raw).hexdigest(),
            evidence_ref=event.evidence_refs[0],
            conditional=True,
            spec_digest="0" * 64,
        )
        spec = replace(
            provisional,
            spec_digest=governance_adapter._digest(
                governance_adapter._progress_spec_payload(provisional)
            ),
        )

        absent = materialize_train_progress_events((spec,), resolver=lambda _ref: None)
        self.assertFalse(absent[0].materialized)
        self.assertEqual(absent[0].blocker, "public-evidence-ref-absent")
        wrong = ProgressEvidenceResolution(
            schema_version=governance_adapter.PROGRESS_EVIDENCE_RESOLUTION_SCHEMA,
            ref_name=spec.evidence_ref,
            object_id="2" * 40,
            evidence_digest="3" * 64,
            event_ids=("EV-unrelated",),
        )
        self.assertFalse(
            materialize_train_progress_events((spec,), resolver=lambda _ref: wrong)[0].materialized
        )
        exact = replace(wrong, event_ids=(event.event_id,))
        materialized = materialize_train_progress_events(
            (spec,),
            resolver=lambda _ref: exact,
        )
        self.assertTrue(materialized[0].materialized)
        self.assertIsNone(materialized[0].blocker)

    def setUp(self) -> None:
        self.git_executable = shutil.which("git")
        if not self.git_executable:
            self.skipTest("git is required")
        self.temporary = tempfile.TemporaryDirectory(prefix="train governance tests ")
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "primary project"
        self.root.mkdir()
        self.feature_a = self.sandbox / "feature A"
        self.feature_b = self.sandbox / "feature B"
        self.git_config = self.sandbox / "isolated gitconfig"
        subprocess.run(
            [self.git_executable, "config", "--file", str(self.git_config), "user.name", "Governance Tests"],
            check=True,
        )
        subprocess.run(
            [self.git_executable, "config", "--file", str(self.git_config), "user.email", "governance@example.invalid"],
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
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Governance Tests")
        self.git("config", "user.email", "governance@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write_bytes("harness/principle.md", OWNER + b"# Principle\n\n- stable\n")
        self.write_bytes("harness/progress.md", progress(legacy_event()))
        self.write_bytes("harness/README.md", root_readme("baseline focus", "baseline registry"))
        for number in ("001", "002"):
            self.write_bytes(f"harness/iterations/{number}/README.md", l1(number, "baseline"))
            self.write_authority(number)
        self.write_bytes("app.txt", b"baseline\n")
        self.git("add", "--", ".")
        self.git("commit", "--no-gpg-sign", "-m", "baseline")
        self.base = self.oid("HEAD")
        self.principle_sha256 = hashlib.sha256((self.root / "harness/principle.md").read_bytes()).hexdigest()
        for number in ("001", "002"):
            self.reserve_workspace(number)

    def tearDown(self) -> None:
        self.environment.stop()
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
            env=os.environ.copy(),
        )

    def oid(self, value: str, *, cwd: Path | None = None) -> str:
        return self.git("rev-parse", "--verify", value, cwd=cwd).stdout.strip()

    def write_bytes(self, relative: str, content: bytes, *, root: Path | None = None) -> None:
        target = (root or self.root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def write_authority(self, number: str) -> None:
        directory = f"harness/iterations/{number}"
        self.write_bytes(
            f"{directory}/prd-{number}.md",
            (
                f"# PRD-{number}: Fixture\n\n"
                "- 状态：`实施中`\n"
                f"- 批准依据：用户已批准 PRD-{number} 基线（AUTH-PRD-{number}）\n\n"
                f"## 验收标准\n\n### AC-{number}-01\n\nEvidence required.\n"
            ).encode("utf-8"),
        )
        self.write_bytes(
            f"{directory}/spec-{number}.md",
            (
                f"# SPEC-{number}: Fixture\n\n"
                "- 状态：`实施中`\n"
                f"- 批准依据：用户已批准 SPEC-{number} 基线（AUTH-SPEC-{number}）\n"
                f"- 实施授权：用户已授权开始实施（AUTH-IMPLEMENT-{number}）\n"
            ).encode("utf-8"),
        )
        self.write_bytes(
            f"{directory}/deviation-{number}.md",
            f"# Deviation {number}\n\n当前开放偏差：`0`。\n".encode("utf-8"),
        )

    def reserve_workspace(self, number: str) -> None:
        metadata = {
            "schema_version": "harness-lite.allocation-metadata.v1",
            "operation_id": "OP-" + uuid.uuid4().hex,
            "plan_digest": hashlib.sha256(f"allocation-{number}".encode()).hexdigest(),
            "iteration": number,
            "base_commit": self.base,
            "base_branch": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.base,
            "governance_tree": self.oid(f"{self.base}^{{tree}}"),
            "principle_sha256": self.principle_sha256,
            "title": f"Governance fixture {number}",
        }
        raw = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        blob = self.git("hash-object", "-w", "--stdin", input_text=raw).stdout.strip()
        self.git("update-ref", f"refs/project-harness/v2/allocations/{number}", blob)
        self.git("update-ref", f"refs/project-harness/v2/iterations/{number}/base", self.base)

    def make_feature(
        self,
        number: str,
        worktree: Path,
        *,
        progress_bytes: bytes,
        principle: bytes | None = None,
    ) -> tuple[str, str]:
        branch = f"feature/{number}"
        branch_ref = f"refs/heads/{branch}"
        self.git("worktree", "add", "-b", branch, str(worktree), self.base)
        self.write_bytes(f"feature-{number}.txt", f"feature {number}\n".encode(), root=worktree)
        self.write_bytes("harness/progress.md", progress_bytes, root=worktree)
        self.write_bytes("harness/README.md", root_readme(f"stale candidate {number}", f"stale {number}"), root=worktree)
        self.write_bytes(f"harness/iterations/{number}/README.md", l1(number, f"stale candidate {number}"), root=worktree)
        if principle is not None:
            self.write_bytes("harness/principle.md", principle, root=worktree)
        self.git("add", "--", ".", cwd=worktree)
        self.git("commit", "--no-gpg-sign", "-m", f"feature {number}", cwd=worktree)
        commit = self.oid("HEAD", cwd=worktree)
        return branch_ref, commit

    def registered(self, number: str, feature_ref: str, commit: str) -> RegisteredCandidate:
        self.assertEqual(self.oid(feature_ref), commit)
        generation = "g1"
        operation_id = "OP-" + uuid.uuid4().hex
        pre_seal_tree = self.oid(f"{commit}^{{tree}}")
        worktree = self.feature_a if number == "001" else self.feature_b
        parsed_progress = parse_progress_events(
            (worktree / "harness/progress.md").read_bytes(),
            source=f"fixture:{number}",
        )
        self.assertFalse(parsed_progress.blockers)
        candidate_parent = parsed_progress.events[-1].identity
        candidate_progress = harness_progress.candidate_event(
            generation=1,
            candidate_state="sealed",
            session_id="S-20260812-02",
            iteration=number,
            occurred_at="2026-08-12T12:00:00+08:00",
            source_ref=feature_ref,
            source_commit=commit,
            operation_id=operation_id,
            causal_parent=candidate_parent,
            evidence_refs=(f"candidate:{number}:{generation}",),
            summary=f"PRD-{number} candidate generation {generation} sealed",
        )
        sealed_progress, appended = harness_progress.append_progress_event_exact(
            (worktree / "harness/progress.md").read_bytes(),
            candidate_progress,
        )
        self.assertTrue(appended)
        self.write_bytes("harness/progress.md", sealed_progress, root=worktree)
        self.git("add", "--", "harness/progress.md", cwd=worktree)
        self.git(
            "commit",
            "--no-gpg-sign",
            "-m",
            f"candidate(PRD-{number}): seal generation {generation}",
            cwd=worktree,
        )
        seal_commit = self.oid("HEAD", cwd=worktree)
        seal_tree = self.oid(f"{seal_commit}^{{tree}}")
        candidate_ref = f"refs/project-harness/v2/iterations/{number}/candidates/{generation}"
        evidence_ref = (
            f"refs/project-harness/v2/iterations/{number}/candidate-evidence/{generation}"
        )
        base_ref = f"refs/project-harness/v2/iterations/{number}/base"
        self.git("update-ref", candidate_ref, seal_commit)
        argv = (sys.executable, "-c", "raise SystemExit(0)")

        def receipt(phase: str, receipt_commit: str, receipt_tree: str) -> CandidateVerificationReceipt:
            provisional = CandidateVerificationReceipt(
                schema_version=harness_train.CANDIDATE_VERIFICATION_RECEIPT_SCHEMA,
                phase=phase,  # type: ignore[arg-type]
                evidence_id=f"test:{number}:feature",
                candidate_commit=receipt_commit,
                candidate_tree=receipt_tree,
                argv=argv,
                exit_code=0,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                receipt_digest="0" * 64,
            )
            return replace(
                provisional,
                receipt_digest=candidate_verification_receipt_digest(provisional),
            )

        pre_receipt = receipt("pre-seal", commit, pre_seal_tree)
        seal_receipt = receipt("seal", seal_commit, seal_tree)
        verification_identity = f"candidate-verification:{seal_receipt.receipt_digest}"
        repo = harness_train.open_repository(self.root)
        principle_binding, principle_blockers = harness_train._current_candidate_principle_gate_binding(
            repo,
            number,
            authority_ref="refs/heads/main",
        )
        self.assertEqual(principle_blockers, ())
        self.assertIsNotNone(principle_binding)
        assert principle_binding is not None
        dependency_bindings = ()
        dependency_bindings_digest = harness_train._dependency_bindings_digest(
            dependency_bindings
        )
        verification_identities = (
            verification_identity,
            harness_train._dependency_evidence_id(dependency_bindings_digest),
            harness_train._principle_gate_evidence_id(principle_binding.binding_digest),
        )
        included_paths = tuple(
            line
            for line in self.git(
                "diff", "--name-only", self.base, seal_commit, "--"
            ).stdout.splitlines()
            if line
        )
        candidate_evidence = build_candidate(
            CandidateInput(
                iteration=number,
                generation=generation,
                base_commit=self.base,
                candidate_commit=seal_commit,
                candidate_tree=seal_tree,
                principle_sha256=self.principle_sha256,
                included_paths=included_paths,
                acceptance_ids=(f"AC-{number}-01",),
                acceptance_evidence=(
                    AcceptanceEvidence(
                        acceptance_id=f"AC-{number}-01",
                        evidence_ids=(f"evidence:{number}",),
                        verification_ids=verification_identities,
                    ),
                ),
                verification_ids=verification_identities,
                prd_approved=True,
                spec_approved=True,
                implementation_authorized=True,
                deviations_resolved=True,
                dirty_scope_owned=True,
            )
        )
        self.assertTrue(candidate_evidence.verified, candidate_evidence.blockers)
        registration_plan_digest = hashlib.sha256(
            f"registration:{number}:{operation_id}".encode()
        ).hexdigest()
        seal_plan_digest = hashlib.sha256(
            f"seal:{number}:{operation_id}".encode()
        ).hexdigest()
        authority_digest = hashlib.sha256(f"authority:{number}".encode()).hexdigest()
        workspace_digest = hashlib.sha256(f"workspace:{number}".encode()).hexdigest()
        seal_authorization_id = f"AUTH-CANDIDATE-{number}"
        metadata: dict[str, object] = {
            "schema_version": harness_train.CANDIDATE_EVIDENCE_METADATA_SCHEMA,
            "operation_id": operation_id,
            "iteration": number,
            "generation": generation,
            "candidate_ref": candidate_ref,
            "candidate_evidence_ref": evidence_ref,
            "feature_ref": feature_ref,
            "base_ref": base_ref,
            "main_ref": "refs/heads/main",
            "workspace_ref": feature_ref,
            "pre_seal_commit": commit,
            "pre_seal_tree": pre_seal_tree,
            "seal_commit": seal_commit,
            "seal_tree": seal_tree,
            "parent_commits": [commit],
            "progress_event": candidate_progress.as_dict(),
            "progress_event_bytes_sha256": hashlib.sha256(
                candidate_progress.render(b"\n")
            ).hexdigest(),
            "authority_evidence_digest": authority_digest,
            "workspace_guard_digest": workspace_digest,
            "principle_gate_binding": principle_binding.as_dict(),
            "depends_on": [],
            "dependency_bindings": [],
            "dependency_bindings_digest": dependency_bindings_digest,
            "upstream_evidence_ids": [f"evidence:{number}"],
            "pre_seal_verification_receipts": [pre_receipt.as_dict()],
            "seal_verification_receipts": [seal_receipt.as_dict()],
            "candidate_evidence": candidate_evidence.as_dict(),
            "seal_authorization_id": seal_authorization_id,
            "registration_plan_digest": registration_plan_digest,
            "seal_plan_digest": seal_plan_digest,
            "pushed": False,
            "metadata_digest": "0" * 64,
        }
        metadata["metadata_digest"] = harness_train.digest(
            {key: value for key, value in metadata.items() if key != "metadata_digest"}
        )
        evidence_raw = harness_train.canonical_json(metadata) + b"\n"
        evidence_blob = subprocess.run(
            [
                self.git_executable,
                "-C",
                str(self.root),
                "hash-object",
                "-w",
                "--stdin",
            ],
            check=True,
            input=evidence_raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        ).stdout.decode("ascii").strip()
        self.git("update-ref", evidence_ref, evidence_blob)
        common_raw = Path(self.git("rev-parse", "--git-common-dir").stdout.strip())
        common = (common_raw if common_raw.is_absolute() else self.root / common_raw).resolve()
        journal_path = (
            common
            / "project-harness"
            / "train"
            / "v1"
            / "journal"
            / f"candidate-{operation_id}.json"
        )
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal = {
            "schema_version": harness_train.JOURNAL_SCHEMA,
            "kind": "candidate-register",
            "status": "complete",
            "operation_id": operation_id,
            "registration_plan_digest": registration_plan_digest,
            "seal_plan_digest": seal_plan_digest,
            "candidate_ref": candidate_ref,
            "candidate_evidence_ref": evidence_ref,
            "pre_seal_commit": commit,
            "pre_seal_tree": pre_seal_tree,
            "seal_commit": seal_commit,
            "seal_tree": seal_tree,
            "candidate_evidence": candidate_evidence.as_dict(),
            "candidate_evidence_blob": evidence_blob,
            "candidate_evidence_metadata_digest": metadata["metadata_digest"],
            "seal_authorization_id": seal_authorization_id,
            "verification_receipts": [seal_receipt.as_dict()],
            "pre_seal_verification_receipts": [pre_receipt.as_dict()],
            "principle_gate_binding": principle_binding.as_dict(),
            "dependency_bindings": [],
            "dependency_bindings_digest": dependency_bindings_digest,
            "authority_receipt": {
                "evidence_digest": authority_digest,
                "depends_on": [],
            },
        }
        journal_path.write_bytes(harness_train.canonical_json(journal) + b"\n")
        provisional = RegisteredCandidate(
            schema_version=harness_train.REGISTER_RESULT_SCHEMA,
            operation_id=operation_id,
            project_root=str(self.root),
            iteration=number,
            generation=generation,
            candidate_ref=candidate_ref,
            candidate_evidence_ref=evidence_ref,
            candidate_evidence_blob=evidence_blob,
            candidate_evidence_metadata_digest=str(metadata["metadata_digest"]),
            pre_seal_commit=commit,
            pre_seal_tree=pre_seal_tree,
            candidate_commit=seal_commit,
            candidate_tree=seal_tree,
            base_ref=base_ref,
            base_commit=self.base,
            principle_sha256=self.principle_sha256,
            principle_gate_binding=principle_binding,
            authority_evidence_digest=authority_digest,
            workspace_guard_digest=workspace_digest,
            depends_on=(),
            dependency_bindings=(),
            dependency_bindings_digest=dependency_bindings_digest,
            candidate_evidence=candidate_evidence,
            verification_receipts=(seal_receipt,),
            seal_authorization_id=seal_authorization_id,
            registration_plan_digest=registration_plan_digest,
            seal_plan_digest=seal_plan_digest,
            registration_digest="0" * 64,
            journal_path=str(journal_path),
            idempotent=False,
        )
        registered = replace(
            provisional,
            registration_digest=registered_candidate_digest(provisional),
        )
        return registered

    def integration_plan(self, candidates: tuple[RegisteredCandidate, ...], suffix: str):
        plan = plan_prepare_integration(
            self.root,
            generation=f"i-{suffix}",
            candidates=candidates,
            verify_commands=(
                VerifyCommand(
                    evidence_id=f"test:integration:{suffix}",
                    argv=(sys.executable, "-c", "raise SystemExit(0)"),
                ),
            ),
            operation_id="OP-" + uuid.uuid4().hex,
        )
        self.assertEqual(plan.blockers, ())
        self.assertEqual(plan.plan_digest, integration_prepare_plan_digest(plan))
        return plan

    def confirmation(self, plan):
        authorization_id = "AUTH-TRAIN-GOVERNANCE"
        return ConfirmationToken(
            schema_version="harness-lite.confirm-token/v1",
            action="prepare-integration",
            subject_digest=plan.plan_digest,
            authorization_id=authorization_id,
            token_digest=confirmation_token_digest(
                "prepare-integration", plan.plan_digest, authorization_id
            ),
        )

    def context_with_raw_merge(self, plan, first_candidate: RegisteredCandidate) -> GovernanceContext:
        worktree = Path(plan.worktree_path)
        self.git("worktree", "add", "--detach", str(worktree), plan.target_main)
        self.git("merge", "--no-ff", "--no-commit", first_candidate.candidate_ref, cwd=worktree)
        for candidate in plan.candidates[1:]:
            product = f"feature-{candidate.iteration}.txt"
            self.git("checkout", candidate.candidate_ref, "--", product, cwd=worktree)
        input_tree = self.git("write-tree", cwd=worktree).stdout.strip()
        return GovernanceContext(
            schema_version="harness-lite.governance-apply-receipt/v1",
            operation_id=plan.operation_id,
            project_root=plan.project_root,
            integration_worktree=plan.worktree_path,
            target_main=plan.target_main,
            principle_sha256=plan.principle_sha256,
            candidate_digests=tuple(item.candidate_evidence.evidence_digest for item in plan.candidates),
            pre_governance_tree=input_tree,
        )

    def readme_authority(self):
        root = RootRoutingAuthority(
            authority_id="routing:integrated:001-002",
            current_iteration="001",
            global_gate="latest-main integrated verification",
            next_step="verify exact integrated candidate",
            iterations=(
                IterationRoutingState(
                    "001", "Feature A", "待验收", "已完成", 0, "integration", "evidence ready", "candidate g1", "queued", "A ready", "integrate A", (),
                ),
                IterationRoutingState(
                    "002", "Feature B", "待验收", "已完成", 0, "integration", "evidence ready", "candidate g1", "queued", "B ready", "integrate B", (),
                ),
            ),
        )
        return build_readme_rebuild_authority(
            authority_id=root.authority_id,
            root=root,
            l1_documents=(
                DerivedReadme("harness/iterations/001/README.md", l1("001", "authority rebuilt A"), "routing:001"),
                DerivedReadme("harness/iterations/002/README.md", l1("002", "authority rebuilt B"), "routing:002"),
            ),
        )

    def union_case(self, suffix: str):
        event_a = event("EV-001-A", "A fact")
        event_b = event("EV-002-B", "B fact", parent="EV-001-A")
        ref_a, commit_a = self.make_feature("001", self.feature_a, progress_bytes=progress(legacy_event(), event_a))
        ref_b, commit_b = self.make_feature(
            "002",
            self.feature_b,
            progress_bytes=progress(legacy_event(), event_a, event_b),
        )
        candidate_a = self.registered("001", ref_a, commit_a)
        candidate_b = self.registered("002", ref_b, commit_b)
        plan = self.integration_plan((candidate_a, candidate_b), suffix)
        context = self.context_with_raw_merge(plan, candidate_a)
        return event_a, event_b, plan, context

    def test_two_candidate_progress_union_deduplicates_and_rebuilds_readmes(self) -> None:
        event_a, event_b, plan, context = self.union_case("union")

        callback = build_governance_callback(plan, readme_authority=self.readme_authority())
        preview = callback.preview(context)
        self.assertTrue(preview.ready, preview.blockers)
        self.assertEqual(
            [(item.iteration, item.transition) for item in preview.progress_events],
            [
                ("001", "integration_started"),
                ("001", "integration_verified"),
                ("001", "main_advanced"),
                ("002", "integration_started"),
                ("002", "integration_verified"),
                ("002", "main_advanced"),
            ],
        )
        first_event_step = next(
            index
            for index, label in enumerate(preview.reconciliation_labels)
            if label.startswith("train-event-")
        )
        readme_step = next(
            index
            for index, label in enumerate(preview.reconciliation_labels)
            if label.startswith("readme-")
        )
        self.assertTrue(
            all(
                label.startswith("candidate-")
                for label in preview.reconciliation_labels[:first_event_step]
            )
        )
        self.assertTrue(
            all(
                label.startswith("train-event-")
                for label in preview.reconciliation_labels[first_event_step:readme_step]
            )
        )
        receipt = callback(context)

        worktree = Path(plan.worktree_path)
        merged_progress = (worktree / "harness/progress.md").read_bytes()
        self.assertEqual(merged_progress.count(event_a), 1)
        self.assertEqual(merged_progress.count(event_b), 1)
        parsed = parse_progress_events(merged_progress, source="integrated-result")
        self.assertFalse(parsed.blockers)
        identities = [item.identity for item in parsed.events]
        for spec in preview.progress_events:
            self.assertEqual(identities.count(spec.event.event_id), 1)
            self.assertFalse(any(harness_train.OID_RE.fullmatch(ref) for ref in spec.event.evidence_refs))
        status = materialize_train_progress_events(
            preview.progress_events,
            resolver=lambda _ref: None,
        )
        self.assertEqual(sum(item.materialized for item in status), 2)
        self.assertTrue(all(not item.materialized for item in status if item.conditional))
        rebuilt_l0 = (worktree / "harness/README.md").read_text(encoding="utf-8")
        self.assertIn("manual: must-survive", rebuilt_l0)
        self.assertIn("Feature A", rebuilt_l0)
        self.assertNotIn("stale candidate", rebuilt_l0)
        self.assertEqual((worktree / "harness/iterations/001/README.md").read_bytes(), l1("001", "authority rebuilt A"))
        index_tree = self.git("write-tree", cwd=worktree).stdout.strip()
        self.assertEqual(receipt.result_tree, index_tree)
        self.assertEqual(governance_receipt_gate(receipt, context, actual_result_tree=index_tree), ())
        candidate_authority_ids = [
            value for value in receipt.evidence_ids if value.startswith("candidate-authority:")
        ]
        self.assertEqual(len(candidate_authority_ids), 2)
        for candidate in plan.candidates:
            self.assertNotEqual(candidate.pre_seal_commit, candidate.candidate_commit)
            self.assertEqual(
                self.git("rev-list", "--parents", "-n", "1", candidate.candidate_commit).stdout.strip().split(),
                [candidate.candidate_commit, candidate.pre_seal_commit],
            )
        self.assertTrue(any(value.startswith("normalize:") for value in receipt.evidence_ids))
        self.assertGreaterEqual(sum(value.startswith("reconcile:") for value in receipt.evidence_ids), 3)
        self.assertEqual(
            sum(value.startswith("train-progress:") for value in receipt.evidence_ids),
            6,
        )

    def test_adapter_rejects_legacy_candidate_journal_schema(self) -> None:
        ref_a, commit_a = self.make_feature(
            "001",
            self.feature_a,
            progress_bytes=progress(legacy_event(), event("EV-001-LEGACY", "legacy reject")),
        )
        candidate = self.registered("001", ref_a, commit_a)
        plan = self.integration_plan((candidate,), "legacy-journal")
        context = self.context_with_raw_merge(plan, candidate)
        Path(candidate.journal_path).write_bytes(
            harness_train.canonical_json(
                {
                    "schema_version": "harness-lite.train-journal/v0",
                    "kind": "candidate-register",
                    "status": "complete",
                    "candidate_ref": candidate.candidate_ref,
                    "candidate_commit": candidate.candidate_commit,
                    "candidate_tree": candidate.candidate_tree,
                }
            )
            + b"\n"
        )

        with self.assertRaisesRegex(GovernanceAdapterError, "public gate blocked"):
            build_governance_callback(
                plan, readme_authority=self.readme_authority()
            ).preview(context)

    def test_adapter_rejects_evidence_ref_and_candidate_ref_drift(self) -> None:
        ref_a, commit_a = self.make_feature(
            "001",
            self.feature_a,
            progress_bytes=progress(legacy_event(), event("EV-001-TAMPER", "tamper reject")),
        )
        candidate = self.registered("001", ref_a, commit_a)
        plan = self.integration_plan((candidate,), "public-ref-tamper")
        context = self.context_with_raw_merge(plan, candidate)
        callback = build_governance_callback(plan, readme_authority=self.readme_authority())
        tampered_blob = self.git(
            "hash-object", "-w", "--stdin", input_text="{\"tampered\":true}\n"
        ).stdout.strip()

        self.git("update-ref", candidate.candidate_evidence_ref, tampered_blob)
        with self.assertRaisesRegex(GovernanceAdapterError, "public gate blocked"):
            callback.preview(context)

        self.git(
            "update-ref",
            candidate.candidate_evidence_ref,
            candidate.candidate_evidence_blob,
            tampered_blob,
        )
        self.git("update-ref", candidate.candidate_ref, self.base, candidate.candidate_commit)
        with self.assertRaisesRegex(GovernanceAdapterError, "public gate blocked"):
            callback.preview(context)

    def test_callback_resumes_after_normalization_crash_with_exact_tree_set(self) -> None:
        event_a, event_b, plan, context = self.union_case("resume-normalization")

        def crash(phase: str) -> None:
            if phase == "after-normalization":
                raise InjectedGovernanceCrash(phase)

        with self.assertRaisesRegex(InjectedGovernanceCrash, "after-normalization"):
            build_governance_callback(
                plan,
                readme_authority=self.readme_authority(),
                failpoint=crash,
            )(context)

        state = inspect_governance_resume(
            plan,
            context,
            readme_authority=self.readme_authority(),
        )
        self.assertTrue(state.resumable, state.blockers)
        self.assertEqual(state.completed_steps, ("normalization",))
        self.assertEqual(state.next_step.startswith("candidate-0000-001-"), True)
        self.assertIn(state.actual_index_tree, state.allowed_intermediate_trees)

        receipt = resume_governance_callback(
            plan,
            context,
            readme_authority=self.readme_authority(),
        )
        replay = resume_governance_callback(
            plan,
            context,
            readme_authority=self.readme_authority(),
        )
        self.assertEqual(replay, receipt)
        self.assertEqual(receipt.result_tree, self.git("write-tree", cwd=Path(plan.worktree_path)).stdout.strip())
        merged = (Path(plan.worktree_path) / "harness/progress.md").read_bytes()
        self.assertEqual(merged.count(event_a), 1)
        self.assertEqual(merged.count(event_b), 1)

    def test_callback_resumes_after_first_train_event_without_duplicate_event(self) -> None:
        event_a, event_b, plan, context = self.union_case("resume-first-train-event")

        def crash(phase: str) -> None:
            if phase.startswith("after-reconcile:train-event-001-integration_started-"):
                raise InjectedGovernanceCrash(phase)

        with self.assertRaisesRegex(InjectedGovernanceCrash, "integration_started"):
            build_governance_callback(
                plan,
                readme_authority=self.readme_authority(),
                failpoint=crash,
            )(context)

        partial = (Path(plan.worktree_path) / "harness/progress.md").read_bytes()
        self.assertEqual(partial.count(event_a), 1)
        self.assertEqual(partial.count(event_b), 1)
        started_summary = f"Integration {plan.operation_id} started for PRD-001".encode()
        self.assertEqual(partial.count(started_summary), 1)
        state = inspect_governance_resume(
            plan,
            context,
            readme_authority=self.readme_authority(),
        )
        self.assertTrue(state.resumable, state.blockers)
        self.assertEqual(len(state.completed_steps), 4)
        self.assertEqual(state.completed_steps[0], "normalization")
        self.assertTrue(state.completed_steps[1].startswith("candidate-0000-001-"))
        self.assertTrue(state.completed_steps[2].startswith("candidate-0001-002-"))
        self.assertTrue(state.completed_steps[3].startswith("train-event-001-integration_started-"))
        self.assertTrue(state.next_step.startswith("train-event-001-integration_verified-"))
        self.assertIn(state.actual_index_tree, state.allowed_intermediate_trees)

        receipt = resume_governance_callback(
            plan,
            context,
            readme_authority=self.readme_authority(),
        )
        replay = resume_governance_callback(
            plan,
            context,
            readme_authority=self.readme_authority(),
        )
        self.assertEqual(replay, receipt)
        self.assertEqual(receipt.result_tree, self.git("write-tree", cwd=Path(plan.worktree_path)).stdout.strip())
        merged = (Path(plan.worktree_path) / "harness/progress.md").read_bytes()
        self.assertEqual(merged.count(event_a), 1)
        self.assertEqual(merged.count(event_b), 1)
        self.assertEqual(merged.count(started_summary), 1)

    def test_same_event_identity_with_different_bytes_blocks_before_mutation(self) -> None:
        event_a = event("EV-COLLISION", "A bytes")
        event_b = event("EV-COLLISION", "B bytes")
        ref_a, commit_a = self.make_feature("001", self.feature_a, progress_bytes=progress(legacy_event(), event_a))
        ref_b, commit_b = self.make_feature("002", self.feature_b, progress_bytes=progress(legacy_event(), event_b))
        candidate_a = self.registered("001", ref_a, commit_a)
        candidate_b = self.registered("002", ref_b, commit_b)
        plan = self.integration_plan((candidate_a, candidate_b), "collision")
        context = self.context_with_raw_merge(plan, candidate_a)
        before_tree = context.pre_governance_tree

        preview = build_governance_callback(plan, readme_authority=self.readme_authority()).preview(context)

        self.assertFalse(preview.ready)
        self.assertIn("progress-same-id-different-bytes", {item.code for item in preview.blockers})
        self.assertEqual(self.git("write-tree", cwd=Path(plan.worktree_path)).stdout.strip(), before_tree)

    def test_principle_diff_without_exact_approval_and_lease_blocks_before_mutation(self) -> None:
        changed_principle = OWNER + b"# Principle\n\n- stable\n- candidate proposal\n"
        ref_a, commit_a = self.make_feature(
            "001",
            self.feature_a,
            progress_bytes=progress(legacy_event(), event("EV-001-A", "A fact")),
            principle=changed_principle,
        )
        candidate_a = self.registered("001", ref_a, commit_a)
        plan = self.integration_plan((candidate_a,), "principle")
        context = self.context_with_raw_merge(plan, candidate_a)
        before_tree = context.pre_governance_tree

        preview = build_governance_callback(plan, readme_authority=self.readme_authority()).preview(context)

        codes = {item.code for item in preview.blockers}
        self.assertIn("principle-approval-required", codes)
        self.assertEqual(self.git("write-tree", cwd=Path(plan.worktree_path)).stdout.strip(), before_tree)

        exact_approval = PrincipleApproval(
            change_id="PCHANGE-001",
            evidence_ref="EV-PRINCIPLE-APPROVAL",
            exact_before=OWNER + b"# Principle\n\n- stable\n",
            exact_after=changed_principle,
        )
        lease_preview = build_governance_callback(
            plan,
            readme_authority=self.readme_authority(),
            principle_approvals={candidate_a.candidate_evidence.evidence_digest: exact_approval},
        ).preview(context)
        self.assertIn("global-principle-lease-required", {item.code for item in lease_preview.blockers})
        self.assertEqual(self.git("write-tree", cwd=Path(plan.worktree_path)).stdout.strip(), before_tree)

    def test_normalization_apply_rejects_wrong_digest_without_writes(self) -> None:
        ref_a, commit_a = self.make_feature(
            "001",
            self.feature_a,
            progress_bytes=progress(legacy_event(), event("EV-001-A", "A fact")),
        )
        candidate_a = self.registered("001", ref_a, commit_a)
        plan = self.integration_plan((candidate_a,), "digest")
        context = self.context_with_raw_merge(plan, candidate_a)
        adapter = build_governance_callback(plan, readme_authority=self.readme_authority())
        normalization = adapter.preview(context).normalization
        before_tree = self.git("write-tree", cwd=Path(plan.worktree_path)).stdout.strip()

        with self.assertRaisesRegex(GovernanceAdapterError, "accepted normalization digest"):
            apply_premerge_normalization(normalization, accepted_plan_digest="0" * 64)

        self.assertEqual(self.git("write-tree", cwd=Path(plan.worktree_path)).stdout.strip(), before_tree)

        result = apply_premerge_normalization(
            normalization,
            accepted_plan_digest=normalization.plan_digest,
        )
        journal_path = Path(result.journal_path)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["plan_digest"] = "f" * 64
        journal_path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        with self.assertRaisesRegex(GovernanceAdapterError, "journal differs"):
            apply_premerge_normalization(
                normalization,
                accepted_plan_digest=normalization.plan_digest,
            )

    def test_governance_only_git_conflict_has_explicit_train_hook_plan(self) -> None:
        ref_a, commit_a = self.make_feature(
            "001",
            self.feature_a,
            progress_bytes=progress(legacy_event(), event("EV-001-A", "A fact")),
        )
        ref_b, commit_b = self.make_feature(
            "002",
            self.feature_b,
            progress_bytes=progress(legacy_event(), event("EV-002-B", "B fact")),
        )
        candidate_a = self.registered("001", ref_a, commit_a)
        candidate_b = self.registered("002", ref_b, commit_b)
        plan = self.integration_plan((candidate_a, candidate_b), "conflict-hook")
        worktree = Path(plan.worktree_path)
        self.git("worktree", "add", "--detach", str(worktree), plan.target_main)

        merged = self.git(
            "merge",
            "--no-ff",
            "--no-commit",
            candidate_a.candidate_ref,
            candidate_b.candidate_ref,
            cwd=worktree,
            check=False,
        )
        self.assertNotEqual(merged.returncode, 0)
        normalization = plan_premerge_normalization(plan, phase="merge-conflict")

        self.assertTrue(normalization.ready)
        self.assertTrue(normalization.requires_train_conflict_hook)
        self.assertTrue(all(path.startswith("harness/") for path in normalization.unmerged_paths))
        self.assertEqual(
            normalization.requires_train_conflict_hook,
            bool(normalization.unmerged_paths),
        )
        result = apply_premerge_normalization(
            normalization,
            accepted_plan_digest=normalization.plan_digest,
        )
        self.assertEqual(result.phase, "APPLIED")
        self.assertEqual(self.git("diff", "--name-only", "--diff-filter=U", cwd=worktree).stdout, "")

    def test_train_consumes_governance_conflict_hook_then_semantic_callback(self) -> None:
        event_a = event("EV-001-A", "A fact")
        event_b = event("EV-002-B", "B fact")
        ref_a, commit_a = self.make_feature(
            "001", self.feature_a, progress_bytes=progress(legacy_event(), event_a)
        )
        ref_b, commit_b = self.make_feature(
            "002", self.feature_b, progress_bytes=progress(legacy_event(), event_b)
        )
        candidate_a = self.registered("001", ref_a, commit_a)
        candidate_b = self.registered("002", ref_b, commit_b)
        plan = self.integration_plan((candidate_a, candidate_b), "conflict-e2e")
        notifications = []

        result = apply_prepare_integration(
            plan,
            accepted_plan_digest=plan.plan_digest,
            confirmation_token=self.confirmation(plan),
            notify=notifications.append,
            governance_callback=build_governance_callback(
                plan, readme_authority=self.readme_authority()
            ),
            governance_conflict_normalizer=build_conflict_normalizer(plan),
        )

        self.assertTrue(result.ready_for_commit, result.blockers)
        merged = (Path(result.worktree_path) / "harness/progress.md").read_bytes()
        self.assertEqual(merged.count(event_a), 1)
        self.assertEqual(merged.count(event_b), 1)
        self.assertEqual([item.phase for item in notifications], ["before", "after"])

    def test_train_resumes_durable_partial_governance_without_duplicate_events(self) -> None:
        event_a = event("EV-001-RESUME", "A resume fact")
        event_b = event("EV-002-RESUME", "B resume fact", parent="EV-001-RESUME")
        ref_a, commit_a = self.make_feature(
            "001", self.feature_a, progress_bytes=progress(legacy_event(), event_a)
        )
        ref_b, commit_b = self.make_feature(
            "002",
            self.feature_b,
            progress_bytes=progress(legacy_event(), event_a, event_b),
        )
        candidate_a = self.registered("001", ref_a, commit_a)
        candidate_b = self.registered("002", ref_b, commit_b)
        plan = self.integration_plan((candidate_a, candidate_b), "train-resume")
        notifications = []

        def crash(phase: str) -> None:
            if phase == "after-first-reconcile":
                raise InjectedGovernanceCrash(phase)

        with self.assertRaisesRegex(InjectedGovernanceCrash, "after-first-reconcile"):
            apply_prepare_integration(
                plan,
                accepted_plan_digest=plan.plan_digest,
                confirmation_token=self.confirmation(plan),
                notify=notifications.append,
                governance_callback=build_governance_callback(
                    plan,
                    readme_authority=self.readme_authority(),
                    failpoint=crash,
                ),
                governance_conflict_normalizer=build_conflict_normalizer(plan),
            )

        partial = (Path(plan.worktree_path) / "harness/progress.md").read_bytes()
        self.assertEqual(partial.count(event_a), 1)
        self.assertEqual(partial.count(event_b), 0)
        recovered = apply_prepare_integration(
            plan,
            accepted_plan_digest=plan.plan_digest,
            confirmation_token=self.confirmation(plan),
            notify=notifications.append,
            governance_callback=build_governance_callback(
                plan,
                readme_authority=self.readme_authority(),
            ),
            governance_conflict_normalizer=build_conflict_normalizer(plan),
        )

        self.assertTrue(recovered.ready_for_commit, recovered.blockers)
        merged = (Path(plan.worktree_path) / "harness/progress.md").read_bytes()
        self.assertEqual(merged.count(event_a), 1)
        self.assertEqual(merged.count(event_b), 1)
        self.assertEqual([item.phase for item in notifications], ["before", "after"])


if __name__ == "__main__":
    unittest.main()
