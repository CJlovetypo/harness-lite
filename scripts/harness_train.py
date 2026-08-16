#!/usr/bin/env python3
"""Git adapter for Harness Lite candidate refs and the local merge train.

The adapter deliberately has no CLI entry point yet.  Every mutation is
digest-bound and journaled, commits require a structured confirmation token,
and advancing main requires a second, independent token plus acceptance of the
exact integrated evidence digest.  It never pushes, stashes, resets, cleans,
forces, amends, rebases, or repairs product conflicts.

Governance reconciliation is an injected boundary in this slice.  A callback
must return a tamper-evident receipt bound to the exact staged tree.  A
preview-only receipt is retained as evidence but blocks the integration commit;
this module does not pretend that the current preview reconciler can apply it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import base64
import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, Literal, Mapping, Sequence
import uuid

try:
    from . import harness_workspace as workspace
except ImportError:  # pragma: no cover - direct execution
    import harness_workspace as workspace
try:
    from . import harness_progress as progress
except ImportError:  # pragma: no cover - direct execution
    import harness_progress as progress
try:
    from . import harness_principle_audit as principle_audit
except ImportError:  # pragma: no cover - direct execution
    import harness_principle_audit as principle_audit
try:
    from . import project_harness as governance_core
except ImportError:  # pragma: no cover - direct execution
    import project_harness as governance_core
try:
    from .harness_candidate import (
        AcceptanceEvidence,
        CandidateEvidence,
        CandidateInput,
        IdentityRebindEvidence,
        IntegratedCandidate,
        IntegrationInput,
        build_candidate,
        build_integrated_candidate,
        candidate_evidence_gate,
        candidate_freshness_gate,
        identity_rebind_evidence_gate,
        integrated_evidence_gate,
        main_advance_gate,
    )
except ImportError:  # pragma: no cover - direct execution
    from harness_candidate import (
        AcceptanceEvidence,
        CandidateEvidence,
        CandidateInput,
        IdentityRebindEvidence,
        IntegratedCandidate,
        IntegrationInput,
        build_candidate,
        build_integrated_candidate,
        candidate_evidence_gate,
        candidate_freshness_gate,
        identity_rebind_evidence_gate,
        integrated_evidence_gate,
        main_advance_gate,
    )
try:
    from .harness_ux import ActionFacts, InteractionEnvelope, interaction
except ImportError:  # pragma: no cover - direct execution
    from harness_ux import ActionFacts, InteractionEnvelope, interaction

if TYPE_CHECKING:
    try:
        from .harness_integrated_evidence import RegisteredIntegratedEvidence
    except ImportError:  # pragma: no cover - direct execution
        from harness_integrated_evidence import RegisteredIntegratedEvidence


TRAIN_SCHEMA = "harness-lite.train/v1"
AUTHORITY_SCHEMA = "harness-lite.authority-receipt/v1"
REGISTER_PLAN_SCHEMA = "harness-lite.candidate-register-plan/v1"
SEAL_PLAN_SCHEMA = "harness-lite.candidate-seal-plan/v1"
REGISTER_RESULT_SCHEMA = "harness-lite.candidate-registration/v2"
CANDIDATE_VERIFICATION_RECEIPT_SCHEMA = "harness-lite.candidate-verification-receipt/v1"
CANDIDATE_EVIDENCE_METADATA_SCHEMA = "harness-lite.candidate-evidence-metadata/v2"
PREPARE_PLAN_SCHEMA = "harness-lite.integration-prepare-plan/v1"
GOVERNANCE_RECEIPT_SCHEMA = "harness-lite.governance-apply-receipt/v1"
VERIFICATION_RECEIPT_SCHEMA = "harness-lite.verification-receipt/v1"
COMMIT_PLAN_SCHEMA = "harness-lite.integration-commit-plan/v1"
COMMIT_RESULT_SCHEMA = "harness-lite.integration-commit-result/v1"
ADVANCE_PLAN_SCHEMA = "harness-lite.main-advance-plan/v2"
ADVANCE_RESULT_SCHEMA = "harness-lite.main-advance-result/v1"
CLEANUP_PLAN_SCHEMA = "harness-lite.integration-cleanup-plan/v1"
CLEANUP_RESULT_SCHEMA = "harness-lite.integration-cleanup-result/v1"
CONFIRM_TOKEN_SCHEMA = "harness-lite.confirm-token/v1"
JOURNAL_SCHEMA = "harness-lite.train-journal/v1"
LEASE_SCHEMA = "harness-lite.main-integration-lease/v1"
WORKSPACE_GUARD_SCHEMA = "harness-lite.workspace-guard-receipt/v3"
PRINCIPLE_GATE_BINDING_SCHEMA = "harness-lite.principle-gate-binding/v1"

OID_RE = re.compile(r"[0-9a-f]{40,64}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
GENERATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
OPERATION_RE = re.compile(r"OP-[0-9a-f]{32}")
AUTHORIZATION_RE = re.compile(r"AUTH-[A-Za-z0-9][A-Za-z0-9._:-]{2,199}")
FIELD_RE = re.compile(
    r"^\s*-\s*(?P<label>[^：:\r\n]+?)\s*[：:]\s*(?P<value>.*?)\s*$",
    re.MULTILINE,
)
HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")

MAX_AUTHORITY_FILE = 2 * 1024 * 1024
MAX_JOURNAL_FILE = 4 * 1024 * 1024
MAX_COMMAND_OUTPUT = 8 * 1024 * 1024

_CANDIDATE_LOAD_STACK: contextvars.ContextVar[tuple[tuple[str, str], ...]] = (
    contextvars.ContextVar("harness_train_candidate_load_stack", default=())
)

DEFAULT_MAIN_REF = "refs/heads/main"
DEFAULT_PRINCIPLE_PATH = "harness/principle.md"
DEFAULT_PROGRESS_PATH = "harness/progress.md"
DEFAULT_MERGE_STRATEGY = "merge-no-ff"
SUPPORTED_ADAPTER_STRATEGIES = frozenset({"merge-no-ff", "squash"})


class TrainError(RuntimeError):
    """Raised when the adapter cannot prove a mutation safe."""


class InjectedCrash(RuntimeError):
    """Test/recovery hook; callers may inject it after a durable stage."""


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Repository:
    git: str
    git_exec_path: str
    root: Path
    common_dir: Path


@dataclass
class AuthorityValidationContext:
    """One immutable authority snapshot shared by a single read derivation.

    The context is process-local and cannot be supplied by a caller.  Cached
    values are scoped to the exact Git-ref and operational-registry snapshot
    captured on entry.  The context manager rechecks both snapshots before a
    successful exit, so a concurrent mutation fails the whole derivation
    instead of allowing a cached receipt to escape as current authority.
    """

    repo: Repository
    refs: dict[str, str]
    ref_object_types: dict[str, str]
    refs_snapshot: bytes
    operational_snapshot: tuple[tuple[str, str, int, str], ...]
    snapshot_digest: str
    candidate_cache: dict[
        tuple[str, str, str, str],
        tuple["RegisteredCandidate | None", tuple[Blocker, ...]],
    ] = field(default_factory=dict)
    object_type_cache: dict[str, str | None] = field(default_factory=dict)
    commit_tree_cache: dict[str, str] = field(default_factory=dict)

    def assert_unchanged(self) -> None:
        refs, _objects, raw = _authority_ref_snapshot(self.repo)
        if raw != self.refs_snapshot or refs != self.refs:
            raise TrainError(
                "authority validation snapshot drifted while deriving: Git refs changed"
            )
        operational = _authority_operational_snapshot(self.repo)
        if operational != self.operational_snapshot:
            raise TrainError(
                "authority validation snapshot drifted while deriving: operational evidence changed"
            )


_AUTHORITY_VALIDATION_CONTEXT: contextvars.ContextVar[
    AuthorityValidationContext | None
] = contextvars.ContextVar("harness_train_authority_validation_context", default=None)


@dataclass(frozen=True)
class AuthorityReceipt:
    schema_version: str
    iteration: str
    authority_commit: str
    prd_path: str
    prd_blob: str
    prd_sha256: str
    prd_status: str
    prd_approval_source_sha256: str
    spec_path: str
    spec_blob: str
    spec_sha256: str
    spec_status: str
    spec_approval_source_sha256: str
    implementation_authorization_source_sha256: str
    deviation_path: str
    deviation_blob: str
    deviation_sha256: str
    deviation_resolved: bool
    depends_on: tuple[str, ...]
    acceptance_ids: tuple[str, ...]
    evidence_digest: str
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DependencyCandidateBinding:
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
    registry_digest: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PrincipleGateBinding:
    schema_version: str
    iteration: str
    authority_ref: str
    mode: Literal["no-drift", "audit-receipt"]
    allocation_principle_sha256: str
    current_principle_sha256: str
    drift: bool
    disposition: str | None
    audit_generation: int | None
    audit_receipt_digest: str | None
    audit_supersedes: str | None
    audit_operation_id: str | None
    audit_plan_digest: str | None
    binding_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceGuardReceipt:
    schema_version: str
    iteration: str
    owner: str
    generation: int
    operation_id: str
    accepted_plan_digest: str
    worktree_path: str
    branch_ref: str
    base_commit: str
    implementation_ref: str
    implementation_commit: str
    reconciliation_ref: str
    reconciliation_commit: str
    dependency_refresh_generation: int
    dependency_bindings: tuple[DependencyCandidateBinding, ...]
    dependency_bindings_digest: str
    lease_digest: str
    guard_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateRegistrationPlan:
    schema_version: str
    operation_id: str
    project_root: str
    git_common_dir: str
    iteration: str
    generation: str
    feature_ref: str
    feature_worktree: str
    base_ref: str
    main_ref: str
    main_commit: str
    candidate_ref: str
    candidate_evidence_ref: str
    candidate_commit: str
    candidate_tree: str
    principle_path: str
    principle_blob: str
    principle_sha256: str
    principle_gate_binding: PrincipleGateBinding | None
    authority: AuthorityReceipt
    workspace_guard: WorkspaceGuardReceipt
    dependency_bindings: tuple[DependencyCandidateBinding, ...]
    dependency_bindings_digest: str
    candidate: CandidateEvidence
    verify_commands: tuple[VerifyCommand, ...]
    deprecated_verification_ids: tuple[str, ...]
    expected_candidate_ref: str | None
    expected_candidate_evidence_ref: str | None
    plan_digest: str
    blockers: tuple[Blocker, ...]
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredCandidate:
    schema_version: str
    operation_id: str
    project_root: str
    iteration: str
    generation: str
    candidate_ref: str
    candidate_evidence_ref: str
    candidate_evidence_blob: str
    candidate_evidence_metadata_digest: str
    pre_seal_commit: str
    pre_seal_tree: str
    candidate_commit: str
    candidate_tree: str
    base_ref: str
    base_commit: str
    implementation_commit: str
    principle_sha256: str
    principle_gate_binding: PrincipleGateBinding
    authority_evidence_digest: str
    workspace_guard: WorkspaceGuardReceipt
    workspace_guard_digest: str
    depends_on: tuple[str, ...]
    dependency_bindings: tuple[DependencyCandidateBinding, ...]
    dependency_bindings_digest: str
    candidate_evidence: CandidateEvidence
    verification_receipts: tuple[CandidateVerificationReceipt, ...]
    seal_authorization_id: str
    registration_plan_digest: str
    seal_plan_digest: str
    registration_digest: str
    journal_path: str
    idempotent: bool
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ConfirmationToken:
    schema_version: str
    action: str
    subject_digest: str
    authorization_id: str
    token_digest: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class VerifyCommand:
    evidence_id: str
    argv: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateVerificationReceipt:
    schema_version: str
    phase: Literal["pre-seal", "seal"]
    evidence_id: str
    candidate_commit: str
    candidate_tree: str
    argv: tuple[str, ...]
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    receipt_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateSealPlan:
    schema_version: str
    registration_plan: CandidateRegistrationPlan
    principle_gate_binding: PrincipleGateBinding
    dependency_bindings: tuple[DependencyCandidateBinding, ...]
    dependency_bindings_digest: str
    progress_path: str
    progress_blob: str
    progress_before_sha256: str
    progress_after_sha256: str
    progress_event: progress.ProgressEventV2
    progress_event_bytes_b64: str
    seal_tree: str
    parent_commits: tuple[str, ...]
    commit_message: str
    commit_bytes_b64: str
    seal_commit: str
    pre_seal_verification_receipts: tuple[CandidateVerificationReceipt, ...]
    seal_plan_digest: str
    blockers: tuple[Blocker, ...]
    requires_confirmation: bool = True
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["progress_event"] = self.progress_event.as_dict()
        return result


@dataclass(frozen=True)
class Notification:
    phase: Literal["before", "after"]
    action: str
    operation_id: str
    generation: str
    path: str
    base_ref: str
    base_commit: str
    candidate_refs: tuple[str, ...]
    iteration: str | None = None
    project_root: str | None = None
    branch_ref: str | None = None
    reason: str | None = None
    remote_involved: bool = False
    force: bool = False
    pushed: bool = False
    affected_prds: tuple[str, ...] = ()
    runtime_namespace: str | None = None
    effect_on_existing_prds: tuple[str, ...] = ()
    source_preserved: bool | None = None
    actual_head: str | None = None
    next_gate: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def interaction_envelope(self) -> InteractionEnvelope:
        return interaction(
            ActionFacts(
                action="create-worktree",
                phase=self.phase,
                iteration=self.iteration,
                operation_id=self.operation_id,
                project_root=self.project_root,
                base_commit=self.base_commit,
                branch_ref=self.branch_ref,
                worktree_path=self.path,
                source_ref=self.base_ref,
                force=self.force,
                pushed=self.pushed,
                reason=self.reason,
                affected_prds=self.affected_prds,
                runtime_namespace=self.runtime_namespace,
                effect_on_existing_prds=self.effect_on_existing_prds,
                remote_involved=self.remote_involved,
                source_preserved=self.source_preserved,
                actual_head=self.actual_head,
                next_gate=self.next_gate,
            )
        )


@dataclass(frozen=True)
class IntegrationPreparePlan:
    schema_version: str
    operation_id: str
    project_root: str
    git_common_dir: str
    generation: str
    main_ref: str
    target_main: str
    principle_path: str
    principle_blob: str
    principle_sha256: str
    candidates: tuple[RegisteredCandidate, ...]
    dependency_order: tuple[str, ...]
    expected_merge_heads: tuple[str, ...]
    merge_strategy: str
    strategy_declaration_digest: str | None
    verify_commands: tuple[VerifyCommand, ...]
    worktree_path: str
    commit_message: str
    plan_digest: str
    blockers: tuple[Blocker, ...]
    governance_apply_connected: bool = False
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GovernanceContext:
    schema_version: str
    operation_id: str
    project_root: str
    integration_worktree: str
    target_main: str
    principle_sha256: str
    candidate_digests: tuple[str, ...]
    pre_governance_tree: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GovernanceReceipt:
    schema_version: str
    operation_id: str
    mode: Literal["preview", "applied"]
    target_main: str
    principle_sha256: str
    candidate_digests: tuple[str, ...]
    input_tree: str
    result_tree: str
    evidence_ids: tuple[str, ...]
    evidence_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationReceipt:
    schema_version: str
    evidence_id: str
    argv: tuple[str, ...]
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IntegrationCommitPlan:
    schema_version: str
    operation_id: str
    project_root: str
    integration_worktree: str
    generation: str
    main_ref: str
    target_main: str
    integrated_tree: str
    parent_commits: tuple[str, ...]
    candidates: tuple[RegisteredCandidate, ...]
    dependency_order: tuple[str, ...]
    principle_sha256: str
    merge_strategy: str
    strategy_declaration_digest: str | None
    governance_receipt: GovernanceReceipt
    verification_receipts: tuple[VerificationReceipt, ...]
    commit_message: str
    prepare_plan_digest: str
    commit_plan_digest: str
    requires_confirmation: bool = True
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IntegrationPreparationResult:
    schema_version: str
    operation_id: str
    status: str
    worktree_path: str
    commit_plan: IntegrationCommitPlan | None
    blockers: tuple[Blocker, ...]
    journal_path: str
    notifications: tuple[InteractionEnvelope, ...]
    governance_apply_connected: bool
    idempotent: bool = False
    pushed: bool = False

    @property
    def ready_for_commit(self) -> bool:
        return self.status == "prepared" and self.commit_plan is not None and not self.blockers

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IntegrationCommitResult:
    schema_version: str
    operation_id: str
    project_root: str
    integration_worktree: str
    generation: str
    integrated_commit: str
    integrated_tree: str
    commit_plan: IntegrationCommitPlan
    commit_confirmation_token: ConfirmationToken
    integrated_candidate: IntegratedCandidate | None
    blockers: tuple[Blocker, ...]
    journal_path: str
    idempotent: bool
    pushed: bool = False

    @property
    def evidence_ready(self) -> bool:
        return self.integrated_candidate is not None and not self.blockers

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MainAdvancePlan:
    schema_version: str
    operation_id: str
    project_root: str
    git_common_dir: str
    integration_worktree: str
    main_ref: str
    expected_main: str
    integrated_commit: str
    integrated_tree: str
    integrated_evidence_digest: str
    integrated_evidence_metadata_digest: str
    integrated_evidence_blob: str
    operation_commit_ref: str
    operation_evidence_ref: str
    iteration_evidence_refs: tuple[tuple[str, str], ...]
    principle_path: str
    principle_sha256: str
    candidate_refs: tuple[tuple[str, str], ...]
    source_ref_bindings: tuple[tuple[str, str], ...]
    ref_updates: tuple[tuple[str, str | None, str], ...]
    integration_commit_result_digest: str
    local_main_release_receipts: tuple[tuple[str, str, int, str], ...]
    plan_digest: str
    blockers: tuple[Blocker, ...]
    requires_confirmation: bool = True
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MainAdvanceResult:
    schema_version: str
    operation_id: str
    project_root: str
    integration_worktree: str
    main_ref: str
    previous_main: str
    current_main: str
    updated_refs: tuple[str, ...]
    journal_path: str
    cleanup_worktree: str
    idempotent: bool
    final_acceptance_digest: str | None = None
    final_acceptance_evidence_blob: str | None = None
    final_acceptance_evidence_ref: str | None = None
    final_acceptance_iteration_evidence_refs: tuple[str, ...] = ()
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IntegrationCleanupPlan:
    schema_version: str
    operation_id: str
    project_root: str
    git_common_dir: str
    integration_worktree: str
    integrated_commit: str
    affected_prds: tuple[str, ...]
    main_advance_journal: str
    main_advance_journal_sha256: str
    plan_digest: str
    blockers: tuple[Blocker, ...]
    requires_notification: bool = True
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IntegrationCleanupResult:
    schema_version: str
    operation_id: str
    integration_worktree: str
    removed: bool
    journal_path: str
    notifications: tuple[InteractionEnvelope, ...]
    idempotent: bool
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


Failpoint = Callable[[str], None]
Notify = Callable[[InteractionEnvelope], None]
GovernanceCallback = Callable[[GovernanceContext], GovernanceReceipt]
GovernanceConflictNormalizer = Callable[["IntegrationPreparePlan"], object]


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def new_operation_id() -> str:
    return f"OP-{uuid.uuid4().hex}"


def _validate_operation(value: str) -> str:
    candidate = value.strip()
    if not OPERATION_RE.fullmatch(candidate):
        raise TrainError("operation_id must be OP- followed by 32 lowercase hexadecimal characters")
    return candidate


def _validate_iteration(value: str) -> str:
    candidate = value.strip()
    if not ITERATION_RE.fullmatch(candidate) or candidate != f"{int(candidate):03d}" or int(candidate) < 1:
        raise TrainError("iteration must be a canonical zero-padded identifier such as 001")
    return candidate


def _validate_generation(value: str) -> str:
    if not isinstance(value, str):
        raise TrainError("generation must be a canonical string")
    candidate = value.strip().lower()
    if not GENERATION_RE.fullmatch(candidate):
        raise TrainError("generation is invalid")
    return candidate


def _validate_oid(value: str, label: str) -> str:
    candidate = value.strip().lower()
    if not OID_RE.fullmatch(candidate):
        raise TrainError(f"{label} is not a full Git object ID")
    return candidate


def _validate_digest(value: str, label: str) -> str:
    candidate = value.strip().lower()
    if not DIGEST_RE.fullmatch(candidate):
        raise TrainError(f"{label} is not a SHA-256 digest")
    return candidate


def _validate_ref(value: str, label: str) -> str:
    candidate = value.strip()
    if not candidate.startswith("refs/") or any(char.isspace() or ord(char) < 32 for char in candidate):
        raise TrainError(f"{label} must be an explicit full ref")
    if any(char in candidate for char in "~^:?*[\\") or "@{" in candidate or ".." in candidate:
        raise TrainError(f"{label} is malformed")
    if candidate.endswith(("/", ".")) or any(
        part in {"", ".", ".."} or part.endswith(".lock")
        for part in candidate.split("/")
    ):
        raise TrainError(f"{label} is malformed")
    return candidate


def _validate_repo_path(value: str, label: str) -> str:
    candidate = value.replace("\\", "/").strip("/")
    if not candidate or candidate.startswith(".") or any(part in {"", ".", ".."} for part in candidate.split("/")):
        raise TrainError(f"{label} must be a safe repository-relative path")
    if any(ord(char) < 32 for char in candidate):
        raise TrainError(f"{label} contains a control character")
    return candidate


def _git(
    repo: Repository,
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    input_bytes: bytes | None = None,
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
        [repo.git, "-C", str(cwd or repo.root), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise TrainError(f"git {' '.join(arguments)} failed: {detail or 'unknown Git error'}")
    return result


def _git_without_hooks(
    repo: Repository,
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run a Git mutation without project hooks adding unplanned side effects."""

    with tempfile.TemporaryDirectory(prefix="harness-train-empty-hooks-") as hooks:
        return _git(
            repo,
            ["-c", f"core.hooksPath={hooks}", *arguments],
            cwd=cwd,
            check=check,
        )


