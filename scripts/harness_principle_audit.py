#!/usr/bin/env python3
"""Durable global-principle impact audits for Harness Lite lifecycle-v2.

The committed ``refs/heads/main:harness/principle.md`` blob is the only
current principle authority.  An allocation metadata blob preserves each
iteration's older ``principle_sha256``.  This module compares those identities,
binds an explicit per-iteration disposition to exact committed PRD/SPEC
authority, and persists only operational receipts below the Git common
directory.

Planning is read-only.  Applying does not edit ``harness/``, create commits,
move refs, create worktrees, or write progress.  Instead, the receipt exposes a
deterministic CHECKPOINT event specification that a coordinating transaction
may append separately through :mod:`harness_progress`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from typing import Callable, Mapping, Sequence
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import project_harness as governance_core  # noqa: E402
from harness_progress import deterministic_event_id  # noqa: E402


PLAN_SCHEMA = "harness-lite.principle-impact-audit-plan/v2"
RECEIPT_SCHEMA = "harness-lite.principle-impact-audit-receipt/v2"
JOURNAL_SCHEMA = "harness-lite.principle-impact-audit-journal/v2"
RESULT_SCHEMA = "harness-lite.principle-impact-audit-result/v2"
GATE_SCHEMA = "harness-lite.principle-impact-gate/v2"
PROGRESS_SPEC_SCHEMA = "harness-lite.progress-checkpoint-spec/v1"

MAIN_REF = "refs/heads/main"
PRINCIPLE_PATH = "harness/principle.md"
REGISTRY_PARTS = ("project-harness", "principle-audit", "v2")

DISPOSITION_NO_IMPACT = "no-impact"
DISPOSITION_IMPACT = "impact-requires-reapproval"
DISPOSITION_REAPPROVED = "reapproved-current-baseline"
DISPOSITIONS = frozenset(
    {DISPOSITION_NO_IMPACT, DISPOSITION_IMPACT, DISPOSITION_REAPPROVED}
)

OPERATION_RE = re.compile(r"OP-[0-9a-f]{32}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}")
AFFECTED_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9._:\-]{1,127}")
V2_ALLOCATION_RE = re.compile(r"refs/project-harness/v2/allocations/([0-9]{3,})")
RECEIPT_FILE_RE = re.compile(r"G([0-9]{6,})-(OP-[0-9a-f]{32})\.json")

MAX_AUTHORITY_BYTES = 2 * 1024 * 1024
MAX_ALLOCATION_BYTES = 256 * 1024
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_IDS = 48

TRAIN_JOURNAL_SCHEMA = "harness-lite.train-journal/v1"
TRAIN_ADVANCE_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "operation_id",
        "plan_digest",
        "accepted_plan_digest",
        "accepted_integrated_evidence_digest",
        "confirmation_id",
        "expected_main",
        "integrated_commit",
        "project_root",
        "integration_worktree",
        "ref_updates",
        "status",
        "pushed",
    }
)

PRD_APPROVED_STATUSES = frozenset({"已批准", "实施中", "待验收", "已验收"})
SPEC_APPROVED_STATUSES = frozenset({"已批准", "实施中", "已完成"})

EXCLUSIONS = (
    "no governance document write",
    "no progress write",
    "no commit",
    "no merge",
    "no push",
    "no ref update",
    "no worktree mutation",
)


class PrincipleAuditError(RuntimeError):
    """Raised when an impact audit cannot prove an exact safe transition."""


class SimulatedCrash(BaseException):
    """Fault-injection signal deliberately leaving resumable durable state."""


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Repository:
    git: str
    root: Path
    common_dir: Path


@dataclass(frozen=True)
class PrincipleAuditDecision:
    """Explicit, opaque evidence supplied for one open iteration.

    IDs are deliberately treated as opaque tokens.  This module binds them but
    never interprets them as prose, approval content, or caller booleans.
    """

    iteration: str
    authority_ref: str
    disposition: str
    affected_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    authorization_ids: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        iteration: str,
        authority_ref: str,
        disposition: str,
        affected_ids: Sequence[str],
        evidence_ids: Sequence[str],
        authorization_ids: Sequence[str],
    ) -> "PrincipleAuditDecision":
        return _normalize_decision(
            cls(
                iteration=iteration,
                authority_ref=authority_ref,
                disposition=disposition,
                affected_ids=tuple(affected_ids),
                evidence_ids=tuple(evidence_ids),
                authorization_ids=tuple(authorization_ids),
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "iteration": self.iteration,
            "authority_ref": self.authority_ref,
            "disposition": self.disposition,
            "affected_ids": list(self.affected_ids),
            "evidence_ids": list(self.evidence_ids),
            "authorization_ids": list(self.authorization_ids),
        }


@dataclass(frozen=True)
class OpenIteration:
    iteration: str
    allocation_ref: str
    allocation_object: str
    allocation_base_ref: str
    allocation_base_commit: str
    principle_base_sha256: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PrincipleImpactAuditPlan:
    plan_digest: str
    manifest: dict[str, object]
    blockers: tuple[Blocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def operation_id(self) -> str:
        return str(self.manifest["operation_id"])

    @property
    def iteration(self) -> str:
        return str(self.manifest["iteration"])

    @property
    def project_root(self) -> str:
        return str(self.manifest["project_root"])

    @property
    def git_common_dir(self) -> str:
        return str(self.manifest["git_common_dir"])

    @property
    def current_principle_sha256(self) -> str:
        latest = self.manifest["latest_main"]
        assert isinstance(latest, dict)
        return str(latest["principle_sha256"])

    @property
    def disposition(self) -> str:
        decision = self.manifest["decision"]
        assert isinstance(decision, dict)
        return str(decision["disposition"])

    @property
    def generation(self) -> int:
        chain = self.manifest["audit_chain"]
        assert isinstance(chain, dict)
        return int(chain["generation"])

    @property
    def supersedes(self) -> str | None:
        chain = self.manifest["audit_chain"]
        assert isinstance(chain, dict)
        value = chain["supersedes"]
        return str(value) if value is not None else None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLAN_SCHEMA,
            "operation_id": self.operation_id,
            "iteration": self.iteration,
            "project_root": self.project_root,
            "plan_digest": self.plan_digest,
            "disposition": self.disposition,
            "generation": self.generation,
            "supersedes": self.supersedes,
            "clears_drift": bool(_nested(self.manifest, "derived", "clears_drift")),
            "progress_checkpoint": self.manifest["progress_checkpoint"],
            "blocking_reasons": [item.as_dict() for item in self.blockers],
            "exclusions": list(EXCLUSIONS),
            "pushed": False,
        }


@dataclass(frozen=True)
class PrincipleImpactAuditReceipt:
    receipt_digest: str
    payload: dict[str, object]

    @property
    def operation_id(self) -> str:
        return str(self.payload["operation_id"])

    @property
    def plan_digest(self) -> str:
        return str(self.payload["plan_digest"])

    @property
    def iteration(self) -> str:
        return str(self.payload["iteration"])

    @property
    def disposition(self) -> str:
        return str(self.payload["disposition"])

    @property
    def generation(self) -> int:
        return int(self.payload["generation"])

    @property
    def supersedes(self) -> str | None:
        value = self.payload["supersedes"]
        return str(value) if value is not None else None

    @property
    def clears_drift(self) -> bool:
        return bool(self.payload["clears_drift"])

    @property
    def current_principle_sha256(self) -> str:
        latest = self.payload["latest_main"]
        assert isinstance(latest, dict)
        return str(latest["principle_sha256"])

    @property
    def progress_checkpoint(self) -> dict[str, object]:
        value = self.payload["progress_checkpoint"]
        assert isinstance(value, dict)
        return dict(value)

    @property
    def progress_evidence_refs(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.progress_checkpoint["evidence_refs"])  # type: ignore[index]

    def as_dict(self) -> dict[str, object]:
        return {**self.payload, "receipt_digest": self.receipt_digest}


@dataclass(frozen=True)
class PrincipleAuditApplyResult:
    receipt: PrincipleImpactAuditReceipt
    journal_path: str
    receipt_path: str
    phase: str
    resumed: bool
    idempotent: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESULT_SCHEMA,
            "operation_id": self.receipt.operation_id,
            "iteration": self.receipt.iteration,
            "plan_digest": self.receipt.plan_digest,
            "receipt_digest": self.receipt.receipt_digest,
            "disposition": self.receipt.disposition,
            "generation": self.receipt.generation,
            "supersedes": self.receipt.supersedes,
            "clears_drift": self.receipt.clears_drift,
            "phase": self.phase,
            "resumed": self.resumed,
            "idempotent": self.idempotent,
            "journal_path": self.journal_path,
            "receipt_path": self.receipt_path,
            "progress_checkpoint": self.receipt.progress_checkpoint,
            "exclusions": list(EXCLUSIONS),
            "pushed": False,
        }


@dataclass(frozen=True)
class PrincipleAuditGate:
    iteration: str
    allowed: bool
    drift: bool
    allocation_principle_sha256: str
    current_principle_sha256: str
    disposition: str | None
    receipt_digest: str | None
    blockers: tuple[str, ...]
    next_gate: str

    def as_dict(self) -> dict[str, object]:
        return {"schema_version": GATE_SCHEMA, **asdict(self)}


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _nested(value: Mapping[str, object], first: str, second: str) -> object:
    child = value.get(first)
    if not isinstance(child, Mapping):
        raise PrincipleAuditError(f"audit payload lacks {first}")
    return child.get(second)


def _validate_operation(value: object) -> str:
    if not isinstance(value, str) or OPERATION_RE.fullmatch(value) is None:
        raise PrincipleAuditError("operation_id must use OP- plus 32 lowercase hexadecimal characters")
    return value


def _validate_iteration(value: object) -> str:
    if not isinstance(value, str) or ITERATION_RE.fullmatch(value) is None:
        raise PrincipleAuditError("iteration must be a canonical NNN identity")
    if value != f"{int(value):03d}" or int(value) < 1:
        raise PrincipleAuditError("iteration must be a canonical NNN identity")
    return value


def _validate_oid(value: object, label: str) -> str:
    if not isinstance(value, str) or OID_RE.fullmatch(value) is None:
        raise PrincipleAuditError(f"{label} must be a full lowercase Git object ID")
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise PrincipleAuditError(f"{label} must be a SHA-256 digest")
    return value


def _validate_ref(value: object, label: str = "authority_ref") -> str:
    if not isinstance(value, str):
        raise PrincipleAuditError(f"{label} must be a full Git ref")
    if (
        not value.startswith("refs/")
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(character in value for character in "~^:?*[\\")
        or any(part in {"", ".", ".."} or part.endswith(".lock") for part in value.split("/"))
    ):
        raise PrincipleAuditError(f"{label} must be a canonical full Git ref")
    return value


def _normalize_ids(
    values: Sequence[str],
    *,
    label: str,
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise PrincipleAuditError(f"{label} must be a sequence of IDs")
    if len(values) > MAX_IDS:
        raise PrincipleAuditError(f"{label} exceeds the safe ID count")
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or pattern.fullmatch(raw) is None:
            raise PrincipleAuditError(f"{label} contains a non-opaque or malformed ID")
        if raw in seen:
            raise PrincipleAuditError(f"{label} contains a duplicate ID")
        seen.add(raw)
        result.append(raw)
    return tuple(result)


def _normalize_decision(value: PrincipleAuditDecision) -> PrincipleAuditDecision:
    if not isinstance(value, PrincipleAuditDecision):
        raise TypeError("decision must be PrincipleAuditDecision")
    iteration = _validate_iteration(value.iteration)
    authority_ref = _validate_ref(value.authority_ref)
    disposition = value.disposition.strip() if isinstance(value.disposition, str) else ""
    if disposition not in DISPOSITIONS:
        raise PrincipleAuditError("unsupported principle impact disposition")
    affected = _normalize_ids(
        value.affected_ids, label="affected_ids", pattern=AFFECTED_ID_RE
    )
    evidence = _normalize_ids(value.evidence_ids, label="evidence_ids", pattern=OPAQUE_ID_RE)
    authorizations = _normalize_ids(
        value.authorization_ids, label="authorization_ids", pattern=OPAQUE_ID_RE
    )
    return PrincipleAuditDecision(
        iteration,
        authority_ref,
        disposition,
        affected,
        evidence,
        authorizations,
    )


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ):
        environment.pop(key, None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _git(
    repo: Repository,
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [repo.git, "-C", str(repo.root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PrincipleAuditError(
            f"git {' '.join(arguments)} failed: {detail or result.returncode}"
        )
    return result


def _open_repository(project_root: Path | str) -> Repository:
    git = shutil.which("git")
    if not git:
        raise PrincipleAuditError("git is required")
    supplied = Path(project_root).absolute().resolve()
    if not supplied.is_dir():
        raise PrincipleAuditError(f"project_root is not a directory: {supplied}")
    probe = subprocess.run(
        [git, "-C", str(supplied), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
    )
    if probe.returncode != 0:
        raise PrincipleAuditError("project_root is not a Git worktree")
    root = Path(probe.stdout.decode("utf-8", errors="strict").strip()).resolve()
    if os.path.normcase(str(root)) != os.path.normcase(str(supplied)):
        raise PrincipleAuditError(f"project_root must name the exact worktree root: {root}")
    temporary = Repository(git=git, root=root, common_dir=root)
    common_result = _git(temporary, ["rev-parse", "--git-common-dir"])
    raw = Path(common_result.stdout.decode("utf-8", errors="strict").strip())
    common = (raw if raw.is_absolute() else root / raw).resolve()
    if not common.is_dir() or not (common / "objects").is_dir():
        raise PrincipleAuditError(f"Git common directory is invalid: {common}")
    return Repository(git=git, root=root, common_dir=common)


def _resolve_ref(repo: Repository, reference: str) -> str | None:
    ref = _validate_ref(reference, "reference")
    result = _git(repo, ["rev-parse", "--verify", "--quiet", ref], check=False)
    if result.returncode != 0:
        if result.stdout.strip():
            raise PrincipleAuditError(f"cannot resolve ref: {ref}")
        return None
    return _validate_oid(result.stdout.decode("ascii", errors="strict").strip(), ref)


def _object_type(repo: Repository, oid: str) -> str | None:
    result = _git(repo, ["cat-file", "-t", oid], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="strict").strip()


def _commit_tree(repo: Repository, commit: str) -> str:
    result = _git(repo, ["rev-parse", f"{_validate_oid(commit, 'commit')}^{{tree}}"])
    return _validate_oid(result.stdout.decode("ascii", errors="strict").strip(), "commit tree")


def _blob_at(repo: Repository, commit: str, path: str) -> tuple[str, bytes]:
    if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        raise PrincipleAuditError(f"unsafe authority path: {path}")
    result = _git(repo, ["rev-parse", f"{commit}:{path}"], check=False)
    if result.returncode != 0:
        raise PrincipleAuditError(f"committed authority file is missing: {path}")
    blob = _validate_oid(result.stdout.decode("ascii", errors="strict").strip(), f"blob {path}")
    if _object_type(repo, blob) != "blob":
        raise PrincipleAuditError(f"authority path is not a blob: {path}")
    raw = _git(repo, ["cat-file", "blob", blob]).stdout
    if len(raw) > MAX_AUTHORITY_BYTES:
        raise PrincipleAuditError(f"authority file exceeds the safe size: {path}")
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PrincipleAuditError(f"authority file is not UTF-8: {path}") from exc
    return blob, raw


def _is_ancestor(repo: Repository, ancestor: str, descendant: str) -> bool:
    return (
        _git(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode
        == 0
    )


def _allocation_identity(repo: Repository, iteration: str) -> dict[str, object]:
    number = _validate_iteration(iteration)
    allocation_ref = f"refs/project-harness/v2/allocations/{number}"
    base_ref = f"refs/project-harness/v2/iterations/{number}/base"
    allocation_object = _resolve_ref(repo, allocation_ref)
    base_commit = _resolve_ref(repo, base_ref)
    if allocation_object is None or base_commit is None:
        raise PrincipleAuditError(f"PRD-{number} lacks a complete v2 allocation/base identity")
    if _object_type(repo, allocation_object) != "blob":
        raise PrincipleAuditError(f"allocation ref does not name metadata blob: {allocation_ref}")
    if _object_type(repo, base_commit) != "commit":
        raise PrincipleAuditError(f"allocation base ref does not name a commit: {base_ref}")
    try:
        metadata = governance_core.read_allocation_metadata(
            repo.git, repo.root, allocation_object
        )
    except governance_core.HarnessError as exc:
        raise PrincipleAuditError(f"allocation metadata is invalid: {exc}") from exc
    if metadata.get("iteration") != number or metadata.get("base_commit") != base_commit:
        raise PrincipleAuditError("allocation metadata and immutable base ref disagree")
    governance_commit = _validate_oid(metadata.get("governance_commit"), "allocation governance commit")
    governance_tree = _validate_oid(metadata.get("governance_tree"), "allocation governance tree")
    if _object_type(repo, governance_commit) != "commit" or _commit_tree(repo, governance_commit) != governance_tree:
        raise PrincipleAuditError("allocation governance commit/tree identity is invalid")
    _, base_principle = _blob_at(repo, governance_commit, PRINCIPLE_PATH)
    base_principle_sha = _sha256(base_principle)
    if base_principle_sha != metadata.get("principle_sha256"):
        raise PrincipleAuditError("allocation principle hash differs from its committed governance blob")
    raw = _git(repo, ["cat-file", "blob", allocation_object]).stdout
    if len(raw) > MAX_ALLOCATION_BYTES:
        raise PrincipleAuditError("allocation metadata exceeds the safe size")
    return {
        "allocation_ref": allocation_ref,
        "allocation_object": allocation_object,
        "allocation_metadata_sha256": _sha256(raw),
        "allocation_base_ref": base_ref,
        "allocation_base_commit": base_commit,
        "allocation_source_ref": str(metadata["base_branch"]),
        "allocation_governance_ref": str(metadata["governance_ref"]),
        "allocation_governance_commit": governance_commit,
        "allocation_governance_tree": governance_tree,
        "principle_base_sha256": base_principle_sha,
    }


def _latest_main_identity(repo: Repository) -> dict[str, object]:
    commit = _resolve_ref(repo, MAIN_REF)
    if commit is None or _object_type(repo, commit) != "commit":
        raise PrincipleAuditError("refs/heads/main must resolve to a committed authority")
    tree = _commit_tree(repo, commit)
    principle_blob, principle_raw = _blob_at(repo, commit, PRINCIPLE_PATH)
    return {
        "ref": MAIN_REF,
        "commit": commit,
        "tree": tree,
        "principle_path": PRINCIPLE_PATH,
        "principle_blob": principle_blob,
        "principle_sha256": _sha256(principle_raw),
    }


def _authority_identity(
    repo: Repository,
    *,
    iteration: str,
    authority_ref: str,
    allocation_base_commit: str,
) -> dict[str, object]:
    number = _validate_iteration(iteration)
    reference = _validate_ref(authority_ref)
    commit = _resolve_ref(repo, reference)
    if commit is None or _object_type(repo, commit) != "commit":
        raise PrincipleAuditError(f"authority ref must resolve to a commit: {reference}")
    if not _is_ancestor(repo, allocation_base_commit, commit):
        raise PrincipleAuditError("authority commit does not descend from the allocation base")
    tree = _commit_tree(repo, commit)
    prd_path = f"harness/iterations/{number}/prd-{number}.md"
    spec_path = f"harness/iterations/{number}/spec-{number}.md"
    prd_blob, prd_raw = _blob_at(repo, commit, prd_path)
    spec_blob, spec_raw = _blob_at(repo, commit, spec_path)
    prd_text = prd_raw.decode("utf-8-sig")
    spec_text = spec_raw.decode("utf-8-sig")
    if not governance_core.has_owner_marker(prd_text) or not governance_core.has_owner_marker(spec_text):
        raise PrincipleAuditError("committed PRD/SPEC authority lacks a Harness ownership marker")
    prd_status = governance_core.parse_status(prd_text, "状态") or ""
    spec_status = governance_core.parse_status(spec_text, "状态") or ""
    prd_source = governance_core.bullet_value(prd_text, "批准依据") or ""
    spec_source = governance_core.bullet_value(spec_text, "批准依据") or ""
    implementation_source = governance_core.bullet_value(spec_text, "实施授权") or ""
    principle_field = governance_core.bullet_value(prd_text, "principle_base_hash") or ""
    represented_ids = sorted(
        set(
            re.findall(
                rf"(?:P-[0-9]{{3,}}|R-{re.escape(number)}-[0-9]{{2,}}|AC-{re.escape(number)}-[0-9]{{2,}})",
                prd_text + "\n" + spec_text,
            )
        )
    )
    return {
        "ref": reference,
        "commit": commit,
        "tree": tree,
        "prd_path": prd_path,
        "prd_blob": prd_blob,
        "prd_sha256": _sha256(prd_raw),
        "prd_status": prd_status,
        "prd_approval_source_sha256": _sha256(prd_source.encode("utf-8")),
        "prd_approved": prd_status in PRD_APPROVED_STATUSES
        and governance_core.explicit_user_baseline_approval(prd_source, f"PRD-{number}"),
        "prd_principle_base_hash": principle_field,
        "spec_path": spec_path,
        "spec_blob": spec_blob,
        "spec_sha256": _sha256(spec_raw),
        "spec_status": spec_status,
        "spec_approval_source_sha256": _sha256(spec_source.encode("utf-8")),
        "spec_approved": spec_status in SPEC_APPROVED_STATUSES
        and governance_core.explicit_user_baseline_approval(spec_source, f"SPEC-{number}"),
        "implementation_authorization_source_sha256": _sha256(
            implementation_source.encode("utf-8")
        ),
        "implementation_authorized": governance_core.explicit_user_implementation_authorization(
            implementation_source
        ),
        "represented_ids": represented_ids,
    }


def _progress_checkpoint_spec(
    *,
    operation_id: str,
    iteration: str,
    allocation: Mapping[str, object],
    latest_main: Mapping[str, object],
    authority: Mapping[str, object],
    decision: PrincipleAuditDecision,
    generation: int,
    supersedes: str | None,
) -> dict[str, object]:
    current_hash = str(latest_main["principle_sha256"])
    key = f"principle-impact-audit:{current_hash}"
    evidence_refs = [
        f"principle:{current_hash}",
        f"allocation-object:{allocation['allocation_object']}",
        f"authority-commit:{authority['commit']}",
        *(f"audit-evidence:{item}" for item in decision.evidence_ids),
        *(f"audit-authorization:{item}" for item in decision.authorization_ids),
    ]
    if supersedes is not None:
        evidence_refs.append(f"audit-supersedes:{supersedes}")
    if len(evidence_refs) > 63:
        raise PrincipleAuditError("progress evidence refs exceed the v2 event limit")
    return {
        "schema_version": PROGRESS_SPEC_SCHEMA,
        "event_id": deterministic_event_id(
            iteration=iteration,
            operation_id=operation_id,
            scope="principle",
            event_type="CHECKPOINT",
            event_key=key,
        ),
        "iteration": iteration,
        "scope": "principle",
        "event_type": "CHECKPOINT",
        "event_key": key,
        "operation_id": operation_id,
        "source_ref": authority["ref"],
        "source_commit": authority["commit"],
        "evidence_refs": evidence_refs,
        "summary_code": f"principle-impact-audit:g{generation}:{decision.disposition}",
        "requires_session_id": True,
        "requires_occurred_at": True,
        "requires_causal_parent": True,
        "write_progress": False,
    }


def _decision_from_manifest(value: object) -> PrincipleAuditDecision:
    expected = {
        "iteration",
        "authority_ref",
        "disposition",
        "affected_ids",
        "evidence_ids",
        "authorization_ids",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise PrincipleAuditError("audit decision fields are invalid")
    for key in ("affected_ids", "evidence_ids", "authorization_ids"):
        if not isinstance(value.get(key), list):
            raise PrincipleAuditError(f"audit decision {key} must be an array")
    return PrincipleAuditDecision.create(
        iteration=value["iteration"],  # type: ignore[arg-type]
        authority_ref=value["authority_ref"],  # type: ignore[arg-type]
        disposition=value["disposition"],  # type: ignore[arg-type]
        affected_ids=value["affected_ids"],  # type: ignore[arg-type]
        evidence_ids=value["evidence_ids"],  # type: ignore[arg-type]
        authorization_ids=value["authorization_ids"],  # type: ignore[arg-type]
    )


def _derive_plan_manifest(
    repo: Repository,
    *,
    operation_id: str,
    decision: PrincipleAuditDecision,
) -> tuple[dict[str, object], tuple[Blocker, ...]]:
    operation = _validate_operation(operation_id)
    normalized = _normalize_decision(decision)
    allocation = _allocation_identity(repo, normalized.iteration)
    latest_main = _latest_main_identity(repo)
    authority = _authority_identity(
        repo,
        iteration=normalized.iteration,
        authority_ref=normalized.authority_ref,
        allocation_base_commit=str(allocation["allocation_base_commit"]),
    )
    old_hash = str(allocation["principle_base_sha256"])
    current_hash = str(latest_main["principle_sha256"])
    drift = old_hash != current_hash
    chain = _load_audit_chain(
        repo.common_dir,
        normalized.iteration,
        current_hash,
        allowed_pending_operation=operation,
    )
    tip = chain[-1] if chain else None
    generation = (tip.generation + 1) if tip is not None else 1
    supersedes = tip.receipt_digest if tip is not None else None
    blockers: list[Blocker] = []
    if not drift:
        blockers.append(
            Blocker(
                "principle-drift-absent",
                "allocation principle already equals committed main; no impact audit may clear nonexistent drift",
            )
        )
    if not normalized.affected_ids:
        blockers.append(Blocker("affected-ids-missing", "impact audit requires explicit affected IDs"))
    if not normalized.evidence_ids:
        blockers.append(Blocker("audit-evidence-missing", "impact audit requires explicit evidence IDs"))
    if not normalized.authorization_ids:
        blockers.append(
            Blocker(
                "audit-authorization-missing",
                "impact disposition requires an explicit authorization identity",
            )
        )
    blockers.extend(
        _audit_successor_blockers(
            repo=repo,
            tip=tip,
            authority=authority,
            decision=normalized,
        )
    )

    represented = set(str(item) for item in authority["represented_ids"])  # type: ignore[index]
    reapproval_proof = {
        "prd_approved": bool(authority["prd_approved"]),
        "spec_approved": bool(authority["spec_approved"]),
        "implementation_authorized": bool(authority["implementation_authorized"]),
        "prd_principle_matches_current": authority["prd_principle_base_hash"] == current_hash,
        "all_affected_ids_represented": set(normalized.affected_ids).issubset(represented),
    }
    if normalized.disposition == DISPOSITION_REAPPROVED:
        proof_codes = {
            "reapproval-prd-not-approved": reapproval_proof["prd_approved"],
            "reapproval-spec-not-approved": reapproval_proof["spec_approved"],
            "reapproval-implementation-not-authorized": reapproval_proof[
                "implementation_authorized"
            ],
            "reapproval-principle-baseline-stale": reapproval_proof[
                "prd_principle_matches_current"
            ],
            "reapproval-affected-ids-unrepresented": reapproval_proof[
                "all_affected_ids_represented"
            ],
        }
        for code, proven in proof_codes.items():
            if not proven:
                blockers.append(
                    Blocker(code, "committed PRD/SPEC authority does not prove the exact current baseline")
                )

    clears_drift = normalized.disposition in {
        DISPOSITION_NO_IMPACT,
        DISPOSITION_REAPPROVED,
    }
    gate_status = "clear" if clears_drift else "blocked"
    next_gate = (
        "candidate-or-integration-principle-gate"
        if normalized.disposition == DISPOSITION_NO_IMPACT
        else "regenerate-candidate-from-reapproved-baseline"
        if normalized.disposition == DISPOSITION_REAPPROVED
        else "revise-and-reapprove-prd-spec"
    )
    progress = _progress_checkpoint_spec(
        operation_id=operation,
        iteration=normalized.iteration,
        allocation=allocation,
        latest_main=latest_main,
        authority=authority,
        decision=normalized,
        generation=generation,
        supersedes=supersedes,
    )
    manifest: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "operation_id": operation,
        "project_root": str(repo.root),
        "git_common_dir": str(repo.common_dir),
        "iteration": normalized.iteration,
        "audit_chain": {
            "generation": generation,
            "supersedes": supersedes,
            "previous_disposition": tip.disposition if tip is not None else None,
            "observed_tip": supersedes,
        },
        "allocation": allocation,
        "latest_main": latest_main,
        "authority": authority,
        "decision": normalized.as_dict(),
        "derived": {
            "drift": drift,
            "clears_drift": clears_drift,
            "gate_status": gate_status,
            "next_gate": next_gate,
            "reapproval_proof": reapproval_proof,
        },
        "progress_checkpoint": progress,
        "blocking_reasons": sorted({item.code for item in blockers}),
        "exclusions": list(EXCLUSIONS),
    }
    return manifest, tuple(blockers)


def plan_principle_impact_audit(
    project_root: Path | str,
    *,
    decision: PrincipleAuditDecision,
    operation_id: str | None = None,
) -> PrincipleImpactAuditPlan:
    """Build one exact zero-write impact audit plan from committed authority."""

    repo = _open_repository(project_root)
    operation = _validate_operation(operation_id or f"OP-{uuid.uuid4().hex}")
    manifest, blockers = _derive_plan_manifest(
        repo, operation_id=operation, decision=decision
    )
    digest = _sha256(_canonical_json(manifest))
    return PrincipleImpactAuditPlan(digest, manifest, blockers)


def _validate_plan_manifest(manifest: object, plan_digest: object) -> PrincipleImpactAuditPlan:
    required = {
        "schema_version",
        "operation_id",
        "project_root",
        "git_common_dir",
        "iteration",
        "audit_chain",
        "allocation",
        "latest_main",
        "authority",
        "decision",
        "derived",
        "progress_checkpoint",
        "blocking_reasons",
        "exclusions",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise PrincipleAuditError("principle audit manifest fields are invalid")
    if manifest.get("schema_version") != PLAN_SCHEMA:
        raise PrincipleAuditError("principle audit plan schema is invalid")
    digest = _validate_digest(plan_digest, "plan_digest")
    if _sha256(_canonical_json(manifest)) != digest:
        raise PrincipleAuditError("principle audit manifest differs from its digest")
    _validate_operation(manifest.get("operation_id"))
    number = _validate_iteration(manifest.get("iteration"))
    decision = _decision_from_manifest(manifest.get("decision"))
    if decision.iteration != number:
        raise PrincipleAuditError("principle audit decision names another iteration")
    if manifest.get("exclusions") != list(EXCLUSIONS):
        raise PrincipleAuditError("principle audit exclusions were altered")
    for key in (
        "audit_chain",
        "allocation",
        "latest_main",
        "authority",
        "derived",
        "progress_checkpoint",
    ):
        if not isinstance(manifest.get(key), dict):
            raise PrincipleAuditError(f"principle audit {key} must be an object")
    chain = manifest["audit_chain"]
    assert isinstance(chain, dict)
    if set(chain) != {
        "generation",
        "supersedes",
        "previous_disposition",
        "observed_tip",
    }:
        raise PrincipleAuditError("principle audit chain fields are invalid")
    generation = chain.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise PrincipleAuditError("principle audit generation is invalid")
    supersedes = chain.get("supersedes")
    if generation == 1:
        if supersedes is not None or chain.get("previous_disposition") is not None:
            raise PrincipleAuditError("first principle audit generation may not supersede another")
    else:
        _validate_digest(supersedes, "superseded receipt digest")
        if chain.get("previous_disposition") not in DISPOSITIONS:
            raise PrincipleAuditError("principle audit previous disposition is invalid")
    if chain.get("observed_tip") != supersedes:
        raise PrincipleAuditError("principle audit observed tip differs from supersedes")
    codes = manifest.get("blocking_reasons")
    if not isinstance(codes, list) or not all(isinstance(item, str) for item in codes):
        raise PrincipleAuditError("principle audit blocking reasons are invalid")
    progress = manifest["progress_checkpoint"]
    assert isinstance(progress, dict)
    if (
        progress.get("schema_version") != PROGRESS_SPEC_SCHEMA
        or progress.get("iteration") != number
        or progress.get("operation_id") != manifest.get("operation_id")
        or progress.get("event_type") != "CHECKPOINT"
        or progress.get("scope") != "principle"
        or progress.get("write_progress") is not False
    ):
        raise PrincipleAuditError("principle audit progress checkpoint spec is invalid")
    blockers = tuple(Blocker(item, "durable blocked plan") for item in codes)
    return PrincipleImpactAuditPlan(digest, dict(manifest), blockers)


def _registry_root(common_dir: Path) -> Path:
    return common_dir.joinpath(*REGISTRY_PARTS)


def _journal_path(common_dir: Path, operation_id: str) -> Path:
    return _registry_root(common_dir) / "operations" / f"{_validate_operation(operation_id)}.json"


def _receipt_directory(common_dir: Path, iteration: str, principle_sha256: str) -> Path:
    return (
        _registry_root(common_dir)
        / "receipts"
        / f"I{_validate_iteration(iteration)}"
        / _validate_digest(principle_sha256, "current principle hash")
    )


def _receipt_path(
    common_dir: Path,
    iteration: str,
    principle_sha256: str,
    generation: int,
    operation_id: str,
) -> Path:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise PrincipleAuditError("audit generation must be a positive integer")
    operation = _validate_operation(operation_id)
    return _receipt_directory(common_dir, iteration, principle_sha256) / (
        f"G{generation:06d}-{operation}.json"
    )


def _lock_path(common_dir: Path, iteration: str, principle_sha256: str) -> Path:
    return (
        _registry_root(common_dir)
        / "locks"
        / f"I{_validate_iteration(iteration)}-{_validate_digest(principle_sha256, 'current principle hash')}.lock"
    )


def _ensure_operational_path(path: Path, common_dir: Path) -> None:
    common = common_dir.resolve()
    absolute = path.absolute()
    try:
        absolute.relative_to(common)
        absolute.resolve(strict=False).relative_to(common)
    except ValueError as exc:
        raise PrincipleAuditError(f"operational path escapes Git common directory: {path}") from exc
    current = absolute
    while current != common:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.exists() and (current.is_symlink() or is_junction()):
            raise PrincipleAuditError(f"operational path traverses a link or junction: {current}")
        if current.parent == current:
            raise PrincipleAuditError(f"cannot prove operational path containment: {path}")
        current = current.parent


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json_replace(path: Path, value: Mapping[str, object], common_dir: Path) -> None:
    _ensure_operational_path(path, common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_operational_path(path.parent, common_dir)
    raw = _canonical_json(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".audit.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _ensure_operational_path(path, common_dir)
        os.replace(temporary, path)
        temporary = None  # type: ignore[assignment]
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


@contextlib.contextmanager
def _file_lock(path: Path, common_dir: Path, *, timeout_seconds: float = 30.0):
    _ensure_operational_path(path, common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_operational_path(path.parent, common_dir)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise PrincipleAuditError(f"timed out waiting for principle audit lock: {path}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_json(path: Path, common_dir: Path) -> dict[str, object]:
    _ensure_operational_path(path, common_dir)
    try:
        size = path.stat().st_size
        if size > MAX_JOURNAL_BYTES:
            raise PrincipleAuditError(f"principle audit state exceeds the safe size: {path}")
        raw = path.read_bytes()
    except OSError as exc:
        raise PrincipleAuditError(f"cannot read principle audit state: {path}") from exc
    if len(raw) != size:
        raise PrincipleAuditError(f"principle audit state changed while read: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrincipleAuditError(f"principle audit state is corrupt: {path}") from exc
    if not isinstance(value, dict):
        raise PrincipleAuditError(f"principle audit state is not an object: {path}")
    return value


def _new_journal(plan: PrincipleImpactAuditPlan) -> dict[str, object]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schema_version": JOURNAL_SCHEMA,
        "operation_id": plan.operation_id,
        "plan_digest": plan.plan_digest,
        "accepted_plan_digest": plan.plan_digest,
        "iteration": plan.iteration,
        "current_principle_sha256": plan.current_principle_sha256,
        "phase": "PLANNED",
        "created_at": now,
        "updated_at": now,
        "manifest": plan.manifest,
        "receipt": None,
        "history": [{"phase": "PLANNED", "at": now}],
        "error": None,
    }


def _validate_journal(value: object, source: Path) -> dict[str, object]:
    required = {
        "schema_version",
        "operation_id",
        "plan_digest",
        "accepted_plan_digest",
        "iteration",
        "current_principle_sha256",
        "phase",
        "created_at",
        "updated_at",
        "manifest",
        "receipt",
        "history",
        "error",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PrincipleAuditError(f"principle audit journal fields are invalid: {source}")
    if value.get("schema_version") != JOURNAL_SCHEMA:
        raise PrincipleAuditError(f"principle audit journal schema is invalid: {source}")
    _validate_operation(value.get("operation_id"))
    _validate_digest(value.get("plan_digest"), "journal plan digest")
    if value.get("accepted_plan_digest") != value.get("plan_digest"):
        raise PrincipleAuditError(f"principle audit journal lacks exact acceptance: {source}")
    _validate_iteration(value.get("iteration"))
    _validate_digest(value.get("current_principle_sha256"), "journal principle hash")
    if value.get("phase") not in {
        "PLANNED",
        "APPLYING",
        "APPLIED",
        "FAILED_NEEDS_RECONCILE",
    }:
        raise PrincipleAuditError(f"principle audit journal phase is invalid: {source}")
    if not isinstance(value.get("manifest"), dict) or not isinstance(value.get("history"), list):
        raise PrincipleAuditError(f"principle audit journal payload is invalid: {source}")
    if value.get("receipt") is not None and not isinstance(value.get("receipt"), dict):
        raise PrincipleAuditError(f"principle audit journal receipt is invalid: {source}")
    return dict(value)


def _advance_journal(
    path: Path,
    journal: Mapping[str, object],
    phase: str,
    common_dir: Path,
    *,
    receipt: PrincipleImpactAuditReceipt | None = None,
    error: str | None = None,
) -> dict[str, object]:
    updated = dict(journal)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    updated["phase"] = phase
    updated["updated_at"] = now
    updated["error"] = error
    if receipt is not None:
        updated["receipt"] = receipt.as_dict()
    history = list(updated["history"])
    if not history or history[-1].get("phase") != phase:
        entry: dict[str, object] = {"phase": phase, "at": now}
        if error:
            entry["error"] = error
        history.append(entry)
    updated["history"] = history
    _atomic_json_replace(path, updated, common_dir)
    return updated


def _receipt_digest_payload(payload: Mapping[str, object]) -> dict[str, object]:
    identity = dict(payload)
    progress = identity.get("progress_checkpoint")
    if not isinstance(progress, dict):
        raise PrincipleAuditError("receipt progress checkpoint is invalid")
    progress_identity = dict(progress)
    # The final progress evidence includes this receipt's own digest.  Exclude
    # that derived list from the preimage and validate it deterministically.
    progress_identity.pop("evidence_refs", None)
    identity["progress_checkpoint"] = progress_identity
    return identity


def _receipt_from_plan(plan: PrincipleImpactAuditPlan) -> PrincipleImpactAuditReceipt:
    decision = plan.manifest["decision"]
    derived = plan.manifest["derived"]
    progress = plan.manifest["progress_checkpoint"]
    assert isinstance(decision, dict) and isinstance(derived, dict) and isinstance(progress, dict)
    payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "operation_id": plan.operation_id,
        "plan_digest": plan.plan_digest,
        "iteration": plan.iteration,
        "generation": plan.generation,
        "supersedes": plan.supersedes,
        "allocation": plan.manifest["allocation"],
        "latest_main": plan.manifest["latest_main"],
        "authority": plan.manifest["authority"],
        "disposition": decision["disposition"],
        "affected_ids": decision["affected_ids"],
        "evidence_ids": decision["evidence_ids"],
        "authorization_ids": decision["authorization_ids"],
        "clears_drift": derived["clears_drift"],
        "gate_status": derived["gate_status"],
        "next_gate": derived["next_gate"],
        "reapproval_proof": derived["reapproval_proof"],
        "progress_event_spec_digest": _sha256(_canonical_json(progress)),
        "progress_checkpoint": dict(progress),
        "exclusions": list(EXCLUSIONS),
        "pushed": False,
    }
    receipt_digest = _sha256(_canonical_json(_receipt_digest_payload(payload)))
    final_progress = dict(progress)
    refs = list(final_progress["evidence_refs"])  # type: ignore[arg-type]
    refs.extend((f"audit-plan:{plan.plan_digest}", f"audit-receipt:{receipt_digest}"))
    if len(refs) > 64:
        raise PrincipleAuditError("receipt progress evidence refs exceed the v2 event limit")
    final_progress["evidence_refs"] = refs
    payload["progress_checkpoint"] = final_progress
    return PrincipleImpactAuditReceipt(receipt_digest, payload)


def _receipt_from_dict(value: object) -> PrincipleImpactAuditReceipt:
    required = {
        "schema_version",
        "operation_id",
        "plan_digest",
        "iteration",
        "generation",
        "supersedes",
        "allocation",
        "latest_main",
        "authority",
        "disposition",
        "affected_ids",
        "evidence_ids",
        "authorization_ids",
        "clears_drift",
        "gate_status",
        "next_gate",
        "reapproval_proof",
        "progress_event_spec_digest",
        "progress_checkpoint",
        "exclusions",
        "pushed",
        "receipt_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PrincipleAuditError("principle audit receipt fields are invalid")
    if value.get("schema_version") != RECEIPT_SCHEMA:
        raise PrincipleAuditError("principle audit receipt schema is invalid")
    receipt_digest = _validate_digest(value.get("receipt_digest"), "receipt digest")
    payload = dict(value)
    payload.pop("receipt_digest")
    if _sha256(_canonical_json(_receipt_digest_payload(payload))) != receipt_digest:
        raise PrincipleAuditError("principle audit receipt differs from its digest")
    _validate_operation(payload.get("operation_id"))
    _validate_digest(payload.get("plan_digest"), "receipt plan digest")
    number = _validate_iteration(payload.get("iteration"))
    generation = payload.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise PrincipleAuditError("principle audit receipt generation is invalid")
    supersedes = payload.get("supersedes")
    if generation == 1:
        if supersedes is not None:
            raise PrincipleAuditError("first principle audit receipt may not supersede another")
    else:
        _validate_digest(supersedes, "superseded receipt digest")
    if payload.get("disposition") not in DISPOSITIONS:
        raise PrincipleAuditError("principle audit receipt disposition is invalid")
    if not isinstance(payload.get("clears_drift"), bool):
        raise PrincipleAuditError("principle audit receipt clears_drift is invalid")
    if payload.get("exclusions") != list(EXCLUSIONS) or payload.get("pushed") is not False:
        raise PrincipleAuditError("principle audit receipt exclusions/push state changed")
    progress = payload.get("progress_checkpoint")
    if not isinstance(progress, dict) or progress.get("iteration") != number:
        raise PrincipleAuditError("principle audit receipt progress checkpoint is invalid")
    refs = progress.get("evidence_refs")
    if not isinstance(refs, list) or len(refs) < 2:
        raise PrincipleAuditError("principle audit receipt progress evidence is invalid")
    if refs[-2:] != [
        f"audit-plan:{payload['plan_digest']}",
        f"audit-receipt:{receipt_digest}",
    ]:
        raise PrincipleAuditError("principle audit receipt progress self-binding is invalid")
    original_progress = dict(progress)
    original_progress["evidence_refs"] = refs[:-2]
    if _sha256(_canonical_json(original_progress)) != payload.get("progress_event_spec_digest"):
        raise PrincipleAuditError("principle audit progress spec digest is invalid")
    return PrincipleImpactAuditReceipt(receipt_digest, payload)


def _audit_successor_blockers(
    *,
    repo: Repository,
    tip: PrincipleImpactAuditReceipt | None,
    authority: Mapping[str, object],
    decision: PrincipleAuditDecision,
) -> tuple[Blocker, ...]:
    """Admit only the one meaningful state advance after an impact finding."""

    if tip is None:
        return ()
    if tip.disposition in {DISPOSITION_NO_IMPACT, DISPOSITION_REAPPROVED}:
        return (
            Blocker(
                "principle-audit-chain-terminal",
                "the current principle audit tip already clears this exact drift; reuse its receipt",
            ),
        )
    if tip.disposition != DISPOSITION_IMPACT or decision.disposition != DISPOSITION_REAPPROVED:
        return (
            Blocker(
                "principle-audit-transition-invalid",
                "an impact finding can only advance to an exact reapproved-current-baseline receipt",
            ),
        )
    blockers: list[Blocker] = []
    parent = tip.payload.get("authority")
    if not isinstance(parent, dict):
        return (Blocker("principle-audit-parent-authority-invalid", "parent authority is invalid"),)
    if parent.get("ref") != authority.get("ref"):
        blockers.append(
            Blocker(
                "reapproval-authority-ref-changed",
                "reapproval must advance the same exact authority ref audited as impacted",
            )
        )
    parent_commit = parent.get("commit")
    child_commit = authority.get("commit")
    if (
        not isinstance(parent_commit, str)
        or not isinstance(child_commit, str)
        or parent_commit == child_commit
        or not _is_ancestor(repo, parent_commit, child_commit)
    ):
        blockers.append(
            Blocker(
                "reapproval-authority-not-descendant",
                "reapproval authority must be a later committed descendant of the impacted authority",
            )
        )
    if (
        parent.get("prd_blob") == authority.get("prd_blob")
        or parent.get("spec_blob") == authority.get("spec_blob")
    ):
        blockers.append(
            Blocker(
                "reapproval-prd-spec-not-revised",
                "both exact committed PRD and SPEC blobs must be revised after an impact finding",
            )
        )
    if (
        parent.get("prd_approval_source_sha256")
        == authority.get("prd_approval_source_sha256")
        or parent.get("spec_approval_source_sha256")
        == authority.get("spec_approval_source_sha256")
    ):
        blockers.append(
            Blocker(
                "reapproval-evidence-not-renewed",
                "committed PRD and SPEC approval evidence must be renewed after impact",
            )
        )
    parent_affected = tuple(str(item) for item in tip.payload.get("affected_ids", []))
    if tuple(decision.affected_ids) != parent_affected:
        blockers.append(
            Blocker(
                "reapproval-affected-scope-changed",
                "reapproval must resolve the exact affected IDs recorded by the impact receipt",
            )
        )
    return tuple(blockers)


def _receipt_has_exact_journal(
    common: Path,
    receipt: PrincipleImpactAuditReceipt,
    *,
    allowed_pending_operation: str | None,
) -> str:
    """Return ``applied`` or ``pending`` for one exact receipt/journal pair."""

    path = _journal_path(common, receipt.operation_id)
    if not path.is_file():
        raise PrincipleAuditError("principle audit receipt lacks its durable operation journal")
    journal = _validate_journal(_read_json(path, common), path)
    if (
        journal["operation_id"] != receipt.operation_id
        or journal["plan_digest"] != receipt.plan_digest
        or journal["iteration"] != receipt.iteration
        or journal["current_principle_sha256"] != receipt.current_principle_sha256
    ):
        raise PrincipleAuditError("principle audit receipt and journal identities disagree")
    plan = _validate_plan_manifest(journal["manifest"], journal["plan_digest"])
    expected = _receipt_from_plan(plan)
    if expected.as_dict() != receipt.as_dict():
        raise PrincipleAuditError("principle audit receipt differs from its accepted plan")
    if journal["phase"] == "APPLIED":
        if journal.get("receipt") != receipt.as_dict():
            raise PrincipleAuditError("APPLIED principle audit journal differs from its receipt")
        return "applied"
    if (
        receipt.operation_id == allowed_pending_operation
        and journal["phase"] in {"PLANNED", "APPLYING"}
        and journal.get("receipt") is None
    ):
        return "pending"
    raise PrincipleAuditError("principle audit receipt has an incomplete or failed journal")


def _validate_audit_chain(
    receipts: Sequence[PrincipleImpactAuditReceipt],
    *,
    iteration: str,
    principle_sha256: str,
) -> tuple[PrincipleImpactAuditReceipt, ...]:
    ordered = tuple(sorted(receipts, key=lambda item: (item.generation, item.receipt_digest)))
    for index, receipt in enumerate(ordered, start=1):
        if receipt.iteration != iteration or receipt.current_principle_sha256 != principle_sha256:
            raise PrincipleAuditError("principle audit receipt is stored under another identity")
        if receipt.generation != index:
            raise PrincipleAuditError("principle audit generations are forked, duplicated, or non-contiguous")
        if index == 1:
            if receipt.supersedes is not None:
                raise PrincipleAuditError("first principle audit generation has a causal parent")
            continue
        parent = ordered[index - 2]
        if receipt.supersedes != parent.receipt_digest:
            raise PrincipleAuditError("principle audit causal chain is forked or out of order")
        if parent.disposition != DISPOSITION_IMPACT or receipt.disposition != DISPOSITION_REAPPROVED:
            raise PrincipleAuditError("principle audit causal transition is not permitted")
        if parent.payload.get("allocation") != receipt.payload.get("allocation"):
            raise PrincipleAuditError("principle audit successor changed allocation identity")
        parent_main = parent.payload.get("latest_main")
        child_main = receipt.payload.get("latest_main")
        if not isinstance(parent_main, dict) or not isinstance(child_main, dict):
            raise PrincipleAuditError("principle audit chain main authority is invalid")
        for field in ("ref", "principle_path", "principle_blob", "principle_sha256"):
            if parent_main.get(field) != child_main.get(field):
                raise PrincipleAuditError("principle audit successor changed current principle identity")
        if parent.payload.get("affected_ids") != receipt.payload.get("affected_ids"):
            raise PrincipleAuditError("principle audit successor changed affected scope")
        parent_authority = parent.payload.get("authority")
        child_authority = receipt.payload.get("authority")
        if not isinstance(parent_authority, dict) or not isinstance(child_authority, dict):
            raise PrincipleAuditError("principle audit chain authority is invalid")
        if (
            parent_authority.get("ref") != child_authority.get("ref")
            or parent_authority.get("commit") == child_authority.get("commit")
            or parent_authority.get("prd_blob") == child_authority.get("prd_blob")
            or parent_authority.get("spec_blob") == child_authority.get("spec_blob")
            or parent_authority.get("prd_approval_source_sha256")
            == child_authority.get("prd_approval_source_sha256")
            or parent_authority.get("spec_approval_source_sha256")
            == child_authority.get("spec_approval_source_sha256")
        ):
            raise PrincipleAuditError("principle audit reapproval did not advance exact authority")
        proof = receipt.payload.get("reapproval_proof")
        if not isinstance(proof, dict) or not proof or not all(value is True for value in proof.values()):
            raise PrincipleAuditError("principle audit reapproval proof is incomplete")
    return ordered


def _load_audit_chain(
    common_dir: Path | str,
    iteration: str,
    principle_sha256: str,
    *,
    allowed_pending_operation: str | None = None,
) -> tuple[PrincipleImpactAuditReceipt, ...]:
    """Load the unique APPLIED causal chain; incomplete foreign state blocks."""

    common = Path(common_dir).absolute().resolve()
    number = _validate_iteration(iteration)
    current_hash = _validate_digest(principle_sha256, "current principle hash")
    allowed = (
        _validate_operation(allowed_pending_operation)
        if allowed_pending_operation is not None
        else None
    )
    directory = _receipt_directory(common, number, current_hash)
    _ensure_operational_path(directory, common)
    receipts: list[PrincipleImpactAuditReceipt] = []
    pending_receipts: set[str] = set()
    if directory.exists():
        if not directory.is_dir():
            raise PrincipleAuditError("principle audit receipt directory is not a directory")
        paths = tuple(sorted(directory.glob("*.json")))
        if len(paths) > 10_000:
            raise PrincipleAuditError("principle audit receipt count exceeds the safe limit")
        for path in paths:
            match = RECEIPT_FILE_RE.fullmatch(path.name)
            if match is None:
                raise PrincipleAuditError(f"unexpected principle audit receipt filename: {path.name}")
            receipt = _receipt_from_dict(_read_json(path, common))
            if receipt.generation != int(match.group(1)) or receipt.operation_id != match.group(2):
                raise PrincipleAuditError("principle audit receipt filename/identity mismatch")
            state = _receipt_has_exact_journal(
                common,
                receipt,
                allowed_pending_operation=allowed,
            )
            if state == "applied":
                receipts.append(receipt)
            else:
                pending_receipts.add(receipt.operation_id)

    operations = _registry_root(common) / "operations"
    _ensure_operational_path(operations, common)
    if operations.is_dir():
        paths = tuple(sorted(operations.glob("OP-*.json")))
        if len(paths) > 10_000:
            raise PrincipleAuditError("principle audit operation count exceeds the safe limit")
        applied_operations = {item.operation_id for item in receipts}
        for path in paths:
            journal = _validate_journal(_read_json(path, common), path)
            if (
                journal["iteration"] != number
                or journal["current_principle_sha256"] != current_hash
            ):
                continue
            operation = str(journal["operation_id"])
            phase = str(journal["phase"])
            if phase == "APPLIED":
                if operation not in applied_operations:
                    raise PrincipleAuditError("APPLIED principle audit journal lacks its immutable receipt")
            elif operation != allowed or phase not in {"PLANNED", "APPLYING"}:
                raise PrincipleAuditError("principle audit chain has incomplete or failed operation state")
            elif journal.get("receipt") is not None:
                raise PrincipleAuditError("pending principle audit journal unexpectedly claims a receipt")
    return _validate_audit_chain(receipts, iteration=number, principle_sha256=current_hash)


def _open_plan_repository(plan: PrincipleImpactAuditPlan) -> Repository:
    validated = _validate_plan_manifest(plan.manifest, plan.plan_digest)
    if validated.manifest != plan.manifest:
        raise PrincipleAuditError("in-memory principle audit plan differs from its manifest")
    repo = _open_repository(plan.project_root)
    if str(repo.common_dir) != plan.git_common_dir:
        raise PrincipleAuditError("Git common directory changed after audit planning")
    return repo


def _validate_plan_against_repository(plan: PrincipleImpactAuditPlan) -> Repository:
    repo = _open_plan_repository(plan)
    decision = _decision_from_manifest(plan.manifest["decision"])
    current_manifest, current_blockers = _derive_plan_manifest(
        repo, operation_id=plan.operation_id, decision=decision
    )
    if current_manifest != plan.manifest or current_blockers != plan.blockers:
        raise PrincipleAuditError("main, authority, allocation metadata, or refs drifted after audit planning")
    return repo


def load_principle_impact_audit_plan(
    common_dir: Path | str,
    operation_id: str,
) -> PrincipleImpactAuditPlan:
    """Rehydrate an accepted plan from its strict common-dir journal."""

    common = Path(common_dir).absolute().resolve()
    path = _journal_path(common, operation_id)
    journal = _validate_journal(_read_json(path, common), path)
    if journal["operation_id"] != operation_id:
        raise PrincipleAuditError("principle audit journal identity differs from its path")
    return _validate_plan_manifest(journal["manifest"], journal["plan_digest"])


def load_principle_impact_audit(
    common_dir: Path | str,
    iteration: str,
    current_principle_sha256: str,
) -> PrincipleImpactAuditReceipt | None:
    """Load the unique legal causal tip for iteration/current-principle."""

    common = Path(common_dir).absolute().resolve()
    chain = _load_audit_chain(common, iteration, current_principle_sha256)
    return chain[-1] if chain else None


def load_principle_impact_audit_receipt(
    common_dir: Path | str,
    iteration: str,
    current_principle_sha256: str,
    receipt_digest: str,
) -> PrincipleImpactAuditReceipt | None:
    """Load one historical generation by immutable digest without selecting it."""

    common = Path(common_dir).absolute().resolve()
    number = _validate_iteration(iteration)
    current_hash = _validate_digest(current_principle_sha256, "current principle hash")
    target = _validate_digest(receipt_digest, "receipt digest")
    chain = _load_audit_chain(common, number, current_hash)
    matches = [item for item in chain if item.receipt_digest == target]
    if len(matches) > 1:
        raise PrincipleAuditError("principle audit receipt digest is duplicated")
    return matches[0] if matches else None


def apply_principle_impact_audit(
    plan: PrincipleImpactAuditPlan,
    *,
    accept_plan_digest: str,
    fault_injector: Callable[[str], None] | None = None,
) -> PrincipleAuditApplyResult:
    """Persist or resume one exact audit receipt without touching governance."""

    if not isinstance(plan, PrincipleImpactAuditPlan):
        raise TypeError("plan must be PrincipleImpactAuditPlan")
    if plan.blockers:
        raise PrincipleAuditError(
            "principle impact audit plan is blocked: "
            + "; ".join(item.code for item in plan.blockers)
        )
    if accept_plan_digest != plan.plan_digest:
        raise PrincipleAuditError("accepted plan digest differs from the reviewed impact audit")
    repo = _open_plan_repository(plan)
    common = repo.common_dir
    journal_path = _journal_path(common, plan.operation_id)
    lock_path = _lock_path(common, plan.iteration, plan.current_principle_sha256)
    expected_receipt = _receipt_from_plan(plan)
    receipt_path = _receipt_path(
        common,
        plan.iteration,
        plan.current_principle_sha256,
        expected_receipt.generation,
        expected_receipt.operation_id,
    )
    with _file_lock(lock_path, common):
        resumed = journal_path.exists()
        if resumed:
            journal = _validate_journal(_read_json(journal_path, common), journal_path)
            if (
                journal["operation_id"] != plan.operation_id
                or journal["plan_digest"] != plan.plan_digest
                or journal["accepted_plan_digest"] != accept_plan_digest
                or journal["iteration"] != plan.iteration
                or journal["current_principle_sha256"] != plan.current_principle_sha256
                or journal["manifest"] != plan.manifest
            ):
                raise PrincipleAuditError("durable audit journal differs from the accepted plan")
            if journal["phase"] == "FAILED_NEEDS_RECONCILE":
                raise PrincipleAuditError(
                    f"principle audit requires reconcile: {journal.get('error')}"
                )
            if journal["phase"] == "APPLIED":
                if not receipt_path.is_file():
                    raise PrincipleAuditError("APPLIED audit journal is missing its immutable receipt")
                actual_receipt = _receipt_from_dict(_read_json(receipt_path, common))
                if (
                    actual_receipt.as_dict() != expected_receipt.as_dict()
                    or journal.get("receipt") != expected_receipt.as_dict()
                ):
                    raise PrincipleAuditError("APPLIED audit journal/receipt differs from the accepted plan")
                return PrincipleAuditApplyResult(
                    expected_receipt,
                    str(journal_path),
                    str(receipt_path),
                    "APPLIED",
                    True,
                    True,
                )
        else:
            # The chain lock serializes successor selection.  Recompute the
            # observed tip before reserving this operation's journal.
            _validate_plan_against_repository(plan)
            journal = _new_journal(plan)
            _atomic_json_replace(journal_path, journal, common)
        try:
            # An incomplete retry may have left its own receipt.  Chain loading
            # ignores only this exact pending operation; every foreign pending
            # operation or newly APPLIED successor blocks.
            _validate_plan_against_repository(plan)
            if fault_injector is not None:
                fault_injector("after_journal")
            if receipt_path.exists():
                actual_receipt = _receipt_from_dict(_read_json(receipt_path, common))
                if actual_receipt.as_dict() != expected_receipt.as_dict():
                    raise PrincipleAuditError(
                        "this iteration/current-principle hash already has a different immutable audit"
                    )
                journal_receipt = journal.get("receipt")
                if journal_receipt is not None and journal_receipt != expected_receipt.as_dict():
                    raise PrincipleAuditError("audit journal and canonical receipt disagree")
                journal = _advance_journal(
                    journal_path,
                    journal,
                    "APPLIED",
                    common,
                    receipt=expected_receipt,
                )
                return PrincipleAuditApplyResult(
                    expected_receipt,
                    str(journal_path),
                    str(receipt_path),
                    str(journal["phase"]),
                    True,
                    True,
                )
            if journal.get("receipt") is not None or journal.get("phase") == "APPLIED":
                raise PrincipleAuditError("applied audit journal is missing its canonical receipt")
            journal = _advance_journal(journal_path, journal, "APPLYING", common)
            _validate_plan_against_repository(plan)
            _atomic_json_replace(receipt_path, expected_receipt.as_dict(), common)
            if fault_injector is not None:
                fault_injector("after_receipt_before_journal")
            actual = _receipt_from_dict(_read_json(receipt_path, common))
            if actual.as_dict() != expected_receipt.as_dict():
                raise PrincipleAuditError("canonical audit receipt failed exact verification")
            journal = _advance_journal(
                journal_path,
                journal,
                "APPLIED",
                common,
                receipt=expected_receipt,
            )
            return PrincipleAuditApplyResult(
                expected_receipt,
                str(journal_path),
                str(receipt_path),
                str(journal["phase"]),
                resumed,
                False,
            )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            with contextlib.suppress(Exception):
                _advance_journal(
                    journal_path,
                    journal,
                    "FAILED_NEEDS_RECONCILE",
                    common,
                    error=message,
                )
            if isinstance(exc, PrincipleAuditError):
                raise
            raise PrincipleAuditError(message) from exc


def _final_ref_has_exact_evidence(
    repo: Repository,
    *,
    iteration: str,
    final_ref: str,
    final_commit: str,
) -> bool:
    """Reject a raw final ref as closure authority.

    Lifecycle-v2 advances main/integrated/final through one train transaction.
    A commit-shaped ref alone can be fabricated and must not hide an iteration
    from the global impact audit.  This read-only check requires the exact
    completed main-advance journal and reachability from current main.
    """

    if _object_type(repo, final_commit) != "commit":
        return False
    main = _resolve_ref(repo, MAIN_REF)
    if main is None or not _is_ancestor(repo, final_commit, main):
        return False
    directory = repo.common_dir / "project-harness" / "train" / "v1" / "journal"
    try:
        _ensure_operational_path(directory, repo.common_dir)
    except PrincipleAuditError:
        return False
    if not directory.is_dir():
        return False
    try:
        paths = tuple(sorted(directory.glob("advance-OP-*.json")))
    except OSError:
        return False
    if len(paths) > 10_000:
        return False
    matches = 0
    for path in paths:
        try:
            value = _read_json(path, repo.common_dir)
        except PrincipleAuditError:
            return False
        if set(value) != TRAIN_ADVANCE_FIELDS:
            continue
        operation = value.get("operation_id")
        if (
            value.get("schema_version") != TRAIN_JOURNAL_SCHEMA
            or value.get("kind") != "main-advance"
            or not isinstance(operation, str)
            or OPERATION_RE.fullmatch(operation) is None
            or path.name != f"advance-{operation}.json"
            or value.get("status") != "complete"
            or value.get("pushed") is not False
            or value.get("project_root") != str(repo.root)
            or value.get("integrated_commit") != final_commit
            or not isinstance(value.get("plan_digest"), str)
            or DIGEST_RE.fullmatch(str(value["plan_digest"])) is None
            or value.get("accepted_plan_digest") != value.get("plan_digest")
            or not isinstance(value.get("accepted_integrated_evidence_digest"), str)
            or DIGEST_RE.fullmatch(str(value["accepted_integrated_evidence_digest"])) is None
            or not isinstance(value.get("expected_main"), str)
            or OID_RE.fullmatch(str(value["expected_main"])) is None
            or not isinstance(value.get("confirmation_id"), str)
            or OPAQUE_ID_RE.fullmatch(str(value["confirmation_id"])) is None
            or not isinstance(value.get("integration_worktree"), str)
            or not value.get("integration_worktree")
        ):
            continue
        updates = value.get("ref_updates")
        if not isinstance(updates, list):
            continue
        exact = [
            item
            for item in updates
            if isinstance(item, list)
            and len(item) == 3
            and item[0] == final_ref
            and item[1] is None
            and item[2] == final_commit
        ]
        if len(exact) == 1:
            matches += 1
    return matches == 1


def discover_open_v2_iterations(project_root: Path | str) -> tuple[OpenIteration, ...]:
    """Discover v2 allocations without final refs; performs no writes."""

    repo = _open_repository(project_root)
    result = _git(
        repo,
        [
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)",
            "refs/project-harness/v2/allocations",
        ],
    )
    identities: list[OpenIteration] = []
    seen: set[str] = set()
    for raw_line in result.stdout.splitlines():
        if not raw_line:
            continue
        parts = raw_line.split(b"\0")
        if len(parts) != 3:
            raise PrincipleAuditError("Git returned malformed v2 allocation inventory")
        reference = parts[0].decode("utf-8", errors="strict")
        match = V2_ALLOCATION_RE.fullmatch(reference)
        if match is None:
            raise PrincipleAuditError(f"unexpected ref below allocation namespace: {reference}")
        number = _validate_iteration(match.group(1))
        if number in seen:
            raise PrincipleAuditError(f"duplicate v2 allocation identity: {number}")
        seen.add(number)
        final_ref = f"refs/project-harness/v2/iterations/{number}/final"
        final = _resolve_ref(repo, final_ref)
        if final is not None and _final_ref_has_exact_evidence(
            repo,
            iteration=number,
            final_ref=final_ref,
            final_commit=final,
        ):
            continue
        allocation = _allocation_identity(repo, number)
        identities.append(
            OpenIteration(
                number,
                str(allocation["allocation_ref"]),
                str(allocation["allocation_object"]),
                str(allocation["allocation_base_ref"]),
                str(allocation["allocation_base_commit"]),
                str(allocation["principle_base_sha256"]),
            )
        )
    return tuple(sorted(identities, key=lambda item: int(item.iteration)))


def plan_open_principle_impact_audits(
    project_root: Path | str,
    *,
    decisions: Mapping[str, PrincipleAuditDecision],
    operation_ids: Mapping[str, str] | None = None,
) -> tuple[PrincipleImpactAuditPlan, ...]:
    """Plan an exact audit for every open v2 iteration with older principles."""

    repo = _open_repository(project_root)
    current = str(_latest_main_identity(repo)["principle_sha256"])
    drifted = tuple(
        item for item in discover_open_v2_iterations(repo.root)
        if item.principle_base_sha256 != current
    )
    expected = {item.iteration for item in drifted}
    supplied = set(decisions)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        raise PrincipleAuditError(
            "impact audit decisions must cover exactly every drifted open iteration; "
            f"missing={missing}, extra={extra}"
        )
    if operation_ids is not None and set(operation_ids) != expected:
        raise PrincipleAuditError("operation_ids must cover exactly every drifted open iteration")
    plans: list[PrincipleImpactAuditPlan] = []
    for item in drifted:
        decision = _normalize_decision(decisions[item.iteration])
        if decision.iteration != item.iteration:
            raise PrincipleAuditError("impact audit decision key/iteration mismatch")
        plans.append(
            plan_principle_impact_audit(
                repo.root,
                decision=decision,
                operation_id=(operation_ids or {}).get(item.iteration),
            )
        )
    return tuple(plans)


def _journal_for_receipt(
    common: Path,
    receipt: PrincipleImpactAuditReceipt,
) -> dict[str, object]:
    path = _journal_path(common, receipt.operation_id)
    journal = _validate_journal(_read_json(path, common), path)
    if (
        journal["phase"] != "APPLIED"
        or journal["operation_id"] != receipt.operation_id
        or journal["plan_digest"] != receipt.plan_digest
        or journal["iteration"] != receipt.iteration
        or journal["current_principle_sha256"] != receipt.current_principle_sha256
        or journal["receipt"] != receipt.as_dict()
    ):
        raise PrincipleAuditError("principle audit receipt lacks its exact APPLIED journal")
    plan = _validate_plan_manifest(journal["manifest"], journal["plan_digest"])
    if _receipt_from_plan(plan).as_dict() != receipt.as_dict():
        raise PrincipleAuditError("principle audit receipt differs from its accepted plan")
    return journal


def _main_receipt_is_current(
    repo: Repository,
    recorded: object,
    current: Mapping[str, object],
) -> bool:
    """Allow an unrelated main fast-forward while preserving principle identity.

    The receipt still binds the exact main commit/tree that was audited.  A
    later gate may reuse it only when that commit remains in current main's
    ancestry and the committed principle path/blob/bytes identity is unchanged.
    This matters because ordinary merge-train progress must not invalidate a
    principle audit when no principle changed.
    """

    if not isinstance(recorded, dict):
        return False
    stable_fields = ("ref", "principle_path", "principle_blob", "principle_sha256")
    if any(recorded.get(field) != current.get(field) for field in stable_fields):
        return False
    recorded_commit = recorded.get("commit")
    current_commit = current.get("commit")
    return (
        isinstance(recorded_commit, str)
        and isinstance(current_commit, str)
        and OID_RE.fullmatch(recorded_commit) is not None
        and OID_RE.fullmatch(current_commit) is not None
        and _is_ancestor(repo, recorded_commit, current_commit)
    )


def _authority_receipt_is_current(
    repo: Repository,
    recorded: object,
    current: Mapping[str, object],
) -> bool:
    """Accept descendant implementation commits only with unchanged PRD/SPEC.

    The audit is normative-baseline evidence, not an implementation snapshot.
    Appending its progress event or committing implementation naturally advances
    the feature ref.  Reuse is therefore allowed only as a fast-forward whose
    exact PRD/SPEC blobs, hashes, statuses, and approval/authorization evidence
    remain byte-identical.
    """

    if not isinstance(recorded, dict):
        return False
    stable_fields = (
        "ref",
        "prd_path",
        "prd_blob",
        "prd_sha256",
        "prd_status",
        "prd_approval_source_sha256",
        "prd_approved",
        "prd_principle_base_hash",
        "spec_path",
        "spec_blob",
        "spec_sha256",
        "spec_status",
        "spec_approval_source_sha256",
        "spec_approved",
        "implementation_authorization_source_sha256",
        "implementation_authorized",
        "represented_ids",
    )
    if any(recorded.get(field) != current.get(field) for field in stable_fields):
        return False
    recorded_commit = recorded.get("commit")
    current_commit = current.get("commit")
    return (
        isinstance(recorded_commit, str)
        and isinstance(current_commit, str)
        and OID_RE.fullmatch(recorded_commit) is not None
        and OID_RE.fullmatch(current_commit) is not None
        and _is_ancestor(repo, recorded_commit, current_commit)
    )


def current_principle_gate(
    project_root: Path | str,
    *,
    iteration: str,
    authority_ref: str | None = None,
) -> PrincipleAuditGate:
    """Return the current candidate/integration principle gate for one PRD."""

    repo = _open_repository(project_root)
    number = _validate_iteration(iteration)
    allocation = _allocation_identity(repo, number)
    latest = _latest_main_identity(repo)
    old_hash = str(allocation["principle_base_sha256"])
    current_hash = str(latest["principle_sha256"])
    if old_hash == current_hash:
        return PrincipleAuditGate(
            number,
            True,
            False,
            old_hash,
            current_hash,
            None,
            None,
            (),
            "candidate-or-integration-principle-gate",
        )
    try:
        receipt = load_principle_impact_audit(repo.common_dir, number, current_hash)
    except PrincipleAuditError:
        return PrincipleAuditGate(
            number,
            False,
            True,
            old_hash,
            current_hash,
            None,
            None,
            ("principle-audit-chain-invalid",),
            "reconcile-principle-audit-chain",
        )
    if receipt is None:
        return PrincipleAuditGate(
            number,
            False,
            True,
            old_hash,
            current_hash,
            None,
            None,
            ("principle-impact-audit-required",),
            "plan-principle-impact-audit",
        )
    blockers: list[str] = []
    try:
        _journal_for_receipt(repo.common_dir, receipt)
        receipt_allocation = receipt.payload.get("allocation")
        receipt_main = receipt.payload.get("latest_main")
        receipt_authority = receipt.payload.get("authority")
        if receipt_allocation != allocation:
            blockers.append("principle-audit-allocation-drift")
        if not _main_receipt_is_current(repo, receipt_main, latest):
            blockers.append("principle-audit-main-drift")
        if not isinstance(receipt_authority, dict):
            blockers.append("principle-audit-authority-invalid")
        else:
            receipt_ref = str(receipt_authority.get("ref"))
            if authority_ref is not None and _validate_ref(authority_ref) != receipt_ref:
                blockers.append("principle-audit-authority-ref-mismatch")
            current_authority = _authority_identity(
                repo,
                iteration=number,
                authority_ref=receipt_ref,
                allocation_base_commit=str(allocation["allocation_base_commit"]),
            )
            if not _authority_receipt_is_current(repo, receipt_authority, current_authority):
                blockers.append("principle-audit-authority-drift")
    except PrincipleAuditError:
        blockers.append("principle-audit-receipt-or-journal-invalid")
    if receipt.disposition == DISPOSITION_IMPACT:
        blockers.append("principle-reapproval-required")
    if receipt.clears_drift is not (
        receipt.disposition in {DISPOSITION_NO_IMPACT, DISPOSITION_REAPPROVED}
    ):
        blockers.append("principle-audit-clearance-invalid")
    allowed = not blockers and receipt.clears_drift
    return PrincipleAuditGate(
        number,
        allowed,
        True,
        old_hash,
        current_hash,
        receipt.disposition,
        receipt.receipt_digest,
        tuple(dict.fromkeys(blockers)),
        str(receipt.payload["next_gate"]) if allowed else "resolve-principle-impact-blockers",
    )


__all__ = [
    "Blocker",
    "DISPOSITION_IMPACT",
    "DISPOSITION_NO_IMPACT",
    "DISPOSITION_REAPPROVED",
    "EXCLUSIONS",
    "OpenIteration",
    "PrincipleAuditApplyResult",
    "PrincipleAuditDecision",
    "PrincipleAuditError",
    "PrincipleAuditGate",
    "PrincipleImpactAuditPlan",
    "PrincipleImpactAuditReceipt",
    "SimulatedCrash",
    "apply_principle_impact_audit",
    "current_principle_gate",
    "discover_open_v2_iterations",
    "load_principle_impact_audit",
    "load_principle_impact_audit_receipt",
    "load_principle_impact_audit_plan",
    "plan_open_principle_impact_audits",
    "plan_principle_impact_audit",
]
