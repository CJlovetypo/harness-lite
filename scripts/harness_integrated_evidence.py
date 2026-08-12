#!/usr/bin/env python3
"""Public, Git-backed registry for verified Harness integrated candidates.

The merge-train adapter creates an integration commit and an in-memory
``IntegratedCandidate``.  This module turns that public result into durable,
canonical evidence without advancing main or consulting the train's private
operation journal.  Planning is read-only.  Applying a reviewed plan writes a
metadata blob and creates only evidence refs in one compare-and-swap ref
transaction.

The public refs and their canonical blob are the authority.  The small local
journal exists only to make the write sequence recoverable; loaders and gates
never need it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Mapping, Sequence
import uuid

try:
    from . import harness_train as train
    from .harness_candidate import IntegratedCandidate, integrated_evidence_gate as core_evidence_gate
except ImportError:  # pragma: no cover - direct execution
    import harness_train as train
    from harness_candidate import IntegratedCandidate, integrated_evidence_gate as core_evidence_gate


EVIDENCE_SCHEMA = "harness-lite.integrated-evidence/v1"
CANDIDATE_BINDING_SCHEMA = "harness-lite.integrated-candidate-binding/v1"
PROGRESS_BINDING_SCHEMA = "harness-lite.integrated-progress-binding/v1"
PLAN_SCHEMA = "harness-lite.integrated-evidence-register-plan/v1"
REGISTRATION_SCHEMA = "harness-lite.integrated-evidence-registration/v1"
JOURNAL_SCHEMA = "harness-lite.integrated-evidence-journal/v1"

REF_ROOT = "refs/project-harness/v2"
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 2 * 1024 * 1024

OID_RE = re.compile(r"[0-9a-f]{40,64}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
GENERATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
OPERATION_RE = re.compile(r"OP-[0-9a-f]{32}")
AUTHORIZATION_RE = re.compile(r"AUTH-[A-Za-z0-9][A-Za-z0-9._:-]{2,199}")
EVENT_ID_RE = re.compile(r"EV-[A-Za-z0-9][A-Za-z0-9._-]*")


class IntegratedEvidenceError(RuntimeError):
    """Raised when integrated evidence cannot be proven safe or authentic."""


class InjectedCrash(RuntimeError):
    """Focused recovery-test hook raised after a durable registry stage."""


@dataclass(frozen=True)
class CandidateBinding:
    schema_version: str
    iteration: str
    generation: str
    candidate_ref: str
    candidate_commit: str
    candidate_tree: str
    candidate_evidence_ref: str
    candidate_evidence_blob: str
    candidate_evidence_digest: str
    candidate_evidence_metadata_digest: str
    registration_digest: str
    dependency_bindings: tuple[train.DependencyCandidateBinding, ...]
    dependency_bindings_digest: str
    principle_gate_binding: train.PrincipleGateBinding
    binding_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProgressBinding:
    schema_version: str
    event_id: str
    ref_name: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class IterationEvidenceRef:
    iteration: str
    generation: str
    ref_name: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class IntegratedEvidenceEnvelope:
    schema_version: str
    operation_id: str
    canonical_operation: str
    generation: str
    main_ref: str
    target_main: str
    commit_ref: str
    evidence_ref: str
    iteration_evidence_refs: tuple[IterationEvidenceRef, ...]
    integrated_commit: str
    integrated_tree: str
    parent_commits: tuple[str, ...]
    commit_message: str
    merge_strategy: str
    strategy_declaration_digest: str | None
    principle_sha256: str
    integrated_candidate: IntegratedCandidate
    integrated_candidate_digest: str
    candidate_bindings: tuple[CandidateBinding, ...]
    governance_receipt: train.GovernanceReceipt
    verification_receipts: tuple[train.VerificationReceipt, ...]
    commit_plan_snapshot: Mapping[str, object]
    commit_plan_digest: str
    commit_result_schema_version: str
    commit_result_digest: str
    commit_confirmation_authorization_id: str
    commit_confirmation_token_digest: str
    progress_bindings: tuple[ProgressBinding, ...]
    metadata_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IntegratedEvidencePlan:
    schema_version: str
    operation_id: str
    project_root: str
    git_common_dir: str
    commit_ref: str
    evidence_ref: str
    iteration_evidence_refs: tuple[IterationEvidenceRef, ...]
    metadata_blob: str
    metadata: IntegratedEvidenceEnvelope
    journal_path: str
    plan_digest: str
    blockers: tuple[train.Blocker, ...]
    requires_confirmation: bool = True
    pushed: bool = False

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredIntegratedEvidence:
    schema_version: str
    operation_id: str
    project_root: str
    commit_ref: str
    evidence_ref: str
    evidence_blob: str
    iteration_evidence_refs: tuple[IterationEvidenceRef, ...]
    metadata: IntegratedEvidenceEnvelope
    registration_digest: str
    journal_path: str
    idempotent: bool
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


Failpoint = Callable[[str], None]


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _validate_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value.strip().lower()) is None:
        raise IntegratedEvidenceError(f"{label} must be a SHA-256 digest")
    return value.strip().lower()


def _validate_oid(value: str, label: str) -> str:
    if not isinstance(value, str) or OID_RE.fullmatch(value.strip().lower()) is None:
        raise IntegratedEvidenceError(f"{label} must be a full Git object ID")
    return value.strip().lower()


def _validate_operation(value: str) -> str:
    if not isinstance(value, str) or OPERATION_RE.fullmatch(value.strip()) is None:
        raise IntegratedEvidenceError("operation_id must be OP- followed by 32 lowercase hexadecimal characters")
    return value.strip()


def _validate_iteration(value: str) -> str:
    if not isinstance(value, str):
        raise IntegratedEvidenceError("iteration must be a canonical NNN identity")
    number = value.strip()
    if (
        ITERATION_RE.fullmatch(number) is None
        or number != f"{int(number):03d}"
        or int(number) < 1
    ):
        raise IntegratedEvidenceError("iteration must be a canonical NNN identity")
    return number


def _validate_generation(value: str) -> str:
    if not isinstance(value, str):
        raise IntegratedEvidenceError("generation is invalid")
    generation = value.strip().lower()
    if GENERATION_RE.fullmatch(generation) is None:
        raise IntegratedEvidenceError("generation is invalid")
    return generation


def _validate_ref(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise IntegratedEvidenceError(f"{label} must be an explicit full ref")
    ref = value.strip()
    if not ref.startswith("refs/") or any(char.isspace() or ord(char) < 32 for char in ref):
        raise IntegratedEvidenceError(f"{label} must be an explicit full ref")
    if any(char in ref for char in "~^:?*[\\") or "@{" in ref or ".." in ref:
        raise IntegratedEvidenceError(f"{label} is malformed")
    if ref.endswith(("/", ".")) or any(
        part in {"", ".", ".."} or part.endswith(".lock") for part in ref.split("/")
    ):
        raise IntegratedEvidenceError(f"{label} is malformed")
    return ref


def canonical_operation(operation_id: str) -> str:
    return _validate_operation(operation_id).lower()


def operation_commit_ref(operation_id: str) -> str:
    return f"{REF_ROOT}/integrations/{canonical_operation(operation_id)}/commit"


def operation_evidence_ref(operation_id: str) -> str:
    return f"{REF_ROOT}/integrations/{canonical_operation(operation_id)}/evidence"


def iteration_evidence_ref(iteration: str, generation: str) -> str:
    return (
        f"{REF_ROOT}/iterations/{_validate_iteration(iteration)}/"
        f"integrated-evidence/{_validate_generation(generation)}"
    )


def _git(
    repo: train.Repository,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["PATH"] = repo.git_exec_path + os.pathsep + environment.get("PATH", "")
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    ):
        environment.pop(name, None)
    result = subprocess.run(
        [repo.git, "-C", str(repo.root), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise IntegratedEvidenceError(
            f"git {' '.join(arguments)} failed: {detail or 'unknown Git error'}"
        )
    return result


def _resolve_ref(repo: train.Repository, reference: str) -> str | None:
    result = _git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", reference],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return _validate_oid(result.stdout.decode("ascii", errors="strict").strip(), reference)
    except (UnicodeDecodeError, IntegratedEvidenceError):
        return None


def _object_type(repo: train.Repository, oid: str) -> str | None:
    result = _git(repo, ["cat-file", "-t", oid], check=False)
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError:
        return None


def _blob_bytes(repo: train.Repository, oid: str) -> bytes:
    if _object_type(repo, oid) != "blob":
        raise IntegratedEvidenceError(f"object is not a blob: {oid}")
    raw = _git(repo, ["cat-file", "blob", oid]).stdout
    if len(raw) > MAX_METADATA_BYTES:
        raise IntegratedEvidenceError("integrated evidence metadata exceeds the size limit")
    return raw


def _commit_tree(repo: train.Repository, commit: str) -> str:
    result = _git(repo, ["show", "-s", "--format=%T", commit])
    return _validate_oid(result.stdout.decode("ascii", errors="strict").strip(), "commit tree")


def _commit_parents(repo: train.Repository, commit: str) -> tuple[str, ...]:
    result = _git(repo, ["rev-list", "--parents", "-n", "1", commit])
    values = result.stdout.decode("ascii", errors="strict").strip().split()
    if not values or values[0] != commit:
        raise IntegratedEvidenceError("Git returned malformed integration commit parents")
    return tuple(_validate_oid(item, "integration parent") for item in values[1:])


def _commit_message(repo: train.Repository, commit: str) -> str:
    result = _git(repo, ["log", "-1", "--format=%B", commit])
    return result.stdout.decode("utf-8", errors="strict").rstrip("\r\n")


def _is_ancestor(repo: train.Repository, ancestor: str, descendant: str) -> bool:
    result = _git(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False)
    return result.returncode == 0


def _metadata_payload(envelope: IntegratedEvidenceEnvelope) -> dict[str, object]:
    payload = envelope.as_dict()
    payload.pop("metadata_digest", None)
    return payload


def metadata_digest(envelope: IntegratedEvidenceEnvelope) -> str:
    return digest(_metadata_payload(envelope))


def metadata_bytes(envelope: IntegratedEvidenceEnvelope) -> bytes:
    return canonical_json(envelope.as_dict()) + b"\n"


def _candidate_binding_payload(binding: CandidateBinding) -> dict[str, object]:
    payload = binding.as_dict()
    payload.pop("binding_digest", None)
    return payload


def candidate_binding_digest(binding: CandidateBinding) -> str:
    return digest(_candidate_binding_payload(binding))


def _candidate_binding(candidate: train.RegisteredCandidate) -> CandidateBinding:
    if not isinstance(candidate, train.RegisteredCandidate):
        raise IntegratedEvidenceError("integration candidates must be RegisteredCandidate receipts")
    principle = candidate.principle_gate_binding
    if not isinstance(principle, train.PrincipleGateBinding):
        raise IntegratedEvidenceError(
            f"PRD-{candidate.iteration}/{candidate.generation} has no public principle gate binding"
        )
    provisional = CandidateBinding(
        schema_version=CANDIDATE_BINDING_SCHEMA,
        iteration=_validate_iteration(candidate.iteration),
        generation=_validate_generation(candidate.generation),
        candidate_ref=_validate_ref(candidate.candidate_ref, "candidate_ref"),
        candidate_commit=_validate_oid(candidate.candidate_commit, "candidate_commit"),
        candidate_tree=_validate_oid(candidate.candidate_tree, "candidate_tree"),
        candidate_evidence_ref=_validate_ref(
            candidate.candidate_evidence_ref,
            "candidate_evidence_ref",
        ),
        candidate_evidence_blob=_validate_oid(
            candidate.candidate_evidence_blob,
            "candidate_evidence_blob",
        ),
        candidate_evidence_digest=_validate_digest(
            candidate.candidate_evidence.evidence_digest,
            "candidate_evidence_digest",
        ),
        candidate_evidence_metadata_digest=_validate_digest(
            candidate.candidate_evidence_metadata_digest,
            "candidate_evidence_metadata_digest",
        ),
        registration_digest=_validate_digest(
            candidate.registration_digest,
            "candidate registration_digest",
        ),
        dependency_bindings=tuple(candidate.dependency_bindings),
        dependency_bindings_digest=_validate_digest(
            candidate.dependency_bindings_digest,
            "candidate dependency_bindings_digest",
        ),
        principle_gate_binding=principle,
        binding_digest="0" * 64,
    )
    return replace(provisional, binding_digest=candidate_binding_digest(provisional))


def _progress_bindings(
    values: Sequence[ProgressBinding | tuple[str, str]],
) -> tuple[ProgressBinding, ...]:
    normalized: list[ProgressBinding] = []
    seen_ids: set[str] = set()
    seen_refs: set[str] = set()
    for raw in values:
        if isinstance(raw, ProgressBinding):
            event_id, ref_name = raw.event_id, raw.ref_name
            if raw.schema_version != PROGRESS_BINDING_SCHEMA:
                raise IntegratedEvidenceError("progress binding schema is unsupported")
        elif isinstance(raw, tuple) and len(raw) == 2:
            event_id, ref_name = raw
        else:
            raise IntegratedEvidenceError("progress bindings must be ProgressBinding or (event_id, ref_name)")
        if not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id.strip()) is None:
            raise IntegratedEvidenceError("progress event_id must be a stable EV-* identity")
        event = event_id.strip()
        reference = _validate_ref(str(ref_name), "progress ref_name")
        if not reference.startswith(f"{REF_ROOT}/"):
            raise IntegratedEvidenceError("progress ref_name must stay in the Harness v2 namespace")
        if event in seen_ids or reference in seen_refs:
            raise IntegratedEvidenceError("progress bindings must have unique event IDs and refs")
        seen_ids.add(event)
        seen_refs.add(reference)
        normalized.append(
            ProgressBinding(
                schema_version=PROGRESS_BINDING_SCHEMA,
                event_id=event,
                ref_name=reference,
            )
        )
    return tuple(normalized)


def integration_commit_result_digest(result: train.IntegrationCommitResult) -> str:
    """Return the stable, path-independent identity bound by registry v1."""

    if not isinstance(result, train.IntegrationCommitResult):
        raise IntegratedEvidenceError("result must be IntegrationCommitResult")
    integrated_digest = (
        result.integrated_candidate.evidence_digest
        if result.integrated_candidate is not None
        else None
    )
    return digest(
        {
            "schema_version": result.schema_version,
            "operation_id": result.operation_id,
            "generation": result.generation,
            "integrated_commit": result.integrated_commit,
            "integrated_tree": result.integrated_tree,
            "commit_plan_digest": result.commit_plan.commit_plan_digest,
            "integrated_candidate_digest": integrated_digest,
            "blockers": [item.as_dict() for item in result.blockers],
        }
    )


def _envelope_result_digest(envelope: IntegratedEvidenceEnvelope) -> str:
    return digest(
        {
            "schema_version": envelope.commit_result_schema_version,
            "operation_id": envelope.operation_id,
            "generation": envelope.generation,
            "integrated_commit": envelope.integrated_commit,
            "integrated_tree": envelope.integrated_tree,
            "commit_plan_digest": envelope.commit_plan_digest,
            "integrated_candidate_digest": envelope.integrated_candidate_digest,
            "blockers": [],
        }
    )


def _plan_payload(plan: IntegratedEvidencePlan) -> dict[str, object]:
    payload = plan.as_dict()
    payload.pop("plan_digest", None)
    return payload


def integrated_evidence_plan_digest(plan: IntegratedEvidencePlan) -> str:
    return digest(_plan_payload(plan))


def _registration_payload(receipt: RegisteredIntegratedEvidence) -> dict[str, object]:
    payload = receipt.as_dict()
    payload.pop("registration_digest", None)
    payload.pop("journal_path", None)
    payload.pop("idempotent", None)
    return payload


def registered_integrated_evidence_digest(receipt: RegisteredIntegratedEvidence) -> str:
    return digest(_registration_payload(receipt))


def _journal_path(repo: train.Repository, operation_id: str) -> Path:
    return (
        repo.common_dir
        / "project-harness"
        / "v2"
        / "integrated-evidence"
        / f"{canonical_operation(operation_id)}.json"
    )


def journal_path(project_root: str | Path, operation_id: str) -> Path:
    """Return the deterministic recovery journal path for diagnostics/tests."""

    return _journal_path(train.open_repository(project_root), operation_id)


def _hash_blob(repo: train.Repository, raw: bytes, *, write: bool) -> str:
    arguments = ["hash-object"]
    if write:
        arguments.append("-w")
    arguments.append("--stdin")
    result = _git(repo, arguments, input_bytes=raw)
    return _validate_oid(result.stdout.decode("ascii", errors="strict").strip(), "metadata blob")


def _live_candidate_blockers(
    repo: train.Repository,
    binding: CandidateBinding,
    *,
    principle_sha256: str,
) -> tuple[train.Blocker, ...]:
    blockers: list[train.Blocker] = []
    if binding.schema_version != CANDIDATE_BINDING_SCHEMA:
        blockers.append(train.Blocker("integrated-candidate-binding-schema", binding.iteration))
    if binding.binding_digest != candidate_binding_digest(binding):
        blockers.append(train.Blocker("integrated-candidate-binding-digest", binding.iteration))
        return tuple(blockers)
    if _resolve_ref(repo, binding.candidate_ref) != binding.candidate_commit:
        blockers.append(train.Blocker("integrated-candidate-ref-drift", binding.candidate_ref))
    elif _object_type(repo, binding.candidate_commit) != "commit":
        blockers.append(train.Blocker("integrated-candidate-object-type", binding.candidate_ref))
    if _resolve_ref(repo, binding.candidate_evidence_ref) != binding.candidate_evidence_blob:
        blockers.append(
            train.Blocker("integrated-candidate-evidence-ref-drift", binding.candidate_evidence_ref)
        )
    elif _object_type(repo, binding.candidate_evidence_blob) != "blob":
        blockers.append(
            train.Blocker("integrated-candidate-evidence-object-type", binding.candidate_evidence_ref)
        )
    try:
        loaded, candidate_blockers = train.load_registered_candidate(
            repo.root,
            iteration=binding.iteration,
            generation=binding.generation,
            current_principle_sha256=principle_sha256,
        )
    except train.TrainError as exc:
        blockers.append(train.Blocker("integrated-candidate-public-load", str(exc)))
        return tuple(blockers)
    blockers.extend(candidate_blockers)
    if loaded is None:
        if not candidate_blockers:
            blockers.append(
                train.Blocker("integrated-candidate-public-load", binding.candidate_ref)
            )
        return tuple(blockers)
    try:
        observed = _candidate_binding(loaded)
    except IntegratedEvidenceError as exc:
        blockers.append(train.Blocker("integrated-candidate-public-binding", str(exc)))
    else:
        if observed != binding:
            blockers.append(
                train.Blocker(
                    "integrated-candidate-public-identity",
                    f"PRD-{binding.iteration}/{binding.generation}",
                )
            )
    return tuple(blockers)


def _verification_blockers(
    envelope: IntegratedEvidenceEnvelope,
) -> tuple[train.Blocker, ...]:
    blockers: list[train.Blocker] = []
    if not envelope.verification_receipts:
        blockers.append(
            train.Blocker("integrated-verification-receipt-missing", "no integration verification receipt")
        )
        return tuple(blockers)
    evidence_ids: list[str] = []
    for receipt in envelope.verification_receipts:
        if not isinstance(receipt, train.VerificationReceipt):
            blockers.append(
                train.Blocker("integrated-verification-receipt-type", "receipt is not structured")
            )
            continue
        evidence_ids.append(receipt.evidence_id)
        if receipt.schema_version != train.VERIFICATION_RECEIPT_SCHEMA:
            blockers.append(
                train.Blocker("integrated-verification-receipt-schema", receipt.evidence_id)
            )
        if receipt.exit_code != 0:
            blockers.append(
                train.Blocker("integrated-verification-receipt-failed", receipt.evidence_id)
            )
        if not receipt.argv:
            blockers.append(
                train.Blocker("integrated-verification-command-missing", receipt.evidence_id)
            )
        for label, supplied in (
            ("stdout", receipt.stdout_sha256),
            ("stderr", receipt.stderr_sha256),
        ):
            if DIGEST_RE.fullmatch(supplied) is None:
                blockers.append(
                    train.Blocker(
                        "integrated-verification-output-digest",
                        f"{receipt.evidence_id}/{label}",
                    )
                )
    if len(set(evidence_ids)) != len(evidence_ids):
        blockers.append(
            train.Blocker("integrated-verification-receipt-duplicate", "evidence IDs repeat")
        )
    if tuple(evidence_ids) != envelope.integrated_candidate.cross_prd_verification_ids:
        blockers.append(
            train.Blocker(
                "integrated-verification-core-mismatch",
                "real receipts differ from core integrated evidence IDs",
            )
        )
    return tuple(blockers)


def _commit_plan_snapshot_blockers(
    envelope: IntegratedEvidenceEnvelope,
) -> tuple[train.Blocker, ...]:
    blockers: list[train.Blocker] = []
    snapshot = envelope.commit_plan_snapshot
    if not isinstance(snapshot, Mapping):
        return (
            train.Blocker("integrated-commit-plan-snapshot", "commit plan snapshot is not an object"),
        )
    copied = dict(snapshot)
    supplied = copied.pop("commit_plan_digest", None)
    if supplied != envelope.commit_plan_digest or train.digest(copied) != envelope.commit_plan_digest:
        blockers.append(
            train.Blocker("integrated-commit-plan-digest", "public commit plan snapshot was changed")
        )
    expected_scalars = {
        "schema_version": train.COMMIT_PLAN_SCHEMA,
        "operation_id": envelope.operation_id,
        "generation": envelope.generation,
        "main_ref": envelope.main_ref,
        "target_main": envelope.target_main,
        "integrated_tree": envelope.integrated_tree,
        "parent_commits": list(envelope.parent_commits),
        "principle_sha256": envelope.principle_sha256,
        "merge_strategy": envelope.merge_strategy,
        "strategy_declaration_digest": envelope.strategy_declaration_digest,
        "governance_receipt": envelope.governance_receipt.as_dict(),
        "verification_receipts": [item.as_dict() for item in envelope.verification_receipts],
        "commit_message": envelope.commit_message,
    }
    for field, expected in expected_scalars.items():
        observed = snapshot.get(field)
        # asdict preserves tuples in memory while JSON parsing produces lists.
        if canonical_json(observed) != canonical_json(expected):
            blockers.append(
                train.Blocker(
                    "integrated-commit-plan-snapshot-field",
                    f"commit plan {field} differs",
                )
            )
    raw_candidates = snapshot.get("candidates")
    if not isinstance(raw_candidates, (list, tuple)) or len(raw_candidates) != len(
        envelope.candidate_bindings
    ):
        blockers.append(
            train.Blocker("integrated-commit-plan-candidates", "commit plan candidate count differs")
        )
    else:
        for raw, binding in zip(raw_candidates, envelope.candidate_bindings, strict=True):
            if not isinstance(raw, Mapping):
                blockers.append(
                    train.Blocker("integrated-commit-plan-candidate", binding.iteration)
                )
                continue
            expected = {
                "iteration": binding.iteration,
                "generation": binding.generation,
                "candidate_ref": binding.candidate_ref,
                "candidate_commit": binding.candidate_commit,
                "candidate_tree": binding.candidate_tree,
                "candidate_evidence_ref": binding.candidate_evidence_ref,
                "candidate_evidence_blob": binding.candidate_evidence_blob,
                "candidate_evidence_metadata_digest": binding.candidate_evidence_metadata_digest,
                "registration_digest": binding.registration_digest,
                "dependency_bindings": [item.as_dict() for item in binding.dependency_bindings],
                "dependency_bindings_digest": binding.dependency_bindings_digest,
                "principle_gate_binding": binding.principle_gate_binding.as_dict(),
            }
            for field, value in expected.items():
                if canonical_json(raw.get(field)) != canonical_json(value):
                    blockers.append(
                        train.Blocker(
                            "integrated-commit-plan-candidate-field",
                            f"PRD-{binding.iteration}/{binding.generation}/{field}",
                        )
                    )
    return tuple(blockers)


def _envelope_structural_blockers(
    envelope: IntegratedEvidenceEnvelope,
) -> tuple[train.Blocker, ...]:
    blockers: list[train.Blocker] = []
    if envelope.schema_version != EVIDENCE_SCHEMA:
        blockers.append(train.Blocker("integrated-evidence-schema", "metadata schema is unsupported"))
    if envelope.metadata_digest != metadata_digest(envelope):
        blockers.append(train.Blocker("integrated-evidence-metadata-digest", "metadata was changed"))
        return tuple(blockers)
    try:
        expected_operation = canonical_operation(envelope.operation_id)
    except IntegratedEvidenceError as exc:
        blockers.append(train.Blocker("integrated-evidence-operation", str(exc)))
        return tuple(blockers)
    if envelope.canonical_operation != expected_operation:
        blockers.append(train.Blocker("integrated-evidence-canonical-operation", envelope.operation_id))
    if envelope.commit_ref != operation_commit_ref(envelope.operation_id):
        blockers.append(train.Blocker("integrated-evidence-commit-ref-name", envelope.commit_ref))
    if envelope.evidence_ref != operation_evidence_ref(envelope.operation_id):
        blockers.append(train.Blocker("integrated-evidence-ref-name", envelope.evidence_ref))
    expected_iteration_refs = tuple(
        IterationEvidenceRef(
            iteration=item.iteration,
            generation=envelope.generation,
            ref_name=iteration_evidence_ref(item.iteration, envelope.generation),
        )
        for item in envelope.candidate_bindings
    )
    if envelope.iteration_evidence_refs != expected_iteration_refs:
        blockers.append(
            train.Blocker("integrated-evidence-iteration-refs", "per-iteration refs are not canonical")
        )
    if len({item.iteration for item in envelope.candidate_bindings}) != len(
        envelope.candidate_bindings
    ):
        blockers.append(train.Blocker("integrated-evidence-candidate-duplicate", "PRD repeats"))
    for binding in envelope.candidate_bindings:
        if binding.binding_digest != candidate_binding_digest(binding):
            blockers.append(
                train.Blocker("integrated-candidate-binding-digest", binding.iteration)
            )
    core = envelope.integrated_candidate
    decision = core_evidence_gate(core)
    if not decision.allowed:
        blockers.extend(
            train.Blocker("integrated-core-evidence", reason) for reason in decision.blockers
        )
    expected_core = {
        "generation": envelope.generation,
        "target_main": envelope.target_main,
        "integrated_commit": envelope.integrated_commit,
        "integrated_tree": envelope.integrated_tree,
        "principle_sha256": envelope.principle_sha256,
        "merge_strategy": envelope.merge_strategy,
        "strategy_declaration_digest": envelope.strategy_declaration_digest,
        "candidate_digests": tuple(
            item.candidate_evidence_digest for item in envelope.candidate_bindings
        ),
        "dependency_order": tuple(item.iteration for item in envelope.candidate_bindings),
        "governance_evidence_digest": envelope.governance_receipt.evidence_digest,
    }
    for field, expected in expected_core.items():
        if getattr(core, field) != expected:
            blockers.append(
                train.Blocker("integrated-core-binding-mismatch", field)
            )
    if envelope.integrated_candidate_digest != core.evidence_digest:
        blockers.append(
            train.Blocker("integrated-core-digest-binding", "core digest field differs")
        )
    context = train.GovernanceContext(
        schema_version=train.GOVERNANCE_RECEIPT_SCHEMA,
        operation_id=envelope.operation_id,
        project_root=str(envelope.commit_plan_snapshot.get("project_root", "")),
        integration_worktree=str(
            envelope.commit_plan_snapshot.get("integration_worktree", "")
        ),
        target_main=envelope.target_main,
        principle_sha256=envelope.principle_sha256,
        candidate_digests=tuple(item.candidate_evidence_digest for item in envelope.candidate_bindings),
        pre_governance_tree=envelope.governance_receipt.input_tree,
    )
    blockers.extend(
        train.governance_receipt_gate(
            envelope.governance_receipt,
            context,
            actual_result_tree=envelope.integrated_tree,
        )
    )
    blockers.extend(_verification_blockers(envelope))
    blockers.extend(_commit_plan_snapshot_blockers(envelope))
    if envelope.commit_plan_digest != envelope.commit_plan_snapshot.get("commit_plan_digest"):
        blockers.append(train.Blocker("integrated-commit-plan-binding", "digest differs"))
    if envelope.commit_result_schema_version != train.COMMIT_RESULT_SCHEMA:
        blockers.append(train.Blocker("integrated-commit-result-schema", "result schema differs"))
    if envelope.commit_result_digest != _envelope_result_digest(envelope):
        blockers.append(train.Blocker("integrated-commit-result-digest", "result digest differs"))
    if AUTHORIZATION_RE.fullmatch(envelope.commit_confirmation_authorization_id) is None:
        blockers.append(
            train.Blocker("integrated-commit-confirmation-identity", "authorization ID is invalid")
        )
    expected_token_digest = train.confirmation_token_digest(
        "create-integration-commit",
        envelope.commit_plan_digest,
        envelope.commit_confirmation_authorization_id,
    )
    if envelope.commit_confirmation_token_digest != expected_token_digest:
        blockers.append(
            train.Blocker("integrated-commit-confirmation-digest", "confirmation identity differs")
        )
    seen_progress_ids: set[str] = set()
    seen_progress_refs: set[str] = set()
    for binding in envelope.progress_bindings:
        if (
            binding.schema_version != PROGRESS_BINDING_SCHEMA
            or EVENT_ID_RE.fullmatch(binding.event_id) is None
        ):
            blockers.append(train.Blocker("integrated-progress-binding", binding.event_id))
        try:
            reference = _validate_ref(binding.ref_name, "progress ref_name")
        except IntegratedEvidenceError as exc:
            blockers.append(train.Blocker("integrated-progress-ref", str(exc)))
        else:
            if not reference.startswith(f"{REF_ROOT}/"):
                blockers.append(train.Blocker("integrated-progress-ref-namespace", reference))
        if binding.event_id in seen_progress_ids or binding.ref_name in seen_progress_refs:
            blockers.append(train.Blocker("integrated-progress-binding-duplicate", binding.event_id))
        seen_progress_ids.add(binding.event_id)
        seen_progress_refs.add(binding.ref_name)
    return tuple(dict.fromkeys(blockers))


def _live_envelope_blockers(
    repo: train.Repository,
    envelope: IntegratedEvidenceEnvelope,
    *,
    require_main_unchanged: bool,
) -> tuple[train.Blocker, ...]:
    blockers = list(_envelope_structural_blockers(envelope))
    if _object_type(repo, envelope.target_main) != "commit":
        blockers.append(train.Blocker("integrated-target-main-object", envelope.target_main))
    if require_main_unchanged and _resolve_ref(repo, envelope.main_ref) != envelope.target_main:
        blockers.append(train.Blocker("integrated-target-main-drift", envelope.main_ref))
    if _object_type(repo, envelope.integrated_commit) != "commit":
        blockers.append(train.Blocker("integrated-commit-object-type", envelope.integrated_commit))
        return tuple(dict.fromkeys(blockers))
    try:
        if _commit_tree(repo, envelope.integrated_commit) != envelope.integrated_tree:
            blockers.append(train.Blocker("integrated-commit-tree-drift", envelope.integrated_commit))
        if _commit_parents(repo, envelope.integrated_commit) != envelope.parent_commits:
            blockers.append(train.Blocker("integrated-commit-parents-drift", envelope.integrated_commit))
        if _commit_message(repo, envelope.integrated_commit) != envelope.commit_message:
            blockers.append(train.Blocker("integrated-commit-message-drift", envelope.integrated_commit))
    except IntegratedEvidenceError as exc:
        blockers.append(train.Blocker("integrated-commit-unreadable", str(exc)))
    if not envelope.parent_commits or envelope.parent_commits[0] != envelope.target_main:
        blockers.append(
            train.Blocker("integrated-target-main-parent", "target main is not the first parent")
        )
    if envelope.merge_strategy == train.DEFAULT_MERGE_STRATEGY:
        for binding in envelope.candidate_bindings:
            if not _is_ancestor(repo, binding.candidate_commit, envelope.integrated_commit):
                blockers.append(
                    train.Blocker(
                        "integrated-candidate-not-preserved",
                        f"PRD-{binding.iteration}/{binding.generation}",
                    )
                )
    for binding in envelope.candidate_bindings:
        blockers.extend(
            _live_candidate_blockers(
                repo,
                binding,
                principle_sha256=envelope.principle_sha256,
            )
        )
    return tuple(dict.fromkeys(blockers))


def plan_register_integrated_evidence(
    result: train.IntegrationCommitResult,
    *,
    commit_confirmation_token: train.ConfirmationToken,
    progress_bindings: Sequence[ProgressBinding | tuple[str, str]] = (),
) -> IntegratedEvidencePlan:
    """Build a zero-write plan for publishing one integrated evidence envelope."""

    if not isinstance(result, train.IntegrationCommitResult):
        raise IntegratedEvidenceError("result must be IntegrationCommitResult")
    if result.schema_version != train.COMMIT_RESULT_SCHEMA:
        raise IntegratedEvidenceError("integration commit result schema is unsupported")
    if not result.evidence_ready or result.integrated_candidate is None:
        raise IntegratedEvidenceError("integration result has no verified IntegratedCandidate")
    repo = train.open_repository(result.project_root)
    operation = _validate_operation(result.operation_id)
    generation = _validate_generation(result.generation)
    plan = result.commit_plan
    if plan.operation_id != operation or plan.generation != generation:
        raise IntegratedEvidenceError("integration result and commit plan identities differ")
    if result.integrated_commit != result.integrated_candidate.integrated_commit:
        raise IntegratedEvidenceError("integration result and core evidence commits differ")
    if result.integrated_tree != result.integrated_candidate.integrated_tree:
        raise IntegratedEvidenceError("integration result and core evidence trees differ")
    if plan.commit_plan_digest != train.integration_commit_plan_digest(plan):
        raise IntegratedEvidenceError("integration commit plan digest is invalid")
    confirmation_blockers = train.confirmation_token_gate(
        commit_confirmation_token,
        action="create-integration-commit",
        subject_digest=plan.commit_plan_digest,
    )
    if confirmation_blockers:
        raise IntegratedEvidenceError(
            "integration commit confirmation is invalid: "
            + "; ".join(item.code for item in confirmation_blockers)
        )
    bindings = tuple(_candidate_binding(item) for item in plan.candidates)
    per_iteration = tuple(
        IterationEvidenceRef(
            iteration=item.iteration,
            generation=generation,
            ref_name=iteration_evidence_ref(item.iteration, generation),
        )
        for item in bindings
    )
    progress = _progress_bindings(progress_bindings)
    commit_ref = operation_commit_ref(operation)
    evidence_ref = operation_evidence_ref(operation)
    provisional_metadata = IntegratedEvidenceEnvelope(
        schema_version=EVIDENCE_SCHEMA,
        operation_id=operation,
        canonical_operation=canonical_operation(operation),
        generation=generation,
        main_ref=_validate_ref(plan.main_ref, "main_ref"),
        target_main=_validate_oid(plan.target_main, "target_main"),
        commit_ref=commit_ref,
        evidence_ref=evidence_ref,
        iteration_evidence_refs=per_iteration,
        integrated_commit=_validate_oid(result.integrated_commit, "integrated_commit"),
        integrated_tree=_validate_oid(result.integrated_tree, "integrated_tree"),
        parent_commits=tuple(_validate_oid(item, "parent commit") for item in plan.parent_commits),
        commit_message=plan.commit_message,
        merge_strategy=plan.merge_strategy,
        strategy_declaration_digest=plan.strategy_declaration_digest,
        principle_sha256=_validate_digest(plan.principle_sha256, "principle_sha256"),
        integrated_candidate=result.integrated_candidate,
        integrated_candidate_digest=_validate_digest(
            result.integrated_candidate.evidence_digest,
            "integrated_candidate_digest",
        ),
        candidate_bindings=bindings,
        governance_receipt=plan.governance_receipt,
        verification_receipts=tuple(plan.verification_receipts),
        # Normalize the snapshot to JSON-native containers at the boundary so
        # an in-memory receipt is exactly equal to the same public blob after
        # loading (dataclass ``asdict`` otherwise preserves nested tuples).
        commit_plan_snapshot=json.loads(canonical_json(plan.as_dict()).decode("utf-8")),
        commit_plan_digest=plan.commit_plan_digest,
        commit_result_schema_version=result.schema_version,
        commit_result_digest=integration_commit_result_digest(result),
        commit_confirmation_authorization_id=commit_confirmation_token.authorization_id,
        commit_confirmation_token_digest=commit_confirmation_token.token_digest,
        progress_bindings=progress,
        metadata_digest="0" * 64,
    )
    metadata = replace(
        provisional_metadata,
        metadata_digest=metadata_digest(provisional_metadata),
    )
    raw = metadata_bytes(metadata)
    if len(raw) > MAX_METADATA_BYTES:
        raise IntegratedEvidenceError("integrated evidence metadata exceeds the size limit")
    metadata_blob = _hash_blob(repo, raw, write=False)
    blockers = list(
        _live_envelope_blockers(repo, metadata, require_main_unchanged=True)
    )
    for reference in (commit_ref, evidence_ref, *(item.ref_name for item in per_iteration)):
        if _resolve_ref(repo, reference) is not None:
            blockers.append(
                train.Blocker("integrated-evidence-ref-exists", reference)
            )
    provisional_plan = IntegratedEvidencePlan(
        schema_version=PLAN_SCHEMA,
        operation_id=operation,
        project_root=str(repo.root),
        git_common_dir=str(repo.common_dir),
        commit_ref=commit_ref,
        evidence_ref=evidence_ref,
        iteration_evidence_refs=per_iteration,
        metadata_blob=metadata_blob,
        metadata=metadata,
        journal_path=str(_journal_path(repo, operation)),
        plan_digest="0" * 64,
        blockers=tuple(dict.fromkeys(blockers)),
    )
    return replace(
        provisional_plan,
        plan_digest=integrated_evidence_plan_digest(provisional_plan),
    )


def _assert_journal_parent(path: Path, common_dir: Path) -> None:
    common = common_dir.resolve()
    try:
        path.resolve(strict=False).relative_to(common)
    except ValueError as exc:
        raise IntegratedEvidenceError("registry journal escapes the Git common directory") from exc
    current = common
    for part in path.parent.relative_to(common).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise IntegratedEvidenceError(f"registry journal path traverses a link: {current}")


def _replace_json(path: Path, value: Mapping[str, object], common_dir: Path) -> None:
    _assert_journal_parent(path, common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_journal_parent(path, common_dir)
    raw = canonical_json(value) + b"\n"
    if len(raw) > MAX_JOURNAL_BYTES:
        raise IntegratedEvidenceError("registry journal exceeds the size limit")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _journal_payload(journal: Mapping[str, object]) -> dict[str, object]:
    payload = dict(journal)
    payload.pop("journal_digest", None)
    return payload


def _journal_with_digest(value: Mapping[str, object]) -> dict[str, object]:
    journal = dict(value)
    journal["journal_digest"] = digest(_journal_payload(journal))
    return journal


def _read_journal(path: Path, common_dir: Path) -> dict[str, object] | None:
    _assert_journal_parent(path, common_dir)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise IntegratedEvidenceError("registry journal is not a regular file")
    raw = path.read_bytes()
    if len(raw) > MAX_JOURNAL_BYTES:
        raise IntegratedEvidenceError("registry journal exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegratedEvidenceError("registry journal is invalid JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) + b"\n" != raw:
        raise IntegratedEvidenceError("registry journal is not canonical JSON")
    if value.get("journal_digest") != digest(_journal_payload(value)):
        raise IntegratedEvidenceError("registry journal digest is invalid")
    return value


def _expected_journal(plan: IntegratedEvidencePlan, *, status: str) -> dict[str, object]:
    return _journal_with_digest(
        {
            "schema_version": JOURNAL_SCHEMA,
            "kind": "integrated-evidence-register",
            "operation_id": plan.operation_id,
            "canonical_operation": plan.metadata.canonical_operation,
            "plan_digest": plan.plan_digest,
            "metadata_digest": plan.metadata.metadata_digest,
            "metadata_blob": plan.metadata_blob,
            "commit_ref": plan.commit_ref,
            "evidence_ref": plan.evidence_ref,
            "iteration_evidence_refs": [item.as_dict() for item in plan.iteration_evidence_refs],
            "commit_confirmation_authorization_id": (
                plan.metadata.commit_confirmation_authorization_id
            ),
            "commit_confirmation_token_digest": plan.metadata.commit_confirmation_token_digest,
            "status": status,
        }
    )


def _validate_existing_journal(
    journal: Mapping[str, object],
    plan: IntegratedEvidencePlan,
) -> str:
    status = journal.get("status")
    if status not in {"planned", "metadata-written", "complete"}:
        raise IntegratedEvidenceError("registry journal status is unsupported")
    expected = _expected_journal(plan, status=str(status))
    if canonical_json(journal) != canonical_json(expected):
        raise IntegratedEvidenceError("old or foreign registry journal does not authorize this plan")
    return str(status)


def _registry_refs_state(
    repo: train.Repository,
    plan: IntegratedEvidencePlan,
) -> str:
    expected = {
        plan.commit_ref: plan.metadata.integrated_commit,
        plan.evidence_ref: plan.metadata_blob,
        **{item.ref_name: plan.metadata_blob for item in plan.iteration_evidence_refs},
    }
    observed = {reference: _resolve_ref(repo, reference) for reference in expected}
    if all(value is None for value in observed.values()):
        return "absent"
    if all(observed[reference] == oid for reference, oid in expected.items()):
        return "exact"
    return "mismatch"


def _apply_ref_transaction(
    repo: train.Repository,
    plan: IntegratedEvidencePlan,
) -> None:
    expected: dict[str, str] = {plan.metadata.main_ref: plan.metadata.target_main}
    for binding in plan.metadata.candidate_bindings:
        for reference, oid in (
            (binding.candidate_ref, binding.candidate_commit),
            (binding.candidate_evidence_ref, binding.candidate_evidence_blob),
        ):
            prior = expected.get(reference)
            if prior is not None and prior != oid:
                raise IntegratedEvidenceError(
                    f"registry transaction has conflicting source identity: {reference}"
                )
            expected[reference] = oid
    lines = ["start"]
    lines.extend(f"verify {reference} {oid}" for reference, oid in expected.items())
    lines.append(f"create {plan.commit_ref} {plan.metadata.integrated_commit}")
    lines.append(f"create {plan.evidence_ref} {plan.metadata_blob}")
    lines.extend(
        f"create {item.ref_name} {plan.metadata_blob}"
        for item in plan.iteration_evidence_refs
    )
    lines.extend(("prepare", "commit", ""))
    result = _git(
        repo,
        ["update-ref", "--stdin"],
        input_bytes="\n".join(lines).encode("ascii"),
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise IntegratedEvidenceError(
            f"integrated evidence ref CAS transaction failed: {detail or 'unknown Git error'}"
        )


def _trigger(failpoint: Failpoint | None, stage: str) -> None:
    if failpoint is not None:
        failpoint(stage)


def _receipt_from_plan(
    plan: IntegratedEvidencePlan,
    *,
    idempotent: bool,
) -> RegisteredIntegratedEvidence:
    provisional = RegisteredIntegratedEvidence(
        schema_version=REGISTRATION_SCHEMA,
        operation_id=plan.operation_id,
        project_root=plan.project_root,
        commit_ref=plan.commit_ref,
        evidence_ref=plan.evidence_ref,
        evidence_blob=plan.metadata_blob,
        iteration_evidence_refs=plan.iteration_evidence_refs,
        metadata=plan.metadata,
        registration_digest="0" * 64,
        journal_path=plan.journal_path,
        idempotent=idempotent,
    )
    return replace(
        provisional,
        registration_digest=registered_integrated_evidence_digest(provisional),
    )


def apply_register_integrated_evidence(
    plan: IntegratedEvidencePlan,
    *,
    accepted_plan_digest: str,
    commit_confirmation_token: train.ConfirmationToken,
    failpoint: Failpoint | None = None,
) -> RegisteredIntegratedEvidence:
    """Publish the exact reviewed evidence refs; never advances main."""

    if not isinstance(plan, IntegratedEvidencePlan):
        raise IntegratedEvidenceError("plan must be IntegratedEvidencePlan")
    if plan.schema_version != PLAN_SCHEMA:
        raise IntegratedEvidenceError("integrated evidence plan schema is unsupported")
    if plan.plan_digest != integrated_evidence_plan_digest(plan):
        raise IntegratedEvidenceError("integrated evidence plan was changed")
    if accepted_plan_digest != plan.plan_digest:
        raise IntegratedEvidenceError("accepted digest differs from integrated evidence plan")
    if plan.blockers:
        raise IntegratedEvidenceError(
            "integrated evidence plan is blocked: "
            + "; ".join(item.code for item in plan.blockers)
        )
    confirmation_blockers = train.confirmation_token_gate(
        commit_confirmation_token,
        action="create-integration-commit",
        subject_digest=plan.metadata.commit_plan_digest,
    )
    if confirmation_blockers:
        raise IntegratedEvidenceError(
            "integration commit confirmation is invalid: "
            + "; ".join(item.code for item in confirmation_blockers)
        )
    if (
        commit_confirmation_token.authorization_id
        != plan.metadata.commit_confirmation_authorization_id
        or commit_confirmation_token.token_digest
        != plan.metadata.commit_confirmation_token_digest
    ):
        raise IntegratedEvidenceError("integration commit confirmation identity differs from the plan")
    repo = train.open_repository(plan.project_root)
    if os.path.normcase(str(repo.common_dir)) != os.path.normcase(plan.git_common_dir):
        raise IntegratedEvidenceError("integrated evidence plan belongs to another Git common directory")
    if str(repo.root) != plan.project_root:
        raise IntegratedEvidenceError("integrated evidence plan belongs to another project root")
    live_blockers = _live_envelope_blockers(
        repo,
        plan.metadata,
        require_main_unchanged=True,
    )
    if live_blockers:
        raise IntegratedEvidenceError(
            "integrated evidence source identity is stale: "
            + "; ".join(item.code for item in live_blockers)
        )
    raw = metadata_bytes(plan.metadata)
    if _hash_blob(repo, raw, write=False) != plan.metadata_blob:
        raise IntegratedEvidenceError("planned metadata blob identity differs")

    path = Path(plan.journal_path)
    existing = _read_journal(path, repo.common_dir)
    if existing is None:
        _replace_json(path, _expected_journal(plan, status="planned"), repo.common_dir)
        status = "planned"
    else:
        status = _validate_existing_journal(existing, plan)
    _trigger(failpoint, "registry-after-journal")

    state = _registry_refs_state(repo, plan)
    if state == "mismatch":
        raise IntegratedEvidenceError("integrated evidence refs are partial or name another identity")
    if state == "exact":
        receipt = _receipt_from_plan(plan, idempotent=True)
        blockers = registered_integrated_evidence_gate(repo.root, receipt)
        if blockers:
            raise IntegratedEvidenceError(
                "existing integrated evidence failed its public gate: "
                + "; ".join(item.code for item in blockers)
            )
        if status != "complete":
            _replace_json(path, _expected_journal(plan, status="complete"), repo.common_dir)
        return receipt
    if status == "complete":
        raise IntegratedEvidenceError("complete registry journal exists but public refs are absent")

    written = _hash_blob(repo, raw, write=True)
    if written != plan.metadata_blob or _blob_bytes(repo, written) != raw:
        raise IntegratedEvidenceError("Git wrote a different integrated evidence metadata blob")
    _replace_json(path, _expected_journal(plan, status="metadata-written"), repo.common_dir)
    _trigger(failpoint, "registry-after-blob")

    # Recheck the volatile sources immediately before the atomic ref CAS.
    live_blockers = _live_envelope_blockers(
        repo,
        plan.metadata,
        require_main_unchanged=True,
    )
    if live_blockers:
        raise IntegratedEvidenceError(
            "integrated evidence source identity changed before ref CAS: "
            + "; ".join(item.code for item in live_blockers)
        )
    _apply_ref_transaction(repo, plan)
    _trigger(failpoint, "registry-after-refs")
    receipt = _receipt_from_plan(plan, idempotent=False)
    blockers = registered_integrated_evidence_gate(repo.root, receipt)
    if blockers:
        raise IntegratedEvidenceError(
            "published integrated evidence failed its public gate: "
            + "; ".join(item.code for item in blockers)
        )
    _replace_json(path, _expected_journal(plan, status="complete"), repo.common_dir)
    return receipt


def _dependency_binding_from_dict(value: object) -> train.DependencyCandidateBinding:
    if not isinstance(value, Mapping):
        raise IntegratedEvidenceError("candidate dependency binding is not an object")
    try:
        return train.DependencyCandidateBinding(**dict(value))
    except (TypeError, ValueError) as exc:
        raise IntegratedEvidenceError("candidate dependency binding is malformed") from exc


def _principle_binding_from_dict(value: object) -> train.PrincipleGateBinding:
    if not isinstance(value, Mapping):
        raise IntegratedEvidenceError("candidate principle binding is not an object")
    try:
        return train.PrincipleGateBinding(**dict(value))
    except (TypeError, ValueError) as exc:
        raise IntegratedEvidenceError("candidate principle binding is malformed") from exc


def _candidate_binding_from_dict(value: object) -> CandidateBinding:
    if not isinstance(value, Mapping):
        raise IntegratedEvidenceError("candidate binding is not an object")
    required = {
        "schema_version",
        "iteration",
        "generation",
        "candidate_ref",
        "candidate_commit",
        "candidate_tree",
        "candidate_evidence_ref",
        "candidate_evidence_blob",
        "candidate_evidence_digest",
        "candidate_evidence_metadata_digest",
        "registration_digest",
        "dependency_bindings",
        "dependency_bindings_digest",
        "principle_gate_binding",
        "binding_digest",
    }
    if set(value) != required:
        raise IntegratedEvidenceError("candidate binding fields are unsupported")
    raw_dependencies = value["dependency_bindings"]
    if not isinstance(raw_dependencies, list):
        raise IntegratedEvidenceError("candidate dependency bindings are not a list")
    return CandidateBinding(
        schema_version=str(value["schema_version"]),
        iteration=str(value["iteration"]),
        generation=str(value["generation"]),
        candidate_ref=str(value["candidate_ref"]),
        candidate_commit=str(value["candidate_commit"]),
        candidate_tree=str(value["candidate_tree"]),
        candidate_evidence_ref=str(value["candidate_evidence_ref"]),
        candidate_evidence_blob=str(value["candidate_evidence_blob"]),
        candidate_evidence_digest=str(value["candidate_evidence_digest"]),
        candidate_evidence_metadata_digest=str(value["candidate_evidence_metadata_digest"]),
        registration_digest=str(value["registration_digest"]),
        dependency_bindings=tuple(
            _dependency_binding_from_dict(item) for item in raw_dependencies
        ),
        dependency_bindings_digest=str(value["dependency_bindings_digest"]),
        principle_gate_binding=_principle_binding_from_dict(value["principle_gate_binding"]),
        binding_digest=str(value["binding_digest"]),
    )


def _integrated_candidate_from_dict(value: object) -> IntegratedCandidate:
    if not isinstance(value, Mapping):
        raise IntegratedEvidenceError("core integrated candidate is not an object")
    required = {
        "schema_version",
        "generation",
        "target_main",
        "integrated_commit",
        "integrated_tree",
        "principle_sha256",
        "merge_strategy",
        "strategy_declaration_digest",
        "candidate_digests",
        "dependency_order",
        "preserved_candidate_commits",
        "identity_rebind_digests",
        "governance_evidence_digest",
        "cross_prd_verification_ids",
        "integration_evidence_ids",
        "evidence_digest",
        "verified",
        "blockers",
        "requires_user_acceptance",
    }
    if set(value) != required:
        raise IntegratedEvidenceError("core integrated candidate fields are unsupported")
    sequence_fields = (
        "candidate_digests",
        "dependency_order",
        "preserved_candidate_commits",
        "identity_rebind_digests",
        "cross_prd_verification_ids",
        "integration_evidence_ids",
        "blockers",
    )
    if any(not isinstance(value[field], list) for field in sequence_fields):
        raise IntegratedEvidenceError("core integrated candidate sequences are malformed")
    try:
        return IntegratedCandidate(
            schema_version=str(value["schema_version"]),
            generation=str(value["generation"]),
            target_main=str(value["target_main"]),
            integrated_commit=str(value["integrated_commit"]),
            integrated_tree=str(value["integrated_tree"]),
            principle_sha256=str(value["principle_sha256"]),
            merge_strategy=str(value["merge_strategy"]),
            strategy_declaration_digest=(
                None
                if value["strategy_declaration_digest"] is None
                else str(value["strategy_declaration_digest"])
            ),
            candidate_digests=tuple(str(item) for item in value["candidate_digests"]),
            dependency_order=tuple(str(item) for item in value["dependency_order"]),
            preserved_candidate_commits=tuple(
                str(item) for item in value["preserved_candidate_commits"]
            ),
            identity_rebind_digests=tuple(
                str(item) for item in value["identity_rebind_digests"]
            ),
            governance_evidence_digest=(
                None
                if value["governance_evidence_digest"] is None
                else str(value["governance_evidence_digest"])
            ),
            cross_prd_verification_ids=tuple(
                str(item) for item in value["cross_prd_verification_ids"]
            ),
            integration_evidence_ids=tuple(
                str(item) for item in value["integration_evidence_ids"]
            ),
            evidence_digest=str(value["evidence_digest"]),
            verified=bool(value["verified"]),
            blockers=tuple(str(item) for item in value["blockers"]),
            requires_user_acceptance=bool(value["requires_user_acceptance"]),
        )
    except (TypeError, ValueError) as exc:
        raise IntegratedEvidenceError("core integrated candidate is malformed") from exc


def _governance_receipt_from_dict(value: object) -> train.GovernanceReceipt:
    if not isinstance(value, Mapping):
        raise IntegratedEvidenceError("governance receipt is not an object")
    payload = dict(value)
    candidate_digests = payload.get("candidate_digests")
    evidence_ids = payload.get("evidence_ids")
    if not isinstance(candidate_digests, list) or not isinstance(evidence_ids, list):
        raise IntegratedEvidenceError("governance receipt sequences are malformed")
    payload["candidate_digests"] = tuple(str(item) for item in candidate_digests)
    payload["evidence_ids"] = tuple(str(item) for item in evidence_ids)
    try:
        return train.GovernanceReceipt(**payload)
    except (TypeError, ValueError) as exc:
        raise IntegratedEvidenceError("governance receipt is malformed") from exc


def _verification_receipt_from_dict(value: object) -> train.VerificationReceipt:
    if not isinstance(value, Mapping):
        raise IntegratedEvidenceError("verification receipt is not an object")
    payload = dict(value)
    argv = payload.get("argv")
    if not isinstance(argv, list):
        raise IntegratedEvidenceError("verification receipt argv is malformed")
    payload["argv"] = tuple(str(item) for item in argv)
    try:
        return train.VerificationReceipt(**payload)
    except (TypeError, ValueError) as exc:
        raise IntegratedEvidenceError("verification receipt is malformed") from exc


def _iteration_ref_from_dict(value: object) -> IterationEvidenceRef:
    if not isinstance(value, Mapping) or set(value) != {"iteration", "generation", "ref_name"}:
        raise IntegratedEvidenceError("iteration evidence ref is malformed")
    return IterationEvidenceRef(
        iteration=str(value["iteration"]),
        generation=str(value["generation"]),
        ref_name=str(value["ref_name"]),
    )


def _progress_binding_from_dict(value: object) -> ProgressBinding:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "event_id",
        "ref_name",
    }:
        raise IntegratedEvidenceError("progress binding is malformed")
    return ProgressBinding(
        schema_version=str(value["schema_version"]),
        event_id=str(value["event_id"]),
        ref_name=str(value["ref_name"]),
    )


def _envelope_from_dict(value: object) -> IntegratedEvidenceEnvelope:
    if not isinstance(value, Mapping):
        raise IntegratedEvidenceError("integrated evidence metadata is not an object")
    required = {field.name for field in IntegratedEvidenceEnvelope.__dataclass_fields__.values()}
    if set(value) != required:
        raise IntegratedEvidenceError("integrated evidence metadata fields are unsupported")
    raw_iteration_refs = value["iteration_evidence_refs"]
    raw_candidates = value["candidate_bindings"]
    raw_verifications = value["verification_receipts"]
    raw_progress = value["progress_bindings"]
    raw_parents = value["parent_commits"]
    snapshot = value["commit_plan_snapshot"]
    if not all(
        isinstance(item, list)
        for item in (
            raw_iteration_refs,
            raw_candidates,
            raw_verifications,
            raw_progress,
            raw_parents,
        )
    ) or not isinstance(snapshot, Mapping):
        raise IntegratedEvidenceError("integrated evidence metadata sequences are malformed")
    return IntegratedEvidenceEnvelope(
        schema_version=str(value["schema_version"]),
        operation_id=str(value["operation_id"]),
        canonical_operation=str(value["canonical_operation"]),
        generation=str(value["generation"]),
        main_ref=str(value["main_ref"]),
        target_main=str(value["target_main"]),
        commit_ref=str(value["commit_ref"]),
        evidence_ref=str(value["evidence_ref"]),
        iteration_evidence_refs=tuple(
            _iteration_ref_from_dict(item) for item in raw_iteration_refs
        ),
        integrated_commit=str(value["integrated_commit"]),
        integrated_tree=str(value["integrated_tree"]),
        parent_commits=tuple(str(item) for item in raw_parents),
        commit_message=str(value["commit_message"]),
        merge_strategy=str(value["merge_strategy"]),
        strategy_declaration_digest=(
            None
            if value["strategy_declaration_digest"] is None
            else str(value["strategy_declaration_digest"])
        ),
        principle_sha256=str(value["principle_sha256"]),
        integrated_candidate=_integrated_candidate_from_dict(value["integrated_candidate"]),
        integrated_candidate_digest=str(value["integrated_candidate_digest"]),
        candidate_bindings=tuple(
            _candidate_binding_from_dict(item) for item in raw_candidates
        ),
        governance_receipt=_governance_receipt_from_dict(value["governance_receipt"]),
        verification_receipts=tuple(
            _verification_receipt_from_dict(item) for item in raw_verifications
        ),
        commit_plan_snapshot=dict(snapshot),
        commit_plan_digest=str(value["commit_plan_digest"]),
        commit_result_schema_version=str(value["commit_result_schema_version"]),
        commit_result_digest=str(value["commit_result_digest"]),
        commit_confirmation_authorization_id=str(
            value["commit_confirmation_authorization_id"]
        ),
        commit_confirmation_token_digest=str(value["commit_confirmation_token_digest"]),
        progress_bindings=tuple(
            _progress_binding_from_dict(item) for item in raw_progress
        ),
        metadata_digest=str(value["metadata_digest"]),
    )


def registered_integrated_evidence_gate(
    project_root: str | Path,
    receipt: RegisteredIntegratedEvidence,
) -> tuple[train.Blocker, ...]:
    """Authenticate one public registry receipt against Git and live candidates."""

    if not isinstance(receipt, RegisteredIntegratedEvidence):
        return (
            train.Blocker("integrated-evidence-registration-type", "receipt is not structured"),
        )
    blockers: list[train.Blocker] = []
    if receipt.schema_version != REGISTRATION_SCHEMA:
        blockers.append(
            train.Blocker("integrated-evidence-registration-schema", "schema is unsupported")
        )
    if receipt.registration_digest != registered_integrated_evidence_digest(receipt):
        blockers.append(
            train.Blocker("integrated-evidence-registration-digest", "receipt was changed")
        )
    repo = train.open_repository(project_root)
    if os.path.normcase(str(repo.root)) != os.path.normcase(receipt.project_root):
        blockers.append(
            train.Blocker("integrated-evidence-project-root", "receipt belongs to another project")
        )
        return tuple(blockers)
    envelope = receipt.metadata
    if receipt.operation_id != envelope.operation_id:
        blockers.append(train.Blocker("integrated-evidence-operation-binding", receipt.operation_id))
    if receipt.commit_ref != envelope.commit_ref:
        blockers.append(train.Blocker("integrated-evidence-commit-ref-binding", receipt.commit_ref))
    if receipt.evidence_ref != envelope.evidence_ref:
        blockers.append(train.Blocker("integrated-evidence-ref-binding", receipt.evidence_ref))
    if receipt.iteration_evidence_refs != envelope.iteration_evidence_refs:
        blockers.append(
            train.Blocker("integrated-evidence-iteration-ref-binding", "receipt differs")
        )
    if _resolve_ref(repo, receipt.commit_ref) != envelope.integrated_commit:
        blockers.append(train.Blocker("integrated-evidence-commit-ref-drift", receipt.commit_ref))
    elif _object_type(repo, envelope.integrated_commit) != "commit":
        blockers.append(train.Blocker("integrated-evidence-commit-ref-type", receipt.commit_ref))
    if _resolve_ref(repo, receipt.evidence_ref) != receipt.evidence_blob:
        blockers.append(train.Blocker("integrated-evidence-ref-drift", receipt.evidence_ref))
    elif _object_type(repo, receipt.evidence_blob) != "blob":
        blockers.append(train.Blocker("integrated-evidence-ref-type", receipt.evidence_ref))
    for item in receipt.iteration_evidence_refs:
        if _resolve_ref(repo, item.ref_name) != receipt.evidence_blob:
            blockers.append(train.Blocker("integrated-evidence-iteration-ref-drift", item.ref_name))
        elif _object_type(repo, receipt.evidence_blob) != "blob":
            blockers.append(train.Blocker("integrated-evidence-iteration-ref-type", item.ref_name))
    try:
        raw = _blob_bytes(repo, receipt.evidence_blob)
    except IntegratedEvidenceError as exc:
        blockers.append(train.Blocker("integrated-evidence-blob-unreadable", str(exc)))
    else:
        if raw != metadata_bytes(envelope):
            blockers.append(
                train.Blocker("integrated-evidence-blob-content", "blob differs from receipt metadata")
            )
    blockers.extend(
        _live_envelope_blockers(repo, envelope, require_main_unchanged=False)
    )
    return tuple(dict.fromkeys(blockers))


def load_registered_integrated_evidence(
    project_root: str | Path,
    *,
    operation_id: str,
) -> tuple[RegisteredIntegratedEvidence | None, tuple[train.Blocker, ...]]:
    """Load one canonical public integrated-evidence ref pair, fail closed."""

    repo = train.open_repository(project_root)
    operation = _validate_operation(operation_id)
    commit_ref = operation_commit_ref(operation)
    evidence_ref = operation_evidence_ref(operation)
    commit_oid = _resolve_ref(repo, commit_ref)
    evidence_oid = _resolve_ref(repo, evidence_ref)
    if commit_oid is None and evidence_oid is None:
        return None, (
            train.Blocker("integrated-evidence-missing", operation),
        )
    if commit_oid is None or evidence_oid is None:
        return None, (
            train.Blocker("integrated-evidence-partial-refs", operation),
        )
    if _object_type(repo, commit_oid) != "commit" or _object_type(repo, evidence_oid) != "blob":
        return None, (
            train.Blocker("integrated-evidence-ref-object-type", operation),
        )
    try:
        raw = _blob_bytes(repo, evidence_oid)
        value = json.loads(raw.decode("utf-8"))
        if canonical_json(value) + b"\n" != raw:
            raise IntegratedEvidenceError("integrated evidence blob is not canonical JSON")
        envelope = _envelope_from_dict(value)
    except (UnicodeDecodeError, json.JSONDecodeError, IntegratedEvidenceError) as exc:
        return None, (
            train.Blocker("integrated-evidence-metadata-invalid", str(exc)),
        )
    if envelope.operation_id != operation:
        return None, (
            train.Blocker("integrated-evidence-operation-mismatch", operation),
        )
    provisional = RegisteredIntegratedEvidence(
        schema_version=REGISTRATION_SCHEMA,
        operation_id=operation,
        project_root=str(repo.root),
        commit_ref=commit_ref,
        evidence_ref=evidence_ref,
        evidence_blob=evidence_oid,
        iteration_evidence_refs=envelope.iteration_evidence_refs,
        metadata=envelope,
        registration_digest="0" * 64,
        journal_path=str(_journal_path(repo, operation)),
        idempotent=True,
    )
    receipt = replace(
        provisional,
        registration_digest=registered_integrated_evidence_digest(provisional),
    )
    blockers = registered_integrated_evidence_gate(repo.root, receipt)
    return (receipt if not blockers else None), blockers


__all__ = [
    "CANDIDATE_BINDING_SCHEMA",
    "EVIDENCE_SCHEMA",
    "InjectedCrash",
    "IntegratedEvidenceEnvelope",
    "IntegratedEvidenceError",
    "IntegratedEvidencePlan",
    "IterationEvidenceRef",
    "JOURNAL_SCHEMA",
    "PLAN_SCHEMA",
    "PROGRESS_BINDING_SCHEMA",
    "ProgressBinding",
    "REGISTRATION_SCHEMA",
    "RegisteredIntegratedEvidence",
    "apply_register_integrated_evidence",
    "candidate_binding_digest",
    "canonical_operation",
    "integrated_evidence_plan_digest",
    "integration_commit_result_digest",
    "iteration_evidence_ref",
    "journal_path",
    "load_registered_integrated_evidence",
    "metadata_digest",
    "operation_commit_ref",
    "operation_evidence_ref",
    "plan_register_integrated_evidence",
    "registered_integrated_evidence_digest",
    "registered_integrated_evidence_gate",
]
