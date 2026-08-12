#!/usr/bin/env python3
"""Exact governance adapter for :mod:`harness_train` integration worktrees.

Git is deliberately not allowed to arbitrate Harness' shared governance files.
This module binds one ``IntegrationPreparePlan`` to its registered candidates,
normalizes only supported governance paths back to the exact latest-main tree,
then replays every candidate through ``harness_reconcile``.  README output is
rebuilt from an explicit authority object; branch README bytes are never chosen
by merge order.

The current train invokes its governance callback only after ``git merge`` has
succeeded.  ``plan_premerge_normalization`` and
``apply_premerge_normalization`` are therefore public so the train can also
invoke the same bounded operation when Git reports conflicts exclusively in
governance paths.  They never resolve an implementation conflict.
"""

from __future__ import annotations

import base64
import contextlib
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable, Literal, Mapping, Sequence

import harness_governance
import harness_progress
import harness_reconcile
import harness_train
import project_harness


ADAPTER_SCHEMA = "harness-lite.train-governance-adapter/v2"
NORMALIZATION_SCHEMA = "harness-lite.train-governance-normalization/v2"
NORMALIZATION_JOURNAL_SCHEMA = "harness-lite.train-governance-normalization-journal/v2"
README_AUTHORITY_SCHEMA = "harness-lite.train-governance-readme-authority/v1"
EXECUTION_JOURNAL_SCHEMA = "harness-lite.train-governance-execution-journal/v2"
RESUME_STATE_SCHEMA = "harness-lite.train-governance-resume-state/v2"
CANDIDATE_AUTHORITY_SCHEMA = "harness-lite.train-governance-candidate-authority/v2"
TRAIN_PROGRESS_SPEC_SCHEMA = "harness-lite.train-progress-event-spec/v1"
PROGRESS_EVIDENCE_RESOLUTION_SCHEMA = "harness-lite.progress-evidence-resolution/v1"
PROGRESS_MATERIALIZATION_SCHEMA = "harness-lite.train-progress-materialization/v1"

PRINCIPLE_PATH = harness_reconcile.PRINCIPLE_PATH
PROGRESS_PATH = harness_reconcile.PROGRESS_PATH
L0_PATH = harness_reconcile.L0_PATH
L1_PATH_RE = harness_reconcile.L1_PATH_RE
REQUIRED_PATHS = harness_reconcile.REQUIRED_PATHS

OID_RE = re.compile(r"[0-9a-f]{40,64}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
MODE_RE = re.compile(r"100(?:644|755)")
MAX_JSON_BYTES = 128 * 1024 * 1024
REGISTRY_PARTS = ("project-harness", "train-governance", "v1")


class GovernanceAdapterError(RuntimeError):
    """The adapter could not prove that a governance mutation is exact."""


class InjectedGovernanceCrash(BaseException):
    """Fault-injection signal used to exercise durable callback recovery."""


@dataclass(frozen=True)
class AdapterBlocker:
    code: str
    message: str
    subject: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DerivedReadme:
    """One exact, authority-produced L1 document."""

    path: str
    content: bytes
    authority_ref: str


@dataclass(frozen=True)
class ReadmeRebuildAuthority:
    """Inputs required to rebuild L0 and candidate-owned L1 documents.

    ``root`` is consumed by ``harness_governance.preview_root_readme``.  L1 has
    no structured renderer in the current core, so callers provide its exact
    authority-produced bytes and an evidence reference.  The adapter binds the
    bytes into ``authority_digest`` and applies them through
    ``harness_reconcile`` rather than accepting a branch-side text merge.
    """

    schema_version: str
    authority_id: str
    root: harness_governance.RootRoutingAuthority | None
    l1_documents: tuple[DerivedReadme, ...]
    authority_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority_id": self.authority_id,
            "root": asdict(self.root) if self.root is not None else None,
            "l1_documents": [
                {
                    "path": item.path,
                    "content_sha256": _sha256(item.content),
                    "size": len(item.content),
                    "authority_ref": item.authority_ref,
                }
                for item in self.l1_documents
            ],
            "authority_digest": self.authority_digest,
        }


@dataclass(frozen=True)
class IndexStage:
    mode: str
    object_id: str
    stage: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizationEntry:
    path: str
    before_stages: tuple[IndexStage, ...]
    before_worktree_exists: bool
    before_worktree_sha256: str | None
    after_exists: bool
    after_mode: str | None
    after_blob: str | None
    after_sha256: str | None
    after_content: bytes | None

    def manifest_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "before_stages": [item.as_dict() for item in self.before_stages],
            "before_worktree_exists": self.before_worktree_exists,
            "before_worktree_sha256": self.before_worktree_sha256,
            "after_exists": self.after_exists,
            "after_mode": self.after_mode,
            "after_blob": self.after_blob,
            "after_sha256": self.after_sha256,
            "after_content_base64": (
                base64.b64encode(self.after_content).decode("ascii")
                if self.after_content is not None
                else None
            ),
        }


@dataclass(frozen=True)
class CandidateAuthorityBinding:
    """Public, recomputable authority consumed by governance reconciliation.

    The adapter deliberately does not copy the candidate operation journal's
    private schema.  It calls the train's public candidate gate, reloads the
    registered generation from its stable candidate/evidence refs, and binds
    the resulting seal plus both verification phases here.  Future public
    principle-gate material is included through ``principle_gate_binding_digest``
    without this adapter claiming to validate that gate itself.
    """

    schema_version: str
    iteration: str
    generation: str
    principle_sha256: str
    candidate_ref: str
    candidate_commit: str
    candidate_tree: str
    candidate_evidence_ref: str
    candidate_evidence_blob: str
    candidate_evidence_metadata_digest: str
    candidate_evidence_digest: str
    pre_seal_verification_receipt_digests: tuple[str, ...]
    seal_verification_receipt_digests: tuple[str, ...]
    verification_binding_digest: str
    principle_gate_binding_digest: str | None
    candidate_progress_event_id: str
    candidate_progress_event_sha256: str
    authority_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PreMergeNormalizationPlan:
    schema_version: str
    phase: Literal["post-merge", "merge-conflict"]
    operation_id: str
    project_root: str
    git_common_dir: str
    integration_worktree: str
    train_plan_digest: str
    main_ref: str
    target_main: str
    expected_head: str
    expected_input_tree: str | None
    candidate_authorities: tuple[CandidateAuthorityBinding, ...]
    entries: tuple[NormalizationEntry, ...]
    unmerged_paths: tuple[str, ...]
    plan_digest: str
    blockers: tuple[AdapterBlocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def requires_train_conflict_hook(self) -> bool:
        """Whether current train would stop before reaching the callback."""

        return self.phase == "merge-conflict" and bool(self.unmerged_paths)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "phase": self.phase,
            "operation_id": self.operation_id,
            "project_root": self.project_root,
            "git_common_dir": self.git_common_dir,
            "integration_worktree": self.integration_worktree,
            "train_plan_digest": self.train_plan_digest,
            "main_ref": self.main_ref,
            "target_main": self.target_main,
            "expected_head": self.expected_head,
            "expected_input_tree": self.expected_input_tree,
            "candidate_authorities": [item.as_dict() for item in self.candidate_authorities],
            "entries": [item.manifest_dict() for item in self.entries],
            "unmerged_paths": list(self.unmerged_paths),
            "requires_train_conflict_hook": self.requires_train_conflict_hook,
            "plan_digest": self.plan_digest,
            "blocking_reasons": [item.as_dict() for item in self.blockers],
            "exclusions": [
                "no implementation conflict resolution",
                "no commit",
                "no push",
                "no stash",
                "no reset",
                "no clean",
                "no force",
            ],
            "pushed": False,
        }


@dataclass(frozen=True)
class NormalizationResult:
    schema_version: str
    operation_id: str
    phase: str
    plan_digest: str
    result_tree: str
    journal_path: str
    changed_paths: tuple[str, ...]
    resumed: bool
    journal_sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GovernanceExecutionPreview:
    schema_version: str
    operation_id: str
    normalization: PreMergeNormalizationPlan
    reconciliations: tuple[harness_reconcile.GovernanceReconcilePlan, ...]
    reconciliation_labels: tuple[str, ...]
    reconciliation_snapshots: tuple[harness_reconcile.GovernanceSnapshot, ...]
    progress_events: tuple["TrainProgressEventSpec", ...]
    final_snapshot: harness_reconcile.GovernanceSnapshot
    blockers: tuple[AdapterBlocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers and self.normalization.ready


@dataclass(frozen=True)
class GovernanceResumeState:
    schema_version: str
    operation_id: str
    original_input_tree: str
    actual_index_tree: str
    allowed_intermediate_trees: tuple[str, ...]
    completed_steps: tuple[str, ...]
    next_step: str
    resumable: bool
    blockers: tuple[AdapterBlocker, ...]
    journal_path: str

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "blockers": [item.as_dict() for item in self.blockers],
        }


@dataclass(frozen=True)
class TrainProgressEventSpec:
    """One exact train-owned event pre-bound into the integrated tree.

    ``integration_started`` is an immediately materialized historical fact.
    The other transitions are proposals embedded before the downstream
    commit/evidence identities exist; status consumers must use
    :func:`materialize_train_progress_events` before projecting them.
    """

    schema_version: str
    transition: Literal["integration_started", "integration_verified", "main_advanced"]
    iteration: str
    generation: str
    event: harness_progress.ProgressEventV2
    event_bytes_b64: str
    event_sha256: str
    evidence_ref: str
    conditional: bool
    spec_digest: str

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["event"] = self.event.as_dict()
        return result


@dataclass(frozen=True)
class ProgressEvidenceResolution:
    """Exact public-evidence result returned by a registry resolver."""

    schema_version: str
    ref_name: str
    object_id: str
    evidence_digest: str
    event_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TrainProgressMaterialization:
    schema_version: str
    transition: str
    iteration: str
    event_id: str
    evidence_ref: str
    conditional: bool
    materialized: bool
    blocker: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


Failpoint = Callable[[str], None]


@dataclass(frozen=True)
class _CommitFile:
    mode: str
    object_id: str
    content: bytes


@dataclass(frozen=True)
class _CommitSnapshot:
    commit: str
    files: Mapping[str, _CommitFile]
    semantic: harness_reconcile.GovernanceSnapshot


_ValidatedCandidate = tuple[
    harness_train.RegisteredCandidate,
    _CommitSnapshot,
    _CommitSnapshot,
    CandidateAuthorityBinding,
    harness_progress.ProgressEventV2,
]
_ValidatedCandidates = tuple[_ValidatedCandidate, ...]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    return _sha256(_canonical_json(value))


def _supported_path(path: str) -> bool:
    return path in REQUIRED_PATHS or L1_PATH_RE.fullmatch(path) is not None


def _canonical_path(value: str) -> str:
    path = value.replace("\\", "/")
    if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        raise GovernanceAdapterError(f"unsafe governance path: {value!r}")
    if not _supported_path(path):
        raise GovernanceAdapterError(f"unsupported governance path: {path}")
    return path


def _git_environment(repo: harness_train.Repository) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PATH"] = repo.git_exec_path + os.pathsep + environment.get("PATH", "")
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(key, None)
    return environment