def _open_repository_uncached(project_root: str | Path) -> Repository:
    git = shutil.which("git")
    if not git:
        raise TrainError("git is required")
    supplied = Path(project_root).resolve()
    if not supplied.is_dir():
        raise TrainError(f"project root is not a directory: {supplied}")
    try:
        workspace_context = workspace.resolve_repository(supplied)
    except workspace.WorkspaceError as exc:
        raise TrainError(f"train mutations require the canonical primary coordinator root: {exc}") from exc
    probe = subprocess.run(
        [git, "-C", str(supplied), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if probe.returncode != 0:
        raise TrainError("project root is not a Git worktree")
    actual = Path(probe.stdout.decode("utf-8", errors="strict").strip()).resolve()
    if os.path.normcase(str(actual)) != os.path.normcase(str(supplied)):
        raise TrainError(f"project root must name the exact worktree root: {actual}")
    common_probe = subprocess.run(
        [git, "-C", str(actual), "rev-parse", "--git-common-dir"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    raw_common = Path(common_probe.stdout.decode("utf-8", errors="strict").strip())
    common = (raw_common if raw_common.is_absolute() else actual / raw_common).resolve()
    exec_probe = subprocess.run(
        [git, "--exec-path"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    git_exec_path = exec_probe.stdout.decode("utf-8", errors="strict").strip()
    if (
        os.path.normcase(str(workspace_context.project_root)) != os.path.normcase(str(actual))
        or os.path.normcase(str(workspace_context.common_dir)) != os.path.normcase(str(common))
    ):
        raise TrainError("workspace coordinator and Git repository identities disagree")
    return Repository(git=git, git_exec_path=git_exec_path, root=actual, common_dir=common)


def open_repository(project_root: str | Path) -> Repository:
    """Open the canonical repository, reusing only the active immutable context."""

    supplied = Path(project_root).resolve()
    context = _AUTHORITY_VALIDATION_CONTEXT.get()
    if context is not None:
        if os.path.normcase(str(supplied)) != os.path.normcase(str(context.repo.root)):
            raise TrainError(
                "authority validation context cannot be reused for another repository"
            )
        return context.repo
    return _open_repository_uncached(supplied)


def _authority_ref_snapshot(
    repo: Repository,
) -> tuple[dict[str, str], dict[str, str], bytes]:
    """Read every ref/object identity in one deterministic Git subprocess."""

    result = _git(
        repo,
        [
            "for-each-ref",
            "--sort=refname",
            "--format=%(refname)%00%(objectname)%00%(objecttype)",
        ],
    )
    raw = result.stdout
    refs: dict[str, str] = {}
    object_types: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split(b"\0")
        if len(parts) != 3:
            raise TrainError("authority ref snapshot is malformed")
        try:
            reference = parts[0].decode("utf-8", errors="strict")
            oid = parts[1].decode("ascii", errors="strict")
            object_type = parts[2].decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise TrainError("authority ref snapshot contains invalid text") from exc
        _validate_ref(reference, "authority snapshot ref")
        _validate_oid(oid, f"authority snapshot object for {reference}")
        if reference in refs:
            raise TrainError(f"authority ref snapshot repeats {reference}")
        refs[reference] = oid
        previous = object_types.get(oid)
        if previous is not None and previous != object_type:
            raise TrainError(f"authority object type is inconsistent for {oid}")
        object_types[oid] = object_type
    return refs, object_types, raw


def _authority_operational_snapshot(
    repo: Repository,
) -> tuple[tuple[str, str, int, str], ...]:
    """Digest local operational evidence that public candidate gates consume."""

    registry = repo.common_dir / "project-harness"
    if not registry.exists():
        return ()
    if registry.is_symlink() or not registry.is_dir():
        raise TrainError("authority operational registry is not a regular directory")
    values: list[tuple[str, str, int, str]] = []
    try:
        for current, directories, files in os.walk(registry, followlinks=False):
            directories.sort()
            files.sort()
            current_path = Path(current)
            retained: list[str] = []
            for name in directories:
                path = current_path / name
                relative = path.relative_to(registry).as_posix()
                # OS lock files/directories coordinate concurrent writers but
                # are not product authority.  On Windows, reading the byte
                # locked file held by the calling lifecycle operation raises
                # PermissionError and makes the operation reject itself.
                # Exclude these ephemeral paths while continuing to bind all
                # journals, leases, manifests, and evidence content.
                if name == "locks" or name.endswith(".lock"):
                    continue
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    values.append((relative, "link", metadata.st_size, os.readlink(path)))
                elif stat.S_ISDIR(metadata.st_mode):
                    retained.append(name)
                else:
                    values.append((relative, "special", metadata.st_size, ""))
            directories[:] = retained
            for name in files:
                path = current_path / name
                relative = path.relative_to(registry).as_posix()
                if name.endswith(".lock") or "locks" in Path(relative).parts:
                    continue
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    values.append((relative, "link", metadata.st_size, os.readlink(path)))
                elif stat.S_ISREG(metadata.st_mode):
                    raw = path.read_bytes()
                    if len(raw) != metadata.st_size:
                        raise TrainError(
                            f"authority operational evidence changed while reading: {relative}"
                        )
                    values.append(
                        (relative, "file", len(raw), hashlib.sha256(raw).hexdigest())
                    )
                else:
                    values.append((relative, "special", metadata.st_size, ""))
    except (OSError, ValueError) as exc:
        raise TrainError(f"cannot snapshot authority operational evidence: {exc}") from exc
    return tuple(sorted(values))


@contextlib.contextmanager
def authority_validation_context(
    project_root: str | Path,
) -> Iterator[AuthorityValidationContext]:
    """Share safe read results inside one derivation and reject snapshot drift."""

    supplied = Path(project_root).resolve()
    active = _AUTHORITY_VALIDATION_CONTEXT.get()
    if active is not None:
        if os.path.normcase(str(supplied)) != os.path.normcase(str(active.repo.root)):
            raise TrainError(
                "nested authority validation belongs to another repository"
            )
        yield active
        return

    repo = _open_repository_uncached(supplied)
    refs, object_types, raw_refs = _authority_ref_snapshot(repo)
    operational = _authority_operational_snapshot(repo)
    snapshot_digest = hashlib.sha256(
        raw_refs + b"\0" + canonical_json(operational)
    ).hexdigest()
    context = AuthorityValidationContext(
        repo=repo,
        refs=refs,
        ref_object_types=object_types,
        refs_snapshot=raw_refs,
        operational_snapshot=operational,
        snapshot_digest=snapshot_digest,
        object_type_cache=dict(object_types),
    )
    token = _AUTHORITY_VALIDATION_CONTEXT.set(context)
    try:
        yield context
        context.assert_unchanged()
    finally:
        _AUTHORITY_VALIDATION_CONTEXT.reset(token)


def _current_principle_audit_blockers(
    repo: Repository,
    iteration: str,
    *,
    authority_ref: str | None = None,
) -> tuple[Blocker, ...]:
    """Recompute the durable principle-impact gate; never trust caller evidence."""

    try:
        gate = principle_audit.current_principle_gate(
            repo.root,
            iteration=iteration,
            authority_ref=authority_ref,
        )
    except principle_audit.PrincipleAuditError as exc:
        return (Blocker("principle-audit-gate-invalid", str(exc)),)
    if gate.allowed:
        return ()
    reasons = gate.blockers or ("principle-audit-gate-denied",)
    return tuple(
        Blocker(
            reason,
            f"PRD-{gate.iteration} current principle gate denied: {gate.next_gate}",
        )
        for reason in reasons
    )


def _principle_gate_binding_payload(binding: PrincipleGateBinding) -> dict[str, object]:
    payload = binding.as_dict()
    payload.pop("binding_digest", None)
    return payload


def principle_gate_binding_digest(binding: PrincipleGateBinding) -> str:
    return digest(_principle_gate_binding_payload(binding))


def _principle_gate_binding_gate(binding: PrincipleGateBinding) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if not isinstance(binding, PrincipleGateBinding):
        return (
            Blocker(
                "principle-gate-binding-type",
                "candidate principle gate binding has an unsupported type",
            ),
        )
    if binding.schema_version != PRINCIPLE_GATE_BINDING_SCHEMA:
        blockers.append(
            Blocker(
                "principle-gate-binding-schema",
                "candidate principle gate binding schema is unsupported",
            )
        )
    try:
        _validate_iteration(binding.iteration)
        _validate_ref(binding.authority_ref, "principle gate authority_ref")
        _validate_digest(
            binding.allocation_principle_sha256,
            "principle gate allocation hash",
        )
        _validate_digest(
            binding.current_principle_sha256,
            "principle gate current hash",
        )
        _validate_digest(binding.binding_digest, "principle gate binding digest")
    except TrainError as exc:
        blockers.append(Blocker("principle-gate-binding-identity", str(exc)))
    else:
        if binding.binding_digest != principle_gate_binding_digest(binding):
            blockers.append(
                Blocker(
                    "principle-gate-binding-digest",
                    "candidate principle gate binding was changed",
                )
            )
    if binding.mode == "no-drift":
        if (
            binding.drift
            or binding.allocation_principle_sha256
            != binding.current_principle_sha256
            or binding.disposition is not None
            or binding.audit_generation is not None
            or binding.audit_receipt_digest is not None
            or binding.audit_supersedes is not None
            or binding.audit_operation_id is not None
            or binding.audit_plan_digest is not None
        ):
            blockers.append(
                Blocker(
                    "principle-gate-no-drift-identity",
                    "no-drift principle binding contains audit or drift state",
                )
            )
    elif binding.mode == "audit-receipt":
        if (
            not binding.drift
            or binding.allocation_principle_sha256
            == binding.current_principle_sha256
            or binding.disposition
            not in {
                principle_audit.DISPOSITION_NO_IMPACT,
                principle_audit.DISPOSITION_REAPPROVED,
            }
            or not isinstance(binding.audit_generation, int)
            or isinstance(binding.audit_generation, bool)
            or binding.audit_generation < 1
            or binding.audit_receipt_digest is None
            or binding.audit_operation_id is None
            or binding.audit_plan_digest is None
        ):
            blockers.append(
                Blocker(
                    "principle-gate-audit-identity",
                    "audit-backed principle binding lacks an exact clearing receipt identity",
                )
            )
        else:
            try:
                _validate_digest(
                    binding.audit_receipt_digest,
                    "principle gate audit receipt digest",
                )
                _validate_operation(binding.audit_operation_id)
                _validate_digest(
                    binding.audit_plan_digest,
                    "principle gate audit plan digest",
                )
                if binding.audit_supersedes is not None:
                    _validate_digest(
                        binding.audit_supersedes,
                        "principle gate superseded receipt digest",
                    )
            except TrainError as exc:
                blockers.append(Blocker("principle-gate-audit-identity", str(exc)))
            if binding.audit_generation == 1 and binding.audit_supersedes is not None:
                blockers.append(
                    Blocker(
                        "principle-gate-audit-chain",
                        "first audit generation may not supersede another receipt",
                    )
                )
            if binding.audit_generation > 1 and binding.audit_supersedes is None:
                blockers.append(
                    Blocker(
                        "principle-gate-audit-chain",
                        "later audit generation must bind the superseded receipt",
                    )
                )
    else:
        blockers.append(
            Blocker(
                "principle-gate-binding-mode",
                "candidate principle gate binding mode is unsupported",
            )
        )
    return tuple(dict.fromkeys(blockers))


def _principle_gate_binding_from_dict(value: object) -> PrincipleGateBinding:
    if not isinstance(value, Mapping):
        raise TrainError("candidate evidence principle gate binding is missing")
    expected = {
        "schema_version",
        "iteration",
        "authority_ref",
        "mode",
        "allocation_principle_sha256",
        "current_principle_sha256",
        "drift",
        "disposition",
        "audit_generation",
        "audit_receipt_digest",
        "audit_supersedes",
        "audit_operation_id",
        "audit_plan_digest",
        "binding_digest",
    }
    if set(value) != expected:
        raise TrainError("candidate evidence principle gate binding fields are invalid")
    mode = value.get("mode")
    if mode not in {"no-drift", "audit-receipt"}:
        raise TrainError("candidate evidence principle gate binding mode is invalid")
    drift = value.get("drift")
    if not isinstance(drift, bool):
        raise TrainError("candidate evidence principle gate drift state is invalid")
    generation = value.get("audit_generation")
    if generation is not None and (
        not isinstance(generation, int) or isinstance(generation, bool)
    ):
        raise TrainError("candidate evidence principle audit generation is invalid")
    optional_strings = (
        "disposition",
        "audit_receipt_digest",
        "audit_supersedes",
        "audit_operation_id",
        "audit_plan_digest",
    )
    if any(
        value.get(field) is not None and not isinstance(value.get(field), str)
        for field in optional_strings
    ):
        raise TrainError("candidate evidence principle audit identity is invalid")
    binding = PrincipleGateBinding(
        schema_version=str(value["schema_version"]),
        iteration=str(value["iteration"]),
        authority_ref=str(value["authority_ref"]),
        mode=mode,
        allocation_principle_sha256=str(value["allocation_principle_sha256"]),
        current_principle_sha256=str(value["current_principle_sha256"]),
        drift=drift,
        disposition=(
            str(value["disposition"]) if value.get("disposition") is not None else None
        ),
        audit_generation=generation,
        audit_receipt_digest=(
            str(value["audit_receipt_digest"])
            if value.get("audit_receipt_digest") is not None
            else None
        ),
        audit_supersedes=(
            str(value["audit_supersedes"])
            if value.get("audit_supersedes") is not None
            else None
        ),
        audit_operation_id=(
            str(value["audit_operation_id"])
            if value.get("audit_operation_id") is not None
            else None
        ),
        audit_plan_digest=(
            str(value["audit_plan_digest"])
            if value.get("audit_plan_digest") is not None
            else None
        ),
        binding_digest=str(value["binding_digest"]),
    )
    blockers = _principle_gate_binding_gate(binding)
    if blockers:
        raise TrainError(
            "candidate evidence principle gate binding is invalid: "
            + "; ".join(item.code for item in blockers)
        )
    return binding


def _current_candidate_principle_gate_binding(
    repo: Repository,
    iteration: str,
    *,
    authority_ref: str,
) -> tuple[PrincipleGateBinding | None, tuple[Blocker, ...]]:
    """Rebuild the exact live principle authority consumed by a candidate."""

    number = _validate_iteration(iteration)
    authority = _validate_ref(authority_ref, "principle gate authority_ref")
    try:
        gate = principle_audit.current_principle_gate(
            repo.root,
            iteration=number,
            authority_ref=authority,
        )
    except principle_audit.PrincipleAuditError as exc:
        return None, (Blocker("principle-audit-gate-invalid", str(exc)),)
    if not gate.allowed:
        reasons = gate.blockers or ("principle-audit-gate-denied",)
        return None, tuple(
            Blocker(
                reason,
                f"PRD-{gate.iteration} current principle gate denied: {gate.next_gate}",
            )
            for reason in reasons
        )
    common = {
        "schema_version": PRINCIPLE_GATE_BINDING_SCHEMA,
        "iteration": number,
        "authority_ref": authority,
        "allocation_principle_sha256": gate.allocation_principle_sha256,
        "current_principle_sha256": gate.current_principle_sha256,
    }
    if not gate.drift:
        provisional = PrincipleGateBinding(
            **common,
            mode="no-drift",
            drift=False,
            disposition=None,
            audit_generation=None,
            audit_receipt_digest=None,
            audit_supersedes=None,
            audit_operation_id=None,
            audit_plan_digest=None,
            binding_digest="0" * 64,
        )
    else:
        if gate.receipt_digest is None or gate.disposition is None:
            return None, (
                Blocker(
                    "principle-audit-receipt-identity-missing",
                    "allowed drift gate lacks its exact durable receipt identity",
                ),
            )
        try:
            receipt = principle_audit.load_principle_impact_audit_receipt(
                repo.common_dir,
                number,
                gate.current_principle_sha256,
                gate.receipt_digest,
            )
        except principle_audit.PrincipleAuditError as exc:
            return None, (Blocker("principle-audit-receipt-invalid", str(exc)),)
        if (
            receipt is None
            or receipt.receipt_digest != gate.receipt_digest
            or receipt.disposition != gate.disposition
            or not receipt.clears_drift
        ):
            return None, (
                Blocker(
                    "principle-audit-receipt-identity-mismatch",
                    "current principle gate differs from its durable clearing receipt",
                ),
            )
        provisional = PrincipleGateBinding(
            **common,
            mode="audit-receipt",
            drift=True,
            disposition=receipt.disposition,
            audit_generation=receipt.generation,
            audit_receipt_digest=receipt.receipt_digest,
            audit_supersedes=receipt.supersedes,
            audit_operation_id=receipt.operation_id,
            audit_plan_digest=receipt.plan_digest,
            binding_digest="0" * 64,
        )
    binding = replace(
        provisional,
        binding_digest=principle_gate_binding_digest(provisional),
    )
    structural = _principle_gate_binding_gate(binding)
    return (binding if not structural else None), structural


def _principle_gate_evidence_id(binding_digest: str) -> str:
    return f"principle-gate:{_validate_digest(binding_digest, 'principle gate binding digest')}"


def _workspace_context(repo: Repository) -> workspace.RepositoryContext:
    try:
        return workspace.resolve_repository(repo.root)
    except workspace.WorkspaceError as exc:
        raise TrainError(f"workspace coordinator is unavailable: {exc}") from exc


def _assert_train_operational_path(repo: Repository, path: Path) -> None:
    context = _workspace_context(repo)
    expected = repo.common_dir / "project-harness" / "train" / "v1"
    try:
        workspace.assert_existing_chain_has_no_links(path, stop=repo.common_dir)
    except workspace.WorkspaceError as exc:
        raise TrainError(f"unsafe train operational path: {exc}") from exc
    if not workspace.is_within(path, expected):
        raise TrainError(f"train operational path escapes its registry: {path}")


def _resolve_ref(repo: Repository, reference: str) -> str | None:
    ref = _validate_ref(reference, "reference")
    context = _AUTHORITY_VALIDATION_CONTEXT.get()
    if context is not None and context.repo == repo:
        return context.refs.get(ref)
    result = _git(repo, ["rev-parse", "--verify", "--quiet", ref], check=False)
    if result.returncode != 0 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise TrainError(f"cannot resolve {ref}: {detail}")
    return _validate_oid(result.stdout.decode("ascii").strip(), ref)


def _commit_tree(repo: Repository, commit: str) -> str:
    oid = _validate_oid(commit, "commit")
    context = _AUTHORITY_VALIDATION_CONTEXT.get()
    if context is not None and context.repo == repo:
        cached = context.commit_tree_cache.get(oid)
        if cached is not None:
            return cached
    result = _git(repo, ["rev-parse", f"{oid}^{{tree}}"])
    tree = _validate_oid(result.stdout.decode("ascii").strip(), "commit tree")
    if context is not None and context.repo == repo:
        context.commit_tree_cache[oid] = tree
    return tree


def _object_type(repo: Repository, oid: str) -> str | None:
    context = _AUTHORITY_VALIDATION_CONTEXT.get()
    if context is not None and context.repo == repo and oid in context.object_type_cache:
        return context.object_type_cache[oid]
    result = _git(repo, ["cat-file", "-t", oid], check=False)
    if result.returncode != 0:
        observed = None
    else:
        observed = result.stdout.decode("ascii", errors="strict").strip()
    if context is not None and context.repo == repo:
        context.object_type_cache[oid] = observed
    return observed


def _blob_at(repo: Repository, commit: str, path: str) -> tuple[str, bytes]:
    safe_path = _validate_repo_path(path, "authority path")
    spec = f"{_validate_oid(commit, 'authority commit')}:{safe_path}"
    oid_result = _git(repo, ["rev-parse", spec], check=False)
    if oid_result.returncode != 0:
        raise TrainError(f"committed authority file is missing: {safe_path}")
    blob = _validate_oid(oid_result.stdout.decode("ascii").strip(), f"blob for {safe_path}")
    raw = _git(repo, ["cat-file", "blob", blob]).stdout
    if len(raw) > MAX_AUTHORITY_FILE:
        raise TrainError(f"authority file exceeds safe size limit: {safe_path}")
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TrainError(f"authority file is not UTF-8: {safe_path}") from exc
    return blob, raw


def _field(text: str, label: str) -> str | None:
    for match in FIELD_RE.finditer(text):
        if match.group("label").strip() == label:
            return match.group("value").strip().strip("`").strip()
    return None


def _iteration_ids_from_field(text: str, *labels: str) -> tuple[str, ...]:
    for label in labels:
        value = _field(text, label)
        if value:
            return tuple(dict.fromkeys(re.findall(r"(?:PRD-)?([0-9]{3,})", value)))
    return ()


def _authority_payload(value: AuthorityReceipt) -> dict[str, object]:
    data = value.as_dict()
    data.pop("evidence_digest", None)
    return data


def authority_evidence_digest(value: AuthorityReceipt) -> str:
    return digest(_authority_payload(value))


def authority_evidence_gate(value: AuthorityReceipt) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if value.schema_version != AUTHORITY_SCHEMA:
        blockers.append(Blocker("authority-schema-unsupported", "authority receipt schema is unsupported"))
    if value.evidence_digest != authority_evidence_digest(value):
        blockers.append(Blocker("authority-evidence-digest-mismatch", "authority receipt was changed"))
    if value.blockers:
        blockers.append(Blocker("authority-not-approved", ", ".join(value.blockers)))
    return tuple(blockers)


def _normalize_dependency_bindings(
    values: Sequence[Mapping[str, object] | DependencyCandidateBinding],
    *,
    label: str,
) -> tuple[DependencyCandidateBinding, ...]:
    raw_values: list[Mapping[str, object]] = []
    for value in values:
        if isinstance(value, DependencyCandidateBinding):
            raw_values.append(value.as_dict())
        elif isinstance(value, Mapping):
            raw_values.append(value)
        else:
            raise TrainError(f"{label} entries must be exact dependency candidate bindings")
    try:
        normalized = workspace.normalize_dependency_bindings(raw_values)
    except workspace.WorkspaceError as exc:
        raise TrainError(f"{label} are invalid: {exc}") from exc
    return tuple(DependencyCandidateBinding(**item) for item in normalized)


def _dependency_bindings_digest(
    values: Sequence[DependencyCandidateBinding],
) -> str:
    return workspace.dependency_bindings_digest(tuple(item.as_dict() for item in values))


def _dependency_evidence_id(bindings_digest: str) -> str:
    return f"dependency-bindings:{_validate_digest(bindings_digest, 'dependency bindings digest')}"


def _dependency_binding_matches_registered(
    binding: DependencyCandidateBinding,
    candidate: RegisteredCandidate,
) -> bool:
    return (
        binding.iteration == candidate.iteration
        and binding.generation == candidate.generation
        and binding.candidate_ref == candidate.candidate_ref
        and binding.candidate_commit == candidate.candidate_commit
        and binding.candidate_tree == candidate.candidate_tree
        and binding.candidate_evidence_ref == candidate.candidate_evidence_ref
        and binding.candidate_evidence_blob == candidate.candidate_evidence_blob
        and binding.candidate_evidence_digest == candidate.candidate_evidence.evidence_digest
        and binding.candidate_evidence_metadata_digest
        == candidate.candidate_evidence_metadata_digest
        and binding.registration_digest == candidate.registration_digest
    )


def _dependency_bindings_live_blockers(
    repo: Repository,
    bindings: Sequence[DependencyCandidateBinding],
    *,
    consuming_iteration: str,
    consuming_commit: str,
) -> tuple[Blocker, ...]:
    if not bindings:
        return ()
    try:
        context = workspace.resolve_repository(repo.root)
        raw_blockers = workspace.dependency_order_blockers(
            context,
            tuple(item.as_dict() for item in bindings),
        )
    except workspace.WorkspaceError as exc:
        return (Blocker("dependency-binding-live-unreadable", str(exc)),)
    blockers = [Blocker(item.code, item.message) for item in raw_blockers]
    for binding in bindings:
        if not _is_ancestor(repo, binding.candidate_commit, consuming_commit):
            blockers.append(
                Blocker(
                    "candidate-dependency-not-ancestor",
                    f"PRD-{consuming_iteration} candidate does not descend from its exact "
                    f"PRD-{binding.iteration}/{binding.generation} dependency candidate",
                )
            )
    return tuple(dict.fromkeys(blockers))


def _dependency_binding_satisfied_by_final(
    repo: Repository,
    binding: DependencyCandidateBinding,
    *,
    target_main: str,
) -> bool:
    final_ref = f"refs/project-harness/v2/iterations/{binding.iteration}/final"
    final_commit = _resolve_ref(repo, final_ref)
    if (
        final_commit is None
        or not _is_ancestor(repo, final_commit, target_main)
        or not _is_ancestor(repo, binding.candidate_commit, final_commit)
    ):
        return False
    integrated_ref = f"refs/project-harness/v2/iterations/{binding.iteration}/integrated"
    if _resolve_ref(repo, integrated_ref) != final_commit:
        return False
    prefix = _train_root(repo) / "journal"
    if not prefix.is_dir():
        return False
    for path in prefix.glob("advance-OP-*.json"):
        try:
            journal = _read_json(path, repo)
        except TrainError:
            continue
        if not isinstance(journal, Mapping) or journal.get("status") != "complete":
            continue
        if journal.get("integrated_commit") != final_commit:
            continue
        updates = journal.get("ref_updates")
        if not isinstance(updates, list):
            continue
        if [final_ref, None, final_commit] not in updates or [integrated_ref, None, final_commit] not in updates:
            continue
        operation_id = journal.get("operation_id")
        if not isinstance(operation_id, str):
            continue
        integration_journal = _read_json(
            _journal_path(repo, "integration", operation_id),
            repo,
        )
        evidence = (
            integration_journal.get("integrated_evidence")
            if isinstance(integration_journal, Mapping)
            else None
        )
        candidate_digests = evidence.get("candidate_digests") if isinstance(evidence, Mapping) else None
        if (
            isinstance(candidate_digests, list)
            and binding.candidate_evidence_digest in candidate_digests
        ):
            return True
    return False


def _integration_dependency_blockers(
    repo: Repository,
    candidates: Sequence[RegisteredCandidate],
    *,
    target_main: str,
) -> tuple[Blocker, ...]:
    by_iteration = {item.iteration: item for item in candidates}
    position = {item.iteration: index for index, item in enumerate(candidates)}
    blockers: list[Blocker] = []
    for index, consumer in enumerate(candidates):
        if tuple(item.iteration for item in consumer.dependency_bindings) != consumer.depends_on:
            blockers.append(
                Blocker(
                    "integration-dependency-bindings-authority",
                    f"PRD-{consumer.iteration} exact dependency bindings differ from depends_on",
                )
            )
        for binding in consumer.dependency_bindings:
            supplied = by_iteration.get(binding.iteration)
            if supplied is not None:
                if position[binding.iteration] >= index:
                    blockers.append(
                        Blocker(
                            "integration-dependency-order-invalid",
                            f"PRD-{binding.iteration} must precede PRD-{consumer.iteration}",
                        )
                    )
                if not _dependency_binding_matches_registered(binding, supplied):
                    blockers.append(
                        Blocker(
                            "integration-dependency-binding-mismatch",
                            f"PRD-{consumer.iteration} binds PRD-{binding.iteration}/"
                            f"{binding.generation}, but the train carries another exact candidate",
                        )
                    )
            elif not _dependency_binding_satisfied_by_final(
                repo,
                binding,
                target_main=target_main,
            ):
                blockers.append(
                    Blocker(
                        "integration-dependency-binding-missing",
                        f"PRD-{consumer.iteration} requires its exact PRD-{binding.iteration}/"
                        f"{binding.generation} evidence in this train or main/final",
                    )
                )
            if not _is_ancestor(repo, binding.candidate_commit, consumer.candidate_commit):
                blockers.append(
                    Blocker(
                        "integration-dependency-not-ancestor",
                        f"PRD-{consumer.iteration} candidate is not descended from its bound "
                        f"PRD-{binding.iteration}/{binding.generation} candidate",
                    )
                )
    return tuple(dict.fromkeys(blockers))


def _workspace_guard_payload(receipt: WorkspaceGuardReceipt) -> dict[str, object]:
    data = receipt.as_dict()
    data.pop("guard_digest", None)
    return data


def workspace_guard_digest(receipt: WorkspaceGuardReceipt) -> str:
    return digest(_workspace_guard_payload(receipt))


def _workspace_guard_from_mapping(value: object) -> WorkspaceGuardReceipt:
    required = {
        "schema_version",
        "iteration",
        "owner",
        "generation",
        "operation_id",
        "accepted_plan_digest",
        "worktree_path",
        "branch_ref",
        "base_commit",
        "implementation_ref",
        "implementation_commit",
        "reconciliation_ref",
        "reconciliation_commit",
        "dependency_refresh_generation",
        "dependency_bindings",
        "dependency_bindings_digest",
        "lease_digest",
        "guard_digest",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise TrainError("workspace guard receipt fields are invalid")
    raw_bindings = value.get("dependency_bindings")
    if not isinstance(raw_bindings, Sequence) or isinstance(raw_bindings, (str, bytes)):
        raise TrainError("workspace guard dependency bindings are invalid")
    bindings = _normalize_dependency_bindings(
        raw_bindings,  # type: ignore[arg-type]
        label="workspace guard dependency bindings",
    )
    generation = value.get("generation")
    refresh_generation = value.get("dependency_refresh_generation")
    worktree_path = value.get("worktree_path")
    owner = value.get("owner")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or not isinstance(refresh_generation, int)
        or isinstance(refresh_generation, bool)
        or refresh_generation < 0
        or not isinstance(worktree_path, str)
        or not Path(worktree_path).is_absolute()
        or not isinstance(owner, str)
        or not owner.strip()
    ):
        raise TrainError("workspace guard scalar identity is invalid")
    iteration = _validate_iteration(str(value["iteration"]))
    base_commit = _validate_oid(str(value["base_commit"]), "workspace guard base_commit")
    implementation_commit = _validate_oid(
        str(value["implementation_commit"]),
        "workspace guard implementation_commit",
    )
    implementation_ref = _validate_ref(
        str(value["implementation_ref"]),
        "workspace guard implementation_ref",
    )
    reconciliation_commit = _validate_oid(
        str(value["reconciliation_commit"]),
        "workspace guard reconciliation_commit",
    )
    reconciliation_ref = _validate_ref(
        str(value["reconciliation_ref"]),
        "workspace guard reconciliation_ref",
    )
    branch_ref = _validate_ref(str(value["branch_ref"]), "workspace guard branch_ref")
    if not branch_ref.startswith("refs/heads/"):
        raise TrainError("workspace guard branch_ref is not a branch")
    if not (
        implementation_ref.startswith("refs/heads/")
        or implementation_ref.startswith("refs/project-harness/v2/iterations/")
    ):
        raise TrainError("workspace guard implementation_ref is invalid")
    bindings_digest = _dependency_bindings_digest(bindings)
    if value.get("dependency_bindings_digest") != bindings_digest:
        raise TrainError("workspace guard dependency binding digest differs")
    if bindings:
        last = bindings[-1]
        if refresh_generation == 0:
            if (
                implementation_ref != last.candidate_ref
                or implementation_commit != last.candidate_commit
            ):
                raise TrainError("workspace guard implementation start differs from its dependency baseline")
    elif implementation_ref != "refs/heads/main":
        raise TrainError("independent workspace guard implementation ref is not main")
    if refresh_generation == 0:
        if reconciliation_ref != implementation_ref or reconciliation_commit != implementation_commit:
            raise TrainError("unrefreshed workspace guard reconciliation baseline differs")
    elif reconciliation_ref != branch_ref:
        raise TrainError("refreshed workspace guard reconciliation ref differs from its branch")
    provisional = WorkspaceGuardReceipt(
        schema_version=str(value["schema_version"]),
        iteration=iteration,
        owner=owner.strip(),
        generation=generation,
        operation_id=_validate_operation(str(value["operation_id"])),
        accepted_plan_digest=_validate_digest(
            str(value["accepted_plan_digest"]),
            "workspace guard accepted_plan_digest",
        ),
        worktree_path=str(Path(worktree_path).resolve(strict=False)),
        branch_ref=branch_ref,
        base_commit=base_commit,
        implementation_ref=implementation_ref,
        implementation_commit=implementation_commit,
        reconciliation_ref=reconciliation_ref,
        reconciliation_commit=reconciliation_commit,
        dependency_refresh_generation=refresh_generation,
        dependency_bindings=bindings,
        dependency_bindings_digest=bindings_digest,
        lease_digest=_validate_digest(str(value["lease_digest"]), "workspace guard lease_digest"),
        guard_digest=_validate_digest(str(value["guard_digest"]), "workspace guard guard_digest"),
    )
    if provisional.schema_version != WORKSPACE_GUARD_SCHEMA:
        raise TrainError("workspace guard schema is unsupported")
    if provisional.guard_digest != workspace_guard_digest(provisional):
        raise TrainError("workspace guard receipt digest differs")
    return provisional


def _derive_workspace_guard(
    repo: Repository,
    *,
    iteration: str,
    owner: str,
    generation: int,
    operation_id: str,
    accepted_plan_digest: str,
    worktree_path: Path,
    branch_ref: str,
    base_commit: str,
) -> tuple[WorkspaceGuardReceipt | None, tuple[Blocker, ...]]:
    blockers: list[Blocker] = []
    try:
        context = workspace.resolve_repository(repo.root)
        lease = workspace.load_lease(context, iteration)
    except workspace.WorkspaceError as exc:
        return None, (Blocker("workspace-guard-unreadable", str(exc)),)
    if lease is None:
        return None, (Blocker("workspace-guard-lease-missing", f"PRD-{iteration} has no active writer lease"),)
    try:
        journal = workspace.load_journal(context, operation_id)
    except workspace.WorkspaceError as exc:
        return None, (Blocker("workspace-guard-journal-invalid", str(exc)),)
    if journal is None or journal.get("phase") != "READY":
        blockers.append(Blocker("workspace-guard-not-ready", "accepted workspace operation is not READY"))
    elif journal.get("plan_digest") != accepted_plan_digest:
        blockers.append(Blocker("workspace-guard-plan-digest", "accepted workspace plan digest is stale"))
    if lease.get("operation_id") != operation_id:
        blockers.append(Blocker("workspace-guard-operation", "writer lease belongs to another workspace operation"))
    if lease.get("owner") != owner:
        blockers.append(Blocker("workspace-guard-owner", "writer lease owner differs"))
    if lease.get("generation") != generation:
        blockers.append(Blocker("workspace-guard-generation", "writer lease generation differs"))
    guard_blockers, _projection = workspace.guard_lease(
        context,
        lease,
        owner=owner,
        generation=generation,
        worktree_path=worktree_path,
        branch_ref=branch_ref,
        base_commit=base_commit,
    )
    blockers.extend(Blocker(f"workspace-{item.code}", item.message) for item in guard_blockers)
    if blockers:
        return None, tuple(blockers)
    raw_bindings = lease.get("dependency_bindings")
    if not isinstance(raw_bindings, list):
        return None, (
            Blocker(
                "workspace-guard-dependency-bindings",
                "writer lease has no exact ordered dependency bindings",
            ),
        )
    try:
        dependency_bindings = _normalize_dependency_bindings(
            raw_bindings,
            label="writer lease dependency bindings",
        )
    except TrainError as exc:
        return None, (Blocker("workspace-guard-dependency-bindings", str(exc)),)
    dependency_digest = _dependency_bindings_digest(dependency_bindings)
    if lease.get("dependency_bindings_digest") != dependency_digest:
        return None, (
            Blocker(
                "workspace-guard-dependency-bindings-digest",
                "writer lease dependency binding digest differs",
            ),
        )
    implementation_commit = str(lease["implementation_commit"])
    if _object_type(repo, implementation_commit) != "commit" or not _is_ancestor(
        repo,
        base_commit,
        implementation_commit,
    ):
        return None, (
            Blocker(
                "workspace-guard-implementation-start",
                "writer lease implementation start is not a commit descended from the immutable allocation base",
            ),
        )
    reconciliation_commit = str(lease["reconciliation_commit"])
    if _object_type(repo, reconciliation_commit) != "commit" or not _is_ancestor(
        repo,
        implementation_commit,
        reconciliation_commit,
    ):
        return None, (
            Blocker(
                "workspace-guard-reconciliation-base",
                "writer lease reconciliation baseline is not a commit descended from its implementation baseline",
            ),
        )
    provisional = WorkspaceGuardReceipt(
        schema_version=WORKSPACE_GUARD_SCHEMA,
        iteration=iteration,
        owner=owner,
        generation=generation,
        operation_id=operation_id,
        accepted_plan_digest=accepted_plan_digest,
        worktree_path=str(worktree_path.resolve()),
        branch_ref=branch_ref,
        base_commit=base_commit,
        implementation_ref=str(lease["implementation_ref"]),
        implementation_commit=implementation_commit,
        reconciliation_ref=str(lease["reconciliation_ref"]),
        reconciliation_commit=reconciliation_commit,
        dependency_refresh_generation=int(lease["dependency_refresh_generation"]),
        dependency_bindings=dependency_bindings,
        dependency_bindings_digest=dependency_digest,
        lease_digest=digest(lease),
        guard_digest="0" * 64,
    )
    return replace(provisional, guard_digest=workspace_guard_digest(provisional)), ()


def _workspace_guard_gate(
    repo: Repository,
    receipt: WorkspaceGuardReceipt,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if receipt.schema_version != WORKSPACE_GUARD_SCHEMA:
        blockers.append(Blocker("workspace-guard-schema", "workspace guard schema is unsupported"))
    if receipt.guard_digest != workspace_guard_digest(receipt):
        blockers.append(Blocker("workspace-guard-digest", "workspace guard receipt was changed"))
        return tuple(blockers)
    try:
        normalized_bindings = _normalize_dependency_bindings(
            receipt.dependency_bindings,
            label="workspace guard dependency bindings",
        )
    except TrainError as exc:
        blockers.append(Blocker("workspace-guard-dependency-bindings", str(exc)))
    else:
        if normalized_bindings != receipt.dependency_bindings:
            blockers.append(
                Blocker(
                    "workspace-guard-dependency-bindings-order",
                    "workspace guard dependency bindings are not canonical",
                )
            )
        if _dependency_bindings_digest(normalized_bindings) != receipt.dependency_bindings_digest:
            blockers.append(
                Blocker(
                    "workspace-guard-dependency-bindings-digest",
                    "workspace guard dependency bindings were changed",
                )
            )
    current, current_blockers = _derive_workspace_guard(
        repo,
        iteration=receipt.iteration,
        owner=receipt.owner,
        generation=receipt.generation,
        operation_id=receipt.operation_id,
        accepted_plan_digest=receipt.accepted_plan_digest,
        worktree_path=Path(receipt.worktree_path),
        branch_ref=receipt.branch_ref,
        base_commit=receipt.base_commit,
    )
    blockers.extend(current_blockers)
    if current is not None and current.guard_digest != receipt.guard_digest:
        blockers.append(Blocker("workspace-guard-drift", "active workspace lease differs from accepted guard"))
    return tuple(blockers)


def _derive_authority(repo: Repository, iteration: str, commit: str) -> AuthorityReceipt:
    number = _validate_iteration(iteration)
    paths = {
        "prd": f"harness/iterations/{number}/prd-{number}.md",
        "spec": f"harness/iterations/{number}/spec-{number}.md",
        "deviation": f"harness/iterations/{number}/deviation-{number}.md",
    }
    prd_blob, prd_raw = _blob_at(repo, commit, paths["prd"])
    spec_blob, spec_raw = _blob_at(repo, commit, paths["spec"])
    deviation_blob, deviation_raw = _blob_at(repo, commit, paths["deviation"])
    prd = prd_raw.decode("utf-8-sig")
    spec = spec_raw.decode("utf-8-sig")
    deviation = deviation_raw.decode("utf-8-sig")
    prd_status = _field(prd, "状态") or ""
    spec_status = _field(spec, "状态") or ""
    prd_source = _field(prd, "批准依据") or ""
    spec_source = _field(spec, "批准依据") or ""
    implementation_source = _field(spec, "实施授权") or ""
    visible_deviation = HTML_COMMENT_RE.sub("", deviation)
    open_count = re.search(r"当前开放偏差\s*[：:]\s*`?([0-9]+)`?", visible_deviation)
    deviation_resolved = bool(open_count and int(open_count.group(1)) == 0)
    deviation_resolved = deviation_resolved and not re.search(
        r"^\s*-\s*状态\s*[：:]\s*`?开放`?\s*$",
        visible_deviation,
        re.MULTILINE,
    )
    acceptance_ids = tuple(
        dict.fromkeys(re.findall(rf"AC-{re.escape(number)}-[0-9]{{2,}}", prd))
    )
    blockers: list[str] = []
    if prd_status not in {"已批准", "实施中", "待验收", "已验收"}:
        blockers.append("prd-not-approved")
    if not governance_core.explicit_user_baseline_approval(prd_source, f"PRD-{number}"):
        blockers.append("prd-approval-evidence-missing")
    if spec_status not in {"已批准", "实施中", "已完成"}:
        blockers.append("spec-not-approved")
    if not governance_core.explicit_user_baseline_approval(spec_source, f"SPEC-{number}"):
        blockers.append("spec-approval-evidence-missing")
    if not governance_core.explicit_user_implementation_authorization(implementation_source):
        blockers.append("implementation-authorization-missing")
    if not deviation_resolved:
        blockers.append("deviation-unresolved")
    if not acceptance_ids:
        blockers.append("acceptance-ids-missing-from-prd")
    provisional = AuthorityReceipt(
        schema_version=AUTHORITY_SCHEMA,
        iteration=number,
        authority_commit=_validate_oid(commit, "authority commit"),
        prd_path=paths["prd"],
        prd_blob=prd_blob,
        prd_sha256=hashlib.sha256(prd_raw).hexdigest(),
        prd_status=prd_status,
        prd_approval_source_sha256=hashlib.sha256(prd_source.encode("utf-8")).hexdigest(),
        spec_path=paths["spec"],
        spec_blob=spec_blob,
        spec_sha256=hashlib.sha256(spec_raw).hexdigest(),
        spec_status=spec_status,
        spec_approval_source_sha256=hashlib.sha256(spec_source.encode("utf-8")).hexdigest(),
        implementation_authorization_source_sha256=hashlib.sha256(
            implementation_source.encode("utf-8")
        ).hexdigest(),
        deviation_path=paths["deviation"],
        deviation_blob=deviation_blob,
        deviation_sha256=hashlib.sha256(deviation_raw).hexdigest(),
        deviation_resolved=deviation_resolved,
        depends_on=_iteration_ids_from_field(prd, "依赖 PRD", "depends_on"),
        acceptance_ids=acceptance_ids,
        evidence_digest="0" * 64,
        blockers=tuple(blockers),
    )
    return replace(provisional, evidence_digest=authority_evidence_digest(provisional))


def _feature_worktree_gate(
    repo: Repository,
    worktree: Path,
    expected_commit: str,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if not worktree.is_dir():
        return (Blocker("feature-worktree-missing", f"feature worktree does not exist: {worktree}"),)
    probe = _git(repo, ["rev-parse", "--show-toplevel"], cwd=worktree, check=False)
    if probe.returncode != 0:
        return (Blocker("feature-worktree-invalid", "feature path is not a Git worktree"),)
    actual = Path(probe.stdout.decode("utf-8", errors="strict").strip()).resolve()
    if os.path.normcase(str(actual)) != os.path.normcase(str(worktree.resolve())):
        blockers.append(Blocker("feature-worktree-not-root", f"feature path must be exact root: {actual}"))
    common_probe = _git(repo, ["rev-parse", "--git-common-dir"], cwd=worktree, check=False)
    if common_probe.returncode != 0:
        blockers.append(Blocker("feature-worktree-common-dir", "cannot resolve feature Git common directory"))
    else:
        raw_common = Path(common_probe.stdout.decode("utf-8", errors="strict").strip())
        actual_common = (raw_common if raw_common.is_absolute() else worktree / raw_common).resolve()
        if os.path.normcase(str(actual_common)) != os.path.normcase(str(repo.common_dir)):
            blockers.append(Blocker("feature-worktree-other-repository", "feature worktree belongs to another repository"))
    head = _git(repo, ["rev-parse", "HEAD"], cwd=worktree).stdout.decode("ascii").strip()
    if head != expected_commit:
        blockers.append(Blocker("feature-worktree-head-drift", "feature worktree HEAD differs from feature ref"))
    dirty = _git(
        repo,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=worktree,
    ).stdout
    if dirty:
        blockers.append(
            Blocker(
                "feature-worktree-dirty",
                "candidate registration only accepts the exact committed tree; tracked or untracked changes remain",
            )
        )
    return tuple(blockers)


def _diff_paths(repo: Repository, base: str, candidate: str) -> tuple[str, ...]:
    result = _git(repo, ["diff", "--name-only", "-z", base, candidate, "--"])
    return tuple(
        item.decode("utf-8", errors="strict")
        for item in result.stdout.split(b"\0")
        if item
    )


def _is_ancestor(repo: Repository, ancestor: str, descendant: str) -> bool:
    return _git(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode == 0


def _candidate_verification_payload(receipt: CandidateVerificationReceipt) -> dict[str, object]:
    payload = receipt.as_dict()
    payload.pop("receipt_digest", None)
    return payload


def candidate_verification_receipt_digest(receipt: CandidateVerificationReceipt) -> str:
    return digest(_candidate_verification_payload(receipt))


def candidate_verification_receipt_gate(
    receipt: CandidateVerificationReceipt,
    *,
    expected_phase: str | None = None,
    expected_commit: str | None = None,
    expected_tree: str | None = None,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if not isinstance(receipt, CandidateVerificationReceipt):
        return (Blocker("candidate-verification-receipt-type", "receipt has an unsupported type"),)
    if receipt.schema_version != CANDIDATE_VERIFICATION_RECEIPT_SCHEMA:
        blockers.append(Blocker("candidate-verification-receipt-schema", receipt.evidence_id))
    if receipt.phase not in {"pre-seal", "seal"}:
        blockers.append(Blocker("candidate-verification-receipt-phase", receipt.evidence_id))
    if expected_phase is not None and receipt.phase != expected_phase:
        blockers.append(Blocker("candidate-verification-receipt-phase-drift", receipt.evidence_id))
    if expected_commit is not None and receipt.candidate_commit != expected_commit:
        blockers.append(Blocker("candidate-verification-receipt-commit-drift", receipt.evidence_id))
    if expected_tree is not None and receipt.candidate_tree != expected_tree:
        blockers.append(Blocker("candidate-verification-receipt-tree-drift", receipt.evidence_id))
    try:
        _validate_oid(receipt.candidate_commit, "candidate verification commit")
        _validate_oid(receipt.candidate_tree, "candidate verification tree")
        _validate_digest(receipt.stdout_sha256, "candidate verification stdout_sha256")
        _validate_digest(receipt.stderr_sha256, "candidate verification stderr_sha256")
    except TrainError as exc:
        blockers.append(Blocker("candidate-verification-receipt-identity", str(exc)))
    if not receipt.evidence_id.strip() or not receipt.argv:
        blockers.append(Blocker("candidate-verification-receipt-command", "receipt command identity is empty"))
    if receipt.exit_code != 0:
        blockers.append(
            Blocker(
                "candidate-verification-nonzero",
                f"{receipt.evidence_id} exited with {receipt.exit_code}",
            )
        )
    if receipt.receipt_digest != candidate_verification_receipt_digest(receipt):
        blockers.append(Blocker("candidate-verification-receipt-digest", receipt.evidence_id))
    return tuple(blockers)


def _candidate_verification_receipts(
    commands: Sequence[VerifyCommand],
    worktree: Path,
    *,
    phase: Literal["pre-seal", "seal"],
    candidate_commit: str,
    candidate_tree: str,
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[CandidateVerificationReceipt, ...], tuple[Blocker, ...]]:
    receipts: list[CandidateVerificationReceipt] = []
    blockers: list[Blocker] = []
    for command in commands:
        try:
            result = subprocess.run(
                list(command.argv),
                cwd=worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=dict(environment) if environment is not None else None,
            )
        except OSError as exc:
            blockers.append(Blocker("candidate-verification-launch", f"{command.evidence_id}: {exc}"))
            continue
        provisional = CandidateVerificationReceipt(
            schema_version=CANDIDATE_VERIFICATION_RECEIPT_SCHEMA,
            phase=phase,
            evidence_id=command.evidence_id,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
            argv=command.argv,
            exit_code=result.returncode,
            stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
            receipt_digest="0" * 64,
        )
        receipt = replace(
            provisional,
            receipt_digest=candidate_verification_receipt_digest(provisional),
        )
        receipts.append(receipt)
        blockers.extend(
            candidate_verification_receipt_gate(
                receipt,
                expected_phase=phase,
                expected_commit=candidate_commit,
                expected_tree=candidate_tree,
            )
        )
        if len(result.stdout) > MAX_COMMAND_OUTPUT or len(result.stderr) > MAX_COMMAND_OUTPUT:
            blockers.append(Blocker("candidate-verification-output-limit", command.evidence_id))
    if len(receipts) != len(commands):
        blockers.append(
            Blocker(
                "candidate-verification-receipt-missing",
                "not every configured candidate verification command produced a receipt",
            )
        )
    return tuple(receipts), tuple(dict.fromkeys(blockers))


def _progress_blob_at(
    repo: Repository,
    commit: str,
    progress_path: str,
) -> tuple[str, bytes, str]:
    path = _validate_repo_path(progress_path, "progress_path")
    spec = f"{_validate_oid(commit, 'candidate pre-seal commit')}:{path}"
    oid_result = _git(repo, ["rev-parse", spec], check=False)
    if oid_result.returncode != 0:
        raise TrainError(f"candidate progress file is missing from the pre-seal commit: {path}")
    blob = _validate_oid(oid_result.stdout.decode("ascii").strip(), "candidate progress blob")
    if _object_type(repo, blob) != "blob":
        raise TrainError("candidate progress path does not identify a blob")
    raw = _git(repo, ["cat-file", "blob", blob]).stdout
    if len(raw) > progress.MAX_PROGRESS_BYTES:
        raise TrainError("candidate progress history exceeds the safe size")
    parsed = progress.parse_progress_events(raw, source="candidate-pre-seal-progress")
    if parsed.blockers:
        raise TrainError(
            "candidate progress history is invalid: "
            + "; ".join(item.code for item in parsed.blockers)
        )
    payload = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    if not any(payload.startswith(marker) for marker in progress.OWNER_MARKERS):
        raise TrainError("candidate progress history is not Harness-managed")
    tree_line = _git(repo, ["ls-tree", "-z", commit, "--", path]).stdout
    entries = [item for item in tree_line.split(b"\0") if item]
    if len(entries) != 1:
        raise TrainError("candidate progress tree entry is ambiguous")
    try:
        metadata, recorded_path = entries[0].split(b"\t", 1)
        mode, kind, recorded_blob = metadata.decode("ascii").split(" ")
        decoded_path = recorded_path.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise TrainError("candidate progress tree entry is malformed") from exc
    if kind != "blob" or recorded_blob != blob or decoded_path != path:
        raise TrainError("candidate progress tree entry identity differs")
    if mode not in {"100644", "100755"}:
        raise TrainError("candidate progress file mode is unsupported")
    return blob, raw, mode


def _progress_newline(raw: bytes) -> bytes:
    if b"\r" in raw.replace(b"\r\n", b""):
        raise TrainError("candidate progress history contains a bare carriage return")
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        raise TrainError("candidate progress history contains mixed line endings")
    return b"\r\n" if crlf else b"\n"


def _append_candidate_event(raw: bytes, event_bytes: bytes, newline: bytes) -> bytes:
    if raw.endswith(newline + newline):
        separator = b""
    elif raw.endswith(newline):
        separator = newline
    else:
        separator = newline + newline
    after = raw + separator + event_bytes
    semantic = progress.plan_progress_union(
        branch_base=raw,
        latest_main=raw,
        branch_candidate=after,
    )
    if not semantic.ready or semantic.preview != after:
        raise TrainError(
            "candidate progress event is not an immutable append: "
            + "; ".join(item.code for item in semantic.blockers)
        )
    if len(after) > progress.MAX_PROGRESS_BYTES:
        raise TrainError("candidate progress history would exceed the safe size")
    return after


def _candidate_event_for_plan(
    repo: Repository,
    plan: CandidateRegistrationPlan,
    raw_progress: bytes,
) -> progress.ProgressEventV2:
    parsed = progress.parse_progress_events(raw_progress, source="candidate-pre-seal-progress")
    if parsed.blockers:
        raise TrainError("candidate progress history is invalid")
    timestamp = _git(
        repo,
        ["show", "-s", "--format=%cI", plan.candidate_commit],
    ).stdout.decode("ascii", errors="strict").strip()
    if not timestamp:
        raise TrainError("candidate pre-seal commit has no committed timestamp")
    day = timestamp[:10].replace("-", "")
    if not re.fullmatch(r"[0-9]{8}", day):
        raise TrainError("candidate pre-seal commit timestamp is malformed")
    session_suffix = int(hashlib.sha256(plan.operation_id.encode("ascii")).hexdigest()[:8], 16) % 100
    upstream: list[str] = [
        plan.candidate_ref,
        plan.candidate_evidence_ref,
        f"operation:{plan.operation_id}",
        f"generation:{plan.generation}",
        f"authority:{plan.authority.evidence_digest}",
        f"workspace:{plan.workspace_guard.guard_digest}",
        _principle_gate_evidence_id(plan.principle_gate_binding.binding_digest),
        _dependency_evidence_id(plan.dependency_bindings_digest),
    ]
    for binding in plan.dependency_bindings:
        upstream.extend(
            (
                binding.candidate_ref,
                binding.candidate_evidence_ref,
                f"dependency-registration:{binding.registration_digest}",
                f"dependency-registry:{binding.registry_digest}",
            )
        )
    for acceptance in plan.candidate.acceptance_evidence:
        upstream.extend(acceptance.evidence_ids)
        upstream.extend(f"verify-command:{item}" for item in acceptance.verification_ids)
    return progress.build_progress_event(
        session_id=f"S-{day}-{session_suffix:02d}",
        iteration=plan.iteration,
        scope="candidate",
        event_type="CHECKPOINT",
        event_key=f"candidate:{plan.generation}:sealed",
        occurred_at=timestamp,
        source_ref=plan.feature_ref,
        source_commit=plan.candidate_commit,
        operation_id=plan.operation_id,
        causal_parent=parsed.events[-1].identity if parsed.events else None,
        evidence_refs=tuple(dict.fromkeys(upstream)),
        summary=(
            f"PRD-{plan.iteration} candidate generation {plan.generation} is sealed through "
            "stable candidate/evidence refs and registered verification commands"
        ),
    )


def _git_process_environment(repo: Repository) -> dict[str, str]:
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
    return environment


def _git_with_environment(
    repo: Repository,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [repo.git, "-C", str(cwd or repo.root), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=dict(environment),
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise TrainError(f"git {' '.join(arguments)} failed: {detail or 'unknown Git error'}")
    return result


def _build_candidate_seal_tree(
    repo: Repository,
    *,
    base_tree: str,
    progress_path: str,
    progress_mode: str,
    progress_bytes: bytes,
    persist: bool,
) -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="harness-candidate-tree-") as temporary_name:
        temporary = Path(temporary_name)
        environment = _git_process_environment(repo)
        environment["GIT_INDEX_FILE"] = str(temporary / "index")
        if not persist:
            object_directory = temporary / "objects"
            object_directory.mkdir()
            environment["GIT_OBJECT_DIRECTORY"] = str(object_directory)
            environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(repo.common_dir / "objects")
        progress_blob = _git_with_environment(
            repo,
            ["hash-object", "-w", "--stdin"],
            environment=environment,
            input_bytes=progress_bytes,
        ).stdout.decode("ascii").strip()
        progress_blob = _validate_oid(progress_blob, "candidate sealed progress blob")
        _git_with_environment(repo, ["read-tree", base_tree], environment=environment)
        _git_with_environment(
            repo,
            [
                "update-index",
                "--add",
                "--cacheinfo",
                progress_mode,
                progress_blob,
                progress_path,
            ],
            environment=environment,
        )
        seal_tree = _git_with_environment(
            repo,
            ["write-tree"],
            environment=environment,
        ).stdout.decode("ascii").strip()
        return progress_blob, _validate_oid(seal_tree, "candidate seal tree")


def _candidate_seal_commit_bytes(
    repo: Repository,
    *,
    tree: str,
    parent: str,
    message: str,
) -> tuple[bytes, str]:
    raw_timestamp = _git(
        repo,
        ["show", "-s", "--format=%ct", parent],
    ).stdout.decode("ascii", errors="strict").strip()
    if not raw_timestamp.isdigit():
        raise TrainError("candidate pre-seal commit timestamp is malformed")
    timestamp = int(raw_timestamp) + 1
    identity = f"Harness Lite <harness-lite@local.invalid> {timestamp} +0000"
    raw = (
        f"tree {tree}\n"
        f"parent {parent}\n"
        f"author {identity}\n"
        f"committer {identity}\n"
        "\n"
        f"{message}\n"
    ).encode("utf-8")
    oid = _git(
        repo,
        ["hash-object", "-t", "commit", "--stdin"],
        input_bytes=raw,
    ).stdout.decode("ascii").strip()
    return raw, _validate_oid(oid, "candidate seal commit")


def _plan_register_payload(plan: CandidateRegistrationPlan) -> dict[str, object]:
    data = plan.as_dict()
    data.pop("plan_digest", None)
    return data


def candidate_registration_plan_digest(plan: CandidateRegistrationPlan) -> str:
    return digest(_plan_register_payload(plan))


def plan_register_candidate(
    project_root: str | Path,
    *,
    iteration: str,
    generation: str,
    feature_ref: str,
    feature_worktree: str | Path,
    workspace_owner: str,
    workspace_generation: int,
    workspace_operation_id: str,
    accepted_workspace_plan_digest: str,
    acceptance_evidence: Sequence[AcceptanceEvidence],
    verify_commands: Sequence[VerifyCommand] = (),
    verification_ids: Sequence[str] | None = None,
    base_ref: str | None = None,
    main_ref: str = DEFAULT_MAIN_REF,
    principle_path: str = DEFAULT_PRINCIPLE_PATH,
    operation_id: str | None = None,
) -> CandidateRegistrationPlan:
    """Build a read-only registration plan from exact repository facts."""

    repo = open_repository(project_root)
    number = _validate_iteration(iteration)
    gen = _validate_generation(generation)
    operation = _validate_operation(operation_id or new_operation_id())
    feature = _validate_ref(feature_ref, "feature_ref")
    base = _validate_ref(
        base_ref or f"refs/project-harness/v2/iterations/{number}/base",
        "base_ref",
    )
    main = _validate_ref(main_ref, "main_ref")
    principle = _validate_repo_path(principle_path, "principle_path")
    candidate_ref = f"refs/project-harness/v2/iterations/{number}/candidates/{gen}"
    candidate_evidence_ref = (
        f"refs/project-harness/v2/iterations/{number}/candidate-evidence/{gen}"
    )
    blockers: list[Blocker] = []
    commands = _normalize_verify_commands(verify_commands)
    deprecated_ids = tuple(str(item).strip() for item in (verification_ids or ()))
    if deprecated_ids:
        blockers.append(
            Blocker(
                "candidate-bare-verification-ids-forbidden",
                "candidate registration requires executed VerifyCommand receipts; bare verification_ids are not evidence",
            )
        )
    if not commands:
        blockers.append(
            Blocker(
                "candidate-verification-command-missing",
                "candidate registration requires at least one executable verification command",
            )
        )

    feature_commit = _resolve_ref(repo, feature)
    base_commit = _resolve_ref(repo, base)
    main_commit = _resolve_ref(repo, main)
    if feature_commit is None:
        raise TrainError(f"feature ref does not exist: {feature}")
    if base_commit is None:
        raise TrainError(f"immutable base ref does not exist: {base}")
    if main_commit is None:
        raise TrainError(f"main ref does not exist: {main}")
    for oid, label in (
        (feature_commit, "feature ref"),
        (base_commit, "base ref"),
        (main_commit, "main ref"),
    ):
        if _object_type(repo, oid) != "commit":
            blockers.append(Blocker(f"{label.replace(' ', '-')}-not-commit", f"{label} must resolve to a commit"))
    candidate_tree = _commit_tree(repo, feature_commit)
    resolved_feature_worktree = Path(feature_worktree).resolve()
    blockers.extend(_feature_worktree_gate(repo, resolved_feature_worktree, feature_commit))
    if not _is_ancestor(repo, base_commit, feature_commit):
        blockers.append(Blocker("candidate-base-not-ancestor", "immutable base is not an ancestor of candidate"))
    principle_blob, principle_raw = _blob_at(repo, main_commit, principle)
    principle_sha = hashlib.sha256(principle_raw).hexdigest()
    principle_gate_binding, principle_gate_blockers = (
        _current_candidate_principle_gate_binding(
            repo,
            number,
            authority_ref=feature,
        )
    )
    blockers.extend(principle_gate_blockers)
    authority = _derive_authority(repo, number, feature_commit)
    blockers.extend(authority_evidence_gate(authority))
    workspace_guard, workspace_blockers = _derive_workspace_guard(
        repo,
        iteration=number,
        owner=workspace_owner.strip(),
        generation=workspace_generation,
        operation_id=_validate_operation(workspace_operation_id),
        accepted_plan_digest=_validate_digest(
            accepted_workspace_plan_digest,
            "accepted_workspace_plan_digest",
        ),
        worktree_path=resolved_feature_worktree,
        branch_ref=feature,
        base_commit=base_commit,
    )
    blockers.extend(workspace_blockers)
    if workspace_guard is None:
        workspace_guard = WorkspaceGuardReceipt(
            schema_version=WORKSPACE_GUARD_SCHEMA,
            iteration=number,
            owner=workspace_owner.strip(),
            generation=workspace_generation,
            operation_id=_validate_operation(workspace_operation_id),
            accepted_plan_digest=_validate_digest(
                accepted_workspace_plan_digest,
                "accepted_workspace_plan_digest",
            ),
            worktree_path=str(resolved_feature_worktree),
            branch_ref=feature,
            base_commit=base_commit,
            implementation_ref="refs/heads/main",
            implementation_commit=base_commit,
            reconciliation_ref="refs/heads/main",
            reconciliation_commit=base_commit,
            dependency_refresh_generation=0,
            dependency_bindings=(),
            dependency_bindings_digest=_dependency_bindings_digest(()),
            lease_digest="0" * 64,
            guard_digest="0" * 64,
        )
    included_paths = _diff_paths(
        repo,
        workspace_guard.implementation_commit,
        feature_commit,
    )
    if not included_paths:
        blockers.append(
            Blocker(
                "candidate-empty",
                "candidate has no committed change from its implementation start",
            )
        )
    dependency_bindings = workspace_guard.dependency_bindings
    dependency_bindings_digest = workspace_guard.dependency_bindings_digest
    bound_dependencies = tuple(item.iteration for item in dependency_bindings)
    if bound_dependencies != authority.depends_on:
        blockers.append(
            Blocker(
                "candidate-dependency-bindings-authority-mismatch",
                "ordered writer-lease dependency bindings do not exactly match committed PRD depends_on",
            )
        )
    blockers.extend(
        _dependency_bindings_live_blockers(
            repo,
            dependency_bindings,
            consuming_iteration=number,
            consuming_commit=feature_commit,
        )
    )
    supplied_evidence = tuple(acceptance_evidence)
    supplied_acceptance_ids = tuple(item.acceptance_id for item in supplied_evidence)
    command_ids = {item.evidence_id for item in commands}
    for acceptance_id in supplied_acceptance_ids:
        if acceptance_id not in authority.acceptance_ids:
            blockers.append(
                Blocker(
                    "acceptance-not-in-approved-prd",
                    f"acceptance ID is not present in committed PRD authority: {acceptance_id}",
                )
            )
    for acceptance in supplied_evidence:
        for verification_id in acceptance.verification_ids:
            if verification_id not in command_ids:
                blockers.append(
                    Blocker(
                        "acceptance-verification-command-unregistered",
                        f"{acceptance.acceptance_id} cites verification command not registered for execution: {verification_id}",
                    )
                )
    candidate = build_candidate(
        CandidateInput(
            iteration=number,
            generation=gen,
            base_commit=base_commit,
            candidate_commit=feature_commit,
            candidate_tree=candidate_tree,
            principle_sha256=principle_sha,
            included_paths=included_paths,
            acceptance_ids=authority.acceptance_ids,
            acceptance_evidence=supplied_evidence,
            verification_ids=(),
            prd_approved=not any(item.startswith("prd-") for item in authority.blockers),
            spec_approved=not any(item.startswith("spec-") for item in authority.blockers),
            implementation_authorized="implementation-authorization-missing" not in authority.blockers,
            deviations_resolved=authority.deviation_resolved,
            dirty_scope_owned=not any(item.code == "feature-worktree-dirty" for item in blockers),
        )
    )
    for reason in candidate.blockers:
        if reason != "candidate-verification-missing":
            blockers.append(Blocker("candidate-authority-or-acceptance-invalid", reason))
    existing = _resolve_ref(repo, candidate_ref)
    if existing is not None:
        blockers.append(
            Blocker(
                "candidate-generation-ref-exists",
                f"generation ref already exists and is immutable: {candidate_ref}",
            )
        )
    existing_evidence = _resolve_ref(repo, candidate_evidence_ref)
    if existing_evidence is not None:
        blockers.append(
            Blocker(
                "candidate-evidence-generation-ref-exists",
                f"generation evidence ref already exists and is immutable: {candidate_evidence_ref}",
            )
        )
    provisional = CandidateRegistrationPlan(
        schema_version=REGISTER_PLAN_SCHEMA,
        operation_id=operation,
        project_root=str(repo.root),
        git_common_dir=str(repo.common_dir),
        iteration=number,
        generation=gen,
        feature_ref=feature,
        feature_worktree=str(resolved_feature_worktree),
        base_ref=base,
        main_ref=main,
        main_commit=main_commit,
        candidate_ref=candidate_ref,
        candidate_evidence_ref=candidate_evidence_ref,
        candidate_commit=feature_commit,
        candidate_tree=candidate_tree,
        principle_path=principle,
        principle_blob=principle_blob,
        principle_sha256=principle_sha,
        principle_gate_binding=principle_gate_binding,
        authority=authority,
        workspace_guard=workspace_guard,
        dependency_bindings=dependency_bindings,
        dependency_bindings_digest=dependency_bindings_digest,
        candidate=candidate,
        verify_commands=commands,
        deprecated_verification_ids=deprecated_ids,
        expected_candidate_ref=None,
        expected_candidate_evidence_ref=None,
        plan_digest="0" * 64,
        blockers=tuple(blockers),
    )
    return replace(provisional, plan_digest=candidate_registration_plan_digest(provisional))


def _train_root(repo: Repository) -> Path:
    root = repo.common_dir / "project-harness" / "train" / "v1"
    _assert_train_operational_path(repo, root)
    return root


def _journal_path(repo: Repository, kind: str, operation_id: str) -> Path:
    safe_kind = re.sub(r"[^a-z-]", "", kind)
    if not safe_kind:
        raise TrainError("journal kind is invalid")
    path = _train_root(repo) / "journal" / f"{safe_kind}-{_validate_operation(operation_id)}.json"
    _assert_train_operational_path(repo, path)
    return path


def _lease_path(repo: Repository) -> Path:
    path = _train_root(repo) / "leases" / "main-integration.json"
    _assert_train_operational_path(repo, path)
    return path


def _assert_local_train_json_path(path: Path) -> None:
    """Fail closed for legacy call sites that only carry the derived path."""

    absolute = path.absolute()
    lowered = tuple(part.casefold() for part in absolute.parts)
    marker = ("project-harness", "train", "v1")
    if not any(lowered[index : index + 3] == marker for index in range(max(0, len(lowered) - 2))):
        raise TrainError(f"local JSON path is outside the train registry: {path}")
    try:
        workspace.assert_existing_chain_has_no_links(absolute)
    except workspace.WorkspaceError as exc:
        raise TrainError(f"unsafe train operational path: {exc}") from exc


def _read_json(path: Path, repo: Repository | None = None) -> dict[str, object] | None:
    if repo is not None:
        _assert_train_operational_path(repo, path)
    else:
        _assert_local_train_json_path(path)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TrainError(f"cannot read local journal {path}: {exc}") from exc
    if len(raw) > MAX_JOURNAL_FILE:
        raise TrainError(f"local journal exceeds safe size limit: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrainError(f"local journal is corrupt: {path}") from exc
    if not isinstance(value, dict):
        raise TrainError(f"local journal is not an object: {path}")
    return value


def _write_new_json(path: Path, value: Mapping[str, object], repo: Repository | None = None) -> None:
    if repo is not None:
        _assert_train_operational_path(repo, path)
    else:
        _assert_local_train_json_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if repo is not None:
        _assert_train_operational_path(repo, path)
    else:
        _assert_local_train_json_path(path)
    raw = canonical_json(value) + b"\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise
    except OSError as exc:
        raise TrainError(f"cannot create local journal {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _replace_json(path: Path, value: Mapping[str, object], repo: Repository | None = None) -> None:
    if repo is not None:
        _assert_train_operational_path(repo, path)
    else:
        _assert_local_train_json_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if repo is not None:
            _assert_train_operational_path(repo, path)
        else:
            _assert_local_train_json_path(path)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _trigger(failpoint: Failpoint | None, stage: str) -> None:
    if failpoint is not None:
        failpoint(stage)


def confirmation_token_digest(
    action: str,
    subject_digest: str,
    authorization_id: str,
) -> str:
    normalized_action = action.strip()
    if normalized_action not in {
        "create-candidate-seal",
        "prepare-integration",
        "create-integration-commit",
        "advance-main",
    }:
        raise TrainError("confirmation action is unsupported")
    subject = _validate_digest(subject_digest, "confirmation subject_digest")
    authorization = authorization_id.strip()
    if not AUTHORIZATION_RE.fullmatch(authorization):
        raise TrainError("authorization_id must be an explicit AUTH-* identity")
    return digest(
        {
            "schema_version": CONFIRM_TOKEN_SCHEMA,
            "action": normalized_action,
            "subject_digest": subject,
            "authorization_id": authorization,
        }
    )


def confirmation_token_gate(
    token: ConfirmationToken,
    *,
    action: str,
    subject_digest: str,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if not isinstance(token, ConfirmationToken):
        return (Blocker("confirmation-token-missing", f"{action} requires a structured confirmation token"),)
    if token.schema_version != CONFIRM_TOKEN_SCHEMA:
        blockers.append(Blocker("confirmation-token-schema", "confirmation token schema is unsupported"))
    if token.action != action:
        blockers.append(Blocker("confirmation-token-action", "confirmation token is for another action"))
    if token.subject_digest != subject_digest:
        blockers.append(Blocker("confirmation-token-stale", "confirmation token is bound to another plan"))
    try:
        expected = confirmation_token_digest(token.action, token.subject_digest, token.authorization_id)
    except TrainError as exc:
        blockers.append(Blocker("confirmation-token-invalid", str(exc)))
    else:
        if token.token_digest != expected:
            blockers.append(Blocker("confirmation-token-digest", "confirmation token was changed"))
    return tuple(blockers)


def _candidate_plan_gate(plan: CandidateRegistrationPlan) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = list(plan.blockers)
    if plan.schema_version != REGISTER_PLAN_SCHEMA:
        blockers.append(Blocker("candidate-plan-schema", "candidate registration schema is unsupported"))
    if plan.plan_digest != candidate_registration_plan_digest(plan):
        blockers.append(Blocker("candidate-plan-digest", "candidate registration plan was changed"))
    blockers.extend(authority_evidence_gate(plan.authority))
    repo = open_repository(plan.project_root)
    if plan.principle_gate_binding is None:
        blockers.append(
            Blocker(
                "candidate-principle-gate-binding-missing",
                "candidate registration lacks an exact principle gate binding",
            )
        )
    else:
        blockers.extend(_principle_gate_binding_gate(plan.principle_gate_binding))
        if (
            plan.principle_gate_binding.iteration != plan.iteration
            or plan.principle_gate_binding.authority_ref != plan.feature_ref
            or plan.principle_gate_binding.current_principle_sha256
            != plan.principle_sha256
        ):
            blockers.append(
                Blocker(
                    "candidate-principle-gate-binding-authority",
                    "candidate principle gate binding differs from its plan authority",
                )
            )
    blockers.extend(_workspace_guard_gate(repo, plan.workspace_guard))
    if (
        plan.dependency_bindings != plan.workspace_guard.dependency_bindings
        or plan.dependency_bindings_digest
        != plan.workspace_guard.dependency_bindings_digest
        or plan.dependency_bindings_digest
        != _dependency_bindings_digest(plan.dependency_bindings)
    ):
        blockers.append(
            Blocker(
                "candidate-plan-dependency-bindings",
                "candidate plan does not preserve the exact workspace dependency bindings",
            )
        )
    if tuple(item.iteration for item in plan.dependency_bindings) != plan.authority.depends_on:
        blockers.append(
            Blocker(
                "candidate-dependency-bindings-authority-mismatch",
                "candidate dependency bindings differ from committed PRD depends_on",
            )
        )
    try:
        normalized_commands = _normalize_verify_commands(plan.verify_commands)
    except TrainError as exc:
        blockers.append(Blocker("candidate-verification-command-invalid", str(exc)))
    else:
        if normalized_commands != plan.verify_commands:
            blockers.append(Blocker("candidate-verification-command-drift", "verification commands are not canonical"))
    if plan.deprecated_verification_ids:
        blockers.append(
            Blocker(
                "candidate-bare-verification-ids-forbidden",
                "bare verification IDs cannot authorize candidate registration",
            )
        )
    return tuple(dict.fromkeys(blockers))


def _candidate_preconditions(
    repo: Repository,
    plan: CandidateRegistrationPlan,
    *,
    allow_created_ref: bool,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if os.path.normcase(str(repo.root)) != os.path.normcase(plan.project_root):
        blockers.append(Blocker("candidate-project-drift", "candidate plan belongs to another project root"))
    if os.path.normcase(str(repo.common_dir)) != os.path.normcase(plan.git_common_dir):
        blockers.append(Blocker("candidate-common-dir-drift", "Git common directory changed"))
    feature = _resolve_ref(repo, plan.feature_ref)
    if feature != plan.candidate_commit:
        blockers.append(Blocker("candidate-feature-ref-drift", "feature ref no longer names candidate commit"))
    base = _resolve_ref(repo, plan.base_ref)
    if base != plan.candidate.base_commit:
        blockers.append(Blocker("candidate-base-drift", "immutable base ref changed"))
    main = _resolve_ref(repo, plan.main_ref)
    if main != plan.main_commit:
        blockers.append(Blocker("candidate-main-drift", "main changed after candidate plan"))
    if _commit_tree(repo, plan.candidate_commit) != plan.candidate_tree:
        blockers.append(Blocker("candidate-tree-drift", "candidate commit tree changed or is invalid"))
    try:
        principle_blob, principle_raw = _blob_at(repo, plan.main_commit, plan.principle_path)
    except TrainError as exc:
        blockers.append(Blocker("candidate-principle-unreadable", str(exc)))
    else:
        if principle_blob != plan.principle_blob or hashlib.sha256(principle_raw).hexdigest() != plan.principle_sha256:
            blockers.append(Blocker("candidate-principle-drift", "main principle identity changed"))
    current_principle_binding, principle_blockers = (
        _current_candidate_principle_gate_binding(
            repo,
            plan.iteration,
            authority_ref=plan.feature_ref,
        )
    )
    blockers.extend(principle_blockers)
    if (
        not principle_blockers
        and current_principle_binding != plan.principle_gate_binding
    ):
        blockers.append(
            Blocker(
                "candidate-principle-gate-binding-stale",
                "live principle gate identity changed after candidate planning",
            )
        )
    authority = _derive_authority(repo, plan.iteration, plan.candidate_commit)
    if authority.evidence_digest != plan.authority.evidence_digest:
        blockers.append(Blocker("candidate-authority-drift", "committed PRD/SPEC/deviation authority differs"))
    blockers.extend(_workspace_guard_gate(repo, plan.workspace_guard))
    blockers.extend(
        _dependency_bindings_live_blockers(
            repo,
            plan.dependency_bindings,
            consuming_iteration=plan.iteration,
            consuming_commit=plan.candidate_commit,
        )
    )
    blockers.extend(
        _feature_worktree_gate(repo, Path(plan.feature_worktree), plan.candidate_commit)
    )
    current_candidate = _resolve_ref(repo, plan.candidate_ref)
    if current_candidate is not None and not (
        allow_created_ref and current_candidate == plan.candidate_commit
    ):
        blockers.append(Blocker("candidate-generation-ref-collision", "candidate generation ref is not absent"))
    current_evidence = _resolve_ref(repo, plan.candidate_evidence_ref)
    if current_evidence is not None and not allow_created_ref:
        blockers.append(
            Blocker(
                "candidate-evidence-generation-ref-collision",
                "candidate evidence generation ref is not absent",
            )
        )
    return tuple(blockers)


def _seal_plan_payload(plan: CandidateSealPlan) -> dict[str, object]:
    data = plan.as_dict()
    data.pop("seal_plan_digest", None)
    return data


def candidate_seal_plan_digest(plan: CandidateSealPlan) -> str:
    return digest(_seal_plan_payload(plan))


def _decode_exact_b64(value: str, label: str) -> bytes:
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise TrainError(f"{label} is not canonical base64") from exc
    if base64.b64encode(raw).decode("ascii") != value:
        raise TrainError(f"{label} is not canonical base64")
    return raw


def _candidate_seal_plan_gate(plan: CandidateSealPlan) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = list(plan.blockers)
    if not isinstance(plan, CandidateSealPlan):
        return (Blocker("candidate-seal-plan-type", "candidate seal plan has an unsupported type"),)
    if plan.schema_version != SEAL_PLAN_SCHEMA:
        blockers.append(Blocker("candidate-seal-plan-schema", "candidate seal plan schema is unsupported"))
    if plan.seal_plan_digest != candidate_seal_plan_digest(plan):
        blockers.append(Blocker("candidate-seal-plan-digest", "candidate seal plan was changed"))
    blockers.extend(_candidate_plan_gate(plan.registration_plan))
    registration = plan.registration_plan
    if (
        registration.principle_gate_binding is None
        or plan.principle_gate_binding != registration.principle_gate_binding
    ):
        blockers.append(
            Blocker(
                "candidate-seal-principle-gate-binding",
                "candidate seal does not preserve its exact principle gate binding",
            )
        )
    else:
        blockers.extend(_principle_gate_binding_gate(plan.principle_gate_binding))
    if (
        plan.dependency_bindings != registration.dependency_bindings
        or plan.dependency_bindings_digest != registration.dependency_bindings_digest
        or plan.dependency_bindings_digest
        != _dependency_bindings_digest(plan.dependency_bindings)
    ):
        blockers.append(
            Blocker(
                "candidate-seal-dependency-bindings",
                "candidate seal does not preserve its exact registration dependency bindings",
            )
        )
    if plan.parent_commits != (registration.candidate_commit,):
        blockers.append(Blocker("candidate-seal-parent", "candidate seal parent differs from the pre-seal commit"))
    if not plan.commit_message or "\n" in plan.commit_message or "\r" in plan.commit_message:
        blockers.append(Blocker("candidate-seal-message", "candidate seal commit message is not one exact line"))
    try:
        event_bytes = _decode_exact_b64(plan.progress_event_bytes_b64, "candidate progress event bytes")
        commit_bytes = _decode_exact_b64(plan.commit_bytes_b64, "candidate seal commit bytes")
    except TrainError as exc:
        blockers.append(Blocker("candidate-seal-bytes", str(exc)))
    else:
        if plan.seal_commit.encode("ascii") in event_bytes:
            blockers.append(
                Blocker(
                    "candidate-event-seal-cycle",
                    "candidate progress event must not name the seal commit",
                )
            )
        computed = _git(
            open_repository(registration.project_root),
            ["hash-object", "-t", "commit", "--stdin"],
            input_bytes=commit_bytes,
        ).stdout.decode("ascii").strip()
        if computed != plan.seal_commit:
            blockers.append(Blocker("candidate-seal-commit-bytes", "seal commit bytes differ from its identity"))
        newline = b"\r\n" if b"\r\n" in event_bytes else b"\n"
        if plan.progress_event.render(newline=newline) != event_bytes:
            blockers.append(Blocker("candidate-progress-event-bytes", "progress event bytes differ from its model"))
    evidence_refs = set(plan.progress_event.evidence_refs)
    if registration.candidate_ref not in evidence_refs or registration.candidate_evidence_ref not in evidence_refs:
        blockers.append(
            Blocker(
                "candidate-progress-stable-refs-missing",
                "candidate event must bind the stable candidate and evidence ref names",
            )
        )
    if _dependency_evidence_id(plan.dependency_bindings_digest) not in evidence_refs:
        blockers.append(
            Blocker(
                "candidate-progress-dependency-bindings-missing",
                "candidate event must bind the exact dependency binding digest",
            )
        )
    for binding in plan.dependency_bindings:
        if (
            binding.candidate_ref not in evidence_refs
            or binding.candidate_evidence_ref not in evidence_refs
        ):
            blockers.append(
                Blocker(
                    "candidate-progress-dependency-refs-missing",
                    f"candidate event does not cite PRD-{binding.iteration} stable refs",
                )
            )
    for receipt in plan.pre_seal_verification_receipts:
        blockers.extend(
            candidate_verification_receipt_gate(
                receipt,
                expected_phase="pre-seal",
                expected_commit=registration.candidate_commit,
                expected_tree=registration.candidate_tree,
            )
        )
    if tuple(item.evidence_id for item in plan.pre_seal_verification_receipts) != tuple(
        item.evidence_id for item in registration.verify_commands
    ):
        blockers.append(
            Blocker(
                "candidate-pre-seal-receipt-set",
                "pre-seal verification receipts do not exactly cover registered commands",
            )
        )
    return tuple(dict.fromkeys(blockers))


def prepare_candidate_registration(
    plan: CandidateRegistrationPlan,
    *,
    accepted_plan_digest: str,
) -> CandidateSealPlan:
    """Run real pre-seal verification and produce an exact, confirmable commit plan.

    Repository refs, commits, trees, and working files are not changed.  Tree
    objects used for planning live only in a temporary object database.
    """

    blockers = list(_candidate_plan_gate(plan))
    if accepted_plan_digest != plan.plan_digest:
        blockers.append(Blocker("candidate-plan-not-accepted", "accepted digest does not match candidate plan"))
    if blockers:
        raise TrainError("candidate seal preparation blocked: " + "; ".join(item.code for item in blockers))
    repo = open_repository(plan.project_root)
    current_blockers = _candidate_preconditions(repo, plan, allow_created_ref=False)
    if current_blockers:
        raise TrainError("candidate seal preparation stale: " + "; ".join(item.code for item in current_blockers))
    receipts, verification_blockers = _candidate_verification_receipts(
        plan.verify_commands,
        Path(plan.feature_worktree),
        phase="pre-seal",
        candidate_commit=plan.candidate_commit,
        candidate_tree=plan.candidate_tree,
    )
    post_verify_blockers = _candidate_preconditions(repo, plan, allow_created_ref=False)
    blockers = [*verification_blockers, *post_verify_blockers]
    progress_blob, progress_before, progress_mode = _progress_blob_at(
        repo,
        plan.candidate_commit,
        DEFAULT_PROGRESS_PATH,
    )
    newline = _progress_newline(progress_before)
    event = _candidate_event_for_plan(repo, plan, progress_before)
    event_bytes = event.render(newline=newline)
    progress_after = _append_candidate_event(progress_before, event_bytes, newline)
    _sealed_progress_blob, seal_tree = _build_candidate_seal_tree(
        repo,
        base_tree=plan.candidate_tree,
        progress_path=DEFAULT_PROGRESS_PATH,
        progress_mode=progress_mode,
        progress_bytes=progress_after,
        persist=False,
    )
    commit_message = f"candidate(PRD-{plan.iteration}): seal generation {plan.generation}"
    commit_bytes, seal_commit = _candidate_seal_commit_bytes(
        repo,
        tree=seal_tree,
        parent=plan.candidate_commit,
        message=commit_message,
    )
    provisional = CandidateSealPlan(
        schema_version=SEAL_PLAN_SCHEMA,
        registration_plan=plan,
        principle_gate_binding=plan.principle_gate_binding,  # type: ignore[arg-type]
        dependency_bindings=plan.dependency_bindings,
        dependency_bindings_digest=plan.dependency_bindings_digest,
        progress_path=DEFAULT_PROGRESS_PATH,
        progress_blob=progress_blob,
        progress_before_sha256=hashlib.sha256(progress_before).hexdigest(),
        progress_after_sha256=hashlib.sha256(progress_after).hexdigest(),
        progress_event=event,
        progress_event_bytes_b64=base64.b64encode(event_bytes).decode("ascii"),
        seal_tree=seal_tree,
        parent_commits=(plan.candidate_commit,),
        commit_message=commit_message,
        commit_bytes_b64=base64.b64encode(commit_bytes).decode("ascii"),
        seal_commit=seal_commit,
        pre_seal_verification_receipts=receipts,
        seal_plan_digest="0" * 64,
        blockers=tuple(dict.fromkeys(blockers)),
    )
    return replace(provisional, seal_plan_digest=candidate_seal_plan_digest(provisional))


@contextlib.contextmanager
def _materialized_candidate_tree(
    repo: Repository,
    *,
    seal_commit: str,
    seal_tree: str,
) -> Iterable[tuple[Path, Mapping[str, str]]]:
    with tempfile.TemporaryDirectory(prefix="harness-candidate-verify-") as temporary_name:
        temporary = Path(temporary_name)
        worktree = temporary / "tree"
        git_dir = temporary / "git"
        worktree.mkdir()
        environment = _git_process_environment(repo)
        init = subprocess.run(
            [repo.git, "init", "--bare", str(git_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )
        if init.returncode != 0:
            raise TrainError("cannot initialize isolated candidate verification repository")
        environment.update(
            {
                "GIT_DIR": str(git_dir),
                "GIT_WORK_TREE": str(worktree),
                "GIT_INDEX_FILE": str(temporary / "index"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(repo.common_dir / "objects"),
            }
        )
        _git_with_environment(
            repo,
            ["update-ref", "refs/heads/candidate", seal_commit],
            environment=environment,
        )
        _git_with_environment(
            repo,
            ["symbolic-ref", "HEAD", "refs/heads/candidate"],
            environment=environment,
        )
        _git_with_environment(repo, ["read-tree", seal_tree], environment=environment)
        prefix = str(worktree) + os.sep
        _git_with_environment(
            repo,
            ["checkout-index", "--all", "--force", f"--prefix={prefix}"],
            environment=environment,
        )
        _git_with_environment(
            repo,
            ["update-index", "--refresh", "--"],
            environment=environment,
        )
        yield worktree, environment
        index_tree = _git_with_environment(
            repo,
            ["write-tree"],
            environment=environment,
        ).stdout.decode("ascii").strip()
        changed = _git_with_environment(
            repo,
            ["diff-files", "--quiet", "--"],
            environment=environment,
            check=False,
        ).returncode
        if index_tree != seal_tree or changed != 0:
            raise TrainError("candidate verification mutated the exact sealed tracked tree")


def _candidate_evidence_from_dict(value: object) -> CandidateEvidence:
    if not isinstance(value, Mapping):
        raise TrainError("candidate evidence journal payload is invalid")
    acceptance_raw = value.get("acceptance_evidence")
    if not isinstance(acceptance_raw, list):
        raise TrainError("candidate acceptance evidence journal payload is invalid")
    try:
        acceptance = tuple(
            AcceptanceEvidence(
                acceptance_id=str(item["acceptance_id"]),
                evidence_ids=tuple(str(entry) for entry in item["evidence_ids"]),
                verification_ids=tuple(str(entry) for entry in item["verification_ids"]),
            )
            for item in acceptance_raw
            if isinstance(item, Mapping)
        )
        candidate = CandidateEvidence(
            schema_version=str(value["schema_version"]),
            iteration=str(value["iteration"]),
            generation=str(value["generation"]),
            base_commit=str(value["base_commit"]),
            candidate_commit=str(value["candidate_commit"]),
            candidate_tree=str(value["candidate_tree"]),
            principle_sha256=str(value["principle_sha256"]),
            included_paths=tuple(str(item) for item in value["included_paths"]),
            acceptance_ids=tuple(str(item) for item in value["acceptance_ids"]),
            acceptance_evidence=acceptance,
            verification_ids=tuple(str(item) for item in value["verification_ids"]),
            evidence_digest=str(value["evidence_digest"]),
            verified=value["verified"] is True,
            blockers=tuple(str(item) for item in value["blockers"]),
        )
    except (KeyError, TypeError) as exc:
        raise TrainError("candidate evidence journal payload is malformed") from exc
    if not candidate_evidence_gate(candidate).allowed:
        raise TrainError("candidate evidence journal payload fails the core gate")
    return candidate


def _candidate_receipt_from_dict(value: object) -> CandidateVerificationReceipt:
    if not isinstance(value, Mapping):
        raise TrainError("candidate verification receipt journal payload is invalid")
    try:
        receipt = CandidateVerificationReceipt(
            schema_version=str(value["schema_version"]),
            phase=str(value["phase"]),  # type: ignore[arg-type]
            evidence_id=str(value["evidence_id"]),
            candidate_commit=str(value["candidate_commit"]),
            candidate_tree=str(value["candidate_tree"]),
            argv=tuple(str(item) for item in value["argv"]),
            exit_code=int(value["exit_code"]),
            stdout_sha256=str(value["stdout_sha256"]),
            stderr_sha256=str(value["stderr_sha256"]),
            receipt_digest=str(value["receipt_digest"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TrainError("candidate verification receipt journal payload is malformed") from exc
    blockers = candidate_verification_receipt_gate(receipt)
    if blockers:
        raise TrainError("candidate verification receipt journal payload is invalid")
    return receipt


def _candidate_seal_receipts_gate(
    receipts: tuple[CandidateVerificationReceipt, ...],
    plan: CandidateSealPlan,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    expected_commands = plan.registration_plan.verify_commands
    if len(receipts) != len(expected_commands):
        blockers.append(
            Blocker(
                "candidate-seal-receipt-set",
                "sealed verification receipts do not exactly cover configured commands",
            )
        )
    for index, receipt in enumerate(receipts):
        blockers.extend(
            candidate_verification_receipt_gate(
                receipt,
                expected_phase="seal",
                expected_commit=plan.seal_commit,
                expected_tree=plan.seal_tree,
            )
        )
        if index < len(expected_commands):
            command = expected_commands[index]
            if receipt.evidence_id != command.evidence_id or receipt.argv != command.argv:
                blockers.append(
                    Blocker(
                        "candidate-seal-receipt-command-drift",
                        receipt.evidence_id,
                    )
                )
    return tuple(dict.fromkeys(blockers))


def _candidate_metadata_payload(value: Mapping[str, object]) -> dict[str, object]:
    payload = dict(value)
    payload.pop("metadata_digest", None)
    return payload


def _candidate_evidence_material(
    repo: Repository,
    plan: CandidateSealPlan,
    seal_receipts: tuple[CandidateVerificationReceipt, ...],
    *,
    seal_authorization_id: str,
) -> tuple[CandidateEvidence, dict[str, object], bytes, str]:
    registration = plan.registration_plan
    receipt_blockers = _candidate_seal_receipts_gate(seal_receipts, plan)
    if receipt_blockers:
        raise TrainError(
            "candidate seal receipts are invalid: "
            + "; ".join(item.code for item in receipt_blockers)
        )
    receipt_by_id = {item.evidence_id: item for item in seal_receipts}
    acceptance_evidence: list[AcceptanceEvidence] = []
    for acceptance in registration.candidate.acceptance_evidence:
        mapped: list[str] = []
        for command_id in acceptance.verification_ids:
            receipt = receipt_by_id.get(command_id)
            if receipt is None:
                raise TrainError(f"acceptance verification command has no sealed receipt: {command_id}")
            mapped.append(f"candidate-verification:{receipt.receipt_digest}")
        acceptance_evidence.append(
            AcceptanceEvidence(
                acceptance_id=acceptance.acceptance_id,
                evidence_ids=acceptance.evidence_ids,
                verification_ids=tuple(mapped),
            )
        )
    candidate = build_candidate(
        CandidateInput(
            iteration=registration.iteration,
            generation=registration.generation,
            base_commit=registration.candidate.base_commit,
            candidate_commit=plan.seal_commit,
            candidate_tree=plan.seal_tree,
            principle_sha256=registration.principle_sha256,
            included_paths=_diff_paths(
                repo,
                registration.workspace_guard.implementation_commit,
                plan.seal_commit,
            ),
            acceptance_ids=registration.authority.acceptance_ids,
            acceptance_evidence=tuple(acceptance_evidence),
            verification_ids=(
                *(f"candidate-verification:{item.receipt_digest}" for item in seal_receipts),
                _principle_gate_evidence_id(
                    registration.principle_gate_binding.binding_digest  # type: ignore[union-attr]
                ),
                _dependency_evidence_id(registration.dependency_bindings_digest),
            ),
            prd_approved=True,
            spec_approved=True,
            implementation_authorized=True,
            deviations_resolved=registration.authority.deviation_resolved,
            dirty_scope_owned=True,
        )
    )
    gate = candidate_evidence_gate(candidate)
    if not gate.allowed:
        raise TrainError("sealed candidate evidence failed the core gate: " + "; ".join(gate.blockers))
    metadata: dict[str, object] = {
        "schema_version": CANDIDATE_EVIDENCE_METADATA_SCHEMA,
        "operation_id": registration.operation_id,
        "iteration": registration.iteration,
        "generation": registration.generation,
        "candidate_ref": registration.candidate_ref,
        "candidate_evidence_ref": registration.candidate_evidence_ref,
        "feature_ref": registration.feature_ref,
        "base_ref": registration.base_ref,
        "main_ref": registration.main_ref,
        "workspace_ref": registration.workspace_guard.branch_ref,
        "pre_seal_commit": registration.candidate_commit,
        "pre_seal_tree": registration.candidate_tree,
        "seal_commit": plan.seal_commit,
        "seal_tree": plan.seal_tree,
        "parent_commits": list(plan.parent_commits),
        "commit_message": plan.commit_message,
        "commit_bytes_sha256": hashlib.sha256(_decode_exact_b64(plan.commit_bytes_b64, "commit bytes")).hexdigest(),
        "progress_path": plan.progress_path,
        "progress_blob": plan.progress_blob,
        "progress_before_sha256": plan.progress_before_sha256,
        "progress_after_sha256": plan.progress_after_sha256,
        "progress_event": plan.progress_event.as_dict(),
        "progress_event_bytes_sha256": hashlib.sha256(
            _decode_exact_b64(plan.progress_event_bytes_b64, "progress event bytes")
        ).hexdigest(),
        "authority_evidence_digest": registration.authority.evidence_digest,
        "workspace_guard": registration.workspace_guard.as_dict(),
        "workspace_guard_digest": registration.workspace_guard.guard_digest,
        "implementation_commit": registration.workspace_guard.implementation_commit,
        "principle_gate_binding": plan.principle_gate_binding.as_dict(),
        "depends_on": list(registration.authority.depends_on),
        "dependency_bindings": [
            item.as_dict() for item in registration.dependency_bindings
        ],
        "dependency_bindings_digest": registration.dependency_bindings_digest,
        "upstream_evidence_ids": list(
            dict.fromkeys(
                evidence
                for acceptance in registration.candidate.acceptance_evidence
                for evidence in acceptance.evidence_ids
            )
        ),
        "pre_seal_verification_receipts": [
            item.as_dict() for item in plan.pre_seal_verification_receipts
        ],
        "seal_verification_receipts": [item.as_dict() for item in seal_receipts],
        "candidate_evidence": candidate.as_dict(),
        "seal_authorization_id": seal_authorization_id,
        "registration_plan_digest": registration.plan_digest,
        "seal_plan_digest": plan.seal_plan_digest,
        "pushed": False,
        "metadata_digest": "0" * 64,
    }
    metadata["metadata_digest"] = digest(_candidate_metadata_payload(metadata))
    raw = canonical_json(metadata) + b"\n"
    blob = _git(repo, ["hash-object", "--stdin"], input_bytes=raw).stdout.decode("ascii").strip()
    return candidate, metadata, raw, _validate_oid(blob, "candidate evidence blob")


def _registered_from_material(
    plan: CandidateSealPlan,
    journal_path: Path,
    *,
    candidate: CandidateEvidence,
    receipts: tuple[CandidateVerificationReceipt, ...],
    evidence_blob: str,
    evidence_metadata_digest: str,
    seal_authorization_id: str,
    idempotent: bool,
) -> RegisteredCandidate:
    registration = plan.registration_plan
    provisional = RegisteredCandidate(
        schema_version=REGISTER_RESULT_SCHEMA,
        operation_id=registration.operation_id,
        project_root=registration.project_root,
        iteration=registration.iteration,
        generation=registration.generation,
        candidate_ref=registration.candidate_ref,
        candidate_evidence_ref=registration.candidate_evidence_ref,
        candidate_evidence_blob=evidence_blob,
        candidate_evidence_metadata_digest=evidence_metadata_digest,
        pre_seal_commit=registration.candidate_commit,
        pre_seal_tree=registration.candidate_tree,
        candidate_commit=plan.seal_commit,
        candidate_tree=plan.seal_tree,
        base_ref=registration.base_ref,
        base_commit=registration.candidate.base_commit,
        implementation_commit=registration.workspace_guard.implementation_commit,
        principle_sha256=registration.principle_sha256,
        principle_gate_binding=plan.principle_gate_binding,
        authority_evidence_digest=registration.authority.evidence_digest,
        workspace_guard=registration.workspace_guard,
        workspace_guard_digest=registration.workspace_guard.guard_digest,
        depends_on=registration.authority.depends_on,
        dependency_bindings=registration.dependency_bindings,
        dependency_bindings_digest=registration.dependency_bindings_digest,
        candidate_evidence=candidate,
        verification_receipts=receipts,
        seal_authorization_id=seal_authorization_id,
        registration_plan_digest=registration.plan_digest,
        seal_plan_digest=plan.seal_plan_digest,
        registration_digest="0" * 64,
        journal_path=str(journal_path),
        idempotent=idempotent,
    )
    return replace(provisional, registration_digest=registered_candidate_digest(provisional))


def _registered_candidate_payload(candidate: RegisteredCandidate) -> dict[str, object]:
    data = candidate.as_dict()
    data.pop("registration_digest", None)
    data.pop("idempotent", None)
    return data


def registered_candidate_digest(candidate: RegisteredCandidate) -> str:
    return digest(_registered_candidate_payload(candidate))


def _candidate_refs_state(
    repo: Repository,
    plan: CandidateSealPlan,
    *,
    evidence_blob: str | None,
) -> str:
    registration = plan.registration_plan
    candidate = _resolve_ref(repo, registration.candidate_ref)
    evidence = _resolve_ref(repo, registration.candidate_evidence_ref)
    if candidate is None and evidence is None:
        return "absent"
    if candidate == plan.seal_commit and evidence_blob is not None and evidence == evidence_blob:
        return "exact"
    return "mismatch"


def _apply_candidate_ref_transaction(
    repo: Repository,
    plan: CandidateSealPlan,
    *,
    evidence_blob: str,
) -> None:
    registration = plan.registration_plan
    expected: list[tuple[str, str]] = [
        (registration.feature_ref, registration.candidate_commit),
        (registration.base_ref, registration.candidate.base_commit),
        (registration.main_ref, registration.main_commit),
        (registration.workspace_guard.branch_ref, registration.candidate_commit),
    ]
    unique: dict[str, str] = {}
    for reference, oid in expected:
        prior = unique.get(reference)
        if prior is not None and prior != oid:
            raise TrainError(f"candidate ref transaction has conflicting verification identities: {reference}")
        unique[reference] = oid
    lines = ["start"]
    lines.extend(f"verify {reference} {oid}" for reference, oid in unique.items())
    lines.append(f"create {registration.candidate_ref} {plan.seal_commit}")
    lines.append(f"create {registration.candidate_evidence_ref} {evidence_blob}")
    lines.extend(("prepare", "commit", ""))
    result = _git(
        repo,
        ["update-ref", "--stdin"],
        input_bytes="\n".join(lines).encode("ascii"),
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise TrainError(f"candidate/evidence ref transaction failed: {detail}")


def apply_register_candidate(
    plan: CandidateRegistrationPlan | CandidateSealPlan,
    *,
    accepted_plan_digest: str | None = None,
    accepted_seal_plan_digest: str | None = None,
    confirmation_token: ConfirmationToken | None = None,
    failpoint: Failpoint | None = None,
) -> RegisteredCandidate:
    """Create a confirmed candidate seal and atomically publish its two refs.

    Passing the legacy registration plan directly is deliberately rejected:
    a caller must first obtain a real-verification ``CandidateSealPlan``.
    """

    if isinstance(plan, CandidateRegistrationPlan):
        raise TrainError(
            "candidate-seal-preparation-required: bare verification IDs and direct ref registration are no longer accepted"
        )
    if not isinstance(plan, CandidateSealPlan):
        raise TypeError("plan must be CandidateSealPlan")
    blockers = list(_candidate_seal_plan_gate(plan))
    if accepted_seal_plan_digest != plan.seal_plan_digest:
        blockers.append(
            Blocker(
                "candidate-seal-plan-not-accepted",
                "accepted digest does not match the exact candidate seal plan",
            )
        )
    blockers.extend(
        confirmation_token_gate(
            confirmation_token,  # type: ignore[arg-type]
            action="create-candidate-seal",
            subject_digest=plan.seal_plan_digest,
        )
    )
    if blockers:
        raise TrainError("candidate registration blocked: " + "; ".join(item.code for item in blockers))
    assert confirmation_token is not None
    registration = plan.registration_plan
    repo = open_repository(registration.project_root)
    journal_path = _journal_path(repo, "candidate", registration.operation_id)
    journal = _read_json(journal_path, repo)
    if journal is not None:
        if (
            journal.get("schema_version") != JOURNAL_SCHEMA
            or journal.get("kind") != "candidate-register"
            or journal.get("operation_id") != registration.operation_id
            or journal.get("registration_plan_digest") != registration.plan_digest
            or journal.get("seal_plan_digest") != plan.seal_plan_digest
            or journal.get("seal_authorization_id") != confirmation_token.authorization_id
        ):
            raise TrainError("candidate journal identity is invalid")
        evidence_blob_raw = journal.get("candidate_evidence_blob")
        evidence_blob = str(evidence_blob_raw) if isinstance(evidence_blob_raw, str) else None
        state = _candidate_refs_state(repo, plan, evidence_blob=evidence_blob)
        if state == "mismatch":
            raise TrainError("candidate/evidence journal ref mismatch requires reconcile")
        if state == "exact":
            candidate = _candidate_evidence_from_dict(journal.get("candidate_evidence"))
            receipt_values = journal.get("verification_receipts")
            if not isinstance(receipt_values, list):
                raise TrainError("candidate journal verification receipts are missing")
            receipts = tuple(_candidate_receipt_from_dict(item) for item in receipt_values)
            receipt_blockers = _candidate_seal_receipts_gate(receipts, plan)
            if receipt_blockers:
                raise TrainError(
                    "candidate journal sealed verification receipts differ: "
                    + "; ".join(item.code for item in receipt_blockers)
                )
            metadata_digest = str(journal.get("candidate_evidence_metadata_digest", ""))
            _validate_digest(metadata_digest, "candidate evidence metadata digest")
            raw_b64 = journal.get("candidate_evidence_bytes_b64")
            if not isinstance(raw_b64, str):
                raise TrainError("candidate journal evidence bytes are missing")
            raw = _decode_exact_b64(raw_b64, "candidate evidence bytes")
            if _git(repo, ["cat-file", "blob", evidence_blob]).stdout != raw:
                raise TrainError("candidate evidence ref blob differs from journal bytes")
            try:
                parsed_metadata = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TrainError("candidate journal evidence bytes are invalid JSON") from exc
            if (
                not isinstance(parsed_metadata, dict)
                or parsed_metadata.get("metadata_digest") != metadata_digest
                or digest(_candidate_metadata_payload(parsed_metadata)) != metadata_digest
                or canonical_json(parsed_metadata) + b"\n" != raw
            ):
                raise TrainError("candidate journal evidence bytes/digest differ")
            if (
                parsed_metadata.get("principle_gate_binding")
                != plan.principle_gate_binding.as_dict()
                or journal.get("principle_gate_binding")
                != plan.principle_gate_binding.as_dict()
                or parsed_metadata.get("dependency_bindings")
                != [item.as_dict() for item in registration.dependency_bindings]
                or parsed_metadata.get("dependency_bindings_digest")
                != registration.dependency_bindings_digest
                or _dependency_evidence_id(registration.dependency_bindings_digest)
                not in candidate.verification_ids
            ):
                raise TrainError(
                    "candidate journal evidence authority bindings differ from the accepted plan"
                )
            if journal.get("status") != "complete":
                journal["status"] = "complete"
                _replace_json(journal_path, journal, repo)
            return _registered_from_material(
                plan,
                journal_path,
                candidate=candidate,
                receipts=receipts,
                evidence_blob=evidence_blob,
                evidence_metadata_digest=metadata_digest,
                seal_authorization_id=confirmation_token.authorization_id,
                idempotent=True,
            )
    current_blockers = _candidate_preconditions(repo, registration, allow_created_ref=False)
    if current_blockers:
        raise TrainError("candidate registration stale: " + "; ".join(item.code for item in current_blockers))
    progress_blob, progress_before, progress_mode = _progress_blob_at(
        repo,
        registration.candidate_commit,
        plan.progress_path,
    )
    if progress_blob != plan.progress_blob or hashlib.sha256(progress_before).hexdigest() != plan.progress_before_sha256:
        raise TrainError("candidate progress source changed after seal planning")
    event_bytes = _decode_exact_b64(plan.progress_event_bytes_b64, "candidate progress event bytes")
    progress_after = _append_candidate_event(progress_before, event_bytes, _progress_newline(progress_before))
    if hashlib.sha256(progress_after).hexdigest() != plan.progress_after_sha256:
        raise TrainError("candidate sealed progress bytes differ from the accepted plan")
    if journal is None:
        journal = {
            "schema_version": JOURNAL_SCHEMA,
            "kind": "candidate-register",
            "operation_id": registration.operation_id,
            "plan_digest": registration.plan_digest,
            "registration_plan_digest": registration.plan_digest,
            "seal_plan_digest": plan.seal_plan_digest,
            "accepted_seal_plan_digest": accepted_seal_plan_digest,
            "seal_authorization_id": confirmation_token.authorization_id,
            "candidate_ref": registration.candidate_ref,
            "candidate_evidence_ref": registration.candidate_evidence_ref,
            "pre_seal_commit": registration.candidate_commit,
            "pre_seal_tree": registration.candidate_tree,
            "seal_commit": plan.seal_commit,
            "seal_tree": plan.seal_tree,
            "candidate_commit": plan.seal_commit,
            "candidate_tree": plan.seal_tree,
            "iteration": registration.iteration,
            "generation": registration.generation,
            "base_ref": registration.base_ref,
            "base_commit": registration.candidate.base_commit,
            "implementation_commit": registration.workspace_guard.implementation_commit,
            "principle_sha256": registration.principle_sha256,
            "principle_gate_binding": plan.principle_gate_binding.as_dict(),
            "authority_evidence_digest": registration.authority.evidence_digest,
            "workspace_guard_digest": registration.workspace_guard.guard_digest,
            "depends_on": list(registration.authority.depends_on),
            "dependency_bindings": [
                item.as_dict() for item in registration.dependency_bindings
            ],
            "dependency_bindings_digest": registration.dependency_bindings_digest,
            "parent_commits": list(plan.parent_commits),
            "commit_message": plan.commit_message,
            "commit_bytes_b64": plan.commit_bytes_b64,
            "progress_path": plan.progress_path,
            "progress_blob": plan.progress_blob,
            "progress_before_sha256": plan.progress_before_sha256,
            "progress_after_sha256": plan.progress_after_sha256,
            "progress_event": plan.progress_event.as_dict(),
            "progress_event_bytes_b64": plan.progress_event_bytes_b64,
            "pre_seal_verification_receipts": [
                item.as_dict() for item in plan.pre_seal_verification_receipts
            ],
            "authority_receipt": registration.authority.as_dict(),
            "workspace_guard": registration.workspace_guard.as_dict(),
            "status": "planned",
            "pushed": False,
        }
        try:
            _write_new_json(journal_path, journal, repo)
        except FileExistsError:
            raise TrainError("candidate journal was created concurrently; retry for reconcile")
    _trigger(failpoint, "candidate-after-journal")
    persisted_progress_blob, persisted_tree = _build_candidate_seal_tree(
        repo,
        base_tree=registration.candidate_tree,
        progress_path=plan.progress_path,
        progress_mode=progress_mode,
        progress_bytes=progress_after,
        persist=True,
    )
    if persisted_tree != plan.seal_tree:
        raise TrainError("persisted candidate seal tree differs from the confirmed plan")
    commit_bytes = _decode_exact_b64(plan.commit_bytes_b64, "candidate seal commit bytes")
    seal_commit = _git(
        repo,
        ["hash-object", "-w", "-t", "commit", "--stdin"],
        input_bytes=commit_bytes,
    ).stdout.decode("ascii").strip()
    if seal_commit != plan.seal_commit or _commit_tree(repo, seal_commit) != plan.seal_tree:
        raise TrainError("persisted candidate seal commit differs from the confirmed bytes/tree")
    journal.update(
        {
            "status": "seal-commit-created",
            "sealed_progress_blob": persisted_progress_blob,
        }
    )
    _replace_json(journal_path, journal, repo)
    _trigger(failpoint, "candidate-after-seal-commit")
    if isinstance(journal.get("verification_receipts"), list):
        seal_receipts = tuple(
            _candidate_receipt_from_dict(item) for item in journal["verification_receipts"]
        )
        receipt_blockers = _candidate_seal_receipts_gate(seal_receipts, plan)
        if receipt_blockers:
            raise TrainError(
                "candidate journal sealed verification receipts differ: "
                + "; ".join(item.code for item in receipt_blockers)
            )
    else:
        try:
            with _materialized_candidate_tree(
                repo,
                seal_commit=plan.seal_commit,
                seal_tree=plan.seal_tree,
            ) as (sealed_worktree, verification_environment):
                seal_receipts, seal_blockers = _candidate_verification_receipts(
                    registration.verify_commands,
                    sealed_worktree,
                    phase="seal",
                    candidate_commit=plan.seal_commit,
                    candidate_tree=plan.seal_tree,
                    environment=verification_environment,
                )
        except TrainError as exc:
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = "candidate-seal-verification-mutated-tree"
            _replace_json(journal_path, journal, repo)
            raise
        if seal_blockers:
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = ";".join(item.code for item in seal_blockers)
            journal["verification_receipts"] = [item.as_dict() for item in seal_receipts]
            _replace_json(journal_path, journal, repo)
            raise TrainError("candidate seal verification failed: " + "; ".join(item.code for item in seal_blockers))
        seal_blockers = _candidate_seal_receipts_gate(seal_receipts, plan)
        if seal_blockers:
            raise TrainError(
                "candidate seal verification receipts differ from the accepted commands: "
                + "; ".join(item.code for item in seal_blockers)
            )
        journal["verification_receipts"] = [item.as_dict() for item in seal_receipts]
        journal["status"] = "seal-verified"
        _replace_json(journal_path, journal, repo)
        _trigger(failpoint, "candidate-after-seal-verification")
    if isinstance(journal.get("candidate_evidence"), Mapping):
        candidate = _candidate_evidence_from_dict(journal["candidate_evidence"])
        metadata = journal.get("candidate_evidence_metadata")
        raw_b64 = journal.get("candidate_evidence_bytes_b64")
        evidence_blob_raw = journal.get("candidate_evidence_blob")
        if not isinstance(metadata, Mapping) or not isinstance(raw_b64, str) or not isinstance(evidence_blob_raw, str):
            raise TrainError("candidate evidence recovery journal is incomplete")
        evidence_raw = _decode_exact_b64(raw_b64, "candidate evidence bytes")
        evidence_blob = evidence_blob_raw
        metadata_digest = str(metadata.get("metadata_digest", ""))
        if (
            metadata_digest != journal.get("candidate_evidence_metadata_digest")
            or metadata_digest != digest(_candidate_metadata_payload(metadata))
            or canonical_json(metadata) + b"\n" != evidence_raw
            or _git(repo, ["hash-object", "--stdin"], input_bytes=evidence_raw).stdout.decode("ascii").strip()
            != evidence_blob
        ):
            raise TrainError("candidate evidence recovery bytes/digest differ")
    else:
        candidate, metadata, evidence_raw, evidence_blob = _candidate_evidence_material(
            repo,
            plan,
            seal_receipts,
            seal_authorization_id=confirmation_token.authorization_id,
        )
        metadata_digest = str(metadata["metadata_digest"])
        journal.update(
            {
                "candidate_evidence": candidate.as_dict(),
                "candidate_evidence_digest": candidate.evidence_digest,
                "candidate_evidence_metadata": metadata,
                "candidate_evidence_metadata_digest": metadata_digest,
                "candidate_evidence_bytes_b64": base64.b64encode(evidence_raw).decode("ascii"),
                "candidate_evidence_blob": evidence_blob,
                "status": "evidence-prepared",
            }
        )
        _replace_json(journal_path, journal, repo)
    persisted_evidence_blob = _git(
        repo,
        ["hash-object", "-w", "--stdin"],
        input_bytes=evidence_raw,
    ).stdout.decode("ascii").strip()
    if persisted_evidence_blob != evidence_blob:
        raise TrainError("persisted candidate evidence blob differs from journal identity")
    state = _candidate_refs_state(repo, plan, evidence_blob=evidence_blob)
    if state == "mismatch":
        raise TrainError("candidate/evidence refs differ before atomic registration")
    if state == "absent":
        final_blockers = _candidate_preconditions(
            repo,
            registration,
            allow_created_ref=False,
        )
        if final_blockers:
            raise TrainError(
                "candidate registration authority changed before ref publication: "
                + "; ".join(item.code for item in final_blockers)
            )
        try:
            _apply_candidate_ref_transaction(repo, plan, evidence_blob=evidence_blob)
        except TrainError:
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = "candidate-ref-transaction-failed"
            _replace_json(journal_path, journal, repo)
            raise
    _trigger(failpoint, "candidate-after-refs")
    if _candidate_refs_state(repo, plan, evidence_blob=evidence_blob) != "exact":
        raise TrainError("candidate/evidence atomic transaction did not produce both exact refs")
    journal["status"] = "complete"
    _replace_json(journal_path, journal, repo)
    return _registered_from_material(
        plan,
        journal_path,
        candidate=candidate,
        receipts=seal_receipts,
        evidence_blob=evidence_blob,
        evidence_metadata_digest=metadata_digest,
        seal_authorization_id=confirmation_token.authorization_id,
        idempotent=False,
    )


def _registered_candidate_gate(
    repo: Repository,
    candidate: RegisteredCandidate,
    *,
    current_principle_sha256: str,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if candidate.schema_version != REGISTER_RESULT_SCHEMA:
        blockers.append(Blocker("registered-candidate-schema", "registered candidate schema is unsupported"))
    if candidate.registration_digest != registered_candidate_digest(candidate):
        blockers.append(Blocker("registered-candidate-digest", "registered candidate receipt was changed"))
    if os.path.normcase(candidate.project_root) != os.path.normcase(str(repo.root)):
        blockers.append(Blocker("registered-candidate-project", "candidate belongs to another repository"))
    try:
        parsed_workspace_guard = _workspace_guard_from_mapping(candidate.workspace_guard.as_dict())
    except TrainError as exc:
        blockers.append(Blocker("registered-candidate-workspace-guard", str(exc)))
    else:
        if parsed_workspace_guard != candidate.workspace_guard:
            blockers.append(
                Blocker(
                    "registered-candidate-workspace-guard-identity",
                    "candidate workspace guard is not canonical",
                )
            )
        if (
            candidate.workspace_guard_digest != candidate.workspace_guard.guard_digest
            or candidate.workspace_guard.iteration != candidate.iteration
            or candidate.workspace_guard.base_commit != candidate.base_commit
            or candidate.workspace_guard.implementation_commit
            != candidate.implementation_commit
        ):
            blockers.append(
                Blocker(
                    "registered-candidate-workspace-guard-binding",
                    "candidate workspace guard differs from its public candidate identity",
                )
            )
    principle_binding_blockers = _principle_gate_binding_gate(
        candidate.principle_gate_binding
    )
    blockers.extend(principle_binding_blockers)
    if (
        candidate.principle_gate_binding.iteration != candidate.iteration
        or candidate.principle_gate_binding.current_principle_sha256
        != candidate.principle_sha256
        or candidate.principle_gate_binding.current_principle_sha256
        != current_principle_sha256
    ):
        blockers.append(
            Blocker(
                "registered-candidate-principle-binding-authority",
                "candidate principle binding differs from its registered/current principle identity",
            )
        )
    if not principle_binding_blockers:
        current_principle_binding, live_principle_blockers = (
            _current_candidate_principle_gate_binding(
                repo,
                candidate.iteration,
                authority_ref=candidate.principle_gate_binding.authority_ref,
            )
        )
        blockers.extend(live_principle_blockers)
        if (
            not live_principle_blockers
            and current_principle_binding != candidate.principle_gate_binding
        ):
            blockers.append(
                Blocker(
                    "registered-candidate-principle-binding-stale",
                    "live principle/audit identity differs from the registered candidate binding",
                )
            )
    actual = _resolve_ref(repo, candidate.candidate_ref)
    if actual != candidate.candidate_commit:
        blockers.append(Blocker("registered-candidate-ref-drift", f"candidate ref changed: {candidate.candidate_ref}"))
    if _object_type(repo, candidate.candidate_commit) != "commit":
        blockers.append(Blocker("registered-candidate-object", "candidate object is not a commit"))
    elif _commit_tree(repo, candidate.candidate_commit) != candidate.candidate_tree:
        blockers.append(Blocker("registered-candidate-tree", "candidate tree identity differs"))
    parents = _git(
        repo,
        ["rev-list", "--parents", "-n", "1", candidate.candidate_commit],
        check=False,
    ).stdout.decode("ascii", errors="replace").strip().split()
    if parents != [candidate.candidate_commit, candidate.pre_seal_commit]:
        blockers.append(Blocker("registered-candidate-parent", "candidate seal parent identity differs"))
    if _commit_tree(repo, candidate.pre_seal_commit) != candidate.pre_seal_tree:
        blockers.append(Blocker("registered-candidate-pre-seal-tree", "pre-seal tree identity differs"))
    actual_evidence = _resolve_ref(repo, candidate.candidate_evidence_ref)
    if actual_evidence != candidate.candidate_evidence_blob:
        blockers.append(
            Blocker(
                "registered-candidate-evidence-ref-drift",
                f"candidate evidence ref changed: {candidate.candidate_evidence_ref}",
            )
        )
    evidence_metadata: Mapping[str, object] | None = None
    if _object_type(repo, candidate.candidate_evidence_blob) != "blob":
        blockers.append(Blocker("registered-candidate-evidence-object", "candidate evidence object is not a blob"))
    else:
        raw_metadata = _git(repo, ["cat-file", "blob", candidate.candidate_evidence_blob]).stdout
        try:
            parsed_metadata = json.loads(raw_metadata.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            blockers.append(Blocker("registered-candidate-evidence-json", "candidate evidence blob is invalid JSON"))
        else:
            if not isinstance(parsed_metadata, dict):
                blockers.append(Blocker("registered-candidate-evidence-json", "candidate evidence blob is not an object"))
            else:
                evidence_metadata = parsed_metadata
                supplied_metadata_digest = parsed_metadata.get("metadata_digest")
                if parsed_metadata.get("schema_version") != CANDIDATE_EVIDENCE_METADATA_SCHEMA:
                    blockers.append(Blocker("registered-candidate-evidence-schema", "candidate evidence schema is unsupported"))
                if (
                    supplied_metadata_digest != candidate.candidate_evidence_metadata_digest
                    or supplied_metadata_digest != digest(_candidate_metadata_payload(parsed_metadata))
                ):
                    blockers.append(Blocker("registered-candidate-evidence-digest", "candidate evidence metadata changed"))
                if parsed_metadata.get("candidate_ref") != candidate.candidate_ref:
                    blockers.append(Blocker("registered-candidate-evidence-candidate-ref", "metadata candidate ref differs"))
                if parsed_metadata.get("candidate_evidence_ref") != candidate.candidate_evidence_ref:
                    blockers.append(Blocker("registered-candidate-evidence-ref", "metadata evidence ref differs"))
                if parsed_metadata.get("implementation_commit") != candidate.implementation_commit:
                    blockers.append(
                        Blocker(
                            "registered-candidate-evidence-implementation-start",
                            "metadata implementation start differs",
                        )
                    )
                try:
                    metadata_workspace_guard = _workspace_guard_from_mapping(
                        parsed_metadata.get("workspace_guard")
                    )
                except TrainError as exc:
                    blockers.append(
                        Blocker(
                            "registered-candidate-evidence-workspace-guard",
                            str(exc),
                        )
                    )
                else:
                    if metadata_workspace_guard != candidate.workspace_guard:
                        blockers.append(
                            Blocker(
                                "registered-candidate-evidence-workspace-guard",
                                "metadata workspace guard differs",
                            )
                        )
                if parsed_metadata.get("principle_gate_binding") != (
                    candidate.principle_gate_binding.as_dict()
                ):
                    blockers.append(
                        Blocker(
                            "registered-candidate-evidence-principle-binding",
                            "metadata principle gate binding differs",
                        )
                    )
                if parsed_metadata.get("seal_commit") != candidate.candidate_commit:
                    blockers.append(Blocker("registered-candidate-evidence-commit", "metadata seal commit differs"))
                if parsed_metadata.get("seal_tree") != candidate.candidate_tree:
                    blockers.append(Blocker("registered-candidate-evidence-tree", "metadata seal tree differs"))
                if digest(parsed_metadata.get("candidate_evidence")) != digest(candidate.candidate_evidence.as_dict()):
                    blockers.append(Blocker("registered-candidate-evidence-core", "metadata core candidate evidence differs"))
                if parsed_metadata.get("depends_on") != list(candidate.depends_on):
                    blockers.append(Blocker("registered-candidate-evidence-dependencies", "metadata dependencies differ"))
                if parsed_metadata.get("dependency_bindings") != [
                    item.as_dict() for item in candidate.dependency_bindings
                ]:
                    blockers.append(
                        Blocker(
                            "registered-candidate-evidence-dependency-bindings",
                            "metadata dependency bindings differ",
                        )
                    )
                if (
                    parsed_metadata.get("dependency_bindings_digest")
                    != candidate.dependency_bindings_digest
                ):
                    blockers.append(
                        Blocker(
                            "registered-candidate-evidence-dependency-bindings-digest",
                            "metadata dependency binding digest differs",
                        )
                    )
    base = _resolve_ref(repo, candidate.base_ref)
    if base != candidate.base_commit:
        blockers.append(Blocker("registered-candidate-base-drift", "candidate immutable base ref changed"))
    if _object_type(repo, candidate.implementation_commit) != "commit":
        blockers.append(
            Blocker(
                "registered-candidate-implementation-start-object",
                "candidate implementation start is not a commit",
            )
        )
    else:
        if not _is_ancestor(repo, candidate.base_commit, candidate.implementation_commit):
            blockers.append(
                Blocker(
                    "registered-candidate-implementation-start-base",
                    "candidate implementation start is not descended from its immutable allocation base",
                )
            )
        if not _is_ancestor(repo, candidate.implementation_commit, candidate.pre_seal_commit):
            blockers.append(
                Blocker(
                    "registered-candidate-implementation-start-ancestry",
                    "candidate pre-seal commit is not descended from its implementation start",
                )
            )
        expected_included_paths = _diff_paths(
            repo,
            candidate.implementation_commit,
            candidate.candidate_commit,
        )
        if candidate.candidate_evidence.included_paths != expected_included_paths:
            blockers.append(
                Blocker(
                    "registered-candidate-included-paths",
                    "candidate included paths differ from the exact implementation-start delta",
                )
            )
    reconciliation_commit = candidate.workspace_guard.reconciliation_commit
    if _object_type(repo, reconciliation_commit) != "commit" or not _is_ancestor(
        repo,
        candidate.implementation_commit,
        reconciliation_commit,
    ) or not _is_ancestor(repo, reconciliation_commit, candidate.pre_seal_commit):
        blockers.append(
            Blocker(
                "registered-candidate-reconciliation-base",
                "candidate reconciliation baseline is not an exact commit between its implementation baseline and pre-seal commit",
            )
        )
    gate = candidate_freshness_gate(
        candidate.candidate_evidence,
        current_base_commit=base or candidate.base_commit,
        current_candidate_commit=actual or candidate.candidate_commit,
        current_candidate_tree=candidate.candidate_tree,
        current_principle_sha256=current_principle_sha256,
    )
    if not gate.allowed:
        blockers.append(Blocker("registered-candidate-core", ", ".join(gate.blockers)))
    try:
        normalized_bindings = _normalize_dependency_bindings(
            candidate.dependency_bindings,
            label="registered candidate dependency bindings",
        )
    except TrainError as exc:
        blockers.append(Blocker("registered-candidate-dependency-bindings", str(exc)))
        normalized_bindings = ()
    if (
        normalized_bindings != candidate.dependency_bindings
        or candidate.dependency_bindings_digest
        != _dependency_bindings_digest(normalized_bindings)
    ):
        blockers.append(
            Blocker(
                "registered-candidate-dependency-bindings-digest",
                "registered candidate dependency bindings were changed",
            )
        )
    if tuple(item.iteration for item in normalized_bindings) != candidate.depends_on:
        blockers.append(
            Blocker(
                "registered-candidate-dependency-authority",
                "registered candidate dependency bindings differ from committed depends_on",
            )
        )
    if _dependency_evidence_id(candidate.dependency_bindings_digest) not in candidate.candidate_evidence.verification_ids:
        blockers.append(
            Blocker(
                "registered-candidate-dependency-evidence",
                "core candidate evidence does not digest-bind its dependency bindings",
            )
        )
    if _principle_gate_evidence_id(
        candidate.principle_gate_binding.binding_digest
    ) not in candidate.candidate_evidence.verification_ids:
        blockers.append(
            Blocker(
                "registered-candidate-principle-evidence",
                "core candidate evidence does not digest-bind its principle gate receipt",
            )
        )
    blockers.extend(
        _dependency_bindings_live_blockers(
            repo,
            normalized_bindings,
            consuming_iteration=candidate.iteration,
            consuming_commit=candidate.candidate_commit,
        )
    )
    if not candidate.verification_receipts:
        blockers.append(Blocker("registered-candidate-verification-missing", "candidate has no sealed verification receipt"))
    for receipt in candidate.verification_receipts:
        blockers.extend(
            candidate_verification_receipt_gate(
                receipt,
                expected_phase="seal",
                expected_commit=candidate.candidate_commit,
                expected_tree=candidate.candidate_tree,
            )
        )
    candidate_journal_path = Path(candidate.journal_path)
    _assert_train_operational_path(repo, candidate_journal_path)
    journal = _read_json(candidate_journal_path, repo)
    try:
        journal_workspace_guard = _workspace_guard_from_mapping(
            journal.get("workspace_guard") if isinstance(journal, Mapping) else None
        )
    except TrainError:
        journal_workspace_guard = None
    try:
        evidence_workspace_guard = _workspace_guard_from_mapping(
            evidence_metadata.get("workspace_guard")
            if isinstance(evidence_metadata, Mapping)
            else None
        )
    except TrainError:
        evidence_workspace_guard = None
    if (
        journal is None
        or journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("kind") != "candidate-register"
        or journal.get("status") != "complete"
        or journal.get("registration_plan_digest") != candidate.registration_plan_digest
        or journal.get("seal_plan_digest") != candidate.seal_plan_digest
        or journal.get("candidate_ref") != candidate.candidate_ref
        or journal.get("candidate_evidence_ref") != candidate.candidate_evidence_ref
        or journal.get("pre_seal_commit") != candidate.pre_seal_commit
        or journal.get("pre_seal_tree") != candidate.pre_seal_tree
        or journal.get("seal_commit") != candidate.candidate_commit
        or journal.get("seal_tree") != candidate.candidate_tree
        or digest(journal.get("candidate_evidence")) != digest(candidate.candidate_evidence.as_dict())
        or journal.get("candidate_evidence_blob") != candidate.candidate_evidence_blob
        or journal.get("candidate_evidence_metadata_digest") != candidate.candidate_evidence_metadata_digest
        or journal.get("seal_authorization_id") != candidate.seal_authorization_id
        or digest(journal.get("verification_receipts"))
        != digest([item.as_dict() for item in candidate.verification_receipts])
        or evidence_metadata is None
        or evidence_metadata.get("iteration") != candidate.iteration
        or evidence_metadata.get("generation") != candidate.generation
        or evidence_metadata.get("authority_evidence_digest") != candidate.authority_evidence_digest
        or evidence_metadata.get("workspace_guard_digest") != candidate.workspace_guard_digest
        or evidence_workspace_guard != candidate.workspace_guard
        or evidence_metadata.get("implementation_commit") != candidate.implementation_commit
        or evidence_metadata.get("principle_gate_binding")
        != candidate.principle_gate_binding.as_dict()
        or evidence_metadata.get("depends_on") != list(candidate.depends_on)
        or evidence_metadata.get("dependency_bindings")
        != [item.as_dict() for item in candidate.dependency_bindings]
        or evidence_metadata.get("dependency_bindings_digest")
        != candidate.dependency_bindings_digest
        or not isinstance(journal.get("authority_receipt"), Mapping)
        or journal["authority_receipt"].get("evidence_digest") != candidate.authority_evidence_digest
        or journal["authority_receipt"].get("depends_on") != list(candidate.depends_on)
        or journal.get("dependency_bindings")
        != [item.as_dict() for item in candidate.dependency_bindings]
        or journal.get("dependency_bindings_digest")
        != candidate.dependency_bindings_digest
        or journal.get("implementation_commit") != candidate.implementation_commit
        or journal_workspace_guard != candidate.workspace_guard
        or journal.get("principle_gate_binding")
        != candidate.principle_gate_binding.as_dict()
    ):
        blockers.append(Blocker("registered-candidate-journal", "candidate registration journal is absent or stale"))
    return tuple(blockers)


def registered_candidate_gate(
    project_root: str | Path,
    candidate: RegisteredCandidate,
    *,
    current_principle_sha256: str,
) -> tuple[Blocker, ...]:
    """Public read-only gate for a caller already carrying a receipt."""

    repo = open_repository(project_root)
    return _registered_candidate_gate(
        repo,
        candidate,
        current_principle_sha256=_validate_digest(
            current_principle_sha256,
            "current_principle_sha256",
        ),
    )


def _load_registered_candidate_impl(
    project_root: str | Path,
    *,
    iteration: str,
    generation: str,
    current_principle_sha256: str,
) -> tuple[RegisteredCandidate | None, tuple[Blocker, ...]]:
    """Rebuild and authenticate one stable candidate/evidence ref pair.

    This is the supported cross-module dependency resolver.  Consumers do not
    need to parse the operational journal or evidence-blob schema themselves.
    Missing generations return a blocker; malformed/tampered generations fail
    closed with an explicit blocker or ``TrainError`` for unsafe local state.
    """

    repo = open_repository(project_root)
    number = _validate_iteration(iteration)
    gen = _validate_generation(generation)
    principle_sha = _validate_digest(current_principle_sha256, "current_principle_sha256")
    candidate_ref = f"refs/project-harness/v2/iterations/{number}/candidates/{gen}"
    evidence_ref = f"refs/project-harness/v2/iterations/{number}/candidate-evidence/{gen}"
    candidate_commit = _resolve_ref(repo, candidate_ref)
    evidence_blob = _resolve_ref(repo, evidence_ref)
    if candidate_commit is None and evidence_blob is None:
        return None, (
            Blocker(
                "registered-candidate-missing",
                f"candidate generation does not exist: PRD-{number}/{gen}",
            ),
        )
    if candidate_commit is None or evidence_blob is None:
        return None, (
            Blocker(
                "registered-candidate-partial-refs",
                f"candidate/evidence refs are not both present: PRD-{number}/{gen}",
            ),
        )
    if _object_type(repo, candidate_commit) != "commit" or _object_type(repo, evidence_blob) != "blob":
        return None, (
            Blocker(
                "registered-candidate-ref-object-type",
                f"candidate/evidence refs name unsupported object types: PRD-{number}/{gen}",
            ),
        )
    raw = _git(repo, ["cat-file", "blob", evidence_blob]).stdout
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrainError(f"candidate evidence blob is invalid JSON: {evidence_ref}") from exc
    if not isinstance(metadata, dict) or canonical_json(metadata) + b"\n" != raw:
        raise TrainError(f"candidate evidence blob is not canonical JSON: {evidence_ref}")
    metadata_digest = metadata.get("metadata_digest")
    if (
        metadata.get("schema_version") != CANDIDATE_EVIDENCE_METADATA_SCHEMA
        or not isinstance(metadata_digest, str)
        or metadata_digest != digest(_candidate_metadata_payload(metadata))
    ):
        raise TrainError(f"candidate evidence metadata digest is invalid: {evidence_ref}")
    expected = {
        "iteration": number,
        "generation": gen,
        "candidate_ref": candidate_ref,
        "candidate_evidence_ref": evidence_ref,
        "seal_commit": candidate_commit,
    }
    for field, expected_value in expected.items():
        if metadata.get(field) != expected_value:
            raise TrainError(f"candidate evidence metadata identity differs: {evidence_ref} / {field}")
    receipts_raw = metadata.get("seal_verification_receipts")
    if not isinstance(receipts_raw, list):
        raise TrainError(f"candidate evidence verification receipts are missing: {evidence_ref}")
    receipts = tuple(_candidate_receipt_from_dict(item) for item in receipts_raw)
    candidate_evidence = _candidate_evidence_from_dict(metadata.get("candidate_evidence"))
    implementation_commit = metadata.get("implementation_commit")
    if (
        not isinstance(implementation_commit, str)
        or OID_RE.fullmatch(implementation_commit) is None
    ):
        raise TrainError(
            f"candidate evidence implementation start is invalid: {evidence_ref}"
        )
    try:
        workspace_guard = _workspace_guard_from_mapping(metadata.get("workspace_guard"))
    except TrainError as exc:
        raise TrainError(
            f"candidate evidence workspace guard is invalid: {evidence_ref}: {exc}"
        ) from exc
    if (
        workspace_guard.iteration != number
        or workspace_guard.base_commit != candidate_evidence.base_commit
        or workspace_guard.implementation_commit != implementation_commit
        or metadata.get("workspace_guard_digest") != workspace_guard.guard_digest
    ):
        raise TrainError(
            f"candidate evidence workspace guard identity differs: {evidence_ref}"
        )
    principle_gate_binding = _principle_gate_binding_from_dict(
        metadata.get("principle_gate_binding")
    )
    depends_raw = metadata.get("depends_on")
    if not isinstance(depends_raw, list) or any(not isinstance(item, str) for item in depends_raw):
        raise TrainError(f"candidate evidence dependencies are invalid: {evidence_ref}")
    depends_on = tuple(depends_raw)
    if tuple(dict.fromkeys(depends_on)) != depends_on or any(
        ITERATION_RE.fullmatch(item) is None or item == number for item in depends_on
    ):
        raise TrainError(f"candidate evidence dependencies are not canonical: {evidence_ref}")
    raw_bindings = metadata.get("dependency_bindings")
    if not isinstance(raw_bindings, list):
        raise TrainError(
            f"candidate evidence dependency bindings are missing: {evidence_ref}"
        )
    try:
        dependency_bindings = _normalize_dependency_bindings(
            raw_bindings,
            label="candidate evidence dependency bindings",
        )
    except TrainError as exc:
        raise TrainError(
            f"candidate evidence dependency bindings are invalid: {evidence_ref}: {exc}"
        ) from exc
    dependency_bindings_digest = metadata.get("dependency_bindings_digest")
    if (
        not isinstance(dependency_bindings_digest, str)
        or dependency_bindings_digest != _dependency_bindings_digest(dependency_bindings)
    ):
        raise TrainError(
            f"candidate evidence dependency binding digest is invalid: {evidence_ref}"
        )
    if tuple(item.iteration for item in dependency_bindings) != depends_on:
        raise TrainError(
            f"candidate evidence dependency bindings differ from depends_on: {evidence_ref}"
        )
    journal_path = _journal_path(repo, "candidate", str(metadata.get("operation_id", "")))
    candidate = RegisteredCandidate(
        schema_version=REGISTER_RESULT_SCHEMA,
        operation_id=str(metadata["operation_id"]),
        project_root=str(repo.root),
        iteration=number,
        generation=gen,
        candidate_ref=candidate_ref,
        candidate_evidence_ref=evidence_ref,
        candidate_evidence_blob=evidence_blob,
        candidate_evidence_metadata_digest=metadata_digest,
        pre_seal_commit=str(metadata.get("pre_seal_commit", "")),
        pre_seal_tree=str(metadata.get("pre_seal_tree", "")),
        candidate_commit=candidate_commit,
        candidate_tree=str(metadata.get("seal_tree", "")),
        base_ref=str(metadata.get("base_ref", "")),
        base_commit=candidate_evidence.base_commit,
        implementation_commit=implementation_commit,
        principle_sha256=candidate_evidence.principle_sha256,
        principle_gate_binding=principle_gate_binding,
        authority_evidence_digest=str(metadata.get("authority_evidence_digest", "")),
        workspace_guard=workspace_guard,
        workspace_guard_digest=str(metadata.get("workspace_guard_digest", "")),
        depends_on=depends_on,
        dependency_bindings=dependency_bindings,
        dependency_bindings_digest=dependency_bindings_digest,
        candidate_evidence=candidate_evidence,
        verification_receipts=receipts,
        seal_authorization_id=str(metadata.get("seal_authorization_id", "")),
        registration_plan_digest=str(metadata.get("registration_plan_digest", "")),
        seal_plan_digest=str(metadata.get("seal_plan_digest", "")),
        registration_digest="0" * 64,
        journal_path=str(journal_path),
        idempotent=True,
    )
    candidate = replace(candidate, registration_digest=registered_candidate_digest(candidate))
    blockers = _registered_candidate_gate(
        repo,
        candidate,
        current_principle_sha256=principle_sha,
    )
    # Cross-module callers must never accidentally promote a receipt that was
    # reconstructed but failed a current live gate into a "stable" binding.
    # Returning no object when blockers exist makes that invariant structural.
    return (candidate if not blockers else None), blockers


def load_registered_candidate(
    project_root: str | Path,
    *,
    iteration: str,
    generation: str,
    current_principle_sha256: str,
) -> tuple[RegisteredCandidate | None, tuple[Blocker, ...]]:
    """Load a current stable candidate, failing closed on dependency cycles."""

    number = _validate_iteration(iteration)
    gen = _validate_generation(generation)
    principle_sha = _validate_digest(
        current_principle_sha256,
        "current_principle_sha256",
    )
    key = (number, gen)
    stack = _CANDIDATE_LOAD_STACK.get()
    if key in stack:
        return None, (
            Blocker(
                "registered-candidate-dependency-cycle",
                f"candidate dependency evidence is cyclic: PRD-{number}/{gen}",
            ),
        )
    validation = _AUTHORITY_VALIDATION_CONTEXT.get()
    cache_key = (
        validation.snapshot_digest,
        number,
        gen,
        principle_sha,
    ) if validation is not None else None
    if validation is not None:
        supplied = Path(project_root).resolve()
        if os.path.normcase(str(supplied)) != os.path.normcase(
            str(validation.repo.root)
        ):
            raise TrainError(
                "authority validation candidate load belongs to another repository"
            )
        cached = validation.candidate_cache.get(cache_key)
        if cached is not None:
            return cached
    token = _CANDIDATE_LOAD_STACK.set((*stack, key))
    try:
        result = _load_registered_candidate_impl(
            project_root,
            iteration=number,
            generation=gen,
            current_principle_sha256=principle_sha,
        )
        if validation is not None and cache_key is not None:
            validation.candidate_cache[cache_key] = result
        return result
    finally:
        _CANDIDATE_LOAD_STACK.reset(token)


def _normalize_verify_commands(commands: Sequence[VerifyCommand]) -> tuple[VerifyCommand, ...]:
    normalized: list[VerifyCommand] = []
    seen: set[str] = set()
    for command in commands:
        if not isinstance(command, VerifyCommand):
            raise TrainError("verify_commands entries must be VerifyCommand")
        evidence_id = command.evidence_id.strip()
        if not evidence_id or len(evidence_id) > 200 or any(ord(char) < 32 for char in evidence_id):
            raise TrainError("verification evidence_id is invalid")
        if evidence_id in seen:
            raise TrainError(f"verification evidence_id is duplicated: {evidence_id}")
        argv = tuple(str(item) for item in command.argv)
        if not argv or any(not item or "\x00" in item for item in argv):
            raise TrainError(f"verification command is empty or malformed: {evidence_id}")
        seen.add(evidence_id)
        normalized.append(VerifyCommand(evidence_id=evidence_id, argv=argv))
    return tuple(normalized)


def _prepare_plan_payload(plan: IntegrationPreparePlan) -> dict[str, object]:
    data = plan.as_dict()
    data.pop("plan_digest", None)
    return data


def integration_prepare_plan_digest(plan: IntegrationPreparePlan) -> str:
    return digest(_prepare_plan_payload(plan))


def _default_integration_path(repo: Repository, generation: str, operation_id: str) -> Path:
    suffix = operation_id.removeprefix("OP-")[:12]
    return (repo.root.parent / f".{repo.root.name}.harness-integration-{generation}-{suffix}").resolve()


def _worktree_paths(repo: Repository) -> tuple[Path, ...]:
    raw = _git(repo, ["worktree", "list", "--porcelain"]).stdout.decode("utf-8", errors="strict")
    return tuple(
        Path(line.removeprefix("worktree ")).resolve()
        for line in raw.splitlines()
        if line.startswith("worktree ")
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(
        str(right.resolve(strict=False))
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve(strict=False)
    right_resolved = right.resolve(strict=False)
    return (
        _same_path(left_resolved, right_resolved)
        or left_resolved in right_resolved.parents
        or right_resolved in left_resolved.parents
    )


def _integration_path_blockers(
    repo: Repository,
    target: Path,
    *,
    allow_exact_registered: bool,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    for existing in _worktree_paths(repo):
        if allow_exact_registered and _same_path(target, existing):
            continue
        if _paths_overlap(target, existing):
            blockers.append(
                Blocker(
                    "integration-worktree-overlap",
                    f"integration path overlaps an existing worktree: {target} / {existing}",
                )
            )
    return tuple(blockers)


def _integration_path_parent_gate(repo: Repository, target: Path) -> tuple[Blocker, ...]:
    """Reject path redirection through a symlink/junction before Git writes."""

    blockers: list[Blocker] = []
    try:
        workspace.assert_existing_chain_has_no_links(target.parent)
    except workspace.WorkspaceError as exc:
        blockers.append(Blocker("integration-worktree-path-link", str(exc)))
    if target.exists() and workspace.is_link_or_junction(target):
        blockers.append(
            Blocker(
                "integration-worktree-path-link",
                f"integration target is a symbolic link or junction: {target}",
            )
        )
    return tuple(blockers)


def plan_prepare_integration(
    project_root: str | Path,
    *,
    generation: str,
    candidates: Sequence[RegisteredCandidate],
    verify_commands: Sequence[VerifyCommand],
    main_ref: str = DEFAULT_MAIN_REF,
    principle_path: str = DEFAULT_PRINCIPLE_PATH,
    merge_strategy: str = DEFAULT_MERGE_STRATEGY,
    strategy_declaration_digest: str | None = None,
    worktree_path: str | Path | None = None,
    commit_message: str | None = None,
    operation_id: str | None = None,
) -> IntegrationPreparePlan:
    """Plan a latest-main integration worktree without changing Git state."""

    repo = open_repository(project_root)
    operation = _validate_operation(operation_id or new_operation_id())
    gen = _validate_generation(generation)
    main = _validate_ref(main_ref, "main_ref")
    principle = _validate_repo_path(principle_path, "principle_path")
    target_main = _resolve_ref(repo, main)
    if target_main is None or _object_type(repo, target_main) != "commit":
        raise TrainError("main_ref must resolve to a commit")
    principle_blob, principle_raw = _blob_at(repo, target_main, principle)
    principle_sha = hashlib.sha256(principle_raw).hexdigest()
    normalized_candidates = tuple(candidates)
    normalized_commands = _normalize_verify_commands(verify_commands)
    strategy = merge_strategy.strip().lower()
    declaration = None
    if strategy_declaration_digest is not None:
        declaration = _validate_digest(strategy_declaration_digest, "strategy_declaration_digest")
    blockers: list[Blocker] = []
    if worktree_path is not None:
        supplied_path = Path(worktree_path).expanduser()
        raw_integration_path = supplied_path if supplied_path.is_absolute() else Path.cwd() / supplied_path
        try:
            workspace.assert_existing_chain_has_no_links(raw_integration_path.parent)
        except workspace.WorkspaceError as exc:
            blockers.append(Blocker("integration-worktree-path-link", str(exc)))
        integration_path = raw_integration_path.resolve()
    else:
        integration_path = _default_integration_path(repo, gen, operation)
    if strategy not in SUPPORTED_ADAPTER_STRATEGIES:
        blockers.append(Blocker("merge-strategy-unsupported", "adapter supports merge-no-ff and declared squash only"))
    if strategy != DEFAULT_MERGE_STRATEGY and declaration is None:
        blockers.append(Blocker("merge-strategy-not-declared", "non-default merge strategy needs an exact declaration digest"))
    if strategy == "squash" and len(normalized_candidates) != 1:
        blockers.append(Blocker("squash-multi-candidate-unsupported", "squash adapter accepts one candidate per integration"))
    if not normalized_candidates:
        blockers.append(Blocker("integration-candidate-missing", "at least one registered candidate is required"))
    iterations: list[str] = []
    candidate_refs: set[str] = set()
    for candidate in normalized_candidates:
        if not isinstance(candidate, RegisteredCandidate):
            raise TrainError("candidates entries must be RegisteredCandidate")
        iterations.append(candidate.iteration)
        if candidate.candidate_ref in candidate_refs:
            blockers.append(Blocker("integration-candidate-duplicate", candidate.candidate_ref))
        candidate_refs.add(candidate.candidate_ref)
        blockers.extend(
            _registered_candidate_gate(
                repo,
                candidate,
                current_principle_sha256=principle_sha,
            )
        )
        blockers.extend(
            _current_principle_audit_blockers(
                repo,
                candidate.iteration,
            )
        )
    if len(set(iterations)) != len(iterations):
        blockers.append(Blocker("integration-iteration-duplicate", "one generation per iteration is allowed"))
    candidate_commits = tuple(item.candidate_commit for item in normalized_candidates)
    if len(set(candidate_commits)) != len(candidate_commits):
        blockers.append(Blocker("integration-candidate-commit-duplicate", "candidate commits must be unique"))
    position = {iteration: index for index, iteration in enumerate(iterations)}
    for index, candidate in enumerate(normalized_candidates):
        for dependency in candidate.depends_on:
            dependency_position = position.get(dependency)
            if dependency_position is not None:
                if dependency_position >= index:
                    blockers.append(
                        Blocker(
                            "integration-dependency-order-invalid",
                            f"PRD-{dependency} must precede PRD-{candidate.iteration}",
                        )
                    )
                continue
            final_ref = f"refs/project-harness/v2/iterations/{dependency}/final"
            final_commit = _resolve_ref(repo, final_ref)
            if final_commit is None or not _is_ancestor(repo, final_commit, target_main):
                blockers.append(
                    Blocker(
                        "integration-dependency-missing",
                        f"PRD-{candidate.iteration} requires PRD-{dependency} in this train or latest main",
                    )
                )
    blockers.extend(
        _integration_dependency_blockers(
            repo,
            normalized_candidates,
            target_main=target_main,
        )
    )
    if not normalized_commands:
        blockers.append(
            Blocker(
                "integration-verification-command-missing",
                "no default verify command is inferred; configure at least one full verification command",
            )
        )
    blockers.extend(_integration_path_blockers(repo, integration_path, allow_exact_registered=False))
    if integration_path.exists():
        blockers.append(Blocker("integration-worktree-collision", f"integration path already exists: {integration_path}"))
    lease = _read_json(_lease_path(repo), repo)
    if lease is not None:
        blockers.append(Blocker("main-integration-lease-held", "another integration operation holds the main lease"))
    message = (commit_message or f"harness: integrate {', '.join(iterations)} ({gen})").strip()
    if not message or len(message) > 500 or "\x00" in message:
        raise TrainError("commit_message is invalid")
    expected_merge_heads: tuple[str, ...] = ()
    if strategy == DEFAULT_MERGE_STRATEGY:
        expected_merge_heads = tuple(
            commit
            for commit in candidate_commits
            if not _is_ancestor(repo, commit, target_main)
            and not any(
                commit != other and _is_ancestor(repo, commit, other)
                for other in candidate_commits
            )
        )
        if not expected_merge_heads and normalized_candidates:
            blockers.append(Blocker("integration-candidates-already-contained", "latest main already contains all candidates"))
    provisional = IntegrationPreparePlan(
        schema_version=PREPARE_PLAN_SCHEMA,
        operation_id=operation,
        project_root=str(repo.root),
        git_common_dir=str(repo.common_dir),
        generation=gen,
        main_ref=main,
        target_main=target_main,
        principle_path=principle,
        principle_blob=principle_blob,
        principle_sha256=principle_sha,
        candidates=normalized_candidates,
        dependency_order=tuple(iterations),
        expected_merge_heads=expected_merge_heads,
        merge_strategy=strategy,
        strategy_declaration_digest=declaration,
        verify_commands=normalized_commands,
        worktree_path=str(integration_path),
        commit_message=message,
        plan_digest="0" * 64,
        blockers=tuple(blockers),
        governance_apply_connected=False,
    )
    return replace(provisional, plan_digest=integration_prepare_plan_digest(provisional))


def _governance_payload(receipt: GovernanceReceipt) -> dict[str, object]:
    data = receipt.as_dict()
    data.pop("evidence_digest", None)
    return data


def governance_receipt_digest(receipt: GovernanceReceipt) -> str:
    return digest(_governance_payload(receipt))


def build_governance_receipt(
    context: GovernanceContext,
    *,
    mode: Literal["preview", "applied"],
    result_tree: str,
    evidence_ids: Sequence[str],
) -> GovernanceReceipt:
    if mode not in {"preview", "applied"}:
        raise TrainError("governance receipt mode must be preview or applied")
    ids = tuple(dict.fromkeys(item.strip() for item in evidence_ids if item.strip()))
    provisional = GovernanceReceipt(
        schema_version=GOVERNANCE_RECEIPT_SCHEMA,
        operation_id=context.operation_id,
        mode=mode,
        target_main=context.target_main,
        principle_sha256=context.principle_sha256,
        candidate_digests=context.candidate_digests,
        input_tree=context.pre_governance_tree,
        result_tree=_validate_oid(result_tree, "governance result_tree"),
        evidence_ids=ids,
        evidence_digest="0" * 64,
    )
    return replace(provisional, evidence_digest=governance_receipt_digest(provisional))


def governance_receipt_gate(
    receipt: GovernanceReceipt,
    context: GovernanceContext,
    *,
    actual_result_tree: str,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if not isinstance(receipt, GovernanceReceipt):
        return (Blocker("governance-receipt-missing", "governance callback did not return a structured receipt"),)
    if receipt.schema_version != GOVERNANCE_RECEIPT_SCHEMA:
        blockers.append(Blocker("governance-receipt-schema", "governance receipt schema is unsupported"))
    if receipt.evidence_digest != governance_receipt_digest(receipt):
        blockers.append(Blocker("governance-receipt-digest", "governance receipt was changed"))
    expected = {
        "operation_id": context.operation_id,
        "target_main": context.target_main,
        "principle_sha256": context.principle_sha256,
        "candidate_digests": context.candidate_digests,
        "input_tree": context.pre_governance_tree,
    }
    for field, value in expected.items():
        if getattr(receipt, field) != value:
            blockers.append(Blocker(f"governance-receipt-{field.replace('_', '-')}", f"governance receipt {field} is stale"))
    if receipt.result_tree != actual_result_tree:
        blockers.append(Blocker("governance-result-tree-drift", "governance receipt is not bound to staged tree"))
    if not receipt.evidence_ids:
        blockers.append(Blocker("governance-evidence-missing", "governance receipt has no evidence identity"))
    if receipt.mode != "applied":
        blockers.append(
            Blocker(
                "governance-apply-not-connected",
                "governance callback returned preview only; reconcile apply is not connected",
            )
        )
    return tuple(blockers)


def _prepare_plan_gate(plan: IntegrationPreparePlan) -> tuple[Blocker, ...]:
    blockers = list(plan.blockers)
    if plan.schema_version != PREPARE_PLAN_SCHEMA:
        blockers.append(Blocker("integration-plan-schema", "integration prepare schema is unsupported"))
    if plan.plan_digest != integration_prepare_plan_digest(plan):
        blockers.append(Blocker("integration-plan-digest", "integration prepare plan was changed"))
    return tuple(blockers)


def _integration_preconditions(
    repo: Repository,
    plan: IntegrationPreparePlan,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if os.path.normcase(str(repo.root)) != os.path.normcase(plan.project_root):
        blockers.append(Blocker("integration-project-drift", "integration plan belongs to another project root"))
    if os.path.normcase(str(repo.common_dir)) != os.path.normcase(plan.git_common_dir):
        blockers.append(Blocker("integration-common-dir-drift", "Git common directory changed"))
    main = _resolve_ref(repo, plan.main_ref)
    if main != plan.target_main:
        blockers.append(Blocker("integration-main-drift", "main changed after integration was planned"))
    try:
        blob, raw = _blob_at(repo, plan.target_main, plan.principle_path)
    except TrainError as exc:
        blockers.append(Blocker("integration-principle-unreadable", str(exc)))
    else:
        if blob != plan.principle_blob or hashlib.sha256(raw).hexdigest() != plan.principle_sha256:
            blockers.append(Blocker("integration-principle-drift", "latest-main principle identity changed"))
    for candidate in plan.candidates:
        blockers.extend(
            _registered_candidate_gate(
                repo,
                candidate,
                current_principle_sha256=plan.principle_sha256,
            )
        )
        blockers.extend(
            _current_principle_audit_blockers(
                repo,
                candidate.iteration,
            )
        )
    blockers.extend(
        _integration_dependency_blockers(
            repo,
            plan.candidates,
            target_main=plan.target_main,
        )
    )
    return tuple(blockers)


def _lease_payload(plan: IntegrationPreparePlan) -> dict[str, object]:
    return {
        "schema_version": LEASE_SCHEMA,
        "operation_id": plan.operation_id,
        "plan_digest": plan.plan_digest,
        "scope": plan.main_ref,
        "generation": plan.generation,
        "expected_main": plan.target_main,
        "worktree_path": plan.worktree_path,
        "status": "active",
    }


def _acquire_integration_lease(repo: Repository, plan: IntegrationPreparePlan) -> Path:
    path = _lease_path(repo)
    existing = _read_json(path, repo)
    expected = _lease_payload(plan)
    if existing is None:
        try:
            _write_new_json(path, expected, repo)
        except FileExistsError:
            existing = _read_json(path, repo)
        else:
            return path
    if existing is None:
        raise TrainError("main integration lease raced and cannot be read")
    identity_fields = ("schema_version", "operation_id", "plan_digest", "scope", "expected_main", "worktree_path")
    if any(existing.get(field) != expected[field] for field in identity_fields):
        raise TrainError("main integration lease is held by another operation")
    return path


def _notification(plan: IntegrationPreparePlan, phase: Literal["before", "after"]) -> Notification:
    iterations = tuple(dict.fromkeys(item.iteration for item in plan.candidates))
    iteration = iterations[0] if len(iterations) == 1 else "+".join(iterations)
    return Notification(
        phase=phase,
        action="create-integration-worktree",
        operation_id=plan.operation_id,
        generation=plan.generation,
        path=plan.worktree_path,
        base_ref=plan.main_ref,
        base_commit=plan.target_main,
        candidate_refs=tuple(item.candidate_ref for item in plan.candidates),
        iteration=iteration,
        project_root=plan.project_root,
        branch_ref="DETACHED",
        reason="latest-main merge-train integration",
        affected_prds=iterations,
        runtime_namespace=f"integration:{plan.generation}",
        effect_on_existing_prds=(
            "existing PRD worktrees, indexes, and dirty files remain in place",
            "no remote operation is involved",
        ),
        source_preserved=True,
        actual_head=plan.target_main if phase == "after" else None,
        next_gate="confirm-integration-commit" if phase == "after" else "create-isolated-integration-worktree",
    )


def _worktree_registered(repo: Repository, path: Path) -> bool:
    expected = os.path.normcase(str(path.resolve()))
    return any(os.path.normcase(str(item)) == expected for item in _worktree_paths(repo))


def _worktree_head(repo: Repository, path: Path) -> str:
    result = _git(repo, ["rev-parse", "HEAD"], cwd=path)
    return _validate_oid(result.stdout.decode("ascii").strip(), "integration worktree HEAD")


def _merge_head(repo: Repository, worktree: Path) -> tuple[str, ...]:
    location = _git(repo, ["rev-parse", "--git-path", "MERGE_HEAD"], cwd=worktree).stdout
    raw_path = Path(location.decode("utf-8", errors="strict").strip())
    path = raw_path if raw_path.is_absolute() else worktree / raw_path
    if not path.is_file():
        return ()
    try:
        raw = path.read_text(encoding="ascii")
    except OSError as exc:
        raise TrainError(f"cannot read integration MERGE_HEAD: {exc}") from exc
    return tuple(_validate_oid(line.strip(), "MERGE_HEAD") for line in raw.splitlines() if line.strip())


def _staged_tree(repo: Repository, worktree: Path) -> str:
    result = _git(repo, ["write-tree"], cwd=worktree)
    return _validate_oid(result.stdout.decode("ascii").strip(), "staged integration tree")


def _head_tree(repo: Repository, worktree: Path) -> str:
    result = _git(repo, ["rev-parse", "HEAD^{tree}"], cwd=worktree)
    return _validate_oid(result.stdout.decode("ascii").strip(), "integration HEAD tree")


def _unmerged_paths(repo: Repository, worktree: Path) -> tuple[str, ...]:
    result = _git(
        repo,
        ["diff", "--name-only", "--diff-filter=U", "-z"],
        cwd=worktree,
        check=False,
    )
    return tuple(
        item.decode("utf-8", errors="strict")
        for item in result.stdout.split(b"\0")
        if item
    )


def _unstaged_or_untracked(repo: Repository, worktree: Path) -> tuple[str, ...]:
    paths: list[str] = []
    unstaged = _git(repo, ["diff", "--name-only", "-z", "--"], cwd=worktree).stdout
    untracked = _git(
        repo,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
    ).stdout
    for raw in (unstaged, untracked):
        paths.extend(
            item.decode("utf-8", errors="strict")
            for item in raw.split(b"\0")
            if item
        )
    return tuple(dict.fromkeys(paths))


def _ignored_paths(repo: Repository, worktree: Path) -> tuple[str, ...]:
    raw = _git(
        repo,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=worktree,
    ).stdout
    return tuple(
        item.decode("utf-8", errors="strict")
        for item in raw.split(b"\0")
        if item
    )


def _unowned_runtime_paths(repo: Repository, worktree: Path) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*_unstaged_or_untracked(repo, worktree), *_ignored_paths(repo, worktree))))


def _cleanup_dirty_paths(repo: Repository, worktree: Path) -> tuple[str, ...]:
    staged = _git(repo, ["diff", "--cached", "--name-only", "-z", "--"], cwd=worktree).stdout
    staged_paths = tuple(
        item.decode("utf-8", errors="strict")
        for item in staged.split(b"\0")
        if item
    )
    return tuple(dict.fromkeys((*staged_paths, *_unowned_runtime_paths(repo, worktree))))


def _git_operation_markers(repo: Repository, worktree: Path) -> tuple[str, ...]:
    markers: list[str] = []
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "REBASE_HEAD",
        "BISECT_LOG",
        "sequencer",
        "rebase-merge",
        "rebase-apply",
    ):
        raw = _git(repo, ["rev-parse", "--git-path", name], cwd=worktree).stdout
        rendered = raw.decode("utf-8", errors="strict").strip()
        path = Path(rendered) if Path(rendered).is_absolute() else worktree / rendered
        if path.exists():
            markers.append(name)
    return tuple(markers)


def _active_workspace_claims(repo: Repository, worktree: Path) -> tuple[str, ...]:
    """Return active writer/runtime leases whose path overlaps this worktree."""

    context = _workspace_context(repo)
    leases, blockers = workspace.load_active_leases(context)
    if blockers:
        raise TrainError(
            "workspace lease registry is invalid during cleanup: "
            + "; ".join(item.code for item in blockers)
        )
    claims: list[str] = []
    for lease in leases:
        raw_path = lease.get("worktree_path")
        if isinstance(raw_path, str) and _paths_overlap(Path(raw_path), worktree):
            claims.append(
                f"PRD-{lease.get('iteration')}:{lease.get('runtime_namespace')}:{raw_path}"
            )
    return tuple(claims)


def _verification_receipts(
    commands: Sequence[VerifyCommand],
    worktree: Path,
) -> tuple[tuple[VerificationReceipt, ...], tuple[Blocker, ...]]:
    receipts: list[VerificationReceipt] = []
    blockers: list[Blocker] = []
    for command in commands:
        try:
            result = subprocess.run(
                list(command.argv),
                cwd=worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            blockers.append(Blocker("integration-verification-launch", f"{command.evidence_id}: {exc}"))
            continue
        stdout_digest = hashlib.sha256(result.stdout).hexdigest()
        stderr_digest = hashlib.sha256(result.stderr).hexdigest()
        receipts.append(
            VerificationReceipt(
                schema_version=VERIFICATION_RECEIPT_SCHEMA,
                evidence_id=command.evidence_id,
                argv=command.argv,
                exit_code=result.returncode,
                stdout_sha256=stdout_digest,
                stderr_sha256=stderr_digest,
            )
        )
        if len(result.stdout) > MAX_COMMAND_OUTPUT or len(result.stderr) > MAX_COMMAND_OUTPUT:
            blockers.append(Blocker("integration-verification-output-limit", command.evidence_id))
        if result.returncode != 0:
            detail = result.stderr[:4096].decode("utf-8", errors="replace").strip()
            blockers.append(
                Blocker(
                    "integration-verification-failed",
                    f"{command.evidence_id} exited with {result.returncode}: {detail}",
                )
            )
    return tuple(receipts), tuple(blockers)


def _commit_plan_payload(plan: IntegrationCommitPlan) -> dict[str, object]:
    data = plan.as_dict()
    data.pop("commit_plan_digest", None)
    return data


def integration_commit_plan_digest(plan: IntegrationCommitPlan) -> str:
    return digest(_commit_plan_payload(plan))


def _integration_changed_paths(repo: Repository, plan: IntegrationCommitPlan) -> tuple[str, ...]:
    """Return the exact tracked paths represented by the accepted integration tree."""

    result = _git(
        repo,
        [
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            plan.target_main,
            plan.integrated_tree,
            "--",
        ],
    )
    try:
        values = tuple(
            item.decode("utf-8", errors="strict")
            for item in result.stdout.split(b"\0")
            if item
        )
    except UnicodeDecodeError as exc:
        raise TrainError("integration changed paths are not valid UTF-8") from exc
    if not values:
        raise TrainError("integration commit has no tracked path changes to disclose")
    return values


def integration_commit_interaction(
    plan: IntegrationCommitPlan,
    phase: Literal["before", "after"],
    *,
    resulting_commit: str | None = None,
) -> InteractionEnvelope:
    """Build the mandatory exact commit card; this function performs no mutation."""

    if phase == "before" and resulting_commit is not None:
        raise TrainError("before commit interaction cannot claim a resulting commit")
    repo = open_repository(plan.project_root)
    actual: str | None = None
    if phase == "after":
        if resulting_commit is None or not OID_RE.fullmatch(resulting_commit):
            raise TrainError("after commit interaction requires the resulting commit")
        if not _commit_matches_plan(repo, resulting_commit, plan):
            raise TrainError("resulting commit does not match the accepted integration plan")
        actual = resulting_commit
    verification_ids = tuple(
        dict.fromkeys(
            (
                *(item.evidence_id for item in plan.verification_receipts),
                *plan.governance_receipt.evidence_ids,
            )
        )
    )
    iterations = tuple(dict.fromkeys(plan.dependency_order))
    return interaction(
        ActionFacts(
            action="commit",
            phase=phase,
            iteration="+".join(iterations),
            operation_id=plan.operation_id,
            project_root=plan.project_root,
            base_commit=plan.target_main,
            branch_ref="DETACHED",
            worktree_path=plan.integration_worktree,
            paths=_integration_changed_paths(repo, plan),
            message=plan.commit_message,
            verification_ids=verification_ids,
            excluded_paths=(
                "unstaged, untracked, and ignored runtime assets (required absent by gate)",
            ),
            resulting_commit=actual,
            affected_prds=iterations,
            runtime_namespace=f"integration:{plan.generation}",
            remote_involved=False,
            source_preserved=True,
            actual_head=actual,
            pushed=False,
            reason="create the exact latest-main integrated candidate commit",
            next_gate="build-integrated-evidence" if phase == "after" else "confirm-exact-commit-card",
        )
    )


def _governance_from_dict(value: Mapping[str, object]) -> GovernanceReceipt:
    return GovernanceReceipt(
        schema_version=str(value["schema_version"]),
        operation_id=str(value["operation_id"]),
        mode=str(value["mode"]),  # type: ignore[arg-type]
        target_main=str(value["target_main"]),
        principle_sha256=str(value["principle_sha256"]),
        candidate_digests=tuple(str(item) for item in value["candidate_digests"]),  # type: ignore[union-attr]
        input_tree=str(value["input_tree"]),
        result_tree=str(value["result_tree"]),
        evidence_ids=tuple(str(item) for item in value["evidence_ids"]),  # type: ignore[union-attr]
        evidence_digest=str(value["evidence_digest"]),
    )


def _verification_from_dict(value: Mapping[str, object]) -> VerificationReceipt:
    return VerificationReceipt(
        schema_version=str(value["schema_version"]),
        evidence_id=str(value["evidence_id"]),
        argv=tuple(str(item) for item in value["argv"]),  # type: ignore[union-attr]
        exit_code=int(value["exit_code"]),
        stdout_sha256=str(value["stdout_sha256"]),
        stderr_sha256=str(value["stderr_sha256"]),
    )


def _commit_plan_from_journal(
    plan: IntegrationPreparePlan,
    value: Mapping[str, object],
) -> IntegrationCommitPlan:
    governance_raw = value.get("governance_receipt")
    verification_raw = value.get("verification_receipts")
    if not isinstance(governance_raw, Mapping) or not isinstance(verification_raw, list):
        raise TrainError("prepared integration journal is incomplete")
    governance = _governance_from_dict(governance_raw)
    verification = tuple(
        _verification_from_dict(item)
        for item in verification_raw
        if isinstance(item, Mapping)
    )
    provisional = IntegrationCommitPlan(
        schema_version=COMMIT_PLAN_SCHEMA,
        operation_id=plan.operation_id,
        project_root=plan.project_root,
        integration_worktree=plan.worktree_path,
        generation=plan.generation,
        main_ref=plan.main_ref,
        target_main=plan.target_main,
        integrated_tree=str(value["integrated_tree"]),
        parent_commits=tuple(str(item) for item in value["parent_commits"]),  # type: ignore[union-attr]
        candidates=plan.candidates,
        dependency_order=plan.dependency_order,
        principle_sha256=plan.principle_sha256,
        merge_strategy=plan.merge_strategy,
        strategy_declaration_digest=plan.strategy_declaration_digest,
        governance_receipt=governance,
        verification_receipts=verification,
        commit_message=plan.commit_message,
        prepare_plan_digest=plan.plan_digest,
        commit_plan_digest="0" * 64,
    )
    commit_plan = replace(provisional, commit_plan_digest=integration_commit_plan_digest(provisional))
    expected = value.get("commit_plan_digest")
    if expected != commit_plan.commit_plan_digest:
        raise TrainError("prepared integration commit-plan digest is corrupt")
    return commit_plan


def _preparation_failure(
    plan: IntegrationPreparePlan,
    journal_path: Path,
    blockers: Sequence[Blocker],
    notifications: Sequence[InteractionEnvelope],
    *,
    governance_connected: bool,
) -> IntegrationPreparationResult:
    return IntegrationPreparationResult(
        schema_version=TRAIN_SCHEMA,
        operation_id=plan.operation_id,
        status="failed-needs-reconcile",
        worktree_path=plan.worktree_path,
        commit_plan=None,
        blockers=tuple(blockers),
        journal_path=str(journal_path),
        notifications=tuple(notifications),
        governance_apply_connected=governance_connected,
    )


def apply_prepare_integration(
    plan: IntegrationPreparePlan,
    *,
    accepted_plan_digest: str,
    confirmation_token: ConfirmationToken,
    notify: Notify,
    governance_callback: GovernanceCallback,
    governance_conflict_normalizer: GovernanceConflictNormalizer | None = None,
    failpoint: Failpoint | None = None,
) -> IntegrationPreparationResult:
    """Create a sibling worktree and prepare, reconcile, and verify its index.

    The operation deliberately stops before ``git commit``.  The returned
    commit plan is the exact object that must be shown to the user before the
    separate commit-confirmation call.
    """

    blockers = list(_prepare_plan_gate(plan))
    if accepted_plan_digest != plan.plan_digest:
        blockers.append(Blocker("integration-plan-not-accepted", "accepted digest differs from prepare plan"))
    blockers.extend(
        confirmation_token_gate(
            confirmation_token,
            action="prepare-integration",
            subject_digest=plan.plan_digest,
        )
    )
    if not callable(notify):
        blockers.append(Blocker("integration-notifier-missing", "worktree before/after notifier is required"))
    if not callable(governance_callback):
        blockers.append(Blocker("governance-callback-missing", "structured governance callback is required"))
    if blockers:
        raise TrainError("integration preparation blocked: " + "; ".join(item.code for item in blockers))
    repo = open_repository(plan.project_root)
    current = _integration_preconditions(repo, plan)
    if current:
        raise TrainError("integration preparation stale: " + "; ".join(item.code for item in current))
    journal_path = _journal_path(repo, "integration", plan.operation_id)
    journal = _read_json(journal_path, repo)
    if journal is not None and (
        journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("kind") != "integration-prepare"
        or journal.get("operation_id") != plan.operation_id
        or journal.get("plan_digest") != plan.plan_digest
    ):
        raise TrainError("integration journal identity is invalid")
    path = Path(plan.worktree_path)
    registered = _worktree_registered(repo, path)
    overlap_blockers = _integration_path_blockers(
        repo,
        path,
        allow_exact_registered=registered and journal is not None,
    )
    path_blockers = (*overlap_blockers, *_integration_path_parent_gate(repo, path))
    if path_blockers:
        raise TrainError("integration path unsafe: " + "; ".join(item.code for item in path_blockers))
    if path.exists() and not registered:
        raise TrainError("integration path exists but is not the operation worktree")
    _acquire_integration_lease(repo, plan)
    if journal is None:
        journal = {
            "schema_version": JOURNAL_SCHEMA,
            "kind": "integration-prepare",
            "operation_id": plan.operation_id,
            "plan_digest": plan.plan_digest,
            "accepted_plan_digest": accepted_plan_digest,
            "status": "planned",
            "worktree_path": plan.worktree_path,
            "expected_main": plan.target_main,
            "pushed": False,
        }
        try:
            _write_new_json(journal_path, journal, repo)
        except FileExistsError:
            journal = _read_json(journal_path, repo)
    if journal is None:
        raise TrainError("integration journal identity is invalid")
    if journal.get("status") in {"prepared", "commit-created", "evidence-ready", "main-advanced"}:
        commit_plan = _commit_plan_from_journal(plan, journal)
        if not registered:
            raise TrainError("prepared integration worktree is no longer registered")
        if _worktree_head(repo, path) not in {plan.target_main, journal.get("integrated_commit")}:
            raise TrainError("prepared integration HEAD changed")
        if journal.get("status") == "prepared":
            if _merge_head(repo, path) != plan.expected_merge_heads:
                raise TrainError("prepared integration MERGE_HEAD changed or gained an extra parent")
            if _staged_tree(repo, path) != commit_plan.integrated_tree:
                raise TrainError("prepared integration staged tree changed")
        return IntegrationPreparationResult(
            schema_version=TRAIN_SCHEMA,
            operation_id=plan.operation_id,
            status="prepared",
            worktree_path=plan.worktree_path,
            commit_plan=commit_plan,
            blockers=(),
            journal_path=str(journal_path),
            notifications=(),
            governance_apply_connected=True,
            idempotent=True,
        )

    notifications: list[InteractionEnvelope] = []
    if not registered:
        if not journal.get("before_notified"):
            before = _notification(plan, "before")
            before_envelope = before.interaction_envelope()
            notify(before_envelope)
            notifications.append(before_envelope)
            journal["before_notified"] = True
            _replace_json(journal_path, journal)
        # The user-visible callback is outside the topology lock.  Recheck all
        # topology and path-link facts after it returns, then hold the shared
        # lock only for the final preflight and Git registration.  A
        # non-cooperating direct Git writer can still race any Git command, but
        # it can no longer exploit the notification window.
        context = _workspace_context(repo)
        with workspace.coordinator_lock(context):
            after_notify_blockers = (
                *_integration_path_blockers(repo, path, allow_exact_registered=False),
                *_integration_path_parent_gate(repo, path),
            )
            if path.exists():
                after_notify_blockers = (
                    *after_notify_blockers,
                    Blocker("integration-worktree-collision", f"integration path appeared after notification: {path}"),
                )
            if after_notify_blockers:
                journal["status"] = "failed-needs-reconcile"
                journal["failure"] = after_notify_blockers[0].code
                _replace_json(journal_path, journal)
                return _preparation_failure(
                    plan,
                    journal_path,
                    after_notify_blockers,
                    notifications,
                    governance_connected=False,
                )
            path.mkdir(parents=False, exist_ok=False)
            created = _git_without_hooks(
                repo,
                ["worktree", "add", "--detach", str(path), plan.target_main],
                check=False,
            )
            if created.returncode != 0:
                with contextlib.suppress(OSError):
                    path.rmdir()
        if created.returncode != 0:
            detail = created.stderr.decode("utf-8", errors="replace").strip()
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = "integration-worktree-create-failed"
            _replace_json(journal_path, journal)
            return _preparation_failure(
                plan,
                journal_path,
                (Blocker("integration-worktree-create-failed", detail),),
                notifications,
                governance_connected=False,
            )
        journal["status"] = "worktree-ready"
        _replace_json(journal_path, journal)
        post_create_blockers = _integration_path_blockers(
            repo,
            path,
            allow_exact_registered=True,
        )
        if post_create_blockers or not _worktree_registered(repo, path):
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = "integration-worktree-postcheck-failed"
            _replace_json(journal_path, journal)
            return _preparation_failure(
                plan,
                journal_path,
                post_create_blockers
                or (Blocker("integration-worktree-postcheck-failed", "new worktree is not registered"),),
                notifications,
                governance_connected=False,
            )
        if _worktree_head(repo, path) != plan.target_main:
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = "integration-worktree-head-drift"
            _replace_json(journal_path, journal)
            return _preparation_failure(
                plan,
                journal_path,
                (Blocker("integration-worktree-head-drift", "created worktree differs from planned latest main"),),
                notifications,
                governance_connected=False,
            )
        if not journal.get("after_notified"):
            after = _notification(plan, "after")
            after_envelope = after.interaction_envelope()
            notify(after_envelope)
            notifications.append(after_envelope)
            journal["after_notified"] = True
            _replace_json(journal_path, journal)
        _trigger(failpoint, "integration-after-worktree")
    elif not journal.get("after_notified"):
        after = _notification(plan, "after")
        after_envelope = after.interaction_envelope()
        notify(after_envelope)
        notifications.append(after_envelope)
        journal["after_notified"] = True
        _replace_json(journal_path, journal)
    if _worktree_head(repo, path) != plan.target_main:
        raise TrainError("integration worktree HEAD differs from planned latest main")

    merge_heads = _merge_head(repo, path)
    staged_tree = _staged_tree(repo, path)
    recorded_merge_tree = journal.get("merge_tree")
    recorded_merge_heads = journal.get("merge_heads")
    governance_resume_admitted = False
    if recorded_merge_tree is None:
        if merge_heads or staged_tree != _head_tree(repo, path) or _unowned_runtime_paths(repo, path):
            raise TrainError("fresh integration worktree is not exact-clean before planned merge")
        refs = [item.candidate_ref for item in plan.candidates]
        if plan.merge_strategy == "merge-no-ff":
            arguments = ["merge", "--no-ff", "--no-commit", *refs]
        else:
            arguments = ["merge", "--squash", "--no-commit", *refs]
        merged = _git(repo, arguments, cwd=path, check=False)
        if merged.returncode != 0:
            conflicts = _unmerged_paths(repo, path)
            if conflicts and callable(governance_conflict_normalizer):
                try:
                    normalized = governance_conflict_normalizer(plan)
                    normalized_tree = getattr(normalized, "result_tree", None)
                    if (
                        not isinstance(normalized_tree, str)
                        or not OID_RE.fullmatch(normalized_tree)
                        or _unmerged_paths(repo, path)
                        or _staged_tree(repo, path) != normalized_tree
                    ):
                        raise TrainError(
                            "governance conflict normalizer did not produce one exact stage-0 index tree"
                        )
                except Exception as exc:
                    journal["status"] = "failed-needs-reconcile"
                    journal["failure"] = "governance-conflict-normalization-failed"
                    journal["conflicts"] = list(conflicts)
                    journal["governance_conflict_error"] = str(exc)[:1000]
                    _replace_json(journal_path, journal, repo)
                    return _preparation_failure(
                        plan,
                        journal_path,
                        (Blocker("governance-conflict-normalization-failed", str(exc)),),
                        notifications,
                        governance_connected=True,
                    )
                journal["governance_conflict_normalization"] = {
                    "plan_digest": getattr(normalized, "plan_digest", None),
                    "journal_path": getattr(normalized, "journal_path", None),
                    "journal_sha256": getattr(normalized, "journal_sha256", None),
                    "result_tree": normalized_tree,
                }
                conflicts = ()
                merged = subprocess.CompletedProcess(arguments, 0, b"", b"")
            code = "integration-merge-conflict" if conflicts else "integration-merge-failed"
            if merged.returncode != 0:
                detail = ", ".join(conflicts) if conflicts else merged.stderr.decode("utf-8", errors="replace").strip()
                journal["status"] = "failed-needs-reconcile"
                journal["failure"] = code
                journal["conflicts"] = list(conflicts)
                _replace_json(journal_path, journal, repo)
                return _preparation_failure(
                    plan,
                    journal_path,
                    (Blocker(code, detail or "Git merge failed"),),
                    notifications,
                    governance_connected=False,
                )
        merge_heads = _merge_head(repo, path)
        staged_tree = _staged_tree(repo, path)
        if merge_heads != plan.expected_merge_heads:
            failure = Blocker(
                "integration-merge-head-drift",
                "actual MERGE_HEAD does not equal the planned candidate parents",
            )
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = failure.code
            journal["actual_merge_heads"] = list(merge_heads)
            _replace_json(journal_path, journal)
            return _preparation_failure(
                plan,
                journal_path,
                (failure,),
                notifications,
                governance_connected=False,
            )
        if staged_tree == _head_tree(repo, path):
            failure = Blocker("integration-empty", "candidate merge produced no integrated tree change")
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = failure.code
            _replace_json(journal_path, journal)
            return _preparation_failure(
                plan,
                journal_path,
                (failure,),
                notifications,
                governance_connected=False,
            )
        journal["status"] = "merged"
        journal["merge_tree"] = staged_tree
        journal["merge_heads"] = list(merge_heads)
        _replace_json(journal_path, journal)
        recorded_merge_tree = staged_tree
        _trigger(failpoint, "integration-after-merge")
    else:
        if not isinstance(recorded_merge_tree, str) or not OID_RE.fullmatch(recorded_merge_tree):
            raise TrainError("integration journal merge tree is invalid")
        if recorded_merge_heads != list(plan.expected_merge_heads):
            raise TrainError("integration journal MERGE_HEAD differs from accepted plan")
        if merge_heads != plan.expected_merge_heads:
            raise TrainError("resumed integration MERGE_HEAD changed or gained an extra parent")
        governance_raw = journal.get("governance_receipt")
        expected_resume_tree = recorded_merge_tree
        if isinstance(governance_raw, Mapping):
            result_tree = governance_raw.get("result_tree")
            if not isinstance(result_tree, str) or not OID_RE.fullmatch(result_tree):
                raise TrainError("integration journal governance tree is invalid")
            expected_resume_tree = result_tree
        # A governance callback may have durably completed only a prefix of
        # its reconciliation plan before the process stopped.  Admit that
        # intermediate index/filesystem state only when the *same* callback
        # can prove it from its own exact journal and allowed-tree set.  This
        # duck-typed boundary avoids a train<->adapter import cycle while
        # keeping fail-closed semantics for every other callback.
        runtime_paths = _unowned_runtime_paths(repo, path)
        if staged_tree != expected_resume_tree or runtime_paths:
            inspector = getattr(governance_callback, "inspect_resume", None)
            if not callable(inspector) or isinstance(governance_raw, Mapping):
                if staged_tree != expected_resume_tree:
                    raise TrainError("resumed integration staged tree differs from its durable planned tree")
                raise TrainError("resumed integration has unowned runtime paths")
            resume_context = GovernanceContext(
                schema_version=GOVERNANCE_RECEIPT_SCHEMA,
                operation_id=plan.operation_id,
                project_root=plan.project_root,
                integration_worktree=plan.worktree_path,
                target_main=plan.target_main,
                principle_sha256=plan.principle_sha256,
                candidate_digests=tuple(
                    item.candidate_evidence.evidence_digest for item in plan.candidates
                ),
                pre_governance_tree=recorded_merge_tree,
            )
            try:
                state = inspector(resume_context)
            except Exception as exc:
                raise TrainError(f"governance resume inspection failed: {exc}") from exc
            allowed = getattr(state, "allowed_intermediate_trees", ())
            if (
                getattr(state, "resumable", False) is not True
                or getattr(state, "operation_id", None) != plan.operation_id
                or getattr(state, "original_input_tree", None) != recorded_merge_tree
                or getattr(state, "actual_index_tree", None) != staged_tree
                or not isinstance(allowed, tuple)
                or staged_tree not in allowed
            ):
                raise TrainError("governance callback cannot prove the resumed intermediate tree")
            governance_resume_admitted = True

    if not governance_resume_admitted and _unowned_runtime_paths(repo, path):
        failure = Blocker(
            "integration-worktree-unowned-files",
            "merge left unstaged or untracked files; adapter will not clean or absorb them",
        )
        journal["status"] = "failed-needs-reconcile"
        journal["failure"] = failure.code
        _replace_json(journal_path, journal)
        return _preparation_failure(
            plan,
            journal_path,
            (failure,),
            notifications,
            governance_connected=False,
        )

    candidate_digests = tuple(item.candidate_evidence.evidence_digest for item in plan.candidates)
    if not isinstance(recorded_merge_tree, str) or not OID_RE.fullmatch(recorded_merge_tree):
        raise TrainError("integration journal does not contain an exact pre-governance tree")
    context = GovernanceContext(
        schema_version=GOVERNANCE_RECEIPT_SCHEMA,
        operation_id=plan.operation_id,
        project_root=plan.project_root,
        integration_worktree=plan.worktree_path,
        target_main=plan.target_main,
        principle_sha256=plan.principle_sha256,
        candidate_digests=candidate_digests,
        pre_governance_tree=recorded_merge_tree,
    )
    governance_raw = journal.get("governance_receipt")
    if isinstance(governance_raw, Mapping):
        governance_receipt = _governance_from_dict(governance_raw)
    else:
        try:
            governance_receipt = governance_callback(context)
        except Exception as exc:
            failure = Blocker("governance-callback-failed", str(exc))
            journal["status"] = "failed-needs-reconcile"
            journal["failure"] = failure.code
            _replace_json(journal_path, journal)
            return _preparation_failure(
                plan,
                journal_path,
                (failure,),
                notifications,
                governance_connected=False,
            )
        journal["governance_receipt"] = governance_receipt.as_dict()
        journal["status"] = "governance-returned"
        _replace_json(journal_path, journal)
        _trigger(failpoint, "integration-after-governance")
    governed_tree = _staged_tree(repo, path)
    governance_blockers = governance_receipt_gate(
        governance_receipt,
        context,
        actual_result_tree=governed_tree,
    )
    if governance_blockers:
        journal["status"] = "failed-needs-reconcile"
        journal["failure"] = governance_blockers[0].code
        _replace_json(journal_path, journal)
        return _preparation_failure(
            plan,
            journal_path,
            governance_blockers,
            notifications,
            governance_connected=governance_receipt.mode == "applied",
        )
    if _unowned_runtime_paths(repo, path):
        failure = Blocker("governance-left-unowned-files", "governance callback left unstaged or untracked files")
        journal["status"] = "failed-needs-reconcile"
        journal["failure"] = failure.code
        _replace_json(journal_path, journal)
        return _preparation_failure(
            plan,
            journal_path,
            (failure,),
            notifications,
            governance_connected=True,
        )

    verification_raw = journal.get("verification_receipts")
    if isinstance(verification_raw, list) and journal.get("verification_tree") == governed_tree:
        verification_receipts = tuple(
            _verification_from_dict(item)
            for item in verification_raw
            if isinstance(item, Mapping)
        )
        verification_blockers: tuple[Blocker, ...] = ()
    else:
        verification_receipts, verification_blockers = _verification_receipts(plan.verify_commands, path)
        journal["verification_receipts"] = [item.as_dict() for item in verification_receipts]
        journal["verification_tree"] = governed_tree
        journal["status"] = "verified" if not verification_blockers else "failed-needs-reconcile"
        _replace_json(journal_path, journal)
        _trigger(failpoint, "integration-after-verification")
    if verification_blockers:
        return _preparation_failure(
            plan,
            journal_path,
            verification_blockers,
            notifications,
            governance_connected=True,
        )
    after_verify_tree = _staged_tree(repo, path)
    if after_verify_tree != governed_tree or _unowned_runtime_paths(repo, path):
        failure = Blocker(
            "integration-verification-mutated-worktree",
            "verification changed the staged, unstaged, or untracked integration state",
        )
        journal["status"] = "failed-needs-reconcile"
        journal["failure"] = failure.code
        _replace_json(journal_path, journal)
        return _preparation_failure(
            plan,
            journal_path,
            (failure,),
            notifications,
            governance_connected=True,
        )

    current_head = _worktree_head(repo, path)
    current_merge_heads = _merge_head(repo, path)
    if current_head != plan.target_main:
        raise TrainError("integration HEAD changed before commit-plan construction")
    if current_merge_heads != plan.expected_merge_heads:
        raise TrainError("integration MERGE_HEAD changed before commit-plan construction")
    if plan.merge_strategy == "merge-no-ff":
        parent_commits = (plan.target_main, *plan.expected_merge_heads)
    else:
        parent_commits = (plan.target_main,)
    provisional = IntegrationCommitPlan(
        schema_version=COMMIT_PLAN_SCHEMA,
        operation_id=plan.operation_id,
        project_root=plan.project_root,
        integration_worktree=plan.worktree_path,
        generation=plan.generation,
        main_ref=plan.main_ref,
        target_main=plan.target_main,
        integrated_tree=after_verify_tree,
        parent_commits=parent_commits,
        candidates=plan.candidates,
        dependency_order=plan.dependency_order,
        principle_sha256=plan.principle_sha256,
        merge_strategy=plan.merge_strategy,
        strategy_declaration_digest=plan.strategy_declaration_digest,
        governance_receipt=governance_receipt,
        verification_receipts=verification_receipts,
        commit_message=plan.commit_message,
        prepare_plan_digest=plan.plan_digest,
        commit_plan_digest="0" * 64,
    )
    commit_plan = replace(provisional, commit_plan_digest=integration_commit_plan_digest(provisional))
    journal.update(
        {
            "status": "prepared",
            "integrated_tree": commit_plan.integrated_tree,
            "parent_commits": list(commit_plan.parent_commits),
            "commit_plan_digest": commit_plan.commit_plan_digest,
        }
    )
    _replace_json(journal_path, journal)
    _trigger(failpoint, "integration-after-prepared")
    return IntegrationPreparationResult(
        schema_version=TRAIN_SCHEMA,
        operation_id=plan.operation_id,
        status="prepared",
        worktree_path=plan.worktree_path,
        commit_plan=commit_plan,
        blockers=(),
        journal_path=str(journal_path),
        notifications=tuple(notifications),
        governance_apply_connected=True,
    )


def _commit_plan_gate(plan: IntegrationCommitPlan) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if plan.schema_version != COMMIT_PLAN_SCHEMA:
        blockers.append(Blocker("integration-commit-plan-schema", "integration commit plan schema is unsupported"))
    if plan.commit_plan_digest != integration_commit_plan_digest(plan):
        blockers.append(Blocker("integration-commit-plan-digest", "integration commit plan was changed"))
    context = GovernanceContext(
        schema_version=GOVERNANCE_RECEIPT_SCHEMA,
        operation_id=plan.operation_id,
        project_root=plan.project_root,
        integration_worktree=plan.integration_worktree,
        target_main=plan.target_main,
        principle_sha256=plan.principle_sha256,
        candidate_digests=tuple(item.candidate_evidence.evidence_digest for item in plan.candidates),
        pre_governance_tree=plan.governance_receipt.input_tree,
    )
    blockers.extend(
        governance_receipt_gate(
            plan.governance_receipt,
            context,
            actual_result_tree=plan.integrated_tree,
        )
    )
    if not plan.verification_receipts:
        blockers.append(Blocker("integration-verification-receipt-missing", "integration has no verification receipt"))
    for receipt in plan.verification_receipts:
        if receipt.schema_version != VERIFICATION_RECEIPT_SCHEMA or receipt.exit_code != 0:
            blockers.append(Blocker("integration-verification-receipt-invalid", receipt.evidence_id))
    return tuple(blockers)


def _commit_parents(repo: Repository, commit: str) -> tuple[str, ...]:
    result = _git(repo, ["rev-list", "--parents", "-n", "1", commit])
    values = result.stdout.decode("ascii", errors="strict").strip().split()
    if not values or values[0] != commit:
        raise TrainError("Git returned malformed commit parents")
    return tuple(_validate_oid(item, "commit parent") for item in values[1:])


def _commit_message(repo: Repository, commit: str) -> str:
    result = _git(repo, ["log", "-1", "--format=%B", commit])
    return result.stdout.decode("utf-8", errors="strict").rstrip("\r\n")


def _commit_matches_plan(
    repo: Repository,
    commit: str,
    plan: IntegrationCommitPlan,
) -> bool:
    return (
        _object_type(repo, commit) == "commit"
        and _commit_tree(repo, commit) == plan.integrated_tree
        and _commit_parents(repo, commit) == plan.parent_commits
        and _commit_message(repo, commit) == plan.commit_message
    )


def _build_integrated_evidence(
    repo: Repository,
    plan: IntegrationCommitPlan,
    integrated_commit: str,
    identity_rebindings: Sequence[IdentityRebindEvidence],
) -> IntegratedCandidate:
    preserved = tuple(
        candidate.candidate_commit
        for candidate in plan.candidates
        if _is_ancestor(repo, candidate.candidate_commit, integrated_commit)
    )
    return build_integrated_candidate(
        IntegrationInput(
            generation=plan.generation,
            target_main=plan.target_main,
            integrated_commit=integrated_commit,
            integrated_tree=plan.integrated_tree,
            principle_sha256=plan.principle_sha256,
            candidates=tuple(item.candidate_evidence for item in plan.candidates),
            merge_strategy=plan.merge_strategy,  # type: ignore[arg-type]
            strategy_declaration_digest=plan.strategy_declaration_digest,
            dependency_order=plan.dependency_order,
            preserved_candidate_commits=preserved,
            identity_rebindings=tuple(identity_rebindings),
            governance_reconciled=True,
            governance_evidence_digest=plan.governance_receipt.evidence_digest,
            cross_prd_verification_ids=tuple(
                item.evidence_id for item in plan.verification_receipts
            ),
            integration_evidence_ids=tuple(
                dict.fromkeys(
                    (
                        *plan.governance_receipt.evidence_ids,
                        f"train:{plan.commit_plan_digest}",
                    )
                )
            ),
        )
    )


def _identity_rebind_adapter_gate(
    plan: IntegrationCommitPlan,
    rebind: IdentityRebindEvidence,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    gate = identity_rebind_evidence_gate(rebind)
    if not gate.allowed:
        blockers.append(Blocker("identity-rebind-core-invalid", ", ".join(gate.blockers)))
    actual_verification_ids = {item.evidence_id for item in plan.verification_receipts}
    if not set(rebind.verification_ids).issubset(actual_verification_ids):
        blockers.append(
            Blocker(
                "identity-rebind-verification-unobserved",
                "identity rebind cites verification not executed by this integration operation",
            )
        )
    actual_evidence_ids = {
        *plan.governance_receipt.evidence_ids,
        f"train:{plan.commit_plan_digest}",
    }
    if not set(rebind.evidence_ids).issubset(actual_evidence_ids):
        blockers.append(
            Blocker(
                "identity-rebind-evidence-unobserved",
                "identity rebind cites evidence not produced by this integration operation",
            )
        )
    return tuple(blockers)


def _integration_journal_for_commit(
    repo: Repository,
    plan: IntegrationCommitPlan,
) -> tuple[Path, dict[str, object]]:
    path = _journal_path(repo, "integration", plan.operation_id)
    journal = _read_json(path, repo)
    if (
        journal is None
        or journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("kind") != "integration-prepare"
        or journal.get("plan_digest") != plan.prepare_plan_digest
        or journal.get("commit_plan_digest") != plan.commit_plan_digest
    ):
        raise TrainError("integration journal does not authorize this commit plan")
    return path, journal


def apply_integration_commit(
    plan: IntegrationCommitPlan,
    *,
    accepted_commit_plan_digest: str,
    confirmation_token: ConfirmationToken,
    identity_rebindings: Sequence[IdentityRebindEvidence] = (),
    failpoint: Failpoint | None = None,
) -> IntegrationCommitResult:
    """Create the integration commit only after exact, explicit confirmation."""

    blockers = list(_commit_plan_gate(plan))
    if accepted_commit_plan_digest != plan.commit_plan_digest:
        blockers.append(Blocker("integration-commit-plan-not-accepted", "accepted digest differs from commit plan"))
    blockers.extend(
        confirmation_token_gate(
            confirmation_token,
            action="create-integration-commit",
            subject_digest=plan.commit_plan_digest,
        )
    )
    if blockers:
        raise TrainError("integration commit blocked: " + "; ".join(item.code for item in blockers))
    repo = open_repository(plan.project_root)
    worktree = Path(plan.integration_worktree)
    if not _worktree_registered(repo, worktree):
        raise TrainError("integration worktree is no longer registered")
    path_blockers = _integration_path_blockers(repo, worktree, allow_exact_registered=True)
    if path_blockers:
        raise TrainError("integration worktree overlap detected: " + "; ".join(item.code for item in path_blockers))
    current_main = _resolve_ref(repo, plan.main_ref)
    if current_main != plan.target_main:
        raise TrainError("main drifted before integration commit; rebuild from latest main")
    for candidate in plan.candidates:
        if _resolve_ref(repo, candidate.candidate_ref) != candidate.candidate_commit:
            raise TrainError(f"candidate ref drifted before integration commit: {candidate.candidate_ref}")
        principle_blockers = _current_principle_audit_blockers(
            repo,
            candidate.iteration,
        )
        if principle_blockers:
            raise TrainError(
                "principle gate denied before integration commit: "
                + "; ".join(item.code for item in principle_blockers)
            )
    journal_path, journal = _integration_journal_for_commit(repo, plan)
    prior = journal.get("integrated_commit")
    idempotent = False
    if isinstance(prior, str) and OID_RE.fullmatch(prior) and _commit_matches_plan(repo, prior, plan):
        integrated_commit = prior
        idempotent = True
    else:
        head = _worktree_head(repo, worktree)
        if head != plan.target_main:
            if _commit_matches_plan(repo, head, plan):
                integrated_commit = head
                idempotent = True
            else:
                raise TrainError("integration worktree HEAD changed outside the accepted commit plan")
        else:
            if _staged_tree(repo, worktree) != plan.integrated_tree:
                raise TrainError("staged integration tree changed after commit confirmation")
            if _unowned_runtime_paths(repo, worktree):
                raise TrainError("integration worktree gained unstaged, untracked, or ignored files")
            if plan.merge_strategy == "merge-no-ff":
                actual_parents = (head, *_merge_head(repo, worktree))
                if actual_parents != plan.parent_commits:
                    raise TrainError("MERGE_HEAD parents changed after commit confirmation")
            committed = _git_without_hooks(
                repo,
                [
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-gpg-sign",
                    "--no-verify",
                    "-m",
                    plan.commit_message,
                ],
                cwd=worktree,
                check=False,
            )
            if committed.returncode != 0:
                detail = committed.stderr.decode("utf-8", errors="replace").strip()
                raise TrainError(f"integration commit failed: {detail}")
            integrated_commit = _worktree_head(repo, worktree)
            if not _commit_matches_plan(repo, integrated_commit, plan):
                raise TrainError("created integration commit does not match accepted tree/parents/message")
            _trigger(failpoint, "integration-after-commit")
        journal["integrated_commit"] = integrated_commit
        journal["status"] = "commit-created"
        journal["commit_confirmation_id"] = confirmation_token.authorization_id
        _replace_json(journal_path, journal)

    if _unowned_runtime_paths(repo, worktree):
        raise TrainError("integration commit or concurrent activity changed the worktree")
    for rebind in identity_rebindings:
        rebind_blockers = _identity_rebind_adapter_gate(plan, rebind)
        if rebind_blockers:
            raise TrainError("identity rebind evidence is invalid: " + "; ".join(item.code for item in rebind_blockers))
    integrated = _build_integrated_evidence(repo, plan, integrated_commit, identity_rebindings)
    evidence_gate = integrated_evidence_gate(integrated)
    result_blockers: list[Blocker] = []
    evidence: IntegratedCandidate | None = integrated
    if not evidence_gate.allowed:
        evidence = None
        for reason in integrated.blockers or evidence_gate.blockers:
            code = (
                "identity-rebind-required"
                if "identity-changed-rebind-required" in reason
                else "integrated-evidence-blocked"
            )
            result_blockers.append(Blocker(code, reason))
    else:
        journal["status"] = "evidence-ready"
        journal["integrated_evidence_digest"] = integrated.evidence_digest
        journal["integrated_evidence"] = integrated.as_dict()
        _replace_json(journal_path, journal)
    return IntegrationCommitResult(
        schema_version=COMMIT_RESULT_SCHEMA,
        operation_id=plan.operation_id,
        project_root=plan.project_root,
        integration_worktree=plan.integration_worktree,
        generation=plan.generation,
        integrated_commit=integrated_commit,
        integrated_tree=plan.integrated_tree,
        commit_plan=plan,
        commit_confirmation_token=confirmation_token,
        integrated_candidate=evidence,
        blockers=tuple(result_blockers),
        journal_path=str(journal_path),
        idempotent=idempotent,
    )


def finalize_integration_evidence(
    result: IntegrationCommitResult,
    *,
    identity_rebindings: Sequence[IdentityRebindEvidence],
) -> IntegrationCommitResult:
    """Bind new verification/evidence after a strategy changed commit identity."""

    if result.schema_version != COMMIT_RESULT_SCHEMA:
        raise TrainError("integration commit result schema is unsupported")
    repo = open_repository(result.project_root)
    if not _commit_matches_plan(repo, result.integrated_commit, result.commit_plan):
        raise TrainError("integration commit changed before identity evidence finalization")
    for rebind in identity_rebindings:
        rebind_blockers = _identity_rebind_adapter_gate(result.commit_plan, rebind)
        if rebind_blockers:
            raise TrainError("identity rebind evidence is invalid: " + "; ".join(item.code for item in rebind_blockers))
    integrated = _build_integrated_evidence(
        repo,
        result.commit_plan,
        result.integrated_commit,
        identity_rebindings,
    )
    gate = integrated_evidence_gate(integrated)
    if not gate.allowed:
        return replace(
            result,
            integrated_candidate=None,
            blockers=tuple(Blocker("integrated-evidence-blocked", item) for item in integrated.blockers),
        )
    journal_path = Path(result.journal_path)
    journal = _read_json(journal_path, repo)
    if journal is None or journal.get("integrated_commit") != result.integrated_commit:
        raise TrainError("integration journal is missing the committed identity")
    journal["status"] = "evidence-ready"
    journal["integrated_evidence_digest"] = integrated.evidence_digest
    journal["integrated_evidence"] = integrated.as_dict()
    _replace_json(journal_path, journal, repo)
    return replace(result, integrated_candidate=integrated, blockers=())


def _ref_checked_out(repo: Repository, reference: str) -> bool:
    raw = _git(repo, ["worktree", "list", "--porcelain"]).stdout.decode("utf-8", errors="strict")
    current_branch: str | None = None
    for line in (*raw.splitlines(), ""):
        if line.startswith("branch "):
            current_branch = line.removeprefix("branch ").strip()
        elif line == "":
            if current_branch == reference:
                return True
            current_branch = None
    return False


def _local_main_release_gate(repo: Repository) -> tuple[tuple[tuple[str, str, int, str], ...], tuple[Blocker, ...]]:
    """Prove that no active Local writer still claims main without an exact bind receipt."""
    try:
        context = workspace.resolve_repository(repo.root)
        leases, lease_blockers = workspace.load_active_leases(context)
    except workspace.WorkspaceError as exc:
        return (), (Blocker("main-release-workspace-unreadable", str(exc)),)
    blockers = [Blocker(f"main-release-{item.code}", item.message) for item in lease_blockers]
    receipts: list[tuple[str, str, int, str]] = []
    for lease in leases:
        if lease.get("execution_topology") != "local":
            continue
        iteration = str(lease["iteration"])
        branch_ref = str(lease["branch_ref"])
        generation = int(lease["generation"])
        operation_id = str(lease["operation_id"])
        if branch_ref == DEFAULT_MAIN_REF:
            blockers.append(
                Blocker(
                    "main-release-local-holder-active",
                    f"PRD-{iteration} still owns main; use bind-local-branch before integration",
                )
            )
            continue
        try:
            journal = workspace.load_journal(context, operation_id)
        except workspace.WorkspaceError as exc:
            blockers.append(Blocker("main-release-bind-journal-invalid", str(exc)))
            continue
        created = journal.get("created_objects") if isinstance(journal, dict) else None
        if (
            not isinstance(journal, dict)
            or journal.get("action") != "bind-local-branch"
            or journal.get("phase") != "READY"
            or journal.get("iteration") != iteration
            or journal.get("lease_generation") != generation - 1
            or not isinstance(created, dict)
            or created.get("local_branch_ref") != branch_ref
            or created.get("writer_lease_generation") != generation
        ):
            blockers.append(
                Blocker(
                    "main-release-bind-receipt-missing",
                    f"PRD-{iteration} Local writer lacks its exact successful main-release receipt",
                )
            )
            continue
        receipts.append((iteration, operation_id, generation, digest(journal)))
    return tuple(sorted(receipts)), tuple(blockers)


def _advance_plan_payload(plan: MainAdvancePlan) -> dict[str, object]:
    data = plan.as_dict()
    data.pop("plan_digest", None)
    return data


def main_advance_plan_digest(plan: MainAdvancePlan) -> str:
    return digest(_advance_plan_payload(plan))


def _integrated_evidence_registry_module():
    try:
        from . import harness_integrated_evidence as registry
    except ImportError:  # pragma: no cover - direct execution
        import harness_integrated_evidence as registry
    return registry


def _final_acceptance_registry_module():
    try:
        from . import harness_final_acceptance as registry
    except ImportError:  # pragma: no cover - direct execution
        import harness_final_acceptance as registry
    return registry


def plan_main_advance(
    registered_evidence: "RegisteredIntegratedEvidence",
    *,
    principle_path: str = DEFAULT_PRINCIPLE_PATH,
) -> MainAdvancePlan:
    """Plan main advancement from canonical public integrated evidence only."""

    registry = _integrated_evidence_registry_module()
    if not isinstance(registered_evidence, registry.RegisteredIntegratedEvidence):
        raise TrainError(
            "main advance requires RegisteredIntegratedEvidence; private integration journal evidence is insufficient"
        )
    repo = open_repository(registered_evidence.project_root)
    blockers: list[Blocker] = list(
        registry.registered_integrated_evidence_gate(repo.root, registered_evidence)
    )
    loaded = None
    try:
        loaded, load_blockers = registry.load_registered_integrated_evidence(
            repo.root,
            operation_id=registered_evidence.operation_id,
        )
    except registry.IntegratedEvidenceError as exc:
        load_blockers = (Blocker("integrated-evidence-public-load", str(exc)),)
    blockers.extend(load_blockers)
    if loaded is None:
        blockers.append(
            Blocker(
                "integrated-evidence-registration-required",
                "canonical operation commit/evidence refs are absent or invalid",
            )
        )
    elif loaded.registration_digest != registered_evidence.registration_digest:
        blockers.append(
            Blocker(
                "integrated-evidence-registration-identity",
                "supplied receipt differs from the canonical public registry",
            )
        )

    envelope = registered_evidence.metadata
    evidence = envelope.integrated_candidate
    current_main = _resolve_ref(repo, envelope.main_ref)
    if current_main != envelope.target_main:
        blockers.append(Blocker("main-advance-main-drift", "main changed after integration preparation"))
    if _object_type(repo, envelope.integrated_commit) != "commit":
        blockers.append(Blocker("main-advance-commit-missing", envelope.integrated_commit))
    else:
        try:
            if (
                _commit_tree(repo, envelope.integrated_commit) != envelope.integrated_tree
                or _commit_parents(repo, envelope.integrated_commit) != envelope.parent_commits
                or _commit_message(repo, envelope.integrated_commit) != envelope.commit_message
            ):
                blockers.append(
                    Blocker("main-advance-commit-drift", "integrated commit differs from public evidence")
                )
        except TrainError as exc:
            blockers.append(Blocker("main-advance-commit-unreadable", str(exc)))
    principle = _validate_repo_path(principle_path, "principle_path")
    principle_sha = "0" * 64
    try:
        _, principle_raw = _blob_at(repo, envelope.target_main, principle)
        principle_sha = hashlib.sha256(principle_raw).hexdigest()
    except TrainError as exc:
        blockers.append(Blocker("main-advance-principle-unreadable", str(exc)))
    if principle_sha != envelope.principle_sha256:
        blockers.append(Blocker("main-advance-principle-drift", "principle differs from integrated evidence"))

    candidate_refs: list[tuple[str, str]] = []
    source_ref_bindings: dict[str, str] = {}

    def bind_source(reference: str, oid: str) -> None:
        prior = source_ref_bindings.get(reference)
        if prior is not None and prior != oid:
            blockers.append(Blocker("main-advance-source-ref-conflict", reference))
        else:
            source_ref_bindings[reference] = oid

    bind_source(registered_evidence.commit_ref, envelope.integrated_commit)
    bind_source(registered_evidence.evidence_ref, registered_evidence.evidence_blob)
    iteration_evidence_refs: list[tuple[str, str]] = []
    for item in registered_evidence.iteration_evidence_refs:
        bind_source(item.ref_name, registered_evidence.evidence_blob)
        iteration_evidence_refs.append((item.ref_name, registered_evidence.evidence_blob))
    for candidate in envelope.candidate_bindings:
        candidate_refs.append((candidate.candidate_ref, candidate.candidate_commit))
        bind_source(candidate.candidate_ref, candidate.candidate_commit)
        bind_source(candidate.candidate_evidence_ref, candidate.candidate_evidence_blob)
        blockers.extend(
            _current_principle_audit_blockers(
                repo,
                candidate.iteration,
            )
        )
        if _resolve_ref(repo, candidate.candidate_ref) != candidate.candidate_commit:
            blockers.append(Blocker("main-advance-candidate-drift", candidate.candidate_ref))
    if _ref_checked_out(repo, envelope.main_ref):
        blockers.append(
            Blocker(
                "main-ref-checked-out",
                "main is checked out in a worktree; release/bind Local before atomic ref advance",
            )
        )
    release_receipts, release_blockers = _local_main_release_gate(repo)
    blockers.extend(release_blockers)
    updates: list[tuple[str, str | None, str]] = [
        (envelope.main_ref, envelope.target_main, envelope.integrated_commit)
    ]
    for iteration in dict.fromkeys(item.iteration for item in envelope.candidate_bindings):
        for suffix in ("integrated", "final"):
            reference = f"refs/project-harness/v2/iterations/{iteration}/{suffix}"
            old = _resolve_ref(repo, reference)
            if old is not None:
                blockers.append(Blocker("main-advance-target-ref-exists", reference))
            updates.append((reference, None, envelope.integrated_commit))
    gate = main_advance_gate(
        evidence,
        current_main=envelope.target_main,
        current_integrated_commit=envelope.integrated_commit,
        current_integrated_tree=envelope.integrated_tree,
        current_principle_sha256=principle_sha,
        current_candidate_digests=tuple(
            item.candidate_evidence_digest for item in envelope.candidate_bindings
        ),
        current_identity_rebind_digests=evidence.identity_rebind_digests,
        user_accepted_evidence_digest=evidence.evidence_digest,
    )
    if not gate.allowed:
        blockers.append(Blocker("main-advance-core-gate", ", ".join(gate.blockers)))
    provisional = MainAdvancePlan(
        schema_version=ADVANCE_PLAN_SCHEMA,
        operation_id=registered_evidence.operation_id,
        project_root=str(repo.root),
        git_common_dir=str(repo.common_dir),
        integration_worktree=str(envelope.commit_plan_snapshot.get("integration_worktree", "")),
        main_ref=envelope.main_ref,
        expected_main=envelope.target_main,
        integrated_commit=envelope.integrated_commit,
        integrated_tree=envelope.integrated_tree,
        integrated_evidence_digest=registered_evidence.registration_digest,
        integrated_evidence_metadata_digest=envelope.metadata_digest,
        integrated_evidence_blob=registered_evidence.evidence_blob,
        operation_commit_ref=registered_evidence.commit_ref,
        operation_evidence_ref=registered_evidence.evidence_ref,
        iteration_evidence_refs=tuple(iteration_evidence_refs),
        principle_path=principle,
        principle_sha256=principle_sha,
        candidate_refs=tuple(candidate_refs),
        source_ref_bindings=tuple(source_ref_bindings.items()),
        ref_updates=tuple(updates),
        integration_commit_result_digest=envelope.commit_result_digest,
        local_main_release_receipts=release_receipts,
        plan_digest="0" * 64,
        blockers=tuple(blockers),
    )
    return replace(provisional, plan_digest=main_advance_plan_digest(provisional))


def _advance_plan_gate(plan: MainAdvancePlan) -> tuple[Blocker, ...]:
    blockers = list(plan.blockers)
    if plan.schema_version != ADVANCE_PLAN_SCHEMA:
        blockers.append(Blocker("main-advance-plan-schema", "main advance plan schema is unsupported"))
    if plan.plan_digest != main_advance_plan_digest(plan):
        blockers.append(Blocker("main-advance-plan-digest", "main advance plan was changed"))
    return tuple(blockers)


def _advance_iterations(plan: MainAdvancePlan) -> tuple[str, ...]:
    values: list[str] = []
    for reference, _old, _new in plan.ref_updates:
        match = re.fullmatch(r"refs/project-harness/v2/iterations/([0-9]{3,})/(?:integrated|final)", reference)
        if match and match.group(1) not in values:
            values.append(match.group(1))
    return tuple(values)


def main_advance_interaction(
    plan: MainAdvancePlan,
    phase: Literal["before", "after"],
) -> InteractionEnvelope:
    """Build the exact, separate main-advance card; never contacts a remote."""

    repo = open_repository(plan.project_root)
    if phase == "after":
        if not _all_ref_updates_applied(repo, plan):
            raise TrainError("cannot report main advance completion before all exact refs are applied")
        actual_head: str | None = plan.integrated_commit
        next_gate = "remove-clean-integration-worktree"
    else:
        if _resolve_ref(repo, plan.main_ref) != plan.expected_main:
            raise TrainError("main changed before the confirmation card could be shown")
        actual_head = None
        next_gate = "confirm-exact-main-advance"
    iterations = _advance_iterations(plan)
    return interaction(
        ActionFacts(
            action="main-advance",
            phase=phase,
            iteration="+".join(iterations),
            operation_id=plan.operation_id,
            project_root=plan.project_root,
            base_commit=plan.expected_main,
            branch_ref=plan.main_ref,
            worktree_path=plan.integration_worktree,
            paths=tuple(reference for reference, _old, _new in plan.ref_updates),
            verification_ids=(f"integrated-evidence:{plan.integrated_evidence_digest}",),
            resulting_commit=plan.integrated_commit if phase == "after" else None,
            source_ref=f"integrated-candidate:{plan.operation_id}",
            target_ref=plan.main_ref,
            commit_range=f"{plan.expected_main}..{plan.integrated_commit}",
            affected_prds=iterations,
            runtime_namespace="main-integration-lane",
            remote_involved=False,
            source_preserved=True,
            actual_head=actual_head,
            force=False,
            pushed=False,
            reason="advance main only to the exact accepted and revalidated integrated candidate",
            next_gate=next_gate,
        )
    )


def _all_ref_updates_applied(repo: Repository, plan: MainAdvancePlan) -> bool:
    return all(_resolve_ref(repo, reference) == new for reference, _old, new in plan.ref_updates)


def _public_integrated_evidence_blockers(
    repo: Repository,
    plan: MainAdvancePlan,
) -> tuple[Blocker, ...]:
    registry = _integrated_evidence_registry_module()
    try:
        loaded, blockers = registry.load_registered_integrated_evidence(
            repo.root,
            operation_id=plan.operation_id,
        )
    except registry.IntegratedEvidenceError as exc:
        return (Blocker("integrated-evidence-public-load", str(exc)),)
    values = list(blockers)
    if loaded is None:
        values.append(
            Blocker(
                "integrated-evidence-registration-required",
                "canonical operation commit/evidence refs are absent or invalid",
            )
        )
        return tuple(dict.fromkeys(values))
    expected_iteration_refs = tuple(
        (item.ref_name, loaded.evidence_blob) for item in loaded.iteration_evidence_refs
    )
    comparisons = (
        (loaded.registration_digest, plan.integrated_evidence_digest, "registration-digest"),
        (loaded.metadata.metadata_digest, plan.integrated_evidence_metadata_digest, "metadata-digest"),
        (loaded.evidence_blob, plan.integrated_evidence_blob, "evidence-blob"),
        (loaded.commit_ref, plan.operation_commit_ref, "operation-commit-ref"),
        (loaded.evidence_ref, plan.operation_evidence_ref, "operation-evidence-ref"),
        (loaded.metadata.integrated_commit, plan.integrated_commit, "integrated-commit"),
        (loaded.metadata.integrated_tree, plan.integrated_tree, "integrated-tree"),
        (loaded.metadata.commit_result_digest, plan.integration_commit_result_digest, "commit-result"),
        (expected_iteration_refs, plan.iteration_evidence_refs, "iteration-evidence-refs"),
    )
    for observed, expected, label in comparisons:
        if observed != expected:
            values.append(Blocker("integrated-evidence-plan-binding", label))
    for reference, oid in plan.source_ref_bindings:
        if _resolve_ref(repo, reference) != oid:
            values.append(Blocker("main-advance-source-ref-drift", reference))
    return tuple(dict.fromkeys(values))


def apply_main_advance(
    plan: MainAdvancePlan,
    *,
    accepted_plan_digest: str,
    accepted_integrated_evidence_digest: str,
    confirmation_token: ConfirmationToken,
    failpoint: Failpoint | None = None,
) -> MainAdvanceResult:
    """Atomically publish final acceptance and advance all accepted refs.

    The final-acceptance registry writes a new canonical evidence blob that
    binds the user's exact confirmation.  Its operation/per-iteration refs,
    main, and every ``integrated``/``final`` ref share one CAS transaction.
    No remote is contacted.
    """

    blockers = list(_advance_plan_gate(plan))
    if accepted_plan_digest != plan.plan_digest:
        blockers.append(Blocker("main-advance-plan-not-accepted", "accepted digest differs from main plan"))
    if accepted_integrated_evidence_digest != plan.integrated_evidence_digest:
        blockers.append(Blocker("final-acceptance-missing-or-stale", "accepted integrated evidence digest differs"))
    blockers.extend(
        confirmation_token_gate(
            confirmation_token,
            action="advance-main",
            subject_digest=plan.plan_digest,
        )
    )
    if blockers:
        raise TrainError("main advance blocked: " + "; ".join(item.code for item in blockers))
    repo = open_repository(plan.project_root)
    if os.path.normcase(str(repo.common_dir)) != os.path.normcase(plan.git_common_dir):
        raise TrainError("Git common directory changed after main advance plan")
    evidence_blockers = _public_integrated_evidence_blockers(repo, plan)
    if evidence_blockers:
        raise TrainError(
            "main advance public evidence changed: "
            + "; ".join(item.code for item in evidence_blockers)
        )
    integrated_registry = _integrated_evidence_registry_module()
    try:
        registered_integrated, integrated_load_blockers = (
            integrated_registry.load_registered_integrated_evidence(
                repo.root,
                operation_id=plan.operation_id,
            )
        )
    except integrated_registry.IntegratedEvidenceError as exc:
        raise TrainError(f"main advance cannot load integrated evidence: {exc}") from exc
    if registered_integrated is None or integrated_load_blockers:
        raise TrainError(
            "main advance canonical integrated evidence is unavailable: "
            + "; ".join(item.code for item in integrated_load_blockers)
        )
    final_registry = _final_acceptance_registry_module()
    try:
        final_plan = final_registry.plan_final_acceptance(
            repo.root,
            main_plan=plan,
            integrated=registered_integrated,
            confirmation=confirmation_token,
        )
    except final_registry.FinalAcceptanceError as exc:
        raise TrainError(f"main advance final acceptance plan failed: {exc}") from exc
    if final_plan.blockers:
        raise TrainError(
            "main advance final acceptance is blocked: "
            + "; ".join(item.code for item in final_plan.blockers)
        )
    if _ref_checked_out(repo, plan.main_ref):
        raise TrainError("main ref became checked out after main advance plan")
    current_releases, release_blockers = _local_main_release_gate(repo)
    if release_blockers or current_releases != plan.local_main_release_receipts:
        raise TrainError("Local main-release authority changed after main advance plan")
    journal_path = _journal_path(repo, "advance", plan.operation_id)
    journal = _read_json(journal_path, repo)
    if journal is not None:
        if (
            journal.get("schema_version") != JOURNAL_SCHEMA
            or journal.get("kind") != "main-advance"
            or journal.get("operation_id") != plan.operation_id
        ):
            raise TrainError("main advance journal identity is invalid")
        journal_status = journal.get("status")
        if journal_status not in {"planned", "complete"}:
            raise TrainError("main advance journal status is invalid")
        if journal_status == "complete" and (
            journal.get("plan_digest") != plan.plan_digest
            or journal.get("final_acceptance_plan_digest") != final_plan.plan_digest
        ):
            raise TrainError("main advance complete journal identity is invalid")
    try:
        existing_final, final_load_blockers = final_registry.load_registered_final_acceptance(
            repo.root,
            operation_id=plan.operation_id,
        )
    except final_registry.FinalAcceptanceError as exc:
        raise TrainError(f"main advance cannot load final acceptance: {exc}") from exc
    exact_final = (
        existing_final is not None
        and not final_load_blockers
        and existing_final.metadata.metadata_digest == final_plan.metadata.metadata_digest
        and existing_final.evidence_blob == final_plan.metadata_blob
        and _all_ref_updates_applied(repo, plan)
    )
    if exact_final and existing_final is not None:
        if journal is None or journal.get("status") != "complete":
            journal = {
                "schema_version": JOURNAL_SCHEMA,
                "kind": "main-advance",
                "operation_id": plan.operation_id,
                "plan_digest": plan.plan_digest,
                "accepted_plan_digest": accepted_plan_digest,
                "accepted_integrated_evidence_digest": accepted_integrated_evidence_digest,
                "confirmation_id": confirmation_token.authorization_id,
                "confirmation_token_digest": confirmation_token.token_digest,
                "expected_main": plan.expected_main,
                "integrated_commit": plan.integrated_commit,
                "integrated_evidence_digest": plan.integrated_evidence_digest,
                "integrated_evidence_metadata_digest": plan.integrated_evidence_metadata_digest,
                "integrated_evidence_blob": plan.integrated_evidence_blob,
                "operation_commit_ref": plan.operation_commit_ref,
                "operation_evidence_ref": plan.operation_evidence_ref,
                "iteration_evidence_refs": [list(item) for item in plan.iteration_evidence_refs],
                "source_ref_bindings": [list(item) for item in plan.source_ref_bindings],
                "project_root": plan.project_root,
                "integration_worktree": plan.integration_worktree,
                "ref_updates": [list(item) for item in plan.ref_updates],
                "final_acceptance_plan_digest": final_plan.plan_digest,
                "final_acceptance_digest": existing_final.registration_digest,
                "final_acceptance_evidence_blob": existing_final.evidence_blob,
                "final_acceptance_evidence_ref": existing_final.evidence_ref,
                "final_acceptance_iteration_evidence_refs": [
                    item.ref_name for item in existing_final.iteration_evidence_refs
                ],
                "status": "complete",
                "pushed": False,
            }
            _replace_json(journal_path, journal, repo)
        lease_path = _lease_path(repo)
        lease = _read_json(lease_path, repo)
        if lease is not None and lease.get("operation_id") == plan.operation_id:
            lease_path.unlink(missing_ok=True)
        final_refs = (
            existing_final.evidence_ref,
            *(item.ref_name for item in existing_final.iteration_evidence_refs),
        )
        return MainAdvanceResult(
            schema_version=ADVANCE_RESULT_SCHEMA,
            operation_id=plan.operation_id,
            project_root=plan.project_root,
            integration_worktree=plan.integration_worktree,
            main_ref=plan.main_ref,
            previous_main=plan.expected_main,
            current_main=plan.integrated_commit,
            updated_refs=tuple(item[0] for item in plan.ref_updates) + final_refs,
            journal_path=str(journal_path),
            cleanup_worktree="pending-explicit-cleanup",
            idempotent=True,
            final_acceptance_digest=existing_final.registration_digest,
            final_acceptance_evidence_blob=existing_final.evidence_blob,
            final_acceptance_evidence_ref=existing_final.evidence_ref,
            final_acceptance_iteration_evidence_refs=tuple(
                item.ref_name for item in existing_final.iteration_evidence_refs
            ),
        )
    if journal is not None and journal.get("status") == "complete":
        raise TrainError("main advance complete journal has no exact public final authority")
    if _resolve_ref(repo, plan.main_ref) != plan.expected_main:
        raise TrainError("main drifted after final acceptance")
    for reference, commit in plan.candidate_refs:
        if _resolve_ref(repo, reference) != commit:
            raise TrainError(f"candidate ref drifted after final acceptance: {reference}")
    _, principle_raw = _blob_at(repo, plan.expected_main, plan.principle_path)
    if hashlib.sha256(principle_raw).hexdigest() != plan.principle_sha256:
        raise TrainError("principle drifted after final acceptance")
    for reference, old, _new in plan.ref_updates:
        if _resolve_ref(repo, reference) != old:
            raise TrainError(f"expected ref changed after final acceptance: {reference}")
    lease = _read_json(_lease_path(repo), repo)
    if (
        lease is None
        or lease.get("schema_version") != LEASE_SCHEMA
        or lease.get("operation_id") != plan.operation_id
        or lease.get("expected_main") != plan.expected_main
    ):
        raise TrainError("matching main integration lease is absent")
    # The final-acceptance registry owns the only pre-CAS recovery journal and
    # its per-operation lock.  Keep this wrapper journal in memory until the
    # public single-CAS result exists; otherwise checkout/lease drift between
    # the wrapper preflight and the registry gate could leave a stale planned
    # journal that blocks a later canonical plan for the same operation.
    if journal is None or journal.get("status") == "planned":
        journal = {
            "schema_version": JOURNAL_SCHEMA,
            "kind": "main-advance",
            "operation_id": plan.operation_id,
            "plan_digest": plan.plan_digest,
            "accepted_plan_digest": accepted_plan_digest,
            "accepted_integrated_evidence_digest": accepted_integrated_evidence_digest,
            "confirmation_id": confirmation_token.authorization_id,
            "confirmation_token_digest": confirmation_token.token_digest,
            "expected_main": plan.expected_main,
            "integrated_commit": plan.integrated_commit,
            "integrated_evidence_digest": plan.integrated_evidence_digest,
            "integrated_evidence_metadata_digest": plan.integrated_evidence_metadata_digest,
            "integrated_evidence_blob": plan.integrated_evidence_blob,
            "operation_commit_ref": plan.operation_commit_ref,
            "operation_evidence_ref": plan.operation_evidence_ref,
            "iteration_evidence_refs": [list(item) for item in plan.iteration_evidence_refs],
            "source_ref_bindings": [list(item) for item in plan.source_ref_bindings],
            "project_root": plan.project_root,
            "integration_worktree": plan.integration_worktree,
            "ref_updates": [list(item) for item in plan.ref_updates],
            "final_acceptance_plan_digest": final_plan.plan_digest,
            "status": "planned",
            "pushed": False,
        }
    # Preserve the historical failpoint name while keeping the wrapper plan
    # in memory.  The authoritative final registry owns durable pre-CAS
    # recovery; a crash here must leave no wrapper journal to poison retry.
    _trigger(failpoint, "main-advance-after-journal")
    try:
        final_receipt = final_registry.apply_final_acceptance(
            final_plan,
            accepted_plan_digest=final_plan.plan_digest,
            confirmation=confirmation_token,
            failpoint=(
                None
                if failpoint is None
                else lambda stage: _trigger(failpoint, stage)
            ),
        )
    except final_registry.FinalAcceptanceError as exc:
        raise TrainError(f"main advance final acceptance failed: {exc}") from exc
    _trigger(failpoint, "main-advance-after-refs")
    journal["final_acceptance_digest"] = final_receipt.registration_digest
    journal["final_acceptance_evidence_blob"] = final_receipt.evidence_blob
    journal["final_acceptance_evidence_ref"] = final_receipt.evidence_ref
    journal["final_acceptance_iteration_evidence_refs"] = [
        item.ref_name for item in final_receipt.iteration_evidence_refs
    ]
    journal["status"] = "complete"
    _replace_json(journal_path, journal, repo)
    lease_path = _lease_path(repo)
    lease_path.unlink(missing_ok=True)
    return MainAdvanceResult(
        schema_version=ADVANCE_RESULT_SCHEMA,
        operation_id=plan.operation_id,
        project_root=plan.project_root,
        integration_worktree=plan.integration_worktree,
        main_ref=plan.main_ref,
        previous_main=plan.expected_main,
        current_main=plan.integrated_commit,
        updated_refs=tuple(item[0] for item in plan.ref_updates)
        + (final_receipt.evidence_ref,)
        + tuple(item.ref_name for item in final_receipt.iteration_evidence_refs),
        journal_path=str(journal_path),
        cleanup_worktree="pending-explicit-cleanup",
        idempotent=False,
        final_acceptance_digest=final_receipt.registration_digest,
        final_acceptance_evidence_blob=final_receipt.evidence_blob,
        final_acceptance_evidence_ref=final_receipt.evidence_ref,
        final_acceptance_iteration_evidence_refs=tuple(
            item.ref_name for item in final_receipt.iteration_evidence_refs
        ),
    )


def _cleanup_plan_payload(plan: IntegrationCleanupPlan) -> dict[str, object]:
    data = plan.as_dict()
    data.pop("plan_digest", None)
    return data


def integration_cleanup_plan_digest(plan: IntegrationCleanupPlan) -> str:
    return digest(_cleanup_plan_payload(plan))


def _cleanup_interaction(
    plan: IntegrationCleanupPlan,
    phase: Literal["before", "after"],
) -> InteractionEnvelope:
    return interaction(
        ActionFacts(
            action="remove-clean-worktree",
            phase=phase,
            iteration="integration",
            operation_id=plan.operation_id,
            project_root=plan.project_root,
            base_commit=plan.integrated_commit,
            branch_ref="DETACHED",
            worktree_path=plan.integration_worktree,
            source_ref="refs/heads/main",
            affected_prds=plan.affected_prds,
            runtime_namespace="main-integration-lane",
            effect_on_existing_prds=(
                "only the exact clean temporary integration worktree is removed",
                "feature PRD worktrees and main ref remain unchanged",
            ),
            remote_involved=False,
            source_preserved=True,
            actual_head=plan.integrated_commit,
            force=False,
            pushed=False,
            reason="integrated candidate reached main; remove exact clean temporary worktree",
            next_gate="iteration-close-or-next-candidate" if phase == "after" else "remove-exact-clean-worktree",
        )
    )


def plan_cleanup_integration(result: MainAdvanceResult) -> IntegrationCleanupPlan:
    """Plan removal of the exact clean integration worktree after main advance."""

    if not isinstance(result, MainAdvanceResult) or result.schema_version != ADVANCE_RESULT_SCHEMA:
        raise TrainError("main advance result is invalid for integration cleanup")
    repo = open_repository(result.project_root)
    journal_path = Path(result.journal_path).resolve()
    _assert_train_operational_path(repo, journal_path)
    journal = _read_json(journal_path, repo)
    if journal is None:
        raise TrainError("main advance journal is missing")
    project_root = journal.get("project_root")
    integration_worktree = journal.get("integration_worktree")
    if not isinstance(project_root, str) or not isinstance(integration_worktree, str):
        raise TrainError("main advance journal predates exact cleanup identity")
    if os.path.normcase(project_root) != os.path.normcase(result.project_root):
        raise TrainError("main advance result and journal project roots differ")
    if os.path.normcase(integration_worktree) != os.path.normcase(result.integration_worktree):
        raise TrainError("main advance result and journal worktree paths differ")
    raw_path = Path(integration_worktree)
    path_link_error: str | None = None
    try:
        workspace.assert_existing_chain_has_no_links(raw_path)
    except workspace.WorkspaceError as exc:
        path_link_error = str(exc)
    path = raw_path.resolve()
    blockers: list[Blocker] = []
    if journal.get("status") != "complete" or journal.get("integrated_commit") != result.current_main:
        blockers.append(Blocker("cleanup-main-advance-incomplete", "main advance journal is not complete"))
    if not _worktree_registered(repo, path):
        blockers.append(Blocker("cleanup-worktree-not-registered", "integration worktree is not registered"))
    else:
        if _worktree_head(repo, path) != result.current_main:
            blockers.append(Blocker("cleanup-head-drift", "integration worktree HEAD differs from main result"))
        runtime_paths = _cleanup_dirty_paths(repo, path)
        if runtime_paths:
            blockers.append(
                Blocker(
                    "cleanup-unowned-assets",
                    "integration worktree contains tracked, untracked, or ignored assets: "
                    + ", ".join(runtime_paths[:20]),
                )
            )
        markers = _git_operation_markers(repo, path)
        if markers:
            blockers.append(
                Blocker(
                    "cleanup-git-operation-active",
                    "integration worktree has active Git operation markers: " + ", ".join(markers),
                )
            )
    claims = _active_workspace_claims(repo, path)
    if claims:
        blockers.append(
            Blocker(
                "cleanup-active-writer-runtime-claim",
                "integration path overlaps active writer/runtime claims: " + ", ".join(claims),
            )
        )
    if _read_json(_lease_path(repo), repo) is not None:
        blockers.append(Blocker("cleanup-integration-lease-active", "main integration lease is still active"))
    if path_link_error is not None:
        blockers.append(Blocker("cleanup-path-link", path_link_error))
    raw_journal = journal_path.read_bytes()
    affected_prds: list[str] = []
    for item in journal.get("ref_updates", []):
        if not isinstance(item, list) or not item or not isinstance(item[0], str):
            continue
        match = re.fullmatch(
            r"refs/project-harness/v2/iterations/([0-9]{3,})/(?:integrated|final)",
            item[0],
        )
        if match and match.group(1) not in affected_prds:
            affected_prds.append(match.group(1))
    if not affected_prds:
        blockers.append(Blocker("cleanup-prd-scope-missing", "main advance journal has no affected PRD refs"))
    provisional = IntegrationCleanupPlan(
        schema_version=CLEANUP_PLAN_SCHEMA,
        operation_id=result.operation_id,
        project_root=str(repo.root),
        git_common_dir=str(repo.common_dir),
        integration_worktree=str(path),
        integrated_commit=result.current_main,
        affected_prds=tuple(affected_prds),
        main_advance_journal=str(journal_path),
        main_advance_journal_sha256=hashlib.sha256(raw_journal).hexdigest(),
        plan_digest="0" * 64,
        blockers=tuple(blockers),
    )
    return replace(provisional, plan_digest=integration_cleanup_plan_digest(provisional))


def apply_cleanup_integration(
    plan: IntegrationCleanupPlan,
    *,
    accepted_plan_digest: str,
    notify: Notify,
    failpoint: Failpoint | None = None,
) -> IntegrationCleanupResult:
    """Remove only the exact clean temporary worktree; never use force."""

    blockers = list(plan.blockers)
    if plan.schema_version != CLEANUP_PLAN_SCHEMA:
        blockers.append(Blocker("cleanup-plan-schema", "cleanup plan schema is unsupported"))
    if plan.plan_digest != integration_cleanup_plan_digest(plan):
        blockers.append(Blocker("cleanup-plan-digest", "cleanup plan was changed"))
    if accepted_plan_digest != plan.plan_digest:
        blockers.append(Blocker("cleanup-plan-not-accepted", "accepted digest differs from cleanup plan"))
    if not callable(notify):
        blockers.append(Blocker("cleanup-notifier-missing", "cleanup before/after notifier is required"))
    if blockers:
        raise TrainError("integration cleanup blocked: " + "; ".join(item.code for item in blockers))
    repo = open_repository(plan.project_root)
    raw_path = Path(plan.integration_worktree)
    try:
        workspace.assert_existing_chain_has_no_links(raw_path)
    except workspace.WorkspaceError as exc:
        raise TrainError(f"cleanup worktree path became a link or junction: {exc}") from exc
    path = raw_path.resolve()
    advance_path = Path(plan.main_advance_journal).resolve()
    _assert_train_operational_path(repo, advance_path)
    if hashlib.sha256(advance_path.read_bytes()).hexdigest() != plan.main_advance_journal_sha256:
        raise TrainError("main advance journal changed after cleanup plan")
    cleanup_journal_path = _journal_path(repo, "cleanup", plan.operation_id)
    existing = _read_json(cleanup_journal_path, repo)
    if existing is not None:
        if (
            existing.get("kind") != "integration-cleanup"
            or existing.get("plan_digest") != plan.plan_digest
            or existing.get("worktree_path") != plan.integration_worktree
        ):
            raise TrainError("cleanup journal identity is invalid")
        status = existing.get("status")
        absent = not _worktree_registered(repo, path) and not path.exists()
        if status in {"removing", "removed", "complete"} and absent:
            notifications: list[InteractionEnvelope] = []
            if status != "complete":
                existing["status"] = "removed"
                existing["removed_observed"] = True
                _replace_json(cleanup_journal_path, existing, repo)
                _trigger(failpoint, "cleanup-after-removed-observed")
                existing["status"] = "complete"
                _replace_json(cleanup_journal_path, existing, repo)
            if not existing.get("after_notified"):
                after = _cleanup_interaction(plan, "after")
                notify(after)
                notifications.append(after)
                existing["after_notified"] = True
                _replace_json(cleanup_journal_path, existing, repo)
            return IntegrationCleanupResult(
                CLEANUP_RESULT_SCHEMA,
                plan.operation_id,
                plan.integration_worktree,
                True,
                str(cleanup_journal_path),
                tuple(notifications),
                True,
            )
        if status == "complete":
            raise TrainError("completed cleanup journal no longer matches worktree state")
    else:
        existing = {
            "schema_version": JOURNAL_SCHEMA,
            "kind": "integration-cleanup",
            "operation_id": plan.operation_id,
            "plan_digest": plan.plan_digest,
            "worktree_path": plan.integration_worktree,
            "integrated_commit": plan.integrated_commit,
            "status": "planned",
            "before_notified": False,
            "after_notified": False,
            "pushed": False,
        }
        _write_new_json(cleanup_journal_path, existing, repo)
    notifications: list[InteractionEnvelope] = []
    if not existing.get("before_notified"):
        before = _cleanup_interaction(plan, "before")
        notify(before)
        notifications.append(before)
        existing["before_notified"] = True
        _replace_json(cleanup_journal_path, existing, repo)
    _trigger(failpoint, "cleanup-after-before-notify")
    context = _workspace_context(repo)
    with workspace.coordinator_lock(context):
        try:
            workspace.assert_existing_chain_has_no_links(raw_path)
        except workspace.WorkspaceError as exc:
            raise TrainError(f"cleanup worktree path became a link or junction: {exc}") from exc
        if not _worktree_registered(repo, path):
            raise TrainError("cleanup worktree registration changed after notification")
        if _worktree_head(repo, path) != plan.integrated_commit:
            raise TrainError("cleanup worktree HEAD changed after notification")
        runtime_paths = _cleanup_dirty_paths(repo, path)
        if runtime_paths:
            raise TrainError("cleanup worktree gained tracked, untracked, or ignored assets")
        markers = _git_operation_markers(repo, path)
        if markers:
            raise TrainError("cleanup worktree gained an active Git operation")
        claims = _active_workspace_claims(repo, path)
        if claims:
            raise TrainError("cleanup worktree gained an active writer/runtime claim")
        if _read_json(_lease_path(repo), repo) is not None:
            raise TrainError("main integration lease became active before cleanup")
        existing["status"] = "removing"
        _replace_json(cleanup_journal_path, existing, repo)
        _trigger(failpoint, "cleanup-before-remove")
        removed = _git_without_hooks(repo, ["worktree", "remove", str(path)], check=False)
        if removed.returncode != 0:
            detail = removed.stderr.decode("utf-8", errors="replace").strip()
            existing["status"] = "failed-needs-reconcile"
            existing["failure"] = "cleanup-worktree-remove-failed"
            _replace_json(cleanup_journal_path, existing, repo)
            raise TrainError(f"non-force integration worktree removal failed: {detail}")
        _trigger(failpoint, "cleanup-after-remove")
    if _worktree_registered(repo, path) or path.exists():
        existing["status"] = "failed-needs-reconcile"
        existing["failure"] = "cleanup-postcheck-failed"
        _replace_json(cleanup_journal_path, existing, repo)
        raise TrainError("integration worktree removal postcheck failed")
    existing["status"] = "removed"
    existing["removed_observed"] = True
    _replace_json(cleanup_journal_path, existing, repo)
    _trigger(failpoint, "cleanup-after-removed-observed")
    existing["status"] = "complete"
    _replace_json(cleanup_journal_path, existing, repo)
    _trigger(failpoint, "cleanup-after-journal-complete")
    if not existing.get("after_notified"):
        after = _cleanup_interaction(plan, "after")
        notify(after)
        notifications.append(after)
        existing["after_notified"] = True
        _replace_json(cleanup_journal_path, existing, repo)
    return IntegrationCleanupResult(
        CLEANUP_RESULT_SCHEMA,
        plan.operation_id,
        plan.integration_worktree,
        True,
        str(cleanup_journal_path),
        tuple(notifications),
        False,
    )


__all__ = [
    "ADVANCE_PLAN_SCHEMA",
    "AUTHORITY_SCHEMA",
    "AuthorityValidationContext",
    "Blocker",
    "CandidateRegistrationPlan",
    "CandidateSealPlan",
    "CandidateVerificationReceipt",
    "IntegrationCleanupPlan",
    "IntegrationCleanupResult",
    "ConfirmationToken",
    "GovernanceContext",
    "GovernanceReceipt",
    "InjectedCrash",
    "IntegrationCommitPlan",
    "IntegrationCommitResult",
    "IntegrationPreparationResult",
    "IntegrationPreparePlan",
    "MainAdvancePlan",
    "MainAdvanceResult",
    "Notification",
    "PrincipleGateBinding",
    "RegisteredCandidate",
    "TrainError",
    "VerificationReceipt",
    "VerifyCommand",
    "apply_integration_commit",
    "apply_cleanup_integration",
    "apply_main_advance",
    "apply_prepare_integration",
    "apply_register_candidate",
    "authority_evidence_digest",
    "authority_evidence_gate",
    "authority_validation_context",
    "build_governance_receipt",
    "candidate_registration_plan_digest",
    "candidate_seal_plan_digest",
    "candidate_verification_receipt_digest",
    "candidate_verification_receipt_gate",
    "confirmation_token_digest",
    "confirmation_token_gate",
    "finalize_integration_evidence",
    "governance_receipt_digest",
    "governance_receipt_gate",
    "integration_commit_interaction",
    "integration_commit_plan_digest",
    "integration_cleanup_plan_digest",
    "integration_prepare_plan_digest",
    "main_advance_interaction",
    "main_advance_plan_digest",
    "load_registered_candidate",
    "new_operation_id",
    "open_repository",
    "plan_main_advance",
    "plan_cleanup_integration",
    "plan_prepare_integration",
    "plan_register_candidate",
    "prepare_candidate_registration",
    "principle_gate_binding_digest",
    "registered_candidate_gate",
    "registered_candidate_digest",
]