def _git(
    repo: harness_train.Repository,
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [repo.git, "-C", str(cwd or repo.root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(repo),
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GovernanceAdapterError(f"git {' '.join(arguments[:2])} failed: {detail}")
    return result


def _git_text(repo: harness_train.Repository, arguments: Sequence[str], *, cwd: Path | None = None) -> str:
    return _git(repo, arguments, cwd=cwd).stdout.decode("utf-8", errors="strict").strip()


def _resolve_ref(repo: harness_train.Repository, reference: str) -> str | None:
    result = _git(repo, ["rev-parse", "--verify", reference], check=False)
    value = result.stdout.decode("ascii", errors="replace").strip()
    return value if result.returncode == 0 and OID_RE.fullmatch(value) else None


def _commit_tree(repo: harness_train.Repository, commit: str) -> str:
    value = _git_text(repo, ["rev-parse", "--verify", f"{commit}^{{tree}}"])
    if OID_RE.fullmatch(value) is None:
        raise GovernanceAdapterError(f"commit tree is invalid: {commit}")
    return value


def _write_tree(repo: harness_train.Repository, worktree: Path) -> str:
    result = _git(repo, ["write-tree"], cwd=worktree, check=False)
    value = result.stdout.decode("ascii", errors="replace").strip()
    if result.returncode != 0 or OID_RE.fullmatch(value) is None:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GovernanceAdapterError(f"integration index cannot form an exact tree: {detail}")
    return value


def _null_paths(raw: bytes) -> tuple[str, ...]:
    values: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        try:
            value = item.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GovernanceAdapterError("Git returned a non-UTF-8 repository path") from exc
        if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise GovernanceAdapterError(f"Git returned an unsafe repository path: {value!r}")
        values.append(value)
    return tuple(values)


def _unmerged_paths(repo: harness_train.Repository, worktree: Path) -> tuple[str, ...]:
    return _null_paths(
        _git(repo, ["diff", "--name-only", "--diff-filter=U", "-z"], cwd=worktree).stdout
    )


def _untracked_and_ignored(repo: harness_train.Repository, worktree: Path) -> tuple[str, ...]:
    untracked = _null_paths(
        _git(repo, ["ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree).stdout
    )
    ignored = _null_paths(
        _git(repo, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], cwd=worktree).stdout
    )
    return tuple(dict.fromkeys((*untracked, *ignored)))


def _index_stages(repo: harness_train.Repository, worktree: Path, path: str) -> tuple[IndexStage, ...]:
    result = _git(repo, ["ls-files", "--stage", "-z", "--", path], cwd=worktree)
    stages: list[IndexStage] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            raw_mode, raw_oid, raw_stage = header.split(b" ", 2)
            observed_path = raw_path.decode("utf-8", errors="strict")
            mode = raw_mode.decode("ascii", errors="strict")
            object_id = raw_oid.decode("ascii", errors="strict")
            stage = int(raw_stage.decode("ascii", errors="strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GovernanceAdapterError(f"cannot parse index identity for {path}") from exc
        if observed_path != path or MODE_RE.fullmatch(mode) is None or OID_RE.fullmatch(object_id) is None or stage not in {0, 1, 2, 3}:
            raise GovernanceAdapterError(f"invalid index identity for {path}")
        stages.append(IndexStage(mode, object_id, stage))
    return tuple(stages)


def _path_state(root: Path, path: str) -> tuple[bool, str | None]:
    target = root / Path(path)
    _assert_safe_target(root, target)
    if not target.exists():
        return False, None
    if not target.is_file():
        raise GovernanceAdapterError(f"governance target is not a regular file: {path}")
    return True, _sha256(target.read_bytes())


def _assert_safe_target(root: Path, target: Path) -> None:
    resolved_root = root.resolve()
    try:
        target.absolute().relative_to(resolved_root)
        target.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise GovernanceAdapterError(f"governance target escapes integration worktree: {target}") from exc
    current = target
    while current != resolved_root:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise GovernanceAdapterError(f"governance target traverses a link or junction: {current}")
        current = current.parent


def _commit_snapshot(
    repo: harness_train.Repository,
    commit: str,
    *,
    source_id: str,
) -> _CommitSnapshot:
    resolved = _resolve_ref(repo, commit)
    if resolved != commit:
        raise GovernanceAdapterError(f"snapshot commit is absent or abbreviated: {commit}")
    try:
        entries = project_harness.read_committed_governance_entries(repo.git, repo.root, commit)
    except project_harness.HarnessError as exc:
        raise GovernanceAdapterError(str(exc)) from exc
    files: dict[str, _CommitFile] = {}
    semantic_files: dict[str, bytes] = {}
    for path, (mode, object_id, content) in entries.items():
        if not _supported_path(path):
            continue
        canonical = _canonical_path(path)
        files[canonical] = _CommitFile(mode, object_id, content)
        semantic_files[canonical] = content
    return _CommitSnapshot(
        commit=commit,
        files=files,
        semantic=harness_reconcile.GovernanceSnapshot.from_files(source_id, semantic_files),
    )


def _snapshot_with_files(
    snapshot: harness_reconcile.GovernanceSnapshot,
    *,
    source_id: str,
    replacements: Mapping[str, bytes | None],
) -> harness_reconcile.GovernanceSnapshot:
    files = snapshot.as_mapping()
    for raw_path, content in replacements.items():
        path = _canonical_path(raw_path)
        if content is None:
            files.pop(path, None)
        else:
            files[path] = content
    return harness_reconcile.GovernanceSnapshot.from_files(source_id, files)


def _semantic_candidate(
    base: harness_reconcile.GovernanceSnapshot,
    candidate: harness_reconcile.GovernanceSnapshot,
    *,
    source_id: str,
) -> harness_reconcile.GovernanceSnapshot:
    """Ignore branch-derived README bytes while retaining principle/progress."""

    base_files = base.as_mapping()
    candidate_files = candidate.as_mapping()
    replacements: dict[str, bytes | None] = {L0_PATH: base_files.get(L0_PATH)}
    l1_paths = {
        path
        for path in (*base_files.keys(), *candidate_files.keys())
        if L1_PATH_RE.fullmatch(path) is not None
    }
    for path in l1_paths:
        replacements[path] = base_files.get(path)
    return _snapshot_with_files(candidate, source_id=source_id, replacements=replacements)


def _apply_previews_to_snapshot(
    snapshot: harness_reconcile.GovernanceSnapshot,
    plan: harness_reconcile.GovernanceReconcilePlan,
    *,
    source_id: str,
) -> harness_reconcile.GovernanceSnapshot:
    replacements = {item.path: item.content for item in plan.previews}
    return _snapshot_with_files(snapshot, source_id=source_id, replacements=replacements)


def _readme_authority_payload(value: ReadmeRebuildAuthority) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "authority_id": value.authority_id,
        "root": asdict(value.root) if value.root is not None else None,
        "l1_documents": [
            {
                "path": item.path,
                "content_base64": base64.b64encode(item.content).decode("ascii"),
                "authority_ref": item.authority_ref,
            }
            for item in value.l1_documents
        ],
    }


def build_readme_rebuild_authority(
    *,
    authority_id: str,
    root: harness_governance.RootRoutingAuthority | None,
    l1_documents: Sequence[DerivedReadme],
) -> ReadmeRebuildAuthority:
    identity = authority_id.strip()
    if not identity or "\n" in identity or "\r" in identity:
        raise GovernanceAdapterError("README authority_id must be one non-empty line")
    normalized: list[DerivedReadme] = []
    seen: set[str] = set()
    for item in l1_documents:
        if not isinstance(item, DerivedReadme):
            raise TypeError("l1_documents entries must be DerivedReadme")
        path = _canonical_path(item.path)
        if L1_PATH_RE.fullmatch(path) is None or path in seen:
            raise GovernanceAdapterError(f"README authority contains an invalid/duplicate L1 path: {path}")
        if not item.authority_ref.strip() or not item.content:
            raise GovernanceAdapterError(f"README authority lacks exact evidence/content: {path}")
        normalized.append(DerivedReadme(path, bytes(item.content), item.authority_ref.strip()))
        seen.add(path)
    if root is not None:
        if not isinstance(root, harness_governance.RootRoutingAuthority):
            raise TypeError("root must be RootRoutingAuthority")
        if root.authority_id != identity:
            raise GovernanceAdapterError("root README authority identity differs from adapter authority")
    provisional = ReadmeRebuildAuthority(
        README_AUTHORITY_SCHEMA,
        identity,
        root,
        tuple(sorted(normalized, key=lambda item: item.path)),
        "0" * 64,
    )
    return replace(provisional, authority_digest=_digest(_readme_authority_payload(provisional)))


def _validate_readme_authority(value: ReadmeRebuildAuthority | None) -> None:
    if value is None:
        return
    if value.schema_version != README_AUTHORITY_SCHEMA:
        raise GovernanceAdapterError("README rebuild authority schema is unsupported")
    if value.authority_digest != _digest(_readme_authority_payload(value)):
        raise GovernanceAdapterError("README rebuild authority digest changed")


def _validate_train_plan(plan: harness_train.IntegrationPreparePlan) -> harness_train.Repository:
    if not isinstance(plan, harness_train.IntegrationPreparePlan):
        raise TypeError("plan must be IntegrationPreparePlan")
    if plan.schema_version != harness_train.PREPARE_PLAN_SCHEMA:
        raise GovernanceAdapterError("integration prepare plan schema is unsupported")
    if plan.plan_digest != harness_train.integration_prepare_plan_digest(plan):
        raise GovernanceAdapterError("integration prepare plan digest changed")
    if plan.blockers:
        raise GovernanceAdapterError("blocked integration plan cannot construct governance adapter")
    repo = harness_train.open_repository(plan.project_root)
    if os.path.normcase(str(repo.common_dir)) != os.path.normcase(str(Path(plan.git_common_dir).resolve())):
        raise GovernanceAdapterError("integration plan Git common directory changed")
    if _resolve_ref(repo, plan.main_ref) != plan.target_main:
        raise GovernanceAdapterError("latest-main changed after integration planning")
    return repo


def _candidate_receipt_from_public_metadata(
    value: object,
    *,
    subject: str,
) -> harness_train.CandidateVerificationReceipt:
    """Parse only the public evidence-blob receipt schema.

    This intentionally does not import or duplicate the train operation
    journal.  The receipt's public gate remains the semantic authority.
    """

    required = {
        "schema_version",
        "phase",
        "evidence_id",
        "candidate_commit",
        "candidate_tree",
        "argv",
        "exit_code",
        "stdout_sha256",
        "stderr_sha256",
        "receipt_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise GovernanceAdapterError(f"candidate public verification receipt fields are invalid: {subject}")
    argv = value.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise GovernanceAdapterError(f"candidate public verification argv is invalid: {subject}")
    exit_code = value.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise GovernanceAdapterError(f"candidate public verification exit code is invalid: {subject}")
    receipt = harness_train.CandidateVerificationReceipt(
        schema_version=str(value["schema_version"]),
        phase=str(value["phase"]),  # type: ignore[arg-type]
        evidence_id=str(value["evidence_id"]),
        candidate_commit=str(value["candidate_commit"]),
        candidate_tree=str(value["candidate_tree"]),
        argv=tuple(argv),
        exit_code=exit_code,
        stdout_sha256=str(value["stdout_sha256"]),
        stderr_sha256=str(value["stderr_sha256"]),
        receipt_digest=str(value["receipt_digest"]),
    )
    blockers = harness_train.candidate_verification_receipt_gate(receipt)
    if blockers:
        raise GovernanceAdapterError(
            f"candidate public verification receipt is invalid: {subject}: "
            + ", ".join(item.code for item in blockers)
        )
    return receipt


def _candidate_metadata_payload(value: Mapping[str, object]) -> dict[str, object]:
    result = dict(value)
    result.pop("metadata_digest", None)
    return result


def _principle_gate_binding_digest(
    candidate: harness_train.RegisteredCandidate,
    metadata: Mapping[str, object],
) -> str | None:
    """Bind future public principle-gate material without asserting its result.

    The train's public candidate gate is responsible for deciding whether such
    a receipt is valid.  Feature detection keeps this adapter compatible while
    that public schema is introduced: any top-level field explicitly named as
    principle gate/audit material becomes part of the adapter authority digest.
    """

    material: dict[str, object] = {}
    for source, values in (("registered", candidate.as_dict()), ("evidence", metadata)):
        for key, value in sorted(values.items()):
            normalized = key.lower().replace("-", "_")
            if "principle" in normalized and ("gate" in normalized or "audit" in normalized):
                material[f"{source}:{key}"] = value
    return _digest(material) if material else None


def _candidate_authority_payload(value: CandidateAuthorityBinding) -> dict[str, object]:
    result = value.as_dict()
    result.pop("authority_digest", None)
    return result


def _candidate_authority_from_mapping(value: object) -> CandidateAuthorityBinding:
    required = {
        "schema_version",
        "iteration",
        "generation",
        "principle_sha256",
        "candidate_ref",
        "candidate_commit",
        "candidate_tree",
        "candidate_evidence_ref",
        "candidate_evidence_blob",
        "candidate_evidence_metadata_digest",
        "candidate_evidence_digest",
        "pre_seal_verification_receipt_digests",
        "seal_verification_receipt_digests",
        "verification_binding_digest",
        "principle_gate_binding_digest",
        "candidate_progress_event_id",
        "candidate_progress_event_sha256",
        "authority_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise GovernanceAdapterError("durable candidate authority fields are invalid")
    pre = value.get("pre_seal_verification_receipt_digests")
    seal = value.get("seal_verification_receipt_digests")
    if (
        not isinstance(pre, list)
        or not pre
        or not all(isinstance(item, str) and DIGEST_RE.fullmatch(item) for item in pre)
        or not isinstance(seal, list)
        or not seal
        or not all(isinstance(item, str) and DIGEST_RE.fullmatch(item) for item in seal)
    ):
        raise GovernanceAdapterError("durable candidate verification receipt identities are invalid")
    optional_principle = value.get("principle_gate_binding_digest")
    if optional_principle is not None and (
        not isinstance(optional_principle, str) or DIGEST_RE.fullmatch(optional_principle) is None
    ):
        raise GovernanceAdapterError("durable candidate principle-gate binding is invalid")
    binding = CandidateAuthorityBinding(
        schema_version=str(value["schema_version"]),
        iteration=str(value["iteration"]),
        generation=str(value["generation"]),
        principle_sha256=str(value["principle_sha256"]),
        candidate_ref=str(value["candidate_ref"]),
        candidate_commit=str(value["candidate_commit"]),
        candidate_tree=str(value["candidate_tree"]),
        candidate_evidence_ref=str(value["candidate_evidence_ref"]),
        candidate_evidence_blob=str(value["candidate_evidence_blob"]),
        candidate_evidence_metadata_digest=str(value["candidate_evidence_metadata_digest"]),
        candidate_evidence_digest=str(value["candidate_evidence_digest"]),
        pre_seal_verification_receipt_digests=tuple(pre),
        seal_verification_receipt_digests=tuple(seal),
        verification_binding_digest=str(value["verification_binding_digest"]),
        principle_gate_binding_digest=optional_principle,
        candidate_progress_event_id=str(value["candidate_progress_event_id"]),
        candidate_progress_event_sha256=str(value["candidate_progress_event_sha256"]),
        authority_digest=str(value["authority_digest"]),
    )
    digest_fields = (
        binding.principle_sha256,
        binding.candidate_evidence_metadata_digest,
        binding.candidate_evidence_digest,
        binding.verification_binding_digest,
        binding.candidate_progress_event_sha256,
        binding.authority_digest,
    )
    oid_fields = (
        binding.candidate_commit,
        binding.candidate_tree,
        binding.candidate_evidence_blob,
    )
    if (
        binding.schema_version != CANDIDATE_AUTHORITY_SCHEMA
        or any(DIGEST_RE.fullmatch(item) is None for item in digest_fields)
        or any(OID_RE.fullmatch(item) is None for item in oid_fields)
        or harness_progress.EVENT_ID_RE.fullmatch(binding.candidate_progress_event_id) is None
        or binding.authority_digest != _digest(_candidate_authority_payload(binding))
    ):
        raise GovernanceAdapterError("durable candidate authority identity/digest is invalid")
    return binding


def _load_public_candidate_metadata(
    repo: harness_train.Repository,
    candidate: harness_train.RegisteredCandidate,
) -> tuple[
    Mapping[str, object],
    tuple[harness_train.CandidateVerificationReceipt, ...],
    tuple[harness_train.CandidateVerificationReceipt, ...],
    harness_progress.ProgressEventV2,
]:
    if _resolve_ref(repo, candidate.candidate_evidence_ref) != candidate.candidate_evidence_blob:
        raise GovernanceAdapterError(
            f"candidate public evidence ref changed: {candidate.candidate_evidence_ref}"
        )
    raw = _git(repo, ["cat-file", "blob", candidate.candidate_evidence_blob], check=False)
    if raw.returncode != 0:
        raise GovernanceAdapterError(
            f"candidate public evidence blob is unreadable: {candidate.candidate_evidence_blob}"
        )
    try:
        metadata = json.loads(raw.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernanceAdapterError("candidate public evidence blob is invalid JSON") from exc
    if not isinstance(metadata, dict) or _canonical_json(metadata) + b"\n" != raw.stdout:
        raise GovernanceAdapterError("candidate public evidence blob is not canonical JSON")
    metadata_digest = metadata.get("metadata_digest")
    if (
        metadata.get("schema_version") != harness_train.CANDIDATE_EVIDENCE_METADATA_SCHEMA
        or metadata_digest != candidate.candidate_evidence_metadata_digest
        or not isinstance(metadata_digest, str)
        or metadata_digest != _digest(_candidate_metadata_payload(metadata))
    ):
        raise GovernanceAdapterError("candidate public evidence metadata digest changed")
    expected = {
        "iteration": candidate.iteration,
        "generation": candidate.generation,
        "candidate_ref": candidate.candidate_ref,
        "candidate_evidence_ref": candidate.candidate_evidence_ref,
        "pre_seal_commit": candidate.pre_seal_commit,
        "pre_seal_tree": candidate.pre_seal_tree,
        "seal_commit": candidate.candidate_commit,
        "seal_tree": candidate.candidate_tree,
    }
    for field, expected_value in expected.items():
        if metadata.get(field) != expected_value:
            raise GovernanceAdapterError(
                f"candidate public evidence identity differs: {candidate.candidate_ref} / {field}"
            )
    pre_raw = metadata.get("pre_seal_verification_receipts")
    seal_raw = metadata.get("seal_verification_receipts")
    if not isinstance(pre_raw, list) or not isinstance(seal_raw, list):
        raise GovernanceAdapterError("candidate public evidence lacks two-phase verification receipts")
    pre = tuple(
        _candidate_receipt_from_public_metadata(item, subject=f"{candidate.iteration}/pre-seal/{index}")
        for index, item in enumerate(pre_raw)
    )
    seal = tuple(
        _candidate_receipt_from_public_metadata(item, subject=f"{candidate.iteration}/seal/{index}")
        for index, item in enumerate(seal_raw)
    )
    if not pre or not seal:
        raise GovernanceAdapterError("candidate public evidence has an empty verification phase")
    for receipt in pre:
        blockers = harness_train.candidate_verification_receipt_gate(
            receipt,
            expected_phase="pre-seal",
            expected_commit=candidate.pre_seal_commit,
            expected_tree=candidate.pre_seal_tree,
        )
        if blockers:
            raise GovernanceAdapterError(
                f"candidate pre-seal verification is stale: {candidate.candidate_ref}: "
                + ", ".join(item.code for item in blockers)
            )
    for receipt in seal:
        blockers = harness_train.candidate_verification_receipt_gate(
            receipt,
            expected_phase="seal",
            expected_commit=candidate.candidate_commit,
            expected_tree=candidate.candidate_tree,
        )
        if blockers:
            raise GovernanceAdapterError(
                f"candidate seal verification is stale: {candidate.candidate_ref}: "
                + ", ".join(item.code for item in blockers)
            )
    pre_commands = tuple((item.evidence_id, item.argv) for item in pre)
    seal_commands = tuple((item.evidence_id, item.argv) for item in seal)
    if pre_commands != seal_commands:
        raise GovernanceAdapterError(
            f"candidate verification phases cover different commands: {candidate.candidate_ref}"
        )
    if seal != candidate.verification_receipts:
        raise GovernanceAdapterError(
            f"candidate public seal receipts differ from registered authority: {candidate.candidate_ref}"
        )
    try:
        candidate_event = harness_progress.ProgressEventV2.from_dict(metadata.get("progress_event"))
    except (TypeError, harness_progress.ProgressError) as exc:
        raise GovernanceAdapterError(
            f"candidate public evidence lacks a valid progress event: {candidate.candidate_ref}"
        ) from exc
    event_hash = metadata.get("progress_event_bytes_sha256")
    exact_hashes = {
        _sha256(candidate_event.render(b"\n")),
        _sha256(candidate_event.render(b"\r\n")),
    }
    if (
        candidate_event.iteration != candidate.iteration
        or candidate_event.scope != "candidate"
        or candidate_event.operation_id != candidate.operation_id
        or not isinstance(event_hash, str)
        or event_hash not in exact_hashes
    ):
        raise GovernanceAdapterError(
            f"candidate public progress event identity/bytes differ: {candidate.candidate_ref}"
        )
    return metadata, pre, seal, candidate_event


def _public_candidate_authority(
    repo: harness_train.Repository,
    *,
    iteration: str,
    generation: str,
    current_principle_sha256: str,
    supplied: harness_train.RegisteredCandidate | None = None,
) -> tuple[
    harness_train.RegisteredCandidate,
    CandidateAuthorityBinding,
    harness_progress.ProgressEventV2,
]:
    if supplied is not None:
        try:
            blockers = harness_train.registered_candidate_gate(
                repo.root,
                supplied,
                current_principle_sha256=current_principle_sha256,
            )
        except harness_train.TrainError as exc:
            raise GovernanceAdapterError(
                f"candidate public gate failed: {supplied.candidate_ref}: {exc}"
            ) from exc
        if blockers:
            raise GovernanceAdapterError(
                f"candidate public gate blocked: {supplied.candidate_ref}: "
                + ", ".join(item.code for item in blockers)
            )
        loaded = supplied
    else:
        try:
            loaded, blockers = harness_train.load_registered_candidate(
                repo.root,
                iteration=iteration,
                generation=generation,
                current_principle_sha256=current_principle_sha256,
            )
        except harness_train.TrainError as exc:
            raise GovernanceAdapterError(
                f"candidate public authority cannot be loaded: PRD-{iteration}/{generation}: {exc}"
            ) from exc
        if loaded is None or blockers:
            raise GovernanceAdapterError(
                f"candidate public authority is blocked: PRD-{iteration}/{generation}: "
                + ", ".join(item.code for item in blockers)
            )
    metadata, pre, seal, candidate_event = _load_public_candidate_metadata(repo, loaded)
    verification_binding = _digest(
        {
            "pre_seal": [item.as_dict() for item in pre],
            "seal": [item.as_dict() for item in seal],
        }
    )
    provisional = CandidateAuthorityBinding(
        schema_version=CANDIDATE_AUTHORITY_SCHEMA,
        iteration=loaded.iteration,
        generation=loaded.generation,
        principle_sha256=loaded.principle_sha256,
        candidate_ref=loaded.candidate_ref,
        candidate_commit=loaded.candidate_commit,
        candidate_tree=loaded.candidate_tree,
        candidate_evidence_ref=loaded.candidate_evidence_ref,
        candidate_evidence_blob=loaded.candidate_evidence_blob,
        candidate_evidence_metadata_digest=loaded.candidate_evidence_metadata_digest,
        candidate_evidence_digest=loaded.candidate_evidence.evidence_digest,
        pre_seal_verification_receipt_digests=tuple(item.receipt_digest for item in pre),
        seal_verification_receipt_digests=tuple(item.receipt_digest for item in seal),
        verification_binding_digest=verification_binding,
        principle_gate_binding_digest=_principle_gate_binding_digest(loaded, metadata),
        candidate_progress_event_id=candidate_event.event_id,
        candidate_progress_event_sha256=str(metadata["progress_event_bytes_sha256"]),
        authority_digest="0" * 64,
    )
    return loaded, replace(
        provisional,
        authority_digest=_digest(_candidate_authority_payload(provisional)),
    ), candidate_event


def _revalidate_candidate_authority(
    repo: harness_train.Repository,
    expected: CandidateAuthorityBinding,
) -> harness_train.RegisteredCandidate:
    loaded, actual, _candidate_event = _public_candidate_authority(
        repo,
        iteration=expected.iteration,
        generation=expected.generation,
        current_principle_sha256=expected.principle_sha256,
    )
    if actual != expected:
        raise GovernanceAdapterError(
            f"normalization candidate public authority changed: {expected.candidate_ref}"
        )
    return loaded


def _validate_candidates(
    repo: harness_train.Repository,
    plan: harness_train.IntegrationPreparePlan,
) -> tuple[_CommitSnapshot, _ValidatedCandidates]:
    main = _commit_snapshot(repo, plan.target_main, source_id=f"main:{plan.target_main}")
    principle = main.files.get(PRINCIPLE_PATH)
    if principle is None or _sha256(principle.content) != plan.principle_sha256:
        raise GovernanceAdapterError("latest-main principle identity differs from integration plan")
    validated: list[_ValidatedCandidate] = []
    for supplied in plan.candidates:
        candidate, binding, candidate_event = _public_candidate_authority(
            repo,
            iteration=supplied.iteration,
            generation=supplied.generation,
            current_principle_sha256=plan.principle_sha256,
            supplied=supplied,
        )
        if candidate.principle_sha256 != plan.principle_sha256:
            raise GovernanceAdapterError(f"candidate principle baseline is stale: {candidate.candidate_ref}")
        base = _commit_snapshot(repo, candidate.base_commit, source_id=f"base:{candidate.base_commit}")
        branch = _commit_snapshot(repo, candidate.candidate_commit, source_id=f"candidate:{candidate.candidate_commit}")
        validated.append((candidate, base, branch, binding, candidate_event))
    return main, tuple(validated)


def _normalization_payload(plan: PreMergeNormalizationPlan) -> dict[str, object]:
    return {
        "schema_version": plan.schema_version,
        "phase": plan.phase,
        "operation_id": plan.operation_id,
        "project_root": plan.project_root,
        "git_common_dir": plan.git_common_dir,
        "integration_worktree": plan.integration_worktree,
        "train_plan_digest": plan.train_plan_digest,
        "main_ref": plan.main_ref,
        "target_main": plan.target_main,
        "expected_head": plan.expected_head,
        "expected_input_tree": plan.expected_input_tree,
        "candidate_authorities": [
            item.as_dict() for item in plan.candidate_authorities
        ],
        "entries": [item.manifest_dict() for item in plan.entries],
        "unmerged_paths": list(plan.unmerged_paths),
    }


def normalization_plan_digest(plan: PreMergeNormalizationPlan) -> str:
    return _digest(_normalization_payload(plan))


def _normalization_from_manifest(
    manifest: Mapping[str, object],
    *,
    plan_digest: str,
) -> PreMergeNormalizationPlan:
    required = {
        "schema_version",
        "phase",
        "operation_id",
        "project_root",
        "git_common_dir",
        "integration_worktree",
        "train_plan_digest",
        "main_ref",
        "target_main",
        "expected_head",
        "expected_input_tree",
        "candidate_authorities",
        "entries",
        "unmerged_paths",
    }
    if set(manifest) != required or manifest.get("schema_version") != NORMALIZATION_SCHEMA:
        raise GovernanceAdapterError("durable normalization manifest schema/fields are invalid")
    raw_entries = manifest.get("entries")
    raw_candidates = manifest.get("candidate_authorities")
    raw_unmerged = manifest.get("unmerged_paths")
    if not isinstance(raw_entries, list) or not isinstance(raw_candidates, list) or not isinstance(raw_unmerged, list):
        raise GovernanceAdapterError("durable normalization manifest collections are invalid")
    entries: list[NormalizationEntry] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "before_stages",
            "before_worktree_exists",
            "before_worktree_sha256",
            "after_exists",
            "after_mode",
            "after_blob",
            "after_sha256",
            "after_content_base64",
        }:
            raise GovernanceAdapterError("durable normalization entry fields are invalid")
        path = _canonical_path(str(raw.get("path")))
        if path in seen:
            raise GovernanceAdapterError(f"durable normalization path is duplicated: {path}")
        seen.add(path)
        raw_stages = raw.get("before_stages")
        if not isinstance(raw_stages, list):
            raise GovernanceAdapterError(f"durable normalization stages are invalid: {path}")
        stages: list[IndexStage] = []
        for value in raw_stages:
            if not isinstance(value, dict) or set(value) != {"mode", "object_id", "stage"}:
                raise GovernanceAdapterError(f"durable normalization stage fields are invalid: {path}")
            mode = value.get("mode")
            object_id = value.get("object_id")
            stage = value.get("stage")
            if (
                not isinstance(mode, str)
                or MODE_RE.fullmatch(mode) is None
                or not isinstance(object_id, str)
                or OID_RE.fullmatch(object_id) is None
                or not isinstance(stage, int)
                or stage not in {0, 1, 2, 3}
            ):
                raise GovernanceAdapterError(f"durable normalization stage identity is invalid: {path}")
            stages.append(IndexStage(mode, object_id, stage))
        before_exists = raw.get("before_worktree_exists")
        after_exists = raw.get("after_exists")
        if not isinstance(before_exists, bool) or not isinstance(after_exists, bool):
            raise GovernanceAdapterError(f"durable normalization existence flags are invalid: {path}")
        before_hash = raw.get("before_worktree_sha256")
        if before_exists != (isinstance(before_hash, str) and DIGEST_RE.fullmatch(before_hash) is not None):
            raise GovernanceAdapterError(f"durable normalization before hash is invalid: {path}")
        after_mode = raw.get("after_mode")
        after_blob = raw.get("after_blob")
        after_hash = raw.get("after_sha256")
        encoded = raw.get("after_content_base64")
        if after_exists:
            if (
                not isinstance(after_mode, str)
                or MODE_RE.fullmatch(after_mode) is None
                or not isinstance(after_blob, str)
                or OID_RE.fullmatch(after_blob) is None
                or not isinstance(after_hash, str)
                or DIGEST_RE.fullmatch(after_hash) is None
                or not isinstance(encoded, str)
            ):
                raise GovernanceAdapterError(f"durable normalization result identity is invalid: {path}")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise GovernanceAdapterError(f"durable normalization content is invalid: {path}") from exc
            if _sha256(content) != after_hash:
                raise GovernanceAdapterError(f"durable normalization content hash differs: {path}")
        else:
            if any(value is not None for value in (after_mode, after_blob, after_hash, encoded)):
                raise GovernanceAdapterError(f"absent durable normalization result has content identity: {path}")
            content = None
        entries.append(
            NormalizationEntry(
                path,
                tuple(stages),
                before_exists,
                before_hash if isinstance(before_hash, str) else None,
                after_exists,
                after_mode if isinstance(after_mode, str) else None,
                after_blob if isinstance(after_blob, str) else None,
                after_hash if isinstance(after_hash, str) else None,
                content,
            )
        )
    candidates = tuple(_candidate_authority_from_mapping(raw) for raw in raw_candidates)
    unmerged = tuple(_canonical_path(str(item)) for item in raw_unmerged)
    input_tree = manifest.get("expected_input_tree")
    if input_tree is not None and (not isinstance(input_tree, str) or OID_RE.fullmatch(input_tree) is None):
        raise GovernanceAdapterError("durable normalization input tree is invalid")
    provisional = PreMergeNormalizationPlan(
        schema_version=str(manifest["schema_version"]),
        phase=str(manifest["phase"]),  # type: ignore[arg-type]
        operation_id=str(manifest["operation_id"]),
        project_root=str(manifest["project_root"]),
        git_common_dir=str(manifest["git_common_dir"]),
        integration_worktree=str(manifest["integration_worktree"]),
        train_plan_digest=str(manifest["train_plan_digest"]),
        main_ref=str(manifest["main_ref"]),
        target_main=str(manifest["target_main"]),
        expected_head=str(manifest["expected_head"]),
        expected_input_tree=input_tree,
        candidate_authorities=candidates,
        entries=tuple(entries),
        unmerged_paths=unmerged,
        plan_digest=plan_digest,
        blockers=(),
    )
    if provisional.phase not in {"post-merge", "merge-conflict"}:
        raise GovernanceAdapterError("durable normalization phase is invalid")
    if provisional.plan_digest != normalization_plan_digest(provisional):
        raise GovernanceAdapterError("durable normalization manifest digest changed")
    return provisional


def plan_premerge_normalization(
    train_plan: harness_train.IntegrationPreparePlan,
    *,
    phase: Literal["post-merge", "merge-conflict"] = "merge-conflict",
    expected_input_tree: str | None = None,
    _validated: tuple[_CommitSnapshot, _ValidatedCandidates] | None = None,
) -> PreMergeNormalizationPlan:
    """Plan exact latest-main restoration for shared governance paths.

    In ``merge-conflict`` mode every unmerged path must be a supported Harness
    governance path.  A train must call this API before declaring such a merge
    failed, then continue to the normal governance callback.  The API never
    accepts or modifies an implementation conflict.
    """

    if phase not in {"post-merge", "merge-conflict"}:
        raise GovernanceAdapterError("normalization phase is invalid")
    repo = _validate_train_plan(train_plan)
    main, candidates = (
        _validate_candidates(repo, train_plan) if _validated is None else _validated
    )
    worktree = Path(train_plan.worktree_path).absolute().resolve()
    if not worktree.is_dir():
        raise GovernanceAdapterError(f"integration worktree does not exist: {worktree}")
    top = Path(_git_text(repo, ["rev-parse", "--show-toplevel"], cwd=worktree)).resolve()
    if os.path.normcase(str(top)) != os.path.normcase(str(worktree)):
        raise GovernanceAdapterError("integration worktree root identity changed")
    head = _git_text(repo, ["rev-parse", "--verify", "HEAD"], cwd=worktree)
    blockers: list[AdapterBlocker] = []
    if head != train_plan.target_main:
        blockers.append(AdapterBlocker("normalization-head-drift", "integration HEAD differs from planned latest-main"))
    unmerged = _unmerged_paths(repo, worktree)
    unsupported_conflicts = tuple(path for path in unmerged if not _supported_path(path))
    if unsupported_conflicts:
        blockers.append(
            AdapterBlocker(
                "implementation-merge-conflict",
                "normalization refuses non-governance conflicts: " + ", ".join(unsupported_conflicts),
            )
        )
    if phase == "post-merge" and unmerged:
        blockers.append(AdapterBlocker("post-merge-index-unmerged", "post-merge callback requires a stage-0 index"))
    unowned = _untracked_and_ignored(repo, worktree)
    if unowned:
        blockers.append(
            AdapterBlocker(
                "normalization-unowned-paths",
                "integration worktree contains untracked/ignored paths: " + ", ".join(unowned),
            )
        )
    actual_tree: str | None = None
    if not unmerged:
        actual_tree = _write_tree(repo, worktree)
        if expected_input_tree is not None and actual_tree != expected_input_tree:
            blockers.append(AdapterBlocker("normalization-input-tree-drift", "integration index differs from callback context"))
    elif expected_input_tree is not None:
        blockers.append(AdapterBlocker("normalization-input-tree-unavailable", "an unmerged index cannot match a tree identity"))

    paths = set(main.files)
    for _candidate, base, branch, _binding, _candidate_event in candidates:
        paths.update(base.files)
        paths.update(branch.files)
    tracked = _null_paths(_git(repo, ["ls-files", "-z", "--", "harness"], cwd=worktree).stdout)
    paths.update(path for path in tracked if _supported_path(path))
    entries: list[NormalizationEntry] = []
    for path in sorted(paths):
        desired = main.files.get(path)
        stages = _index_stages(repo, worktree, path)
        before_exists, before_hash = _path_state(worktree, path)
        if not unmerged and any(item.stage != 0 for item in stages):
            blockers.append(AdapterBlocker("normalization-index-stage-invalid", "post-merge path has non-zero stages", path))
        entries.append(
            NormalizationEntry(
                path=path,
                before_stages=stages,
                before_worktree_exists=before_exists,
                before_worktree_sha256=before_hash,
                after_exists=desired is not None,
                after_mode=desired.mode if desired else None,
                after_blob=desired.object_id if desired else None,
                after_sha256=_sha256(desired.content) if desired else None,
                after_content=desired.content if desired else None,
            )
        )
    provisional = PreMergeNormalizationPlan(
        NORMALIZATION_SCHEMA,
        phase,
        train_plan.operation_id,
        train_plan.project_root,
        train_plan.git_common_dir,
        str(worktree),
        train_plan.plan_digest,
        train_plan.main_ref,
        train_plan.target_main,
        train_plan.target_main,
        expected_input_tree if expected_input_tree is not None else actual_tree,
        tuple(item[3] for item in candidates),
        tuple(entries),
        unmerged,
        "0" * 64,
        tuple(blockers),
    )
    return replace(provisional, plan_digest=normalization_plan_digest(provisional))


def _registry_root(common: Path) -> Path:
    root = common.joinpath(*REGISTRY_PARTS)
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.resolve().relative_to(common.resolve())
    except ValueError as exc:
        raise GovernanceAdapterError("governance adapter registry escaped Git common directory") from exc
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise GovernanceAdapterError("governance adapter registry is redirected")
    return root


def normalization_journal_path(common_dir: Path | str, operation_id: str, phase: str) -> Path:
    suffix = "conflict" if phase == "merge-conflict" else "post"
    return _registry_root(Path(common_dir).resolve()) / f"normalize-{suffix}-{operation_id}.json"


def execution_journal_path(common_dir: Path | str, operation_id: str) -> Path:
    return _registry_root(Path(common_dir).resolve()) / f"execution-{operation_id}.json"


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    raw = _canonical_json(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_JSON_BYTES:
            raise GovernanceAdapterError(f"durable JSON exceeds safe size: {path}")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernanceAdapterError(f"durable JSON is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise GovernanceAdapterError(f"durable JSON is not an object: {path}")
    return value


def _approval_payload(value: harness_governance.PrincipleApproval) -> dict[str, object]:
    return {
        "change_id": value.change_id,
        "evidence_ref": value.evidence_ref,
        "exact_before_base64": base64.b64encode(value.exact_before).decode("ascii"),
        "exact_after_base64": base64.b64encode(value.exact_after).decode("ascii"),
    }


def _adapter_config_payload(
    *,
    candidate_authorities: Sequence[CandidateAuthorityBinding],
    readme_authority: ReadmeRebuildAuthority | None,
    principle_approvals: Mapping[str, harness_governance.PrincipleApproval],
    principle_leases: Mapping[str, harness_reconcile.GlobalPrincipleLease],
) -> dict[str, object]:
    return {
        "candidate_authorities": [item.as_dict() for item in candidate_authorities],
        "readme_authority": _readme_authority_payload(readme_authority) if readme_authority else None,
        "principle_approvals": {
            key: _approval_payload(value)
            for key, value in sorted(principle_approvals.items())
        },
        "principle_leases": {
            key: value.as_dict()
            for key, value in sorted(principle_leases.items())
        },
    }


def _snapshot_manifest(snapshot: harness_reconcile.GovernanceSnapshot) -> list[dict[str, object]]:
    return [
        {"path": item.path, "size": len(item.content), "sha256": _sha256(item.content)}
        for item in snapshot.files
    ]


def _execution_manifest(
    preview: GovernanceExecutionPreview,
    context: harness_train.GovernanceContext,
    *,
    config_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": ADAPTER_SCHEMA,
        "operation_id": context.operation_id,
        "train_plan_digest": preview.normalization.train_plan_digest,
        "context": context.as_dict(),
        "config_digest": config_digest,
        "normalization": {
            "plan_digest": preview.normalization.plan_digest,
            "manifest": _normalization_payload(preview.normalization),
        },
        "reconciliations": [
            {
                "label": label,
                "operation_id": plan.operation_id,
                "plan_digest": plan.plan_digest,
                "result_snapshot": _snapshot_manifest(snapshot),
            }
            for label, plan, snapshot in zip(
                preview.reconciliation_labels,
                preview.reconciliations,
                preview.reconciliation_snapshots,
            )
        ],
        "progress_events": [item.as_dict() for item in preview.progress_events],
        "final_snapshot": _snapshot_manifest(preview.final_snapshot),
    }


def _validate_execution_journal(value: object, path: Path) -> dict[str, object]:
    required = {
        "schema_version",
        "operation_id",
        "manifest_digest",
        "manifest",
        "phase",
        "completed_steps",
        "allowed_trees",
        "step_trees",
        "receipt",
        "error",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise GovernanceAdapterError(f"execution journal fields are invalid: {path}")
    if value.get("schema_version") != EXECUTION_JOURNAL_SCHEMA:
        raise GovernanceAdapterError(f"execution journal schema is invalid: {path}")
    manifest = value.get("manifest")
    digest = value.get("manifest_digest")
    if not isinstance(manifest, dict) or not isinstance(digest, str) or _digest(manifest) != digest:
        raise GovernanceAdapterError(f"execution journal manifest digest changed: {path}")
    if value.get("phase") not in {"PLANNED", "APPLYING", "APPLIED", "FAILED_NEEDS_RECONCILE"}:
        raise GovernanceAdapterError(f"execution journal phase is invalid: {path}")
    if not isinstance(value.get("completed_steps"), list) or not all(
        isinstance(item, str) for item in value["completed_steps"]
    ):
        raise GovernanceAdapterError(f"execution journal completed steps are invalid: {path}")
    allowed = value.get("allowed_trees")
    if not isinstance(allowed, list) or not all(isinstance(item, str) and OID_RE.fullmatch(item) for item in allowed):
        raise GovernanceAdapterError(f"execution journal allowed trees are invalid: {path}")
    step_trees = value.get("step_trees")
    if not isinstance(step_trees, dict) or not all(
        isinstance(key, str) and isinstance(item, str) and OID_RE.fullmatch(item)
        for key, item in step_trees.items()
    ):
        raise GovernanceAdapterError(f"execution journal step trees are invalid: {path}")
    return dict(value)


def _load_execution_journal(common: Path, operation_id: str) -> tuple[dict[str, object], Path] | None:
    path = execution_journal_path(common, operation_id)
    if not path.exists():
        return None
    return _validate_execution_journal(_read_json(path), path), path


def _ensure_execution_journal(
    repo: harness_train.Repository,
    preview: GovernanceExecutionPreview,
    context: harness_train.GovernanceContext,
    *,
    config_digest: str,
) -> tuple[dict[str, object], Path, bool]:
    path = execution_journal_path(repo.common_dir, context.operation_id)
    manifest = _execution_manifest(preview, context, config_digest=config_digest)
    manifest_digest = _digest(manifest)
    lock = path.with_suffix(".lock")
    with _file_lock(lock):
        if path.exists():
            journal = _validate_execution_journal(_read_json(path), path)
            if journal.get("manifest_digest") != manifest_digest or _digest(journal.get("manifest")) != manifest_digest:
                raise GovernanceAdapterError("execution journal differs from exact callback plan/configuration")
            if journal.get("phase") == "FAILED_NEEDS_RECONCILE":
                raise GovernanceAdapterError(f"governance callback requires reconcile: {journal.get('error')}")
            return journal, path, True
        journal = {
            "schema_version": EXECUTION_JOURNAL_SCHEMA,
            "operation_id": context.operation_id,
            "manifest_digest": manifest_digest,
            "manifest": manifest,
            "phase": "PLANNED",
            "completed_steps": [],
            "allowed_trees": [context.pre_governance_tree],
            "step_trees": {},
            "receipt": None,
            "error": None,
        }
        _atomic_json(path, journal)
        return journal, path, False


def _record_execution_step(
    path: Path,
    journal: Mapping[str, object],
    *,
    step: str,
    tree: str,
) -> dict[str, object]:
    updated = dict(journal)
    completed = list(updated["completed_steps"])
    allowed = list(updated["allowed_trees"])
    step_trees = dict(updated["step_trees"])
    if step in completed:
        if step_trees.get(step) != tree:
            raise GovernanceAdapterError(f"completed governance step tree changed: {step}")
        return updated
    completed.append(step)
    if tree not in allowed:
        allowed.append(tree)
    step_trees[step] = tree
    updated.update(
        {
            "phase": "APPLYING",
            "completed_steps": completed,
            "allowed_trees": allowed,
            "step_trees": step_trees,
            "error": None,
        }
    )
    _atomic_json(path, updated)
    return updated


@contextlib.contextmanager
def _file_lock(path: Path, timeout_seconds: float = 30.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.tell() == handle.seek(0, os.SEEK_END):
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise GovernanceAdapterError(f"timed out acquiring adapter lock: {path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _entry_state_matches(entry: NormalizationEntry, root: Path, repo: harness_train.Repository) -> Literal["before", "after", "drift"]:
    exists, worktree_hash = _path_state(root, entry.path)
    stages = _index_stages(repo, root, entry.path)
    if entry.after_exists:
        expected_stage = (IndexStage(entry.after_mode or "", entry.after_blob or "", 0),)
        if stages == expected_stage and exists and worktree_hash == entry.after_sha256:
            return "after"
    elif not stages and not exists:
        return "after"
    if stages == entry.before_stages and exists == entry.before_worktree_exists and worktree_hash == entry.before_worktree_sha256:
        return "before"
    return "drift"


def _apply_normalization_entry(
    repo: harness_train.Repository,
    worktree: Path,
    plan: PreMergeNormalizationPlan,
    entry: NormalizationEntry,
) -> None:
    if _entry_state_matches(entry, worktree, repo) == "after":
        return
    if _entry_state_matches(entry, worktree, repo) != "before":
        raise GovernanceAdapterError(f"governance path changed after normalization planning: {entry.path}")
    target = worktree / Path(entry.path)
    _assert_safe_target(worktree, target)
    if entry.after_exists:
        result = _git(repo, ["checkout", plan.target_main, "--", entry.path], cwd=worktree, check=False)
    else:
        result = _git(repo, ["rm", "-q", "--", entry.path], cwd=worktree, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GovernanceAdapterError(f"cannot normalize {entry.path}: {detail}")
    if _entry_state_matches(entry, worktree, repo) != "after":
        raise GovernanceAdapterError(f"normalized path does not match exact latest-main bytes: {entry.path}")


def apply_premerge_normalization(
    plan: PreMergeNormalizationPlan,
    *,
    accepted_plan_digest: str,
) -> NormalizationResult:
    """Apply a reviewed normalization plan with journaled per-path CAS."""

    if not isinstance(plan, PreMergeNormalizationPlan):
        raise TypeError("plan must be PreMergeNormalizationPlan")
    if plan.schema_version != NORMALIZATION_SCHEMA or plan.plan_digest != normalization_plan_digest(plan):
        raise GovernanceAdapterError("normalization plan schema/digest changed")
    if accepted_plan_digest != plan.plan_digest:
        raise GovernanceAdapterError("accepted normalization digest differs from exact plan")
    if plan.blockers:
        raise GovernanceAdapterError("blocked normalization plan cannot apply")
    repo = harness_train.open_repository(plan.project_root)
    worktree = Path(plan.integration_worktree).resolve()
    if os.path.normcase(str(repo.common_dir)) != os.path.normcase(str(Path(plan.git_common_dir).resolve())):
        raise GovernanceAdapterError("normalization common directory changed")
    if _resolve_ref(repo, plan.main_ref) != plan.target_main:
        raise GovernanceAdapterError("normalization latest-main ref changed")
    head = _git_text(repo, ["rev-parse", "--verify", "HEAD"], cwd=worktree)
    if head != plan.expected_head:
        raise GovernanceAdapterError("normalization integration HEAD changed")
    for authority in plan.candidate_authorities:
        _revalidate_candidate_authority(repo, authority)
    journal_path = normalization_journal_path(repo.common_dir, plan.operation_id, plan.phase)
    lock_path = journal_path.with_suffix(".lock")
    manifest = _normalization_payload(plan)
    with _file_lock(lock_path):
        resumed = journal_path.exists()
        if resumed:
            journal = _read_json(journal_path)
            if (
                journal.get("schema_version") != NORMALIZATION_JOURNAL_SCHEMA
                or journal.get("operation_id") != plan.operation_id
                or journal.get("plan_digest") != plan.plan_digest
                or journal.get("manifest") != manifest
            ):
                raise GovernanceAdapterError("normalization journal differs from accepted plan")
            if journal.get("phase") == "FAILED_NEEDS_RECONCILE":
                raise GovernanceAdapterError(f"normalization requires reconcile: {journal.get('error')}")
        else:
            journal = {
                "schema_version": NORMALIZATION_JOURNAL_SCHEMA,
                "operation_id": plan.operation_id,
                "plan_digest": plan.plan_digest,
                "manifest": manifest,
                "phase": "PLANNED",
                "completed_paths": [],
                "error": None,
            }
            _atomic_json(journal_path, journal)
        try:
            completed = list(journal.get("completed_paths", []))
            if not all(isinstance(item, str) for item in completed):
                raise GovernanceAdapterError("normalization journal completed paths are invalid")
            for entry in plan.entries:
                state = _entry_state_matches(entry, worktree, repo)
                if entry.path in completed:
                    if state != "after":
                        raise GovernanceAdapterError(f"completed normalization path drifted: {entry.path}")
                    continue
                if state == "drift":
                    raise GovernanceAdapterError(f"normalization before-state changed: {entry.path}")
                _apply_normalization_entry(repo, worktree, plan, entry)
                completed.append(entry.path)
                journal.update({"phase": "APPLYING", "completed_paths": completed, "error": None})
                _atomic_json(journal_path, journal)
            if _unmerged_paths(repo, worktree):
                raise GovernanceAdapterError("governance normalization left unmerged index stages")
            if _untracked_and_ignored(repo, worktree):
                raise GovernanceAdapterError("governance normalization left unowned paths")
            result_tree = _write_tree(repo, worktree)
            journal.update({"phase": "APPLIED", "completed_paths": completed, "result_tree": result_tree, "error": None})
            _atomic_json(journal_path, journal)
        except Exception as exc:
            journal.update({"phase": "FAILED_NEEDS_RECONCILE", "error": str(exc)})
            _atomic_json(journal_path, journal)
            raise
    raw = journal_path.read_bytes()
    return NormalizationResult(
        ADAPTER_SCHEMA,
        plan.operation_id,
        "APPLIED",
        plan.plan_digest,
        result_tree,
        str(journal_path),
        tuple(item.path for item in plan.entries),
        resumed,
        _sha256(raw),
    )


def _suboperation_id(operation_id: str, label: str) -> str:
    return "OP-" + hashlib.sha256(f"{operation_id}:{label}".encode("utf-8")).hexdigest()[:32]


def _integrated_progress_evidence_ref(iteration: str, generation: str) -> str:
    normalized_generation = generation.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", normalized_generation) is None:
        raise GovernanceAdapterError("integration generation cannot form a canonical evidence ref")
    return (
        f"refs/project-harness/v2/iterations/{iteration}/"
        f"integrated-evidence/{normalized_generation}"
    )


def _final_progress_evidence_ref(iteration: str) -> str:
    return f"refs/project-harness/v2/iterations/{iteration}/final-evidence"


def _progress_spec_payload(value: TrainProgressEventSpec) -> dict[str, object]:
    result = value.as_dict()
    result.pop("spec_digest", None)
    return result


def _build_train_progress_specs(
    train_plan: harness_train.IntegrationPreparePlan,
    validated: Sequence[_ValidatedCandidate],
    *,
    progress_content: bytes,
) -> tuple[TrainProgressEventSpec, ...]:
    style = harness_progress._pure_eol_style(progress_content, "integrated progress history")
    newline = b"\r\n" if style == "crlf" else b"\n"
    specs: list[TrainProgressEventSpec] = []
    for candidate, _base, _branch, _binding, candidate_event in validated:
        integrated_ref = _integrated_progress_evidence_ref(
            candidate.iteration,
            train_plan.generation,
        )
        final_ref = _final_progress_evidence_ref(candidate.iteration)
        started = harness_progress.integration_event(
            integration_state=f"started:{train_plan.generation}",
            session_id=candidate_event.session_id,
            iteration=candidate.iteration,
            occurred_at=candidate_event.occurred_at,
            source_ref=train_plan.main_ref,
            source_commit=train_plan.target_main,
            operation_id=train_plan.operation_id,
            causal_parent=candidate_event.event_id,
            evidence_refs=(
                f"operation:{train_plan.operation_id}",
                candidate.candidate_ref,
                candidate.candidate_evidence_ref,
            ),
            summary=(
                f"Integration {train_plan.operation_id} started for PRD-{candidate.iteration}; "
                "this event binds only latest-main planning and stable candidate refs."
            ),
        )
        verified = harness_progress.integration_event(
            integration_state=f"verified:{train_plan.generation}",
            session_id=candidate_event.session_id,
            iteration=candidate.iteration,
            occurred_at=candidate_event.occurred_at,
            source_ref=train_plan.main_ref,
            source_commit=train_plan.target_main,
            operation_id=train_plan.operation_id,
            causal_parent=started.event_id,
            evidence_refs=(integrated_ref, candidate.candidate_ref, candidate.candidate_evidence_ref),
            summary=(
                "Conditional transition proposal: integration_verified is not materialized "
                f"until exact public integrated evidence resolves at {integrated_ref}."
            ),
        )
        advanced = harness_progress.integration_event(
            integration_state=f"main-advanced:{train_plan.generation}",
            session_id=candidate_event.session_id,
            iteration=candidate.iteration,
            occurred_at=candidate_event.occurred_at,
            source_ref=train_plan.main_ref,
            source_commit=train_plan.target_main,
            operation_id=train_plan.operation_id,
            causal_parent=verified.event_id,
            evidence_refs=(final_ref, integrated_ref),
            summary=(
                "Conditional transition proposal: main_advanced is not materialized until "
                f"exact public final evidence resolves at {final_ref}."
            ),
        )
        for transition, event, evidence_ref, conditional in (
            ("integration_started", started, candidate.candidate_evidence_ref, False),
            ("integration_verified", verified, integrated_ref, True),
            ("main_advanced", advanced, final_ref, True),
        ):
            event_bytes = event.render(newline)
            provisional = TrainProgressEventSpec(
                schema_version=TRAIN_PROGRESS_SPEC_SCHEMA,
                transition=transition,  # type: ignore[arg-type]
                iteration=candidate.iteration,
                generation=train_plan.generation,
                event=event,
                event_bytes_b64=base64.b64encode(event_bytes).decode("ascii"),
                event_sha256=_sha256(event_bytes),
                evidence_ref=evidence_ref,
                conditional=conditional,
                spec_digest="0" * 64,
            )
            specs.append(
                replace(provisional, spec_digest=_digest(_progress_spec_payload(provisional)))
            )
    return tuple(specs)


def _validate_progress_spec(value: TrainProgressEventSpec) -> bytes:
    if not isinstance(value, TrainProgressEventSpec):
        raise GovernanceAdapterError("train progress spec has an unsupported type")
    try:
        raw = base64.b64decode(value.event_bytes_b64.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise GovernanceAdapterError("train progress event bytes are invalid") from exc
    if (
        value.schema_version != TRAIN_PROGRESS_SPEC_SCHEMA
        or value.iteration != value.event.iteration
        or value.event_sha256 != _sha256(raw)
        or value.spec_digest != _digest(_progress_spec_payload(value))
        or value.conditional != (value.transition != "integration_started")
        or value.event.render(b"\r\n" if b"\r\n" in raw else b"\n") != raw
        or not value.evidence_ref.startswith("refs/")
    ):
        raise GovernanceAdapterError("train progress event spec identity/bytes changed")
    return raw


ProgressEvidenceResolver = Callable[[str], ProgressEvidenceResolution | None]


def materialize_train_progress_events(
    specs: Sequence[TrainProgressEventSpec],
    *,
    resolver: ProgressEvidenceResolver,
) -> tuple[TrainProgressMaterialization, ...]:
    """Project pre-bound transitions only after exact public evidence resolves."""

    if not callable(resolver):
        raise TypeError("resolver must be callable")
    results: list[TrainProgressMaterialization] = []
    seen: set[str] = set()
    for spec in specs:
        _validate_progress_spec(spec)
        if spec.event.event_id in seen:
            raise GovernanceAdapterError("train progress event spec IDs are duplicated")
        seen.add(spec.event.event_id)
        materialized = not spec.conditional
        blocker: str | None = None
        if spec.conditional:
            try:
                resolution = resolver(spec.evidence_ref)
            except Exception as exc:
                resolution = None
                blocker = f"evidence-resolver-failed:{type(exc).__name__}"
            if resolution is None:
                blocker = blocker or "public-evidence-ref-absent"
            elif not isinstance(resolution, ProgressEvidenceResolution):
                blocker = "public-evidence-resolution-type"
            elif (
                resolution.schema_version != PROGRESS_EVIDENCE_RESOLUTION_SCHEMA
                or resolution.ref_name != spec.evidence_ref
                or OID_RE.fullmatch(resolution.object_id) is None
                or DIGEST_RE.fullmatch(resolution.evidence_digest) is None
                or spec.event.event_id not in resolution.event_ids
                or len(set(resolution.event_ids)) != len(resolution.event_ids)
            ):
                blocker = "public-evidence-resolution-mismatch"
            else:
                materialized = True
        results.append(
            TrainProgressMaterialization(
                schema_version=PROGRESS_MATERIALIZATION_SCHEMA,
                transition=spec.transition,
                iteration=spec.iteration,
                event_id=spec.event.event_id,
                evidence_ref=spec.evidence_ref,
                conditional=spec.conditional,
                materialized=materialized,
                blocker=blocker,
            )
        )
    return tuple(results)


def _readme_differences(
    validated: Sequence[_ValidatedCandidate],
) -> tuple[bool, set[str], list[AdapterBlocker]]:
    l0_changed = False
    changed_l1: set[str] = set()
    blockers: list[AdapterBlocker] = []
    for candidate, base, branch, _binding, _candidate_event in validated:
        base_files = base.semantic.as_mapping()
        branch_files = branch.semantic.as_mapping()
        if base_files.get(L0_PATH) != branch_files.get(L0_PATH):
            l0_changed = True
        l1_paths = {
            path
            for path in (*base_files.keys(), *branch_files.keys())
            if L1_PATH_RE.fullmatch(path) is not None
        }
        own_path = f"harness/iterations/{candidate.iteration}/README.md"
        for path in l1_paths:
            if base_files.get(path) == branch_files.get(path):
                continue
            if path != own_path:
                blockers.append(
                    AdapterBlocker(
                        "cross-iteration-readme-change",
                        f"candidate {candidate.candidate_ref} changed another iteration's L1",
                        path,
                    )
                )
            changed_l1.add(path)
    return l0_changed, changed_l1, blockers


def _plan_execution(
    train_plan: harness_train.IntegrationPreparePlan,
    context: harness_train.GovernanceContext,
    *,
    readme_authority: ReadmeRebuildAuthority | None,
    principle_approvals: Mapping[str, harness_governance.PrincipleApproval],
    principle_leases: Mapping[str, harness_reconcile.GlobalPrincipleLease],
) -> GovernanceExecutionPreview:
    repo = _validate_train_plan(train_plan)
    _validate_readme_authority(readme_authority)
    if not isinstance(context, harness_train.GovernanceContext):
        raise TypeError("context must be GovernanceContext")
    expected_context = {
        "schema_version": harness_train.GOVERNANCE_RECEIPT_SCHEMA,
        "operation_id": train_plan.operation_id,
        "project_root": train_plan.project_root,
        "integration_worktree": train_plan.worktree_path,
        "target_main": train_plan.target_main,
        "principle_sha256": train_plan.principle_sha256,
        "candidate_digests": tuple(item.candidate_evidence.evidence_digest for item in train_plan.candidates),
    }
    for field, expected in expected_context.items():
        actual = getattr(context, field)
        if field in {"project_root", "integration_worktree"}:
            if os.path.normcase(str(Path(actual).resolve())) != os.path.normcase(str(Path(expected).resolve())):
                raise GovernanceAdapterError(f"governance context {field} differs from accepted train plan")
        elif actual != expected:
            raise GovernanceAdapterError(f"governance context {field} differs from accepted train plan")
    main, validated = _validate_candidates(repo, train_plan)
    candidate_authorities = tuple(item[3] for item in validated)
    actual_index_tree = _write_tree(repo, Path(context.integration_worktree))
    config_digest = _digest(
        _adapter_config_payload(
            candidate_authorities=candidate_authorities,
            readme_authority=readme_authority,
            principle_approvals=principle_approvals,
            principle_leases=principle_leases,
        )
    )
    existing_execution = _load_execution_journal(repo.common_dir, context.operation_id)
    if existing_execution is None:
        if actual_index_tree != context.pre_governance_tree:
            raise GovernanceAdapterError("governance callback index differs from context input tree")
        normalization = plan_premerge_normalization(
            train_plan,
            phase="post-merge",
            expected_input_tree=context.pre_governance_tree,
            _validated=(main, validated),
        )
    else:
        execution, _execution_path = existing_execution
        if execution.get("phase") == "FAILED_NEEDS_RECONCILE":
            raise GovernanceAdapterError(f"governance callback requires reconcile: {execution.get('error')}")
        manifest = execution.get("manifest")
        if not isinstance(manifest, dict):
            raise GovernanceAdapterError("execution journal manifest is invalid")
        if (
            manifest.get("train_plan_digest") != train_plan.plan_digest
            or manifest.get("config_digest") != config_digest
            or _digest(manifest.get("context")) != _digest(context.as_dict())
        ):
            raise GovernanceAdapterError("execution journal no longer matches callback identity/configuration")
        raw_normalization = manifest.get("normalization")
        if (
            not isinstance(raw_normalization, dict)
            or set(raw_normalization) != {"plan_digest", "manifest"}
            or not isinstance(raw_normalization.get("plan_digest"), str)
            or not isinstance(raw_normalization.get("manifest"), dict)
        ):
            raise GovernanceAdapterError("execution journal normalization identity is invalid")
        normalization = _normalization_from_manifest(
            raw_normalization["manifest"],
            plan_digest=raw_normalization["plan_digest"],
        )
    blockers = list(normalization.blockers)
    l0_changed, changed_l1, readme_blockers = _readme_differences(validated)
    blockers.extend(readme_blockers)
    if l0_changed and (readme_authority is None or readme_authority.root is None):
        blockers.append(AdapterBlocker("l0-rebuild-authority-missing", "candidate L0 changes require structured routing authority", L0_PATH))
    supplied_l1 = {item.path for item in readme_authority.l1_documents} if readme_authority else set()
    for path in sorted(changed_l1 - supplied_l1):
        blockers.append(AdapterBlocker("l1-rebuild-authority-missing", "candidate L1 change requires exact derived authority bytes", path))

    current = main.semantic
    reconcile_plans: list[harness_reconcile.GovernanceReconcilePlan] = []
    labels: list[str] = []
    snapshots: list[harness_reconcile.GovernanceSnapshot] = []
    for index, (candidate, base, branch, binding, _candidate_event) in enumerate(validated):
        label = f"candidate-{index:04d}-{candidate.iteration}-{binding.authority_digest}"
        operation = _suboperation_id(context.operation_id, label)
        semantic_candidate = _semantic_candidate(
            base.semantic,
            branch.semantic,
            source_id=f"semantic:{candidate.candidate_commit}",
        )
        reconcile_plan = harness_reconcile.plan_reconciliation(
            project_root=context.integration_worktree,
            git_common_dir=repo.common_dir,
            operation_id=operation,
            branch_base=base.semantic,
            latest_main=current,
            branch_candidate=semantic_candidate,
            principle_approval=principle_approvals.get(candidate.candidate_evidence.evidence_digest),
            principle_lease=principle_leases.get(candidate.candidate_evidence.evidence_digest),
        )
        reconcile_plans.append(reconcile_plan)
        labels.append(label)
        blockers.extend(AdapterBlocker(item.code, item.message, item.subject) for item in reconcile_plan.blockers)
        if not reconcile_plan.blockers:
            current = _apply_previews_to_snapshot(current, reconcile_plan, source_id=f"after:{label}")
        snapshots.append(current)

    progress_specs: tuple[TrainProgressEventSpec, ...] = ()
    if not blockers:
        current_files = current.as_mapping()
        progress_content = current_files.get(PROGRESS_PATH)
        if progress_content is None:
            blockers.append(
                AdapterBlocker(
                    "train-progress-missing",
                    "integrated governance snapshot lacks harness/progress.md",
                    PROGRESS_PATH,
                )
            )
        else:
            try:
                progress_specs = _build_train_progress_specs(
                    train_plan,
                    validated,
                    progress_content=progress_content,
                )
            except (GovernanceAdapterError, harness_progress.ProgressError) as exc:
                blockers.append(AdapterBlocker("train-progress-spec-invalid", str(exc), PROGRESS_PATH))

    for spec in progress_specs:
        if blockers:
            break
        try:
            updated_progress, _appended = harness_progress.append_progress_event_exact(
                current.as_mapping()[PROGRESS_PATH],
                spec.event,
            )
            exact = _validate_progress_spec(spec)
            parsed = harness_governance.parse_progress_events(
                updated_progress,
                source=f"train-event:{spec.event.event_id}",
            )
            exact_event = next(
                (item for item in parsed.events if item.identity == spec.event.event_id),
                None,
            )
            if parsed.blockers or exact_event is None or exact_event.exact_bytes != exact:
                raise GovernanceAdapterError("train progress event exact bytes were not preserved")
            desired = _snapshot_with_files(
                current,
                source_id=f"train-event:{spec.event.event_id}",
                replacements={PROGRESS_PATH: updated_progress},
            )
            operation = _suboperation_id(
                context.operation_id,
                f"train-event:{spec.event.event_id}:{spec.spec_digest}",
            )
            event_plan = harness_reconcile.plan_reconciliation(
                project_root=context.integration_worktree,
                git_common_dir=repo.common_dir,
                operation_id=operation,
                branch_base=current,
                latest_main=current,
                branch_candidate=desired,
            )
            label = f"train-event-{spec.iteration}-{spec.transition}-{spec.event_sha256}"
            reconcile_plans.append(event_plan)
            labels.append(label)
            blockers.extend(
                AdapterBlocker(item.code, item.message, item.subject)
                for item in event_plan.blockers
            )
            if not event_plan.blockers:
                current = _apply_previews_to_snapshot(
                    current,
                    event_plan,
                    source_id=f"after:{label}",
                )
            snapshots.append(current)
        except (GovernanceAdapterError, harness_progress.ProgressError) as exc:
            blockers.append(
                AdapterBlocker(
                    "train-progress-reconcile-blocked",
                    str(exc),
                    spec.event.event_id,
                )
            )

    if not blockers and readme_authority is not None:
        replacements: dict[str, bytes | None] = {}
        if readme_authority.root is not None:
            preview = harness_governance.preview_root_readme(
                current.as_mapping()[L0_PATH],
                authority=readme_authority.root,
            )
            if not preview.ready or preview.preview is None:
                blockers.extend(AdapterBlocker(item.code, item.message, item.subject) for item in preview.blockers)
            else:
                replacements[L0_PATH] = preview.preview
        for document in readme_authority.l1_documents:
            replacements[document.path] = document.content
        if not blockers and replacements:
            desired = _snapshot_with_files(current, source_id=f"readme:{readme_authority.authority_id}", replacements=replacements)
            operation = _suboperation_id(context.operation_id, f"readme:{readme_authority.authority_digest}")
            readme_plan = harness_reconcile.plan_reconciliation(
                project_root=context.integration_worktree,
                git_common_dir=repo.common_dir,
                operation_id=operation,
                branch_base=current,
                latest_main=current,
                branch_candidate=desired,
            )
            reconcile_plans.append(readme_plan)
            labels.append(f"readme-{readme_authority.authority_digest}")
            blockers.extend(AdapterBlocker(item.code, item.message, item.subject) for item in readme_plan.blockers)
            if not readme_plan.blockers:
                current = _apply_previews_to_snapshot(current, readme_plan, source_id="after:readme")
            snapshots.append(current)

    result = GovernanceExecutionPreview(
        ADAPTER_SCHEMA,
        context.operation_id,
        normalization,
        tuple(reconcile_plans),
        tuple(labels),
        tuple(snapshots),
        progress_specs,
        current,
        tuple(blockers),
    )
    if existing_execution is not None:
        execution, _execution_path = existing_execution
        expected_manifest = _execution_manifest(result, context, config_digest=config_digest)
        if _digest(execution.get("manifest")) != _digest(expected_manifest):
            raise GovernanceAdapterError("recomputed governance callback plan differs from durable execution manifest")
    return result


def _stage_paths(repo: harness_train.Repository, worktree: Path, paths: Sequence[str]) -> None:
    normalized = tuple(dict.fromkeys(_canonical_path(path) for path in paths))
    if not normalized:
        return
    result = _git(repo, ["add", "--all", "--", *normalized], cwd=worktree, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GovernanceAdapterError(f"cannot stage reconciled governance paths: {detail}")


def _unstaged_paths(repo: harness_train.Repository, worktree: Path) -> tuple[str, ...]:
    return _null_paths(_git(repo, ["diff", "--name-only", "-z"], cwd=worktree).stdout)


def _tree_diff_paths(repo: harness_train.Repository, before: str, after: str) -> tuple[str, ...]:
    return _null_paths(
        _git(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", before, after]).stdout
    )


def _snapshot_matches_root(
    snapshot: harness_reconcile.GovernanceSnapshot,
    worktree: Path,
) -> bool:
    try:
        actual = harness_reconcile.read_snapshot_from_root(worktree, source_id="resume-observation")
    except harness_reconcile.ReconcileError:
        return False
    return actual.as_mapping() == snapshot.as_mapping()


def _reconcile_journal_phase(
    repo: harness_train.Repository,
    plan: harness_reconcile.GovernanceReconcilePlan,
) -> str | None:
    path = harness_reconcile.journal_path(repo.common_dir, plan.operation_id)
    if not path.exists():
        return None
    durable = harness_reconcile.load_reconciliation_plan(repo.common_dir, plan.operation_id)
    if durable.plan_digest != plan.plan_digest or _digest(durable.manifest) != _digest(plan.manifest):
        raise GovernanceAdapterError(f"durable reconcile plan differs during resume: {plan.operation_id}")
    raw = _read_json(path)
    phase = raw.get("phase")
    if not isinstance(phase, str):
        raise GovernanceAdapterError(f"durable reconcile phase is invalid: {plan.operation_id}")
    return phase


def _resume_state(
    repo: harness_train.Repository,
    preview: GovernanceExecutionPreview,
    context: harness_train.GovernanceContext,
) -> GovernanceResumeState:
    worktree = Path(context.integration_worktree).resolve()
    actual_tree = _write_tree(repo, worktree)
    journal_path = execution_journal_path(repo.common_dir, context.operation_id)
    loaded = _load_execution_journal(repo.common_dir, context.operation_id)
    if loaded is None:
        blockers = () if actual_tree == context.pre_governance_tree else (
            AdapterBlocker(
                "resume-journal-missing",
                "current integration tree differs from original input without a durable execution journal",
            ),
        )
        return GovernanceResumeState(
            RESUME_STATE_SCHEMA,
            context.operation_id,
            context.pre_governance_tree,
            actual_tree,
            (context.pre_governance_tree,),
            (),
            "normalization",
            not blockers,
            blockers,
            str(journal_path),
        )

    journal, journal_path = loaded
    blockers: list[AdapterBlocker] = []
    expected_steps = ("normalization", *preview.reconciliation_labels)
    completed = tuple(journal["completed_steps"])
    if completed != expected_steps[: len(completed)] or len(completed) > len(expected_steps):
        blockers.append(AdapterBlocker("resume-step-order-invalid", "completed governance steps are not an exact prefix"))
    step_trees = dict(journal["step_trees"])
    recorded_allowed = tuple(journal["allowed_trees"])
    expected_allowed = [context.pre_governance_tree]
    for step in completed:
        tree = step_trees.get(step)
        if not isinstance(tree, str) or OID_RE.fullmatch(tree) is None:
            blockers.append(AdapterBlocker("resume-step-tree-missing", "completed governance step lacks an exact tree", step))
            continue
        if tree not in expected_allowed:
            expected_allowed.append(tree)
    if tuple(expected_allowed) != recorded_allowed:
        blockers.append(AdapterBlocker("resume-allowed-tree-set-invalid", "durable allowed intermediate tree set was changed"))

    # Every completed step must retain its own exact durable apply evidence.
    if "normalization" in completed:
        normalization_path = normalization_journal_path(repo.common_dir, context.operation_id, "post-merge")
        try:
            normalization_journal = _read_json(normalization_path)
            if (
                normalization_journal.get("schema_version") != NORMALIZATION_JOURNAL_SCHEMA
                or normalization_journal.get("plan_digest") != preview.normalization.plan_digest
                or normalization_journal.get("phase") != "APPLIED"
                or normalization_journal.get("result_tree") != step_trees.get("normalization")
                or _digest(normalization_journal.get("manifest")) != _digest(_normalization_payload(preview.normalization))
            ):
                raise GovernanceAdapterError("normalization journal identity/phase differs")
        except GovernanceAdapterError as exc:
            blockers.append(AdapterBlocker("resume-normalization-journal-invalid", str(exc)))
    for label, plan in zip(preview.reconciliation_labels, preview.reconciliations):
        if label not in completed:
            break
        try:
            if _reconcile_journal_phase(repo, plan) != "APPLIED":
                raise GovernanceAdapterError("reconcile journal is not APPLIED")
        except GovernanceAdapterError as exc:
            blockers.append(AdapterBlocker("resume-reconcile-journal-invalid", str(exc), label))

    allowed = list(recorded_allowed)
    next_index = len(completed)
    next_step = expected_steps[next_index] if next_index < len(expected_steps) else "complete"
    unstaged = _unstaged_paths(repo, worktree)
    if any(not _supported_path(path) for path in unstaged):
        blockers.append(
            AdapterBlocker(
                "resume-unstaged-implementation-change",
                "resume refuses unstaged non-governance paths: " + ", ".join(unstaged),
            )
        )

    # A process may die after an apply journal reaches APPLIED but before the
    # top-level execution journal records/stages that step.  Admit the observed
    # index only when the exact next reconcile journal and governance snapshot
    # prove it, and the tree differs from a prior allowed tree solely at managed
    # governance paths.
    if actual_tree not in allowed and next_step == "normalization":
        normalization_path = normalization_journal_path(repo.common_dir, context.operation_id, "post-merge")
        try:
            normalization_journal = _read_json(normalization_path)
            if (
                normalization_journal.get("schema_version") != NORMALIZATION_JOURNAL_SCHEMA
                or normalization_journal.get("plan_digest") != preview.normalization.plan_digest
                or normalization_journal.get("phase") != "APPLIED"
                or normalization_journal.get("result_tree") != actual_tree
                or _digest(normalization_journal.get("manifest")) != _digest(_normalization_payload(preview.normalization))
            ):
                raise GovernanceAdapterError("pending normalization journal/tree is not exact")
            allowed.append(actual_tree)
        except GovernanceAdapterError as exc:
            blockers.append(AdapterBlocker("resume-intermediate-tree-unrecognized", str(exc)))
    elif actual_tree not in allowed and next_step != "complete":
        reconcile_index = next_index - 1  # expected_steps starts with normalization
        plan = preview.reconciliations[reconcile_index]
        snapshot = preview.reconciliation_snapshots[reconcile_index]
        try:
            phase = _reconcile_journal_phase(repo, plan)
            governance_exact = _snapshot_matches_root(snapshot, worktree)
            bounded_tree = any(
                all(_supported_path(path) for path in _tree_diff_paths(repo, prior, actual_tree))
                for prior in allowed
            )
            if phase == "APPLIED" and governance_exact and bounded_tree:
                allowed.append(actual_tree)
            else:
                blockers.append(
                    AdapterBlocker(
                        "resume-intermediate-tree-unrecognized",
                        "current index tree is not a durable completed or exact pending governance step",
                    )
                )
        except GovernanceAdapterError as exc:
            blockers.append(AdapterBlocker("resume-intermediate-tree-unrecognized", str(exc)))
    elif actual_tree not in allowed:
        blockers.append(
            AdapterBlocker(
                "resume-intermediate-tree-unrecognized",
                "current index tree is outside the durable governance intermediate set",
            )
        )

    if unstaged:
        if next_step in {"normalization", "complete"}:
            blockers.append(AdapterBlocker("resume-unstaged-governance-unproven", "unstaged governance bytes lack a pending reconcile step"))
        else:
            reconcile_index = next_index - 1
            plan = preview.reconciliations[reconcile_index]
            snapshot = preview.reconciliation_snapshots[reconcile_index]
            try:
                if _reconcile_journal_phase(repo, plan) != "APPLIED" or not _snapshot_matches_root(snapshot, worktree):
                    raise GovernanceAdapterError("pending reconcile journal/snapshot is not exact")
            except GovernanceAdapterError as exc:
                blockers.append(AdapterBlocker("resume-unstaged-governance-unproven", str(exc)))
    if _unmerged_paths(repo, worktree):
        blockers.append(AdapterBlocker("resume-index-unmerged", "callback recovery does not accept an unmerged index"))
    if _untracked_and_ignored(repo, worktree):
        blockers.append(AdapterBlocker("resume-unowned-paths", "callback recovery found untracked/ignored paths"))
    if journal.get("phase") == "FAILED_NEEDS_RECONCILE":
        blockers.append(AdapterBlocker("resume-failed-needs-reconcile", str(journal.get("error"))))
    return GovernanceResumeState(
        RESUME_STATE_SCHEMA,
        context.operation_id,
        context.pre_governance_tree,
        actual_tree,
        tuple(dict.fromkeys(allowed)),
        completed,
        next_step,
        not blockers,
        tuple(blockers),
        str(journal_path),
    )


def _receipt_from_mapping(value: object) -> harness_train.GovernanceReceipt:
    if not isinstance(value, dict):
        raise GovernanceAdapterError("durable governance receipt is not an object")
    required = {
        "schema_version",
        "operation_id",
        "mode",
        "target_main",
        "principle_sha256",
        "candidate_digests",
        "input_tree",
        "result_tree",
        "evidence_ids",
        "evidence_digest",
    }
    if set(value) != required or not isinstance(value.get("candidate_digests"), list) or not isinstance(value.get("evidence_ids"), list):
        raise GovernanceAdapterError("durable governance receipt fields are invalid")
    return harness_train.GovernanceReceipt(
        schema_version=str(value["schema_version"]),
        operation_id=str(value["operation_id"]),
        mode=str(value["mode"]),  # type: ignore[arg-type]
        target_main=str(value["target_main"]),
        principle_sha256=str(value["principle_sha256"]),
        candidate_digests=tuple(str(item) for item in value["candidate_digests"]),
        input_tree=str(value["input_tree"]),
        result_tree=str(value["result_tree"]),
        evidence_ids=tuple(str(item) for item in value["evidence_ids"]),
        evidence_digest=str(value["evidence_digest"]),
    )


class MergeTrainGovernanceAdapter:
    """Callable governance boundary bound to one exact train plan."""

    def __init__(
        self,
        train_plan: harness_train.IntegrationPreparePlan,
        *,
        readme_authority: ReadmeRebuildAuthority | None = None,
        principle_approvals: Mapping[str, harness_governance.PrincipleApproval] | None = None,
        principle_leases: Mapping[str, harness_reconcile.GlobalPrincipleLease] | None = None,
        failpoint: Failpoint | None = None,
    ) -> None:
        _validate_train_plan(train_plan)
        _validate_readme_authority(readme_authority)
        self.train_plan = train_plan
        self.readme_authority = readme_authority
        self.principle_approvals = dict(principle_approvals or {})
        self.principle_leases = dict(principle_leases or {})
        self.failpoint = failpoint

    def preview(self, context: harness_train.GovernanceContext) -> GovernanceExecutionPreview:
        return _plan_execution(
            self.train_plan,
            context,
            readme_authority=self.readme_authority,
            principle_approvals=self.principle_approvals,
            principle_leases=self.principle_leases,
        )

    def inspect_resume(self, context: harness_train.GovernanceContext) -> GovernanceResumeState:
        preview = self.preview(context)
        if not preview.ready:
            return GovernanceResumeState(
                RESUME_STATE_SCHEMA,
                context.operation_id,
                context.pre_governance_tree,
                _write_tree(harness_train.open_repository(self.train_plan.project_root), Path(context.integration_worktree)),
                (context.pre_governance_tree,),
                (),
                "blocked",
                False,
                preview.blockers,
                str(execution_journal_path(self.train_plan.git_common_dir, context.operation_id)),
            )
        repo = harness_train.open_repository(self.train_plan.project_root)
        return _resume_state(repo, preview, context)

    def _trigger(self, phase: str) -> None:
        if self.failpoint is not None:
            self.failpoint(phase)

    def __call__(self, context: harness_train.GovernanceContext) -> harness_train.GovernanceReceipt:
        preview = self.preview(context)
        if not preview.ready:
            details = "; ".join(
                f"{item.code}{f'[{item.subject}]' if item.subject else ''}: {item.message}"
                for item in preview.blockers
            )
            raise GovernanceAdapterError("governance reconciliation is blocked: " + details)
        repo = harness_train.open_repository(self.train_plan.project_root)
        worktree = Path(context.integration_worktree).resolve()
        journal, execution_path, _resumed = _ensure_execution_journal(
            repo,
            preview,
            context,
            config_digest=_digest(
                _adapter_config_payload(
                    candidate_authorities=preview.normalization.candidate_authorities,
                    readme_authority=self.readme_authority,
                    principle_approvals=self.principle_approvals,
                    principle_leases=self.principle_leases,
                )
            ),
        )
        state = _resume_state(repo, preview, context)
        if not state.resumable:
            raise GovernanceAdapterError(
                "governance callback intermediate state is not resumable: "
                + "; ".join(f"{item.code}: {item.message}" for item in state.blockers)
            )
        if journal.get("phase") == "APPLIED":
            receipt = _receipt_from_mapping(journal.get("receipt"))
            blockers = harness_train.governance_receipt_gate(
                receipt,
                context,
                actual_result_tree=_write_tree(repo, worktree),
            )
            if blockers:
                raise GovernanceAdapterError("durable governance receipt is stale: " + ", ".join(item.code for item in blockers))
            return receipt

        completed = list(journal["completed_steps"])
        evidence_ids: list[str] = [
            f"candidate-authority:{item.authority_digest}"
            for item in preview.normalization.candidate_authorities
        ]
        evidence_ids.extend(
            f"train-progress:{item.transition}:{item.event.event_id}:"
            f"{item.event_sha256}:{item.spec_digest}"
            for item in preview.progress_events
        )
        if "normalization" not in completed:
            normalization = apply_premerge_normalization(
                preview.normalization,
                accepted_plan_digest=preview.normalization.plan_digest,
            )
            journal = _record_execution_step(
                execution_path,
                journal,
                step="normalization",
                tree=normalization.result_tree,
            )
            completed = list(journal["completed_steps"])
            self._trigger("after-normalization")
        normalization_path = normalization_journal_path(repo.common_dir, context.operation_id, "post-merge")
        if not normalization_path.is_file():
            raise GovernanceAdapterError("normalization evidence journal is missing")
        evidence_ids.append(
            f"normalize:{preview.normalization.plan_digest}:{_sha256(normalization_path.read_bytes())}"
        )

        for index, (label, plan) in enumerate(zip(preview.reconciliation_labels, preview.reconciliations)):
            if label in completed:
                journal_path = harness_reconcile.journal_path(repo.common_dir, plan.operation_id)
                if _reconcile_journal_phase(repo, plan) != "APPLIED" or not journal_path.is_file():
                    raise GovernanceAdapterError(f"completed reconcile evidence is missing/stale: {label}")
                evidence_ids.append(f"reconcile:{label}:{plan.plan_digest}:{_sha256(journal_path.read_bytes())}")
                continue
            result = harness_reconcile.apply_reconciliation(plan, accept_plan_digest=plan.plan_digest)
            if result.phase != "APPLIED":
                raise GovernanceAdapterError(f"reconcile apply did not complete: {label}")
            _stage_paths(repo, worktree, result.changed_paths)
            reconcile_journal = Path(result.journal_path)
            if not reconcile_journal.is_file():
                raise GovernanceAdapterError(f"reconcile journal is missing after apply: {label}")
            step_tree = _write_tree(repo, worktree)
            journal = _record_execution_step(execution_path, journal, step=label, tree=step_tree)
            completed = list(journal["completed_steps"])
            evidence_ids.append(f"reconcile:{label}:{plan.plan_digest}:{_sha256(reconcile_journal.read_bytes())}")
            self._trigger(f"after-reconcile:{label}")
            if index == 0:
                self._trigger("after-first-reconcile")
        if _unmerged_paths(repo, worktree):
            raise GovernanceAdapterError("governance adapter left unmerged index entries")
        if _untracked_and_ignored(repo, worktree):
            raise GovernanceAdapterError("governance adapter left unowned paths")
        result_tree = _write_tree(repo, worktree)
        # Rebind the receipt to still-live main/candidate refs after every
        # filesystem and index mutation; a direct ref writer must invalidate
        # the callback rather than leave apparently valid evidence behind.
        _validate_train_plan(self.train_plan)
        _validate_candidates(repo, self.train_plan)
        actual = harness_reconcile.read_snapshot_from_root(worktree, source_id=f"result:{result_tree}")
        actual_files = actual.as_mapping()
        expected_files = preview.final_snapshot.as_mapping()
        if actual_files != expected_files:
            raise GovernanceAdapterError("applied governance files differ from semantic preview")
        if self.readme_authority is not None:
            evidence_ids.append(f"readme-authority:{self.readme_authority.authority_digest}")
        receipt = harness_train.build_governance_receipt(
            context,
            mode="applied",
            result_tree=result_tree,
            evidence_ids=tuple(evidence_ids),
        )
        journal = dict(journal)
        allowed = list(journal["allowed_trees"])
        if result_tree not in allowed:
            allowed.append(result_tree)
        journal.update(
            {
                "phase": "APPLIED",
                "allowed_trees": allowed,
                "receipt": receipt.as_dict(),
                "error": None,
            }
        )
        _atomic_json(execution_path, journal)
        return receipt


def build_governance_callback(
    train_plan: harness_train.IntegrationPreparePlan,
    *,
    readme_authority: ReadmeRebuildAuthority | None = None,
    principle_approvals: Mapping[str, harness_governance.PrincipleApproval] | None = None,
    principle_leases: Mapping[str, harness_reconcile.GlobalPrincipleLease] | None = None,
    failpoint: Failpoint | None = None,
) -> MergeTrainGovernanceAdapter:
    return MergeTrainGovernanceAdapter(
        train_plan,
        readme_authority=readme_authority,
        principle_approvals=principle_approvals,
        principle_leases=principle_leases,
        failpoint=failpoint,
    )


def inspect_governance_resume(
    train_plan: harness_train.IntegrationPreparePlan,
    context: harness_train.GovernanceContext,
    *,
    readme_authority: ReadmeRebuildAuthority | None = None,
    principle_approvals: Mapping[str, harness_governance.PrincipleApproval] | None = None,
    principle_leases: Mapping[str, harness_reconcile.GlobalPrincipleLease] | None = None,
) -> GovernanceResumeState:
    """Return the exact durable intermediate-tree set a train may admit."""

    return build_governance_callback(
        train_plan,
        readme_authority=readme_authority,
        principle_approvals=principle_approvals,
        principle_leases=principle_leases,
    ).inspect_resume(context)


def resume_governance_callback(
    train_plan: harness_train.IntegrationPreparePlan,
    context: harness_train.GovernanceContext,
    *,
    readme_authority: ReadmeRebuildAuthority | None = None,
    principle_approvals: Mapping[str, harness_governance.PrincipleApproval] | None = None,
    principle_leases: Mapping[str, harness_reconcile.GlobalPrincipleLease] | None = None,
) -> harness_train.GovernanceReceipt:
    """Resume only a state admitted by ``inspect_governance_resume``."""

    adapter = build_governance_callback(
        train_plan,
        readme_authority=readme_authority,
        principle_approvals=principle_approvals,
        principle_leases=principle_leases,
    )
    state = adapter.inspect_resume(context)
    if not state.resumable:
        raise GovernanceAdapterError(
            "governance callback resume is blocked: "
            + "; ".join(f"{item.code}: {item.message}" for item in state.blockers)
        )
    return adapter(context)


def build_conflict_normalizer(
    train_plan: harness_train.IntegrationPreparePlan,
):
    """Return the exact digest-bound hook consumed by ``harness_train``.

    The hook accepts only merge conflicts wholly contained in supported
    governance paths.  It restores latest-main governance bytes through the
    normalization journal so the ordinary callback can then perform semantic
    principle/progress/README reconciliation.
    """

    def normalize(observed_plan: harness_train.IntegrationPreparePlan) -> NormalizationResult:
        if (
            observed_plan.plan_digest != train_plan.plan_digest
            or observed_plan.operation_id != train_plan.operation_id
        ):
            raise GovernanceAdapterError("train conflict hook received a different integration plan")
        plan = plan_premerge_normalization(train_plan, phase="merge-conflict")
        return apply_premerge_normalization(plan, accepted_plan_digest=plan.plan_digest)

    return normalize


__all__ = [
    "ADAPTER_SCHEMA",
    "AdapterBlocker",
    "DerivedReadme",
    "GovernanceAdapterError",
    "GovernanceExecutionPreview",
    "GovernanceResumeState",
    "InjectedGovernanceCrash",
    "MergeTrainGovernanceAdapter",
    "NormalizationResult",
    "PreMergeNormalizationPlan",
    "ProgressEvidenceResolution",
    "ReadmeRebuildAuthority",
    "TrainProgressEventSpec",
    "TrainProgressMaterialization",
    "apply_premerge_normalization",
    "build_governance_callback",
    "build_conflict_normalizer",
    "build_readme_rebuild_authority",
    "execution_journal_path",
    "inspect_governance_resume",
    "materialize_train_progress_events",
    "normalization_journal_path",
    "normalization_plan_digest",
    "plan_premerge_normalization",
    "resume_governance_callback",
]
