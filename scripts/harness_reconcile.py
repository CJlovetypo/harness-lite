#!/usr/bin/env python3
"""Durable apply layer for Harness Lite governance reconciliation.

The semantic merge rules live in ``harness_governance.py``.  This module binds
their previews to three explicit snapshots, an accepted digest, durable
common-dir state, exact target hashes, and crash-safe file replacement.  It
never invokes commit, merge, push, reset, stash, clean, or worktree mutation.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from .harness_governance import (  # type: ignore[import-not-found]
        FOCUS_END,
        FOCUS_START,
        ITERATIONS_END,
        ITERATIONS_START,
        Blocker as SemanticBlocker,
        ManagedSection,
        PrincipleApproval,
        plan_principle_reconciliation,
        plan_progress_union,
        preview_managed_markdown,
    )
except ImportError:  # pragma: no cover - direct script execution
    from harness_governance import (  # noqa: E402
        FOCUS_END,
        FOCUS_START,
        ITERATIONS_END,
        ITERATIONS_START,
        Blocker as SemanticBlocker,
        ManagedSection,
        PrincipleApproval,
        plan_principle_reconciliation,
        plan_progress_union,
        preview_managed_markdown,
    )


PLAN_SCHEMA = "harness-lite.governance-reconcile-apply-plan/v1"
JOURNAL_SCHEMA = "harness-lite.governance-reconcile-journal/v1"
LEASE_SCHEMA = "harness-lite.global-principle-lease/v1"
PUBLIC_SCHEMA = "harness-lite.governance-reconcile-result/v1"

OPERATION_ID_RE = re.compile(r"OP-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
LEASE_ID_RE = re.compile(r"PL-[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
L1_PATH_RE = re.compile(r"harness/iterations/([0-9]{3,})/README\.md")

PRINCIPLE_PATH = "harness/principle.md"
PROGRESS_PATH = "harness/progress.md"
L0_PATH = "harness/README.md"
REQUIRED_PATHS = (PRINCIPLE_PATH, PROGRESS_PATH, L0_PATH)
OWNER_MARKERS = (
    b"<!-- managed-by: harness-lite v1 -->",
    b"<!-- managed-by: init-project-harness v1 -->",
)

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_FILES = 10_003
MAX_JOURNAL_BYTES = 128 * 1024 * 1024

REGISTRY_PARTS = ("project-harness", "reconcile", "v1")
EXCLUSIONS = (
    "no commit",
    "no merge",
    "no push",
    "no rebase",
    "no cherry-pick",
    "no stash",
    "no reset",
    "no clean",
    "no worktree mutation",
)


class ReconcileError(RuntimeError):
    """Raised when a durable reconcile operation cannot prove a safe write."""


class SimulatedCrash(BaseException):
    """Fault-injection signal that deliberately bypasses failure reconciliation."""


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str
    subject: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    content: bytes


@dataclass(frozen=True)
class GovernanceSnapshot:
    source_id: str
    files: tuple[SnapshotFile, ...]

    @classmethod
    def from_files(cls, source_id: str, files: Mapping[str, bytes]) -> "GovernanceSnapshot":
        identity = _one_line(source_id, "snapshot source_id")
        if len(files) > MAX_SNAPSHOT_FILES:
            raise ReconcileError(f"snapshot {identity} contains too many files")
        normalized: list[SnapshotFile] = []
        seen_paths: set[str] = set()
        total = 0
        for raw_path, raw_content in sorted(files.items()):
            path = _canonical_governance_path(raw_path)
            if path in seen_paths:
                raise ReconcileError(f"snapshot contains a duplicated normalized path: {path}")
            seen_paths.add(path)
            if not isinstance(raw_content, bytes):
                raise TypeError(f"snapshot file {path} must be bytes")
            if len(raw_content) > MAX_FILE_BYTES:
                raise ReconcileError(f"snapshot file exceeds safe size: {path}")
            total += len(raw_content)
            if total > MAX_SNAPSHOT_BYTES:
                raise ReconcileError(f"snapshot {identity} exceeds safe total size")
            normalized.append(SnapshotFile(path=path, content=raw_content))
        return cls(source_id=identity, files=tuple(normalized))

    def as_mapping(self) -> dict[str, bytes]:
        return {item.path: item.content for item in self.files}


@dataclass(frozen=True)
class GlobalPrincipleLease:
    lease_id: str
    operation_id: str
    holder: str
    generation: int
    before_sha256: str
    after_sha256: str
    approval_change_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": LEASE_SCHEMA,
            "lease_id": self.lease_id,
            "operation_id": self.operation_id,
            "holder": self.holder,
            "generation": self.generation,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "approval_change_id": self.approval_change_id,
        }


@dataclass(frozen=True)
class FilePreview:
    path: str
    category: str
    before_exists: bool
    before_sha256: str | None
    after_sha256: str
    content: bytes

    def manifest_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "category": self.category,
            "before_exists": self.before_exists,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "content_base64": base64.b64encode(self.content).decode("ascii"),
        }


@dataclass(frozen=True)
class GovernanceReconcilePlan:
    operation_id: str
    project_root: str
    git_common_dir: str
    plan_digest: str
    manifest: dict[str, object]
    previews: tuple[FilePreview, ...]
    blockers: tuple[Blocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLAN_SCHEMA,
            "operation_id": self.operation_id,
            "project_root": self.project_root,
            "git_common_dir": self.git_common_dir,
            "plan_digest": self.plan_digest,
            "phase": "planned" if self.ready else "blocked",
            "files": [
                {
                    "path": item.path,
                    "category": item.category,
                    "before_sha256": item.before_sha256,
                    "after_sha256": item.after_sha256,
                }
                for item in self.previews
            ],
            "blocking_reasons": [item.as_dict() for item in self.blockers],
            "exclusions": list(EXCLUSIONS),
            "pushed": False,
        }


@dataclass(frozen=True)
class ApplyResult:
    operation_id: str
    plan_digest: str
    project_root: str
    phase: str
    journal_path: str
    changed_paths: tuple[str, ...]
    resumed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PUBLIC_SCHEMA,
            "operation_id": self.operation_id,
            "plan_digest": self.plan_digest,
            "project_root": self.project_root,
            "phase": self.phase,
            "journal_path": self.journal_path,
            "changed_paths": list(self.changed_paths),
            "resumed": self.resumed,
            "exclusions": list(EXCLUSIONS),
            "pushed": False,
        }


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _one_line(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 256 or "\n" in normalized or "\r" in normalized:
        raise ReconcileError(f"{label} must be a non-empty bounded single line")
    return normalized


def _validate_operation_id(value: str) -> str:
    if not isinstance(value, str) or OPERATION_ID_RE.fullmatch(value) is None:
        raise ReconcileError("operation_id must use OP- plus 32 lowercase hexadecimal characters")
    return value


def _canonical_governance_path(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("snapshot paths must be strings")
    path = value.replace("\\", "/")
    if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ReconcileError(f"unsafe governance path: {value!r}")
    if path not in REQUIRED_PATHS and L1_PATH_RE.fullmatch(path) is None:
        raise ReconcileError(f"unsupported governance reconcile path: {path}")
    return path


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _snapshot_identity(snapshot: GovernanceSnapshot) -> dict[str, object]:
    return {
        "source_id": snapshot.source_id,
        "files": [
            {"path": item.path, "size": len(item.content), "sha256": sha256_bytes(item.content)}
            for item in snapshot.files
        ],
    }


def _semantic_blockers(values: Iterable[SemanticBlocker]) -> list[Blocker]:
    return [Blocker(item.code, item.message, item.subject) for item in values]


def _ensure_root_path_safe(root: Path, path: Path) -> None:
    resolved_root = root.resolve()
    try:
        path.absolute().relative_to(resolved_root)
        path.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise ReconcileError(f"path resolves outside project root: {path}") from exc
    current = path
    while current != resolved_root:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise ReconcileError(f"governance path traverses a symlink or junction: {current}")
        if current.parent == current:
            raise ReconcileError(f"cannot prove path containment: {path}")
        current = current.parent


def read_snapshot_from_root(root: Path | str, *, source_id: str) -> GovernanceSnapshot:
    """Read only supported governance files from one explicit worktree root."""

    resolved = Path(root).absolute().resolve()
    if not resolved.is_dir():
        raise ReconcileError(f"snapshot root is not a directory: {resolved}")
    files: dict[str, bytes] = {}
    for relative in REQUIRED_PATHS:
        path = resolved / Path(relative)
        _ensure_root_path_safe(resolved, path)
        if not path.is_file():
            raise ReconcileError(f"snapshot is missing required file: {relative}")
        files[relative] = path.read_bytes()
    iterations = resolved / "harness" / "iterations"
    _ensure_root_path_safe(resolved, iterations)
    if iterations.is_dir():
        for child in sorted(iterations.iterdir(), key=lambda item: item.name):
            _ensure_root_path_safe(resolved, child)
            if not child.is_dir() or re.fullmatch(r"[0-9]{3,}", child.name) is None:
                continue
            readme = child / "README.md"
            _ensure_root_path_safe(resolved, readme)
            if readme.is_file():
                files[f"harness/iterations/{child.name}/README.md"] = readme.read_bytes()
    return GovernanceSnapshot.from_files(source_id, files)


def resolve_git_common_dir(root: Path | str) -> Path:
    git = shutil.which("git")
    if not git:
        raise ReconcileError("git is required to resolve the common directory")
    project_root = Path(root).absolute().resolve()
    environment = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(key, None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        [git, "-C", str(project_root), "rev-parse", "--git-common-dir"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    if result.returncode != 0:
        raise ReconcileError("target root is not a usable Git worktree")
    raw = result.stdout.decode("utf-8", errors="strict").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = project_root / common
    common = common.resolve()
    if not common.is_dir():
        raise ReconcileError(f"Git common directory does not exist: {common}")
    return common


def _marker_span(document: bytes, marker: str) -> tuple[int, int]:
    target = marker.encode("utf-8")
    matches: list[tuple[int, int]] = []
    offset = 0
    for line in document.splitlines(keepends=True):
        if line.rstrip(b"\r\n") == target:
            matches.append((offset, offset + len(line)))
        offset += len(line)
    if len(matches) != 1:
        raise ReconcileError(f"managed marker is missing or duplicated: {marker}")
    return matches[0]


def _managed_body(document: bytes, start: str, end: str) -> bytes:
    start_span = _marker_span(document, start)
    end_span = _marker_span(document, end)
    if start_span[0] >= end_span[0]:
        raise ReconcileError(f"managed markers are reversed: {start} / {end}")
    return document[start_span[1] : end_span[0]]


def _managed_shell(document: bytes, pairs: Sequence[tuple[str, str]]) -> bytes:
    ranges: list[tuple[int, int]] = []
    for start, end in pairs:
        start_span = _marker_span(document, start)
        end_span = _marker_span(document, end)
        if start_span[0] >= end_span[0]:
            raise ReconcileError(f"managed markers are reversed: {start} / {end}")
        ranges.append((start_span[1], end_span[0]))
    ranges.sort()
    output = bytearray()
    cursor = 0
    for begin, finish in ranges:
        if begin < cursor:
            raise ReconcileError("managed README sections overlap")
        output.extend(document[cursor:begin])
        output.extend(b"<harness-managed-body>\n")
        cursor = finish
    output.extend(document[cursor:])
    return bytes(output)


def _choose_three_way(
    *,
    base: bytes,
    main: bytes,
    candidate: bytes,
    subject: str,
) -> tuple[bytes | None, Blocker | None]:
    if candidate == base:
        return main, None
    if main == base or main == candidate:
        return candidate, None
    return None, Blocker(
        "managed-three-way-conflict",
        f"latest-main and branch-candidate changed {subject} differently from their base",
        subject,
    )


def _plan_l0(base: bytes, main: bytes, candidate: bytes) -> tuple[bytes | None, list[Blocker]]:
    pairs = ((FOCUS_START, FOCUS_END), (ITERATIONS_START, ITERATIONS_END))
    blockers: list[Blocker] = []
    try:
        if _managed_shell(candidate, pairs) != _managed_shell(base, pairs):
            blockers.append(
                Blocker(
                    "l0-unmanaged-candidate-change",
                    "branch-candidate changed L0 bytes outside Harness managed sections",
                    L0_PATH,
                )
            )
        sections: list[ManagedSection] = []
        for name, (start, end) in zip(("focus", "iterations"), pairs):
            selected, conflict = _choose_three_way(
                base=_managed_body(base, start, end),
                main=_managed_body(main, start, end),
                candidate=_managed_body(candidate, start, end),
                subject=f"L0 {name} section",
            )
            if conflict is not None:
                blockers.append(conflict)
                continue
            assert selected is not None
            sections.append(ManagedSection(name, start, end, selected))
    except ReconcileError as exc:
        blockers.append(Blocker("l0-managed-marker-invalid", str(exc), L0_PATH))
        return None, blockers
    if blockers:
        return None, blockers
    preview = preview_managed_markdown(main, sections=sections, authority_id="three-way-snapshot")
    if not preview.ready or preview.preview is None:
        return None, _semantic_blockers(preview.blockers)
    return preview.preview, []


def _has_owner_marker(content: bytes) -> bool:
    payload = content[3:] if content.startswith(b"\xef\xbb\xbf") else content
    return any(payload.startswith(marker) for marker in OWNER_MARKERS)


def _plan_l1(
    path: str,
    base: bytes | None,
    main: bytes | None,
    candidate: bytes | None,
) -> tuple[bytes | None, list[Blocker]]:
    for label, content in (("base", base), ("main", main), ("candidate", candidate)):
        if content is not None and not _has_owner_marker(content):
            return None, [
                Blocker("l1-owner-marker-missing", f"{label} L1 is not Harness-managed", path)
            ]
    if base is None:
        if candidate is None:
            return main, []
        if main is None or main == candidate:
            return candidate, []
        return None, [
            Blocker("l1-add-add-conflict", "main and candidate created different L1 documents", path)
        ]
    if candidate is None:
        return None, [Blocker("l1-candidate-deletion", "candidate may not delete a derived L1", path)]
    if main is None:
        return None, [Blocker("l1-main-deletion", "latest main deleted an existing L1", path)]
    selected, conflict = _choose_three_way(base=base, main=main, candidate=candidate, subject=path)
    return selected, ([] if conflict is None else [conflict])


def _validate_lease(
    lease: GlobalPrincipleLease | None,
    *,
    operation_id: str,
    before_sha256: str,
    after_sha256: str,
    change_id: str | None,
) -> list[Blocker]:
    if lease is None:
        return [Blocker("global-principle-lease-required", "principle change requires a global lease")]
    blockers: list[Blocker] = []
    if not isinstance(lease.lease_id, str) or LEASE_ID_RE.fullmatch(lease.lease_id) is None:
        blockers.append(Blocker("principle-lease-id-invalid", "principle lease ID is invalid"))
    if lease.operation_id != operation_id:
        blockers.append(Blocker("principle-lease-operation-mismatch", "principle lease belongs to another operation"))
    if (
        not isinstance(lease.holder, str)
        or not lease.holder.strip()
        or not isinstance(lease.generation, int)
        or lease.generation < 1
    ):
        blockers.append(Blocker("principle-lease-owner-invalid", "principle lease owner/generation is invalid"))
    if lease.before_sha256 != before_sha256 or lease.after_sha256 != after_sha256:
        blockers.append(Blocker("principle-lease-content-mismatch", "principle lease is not bound to exact before/after bytes"))
    if not change_id or lease.approval_change_id != change_id:
        blockers.append(Blocker("principle-lease-approval-mismatch", "principle lease is not bound to the approved change ID"))
    return blockers


def plan_reconciliation(
    *,
    project_root: Path | str,
    git_common_dir: Path | str,
    operation_id: str,
    branch_base: GovernanceSnapshot,
    latest_main: GovernanceSnapshot,
    branch_candidate: GovernanceSnapshot,
    principle_approval: PrincipleApproval | None = None,
    principle_lease: GlobalPrincipleLease | None = None,
    progress_allow_divergent_main_history: bool = False,
) -> GovernanceReconcilePlan:
    """Bind semantic previews from three explicit immutable file snapshots."""

    operation = _validate_operation_id(operation_id)
    root = Path(project_root).absolute().resolve()
    common = Path(git_common_dir).absolute().resolve()
    if not root.is_dir() or not common.is_dir():
        raise ReconcileError("project_root and git_common_dir must be existing directories")
    actual_common = resolve_git_common_dir(root)
    if common != actual_common:
        raise ReconcileError(
            f"git_common_dir does not belong to project_root: expected {actual_common}, got {common}"
        )
    snapshots = (branch_base, latest_main, branch_candidate)
    if any(not isinstance(item, GovernanceSnapshot) for item in snapshots):
        raise TypeError("branch_base/latest_main/branch_candidate must be GovernanceSnapshot")
    base_files, main_files, candidate_files = (item.as_mapping() for item in snapshots)
    blockers: list[Blocker] = []
    for label, files in zip(("branch-base", "latest-main", "branch-candidate"), (base_files, main_files, candidate_files)):
        for path in REQUIRED_PATHS:
            if path not in files:
                blockers.append(Blocker("snapshot-required-file-missing", f"{label} lacks {path}", path))

    previews: list[FilePreview] = []
    principle_change_planned = False
    principle_details: dict[str, object] = {}
    progress_details: dict[str, object] = {}
    if not blockers:
        for label, files in zip(
            ("branch-base", "latest-main", "branch-candidate"),
            (base_files, main_files, candidate_files),
        ):
            for path in REQUIRED_PATHS:
                if not _has_owner_marker(files[path]):
                    blockers.append(
                        Blocker(
                            "governance-owner-marker-missing",
                            f"{label} {path} is not Harness-managed",
                            path,
                        )
                    )
        principle = plan_principle_reconciliation(
            branch_base=base_files[PRINCIPLE_PATH],
            latest_main=main_files[PRINCIPLE_PATH],
            branch_candidate=candidate_files[PRINCIPLE_PATH],
            approval=principle_approval,
        )
        blockers.extend(_semantic_blockers(principle.blockers))
        principle_details = {
            "action": principle.action,
            "base_sha256": principle.base_sha256,
            "latest_main_sha256": principle.latest_main_sha256,
            "candidate_sha256": principle.candidate_sha256,
            "result_sha256": principle.result_sha256,
            "change_id": principle.change_id,
            "evidence_ref": principle.evidence_ref,
        }
        if principle.ready and principle.preview is not None and principle.preview != main_files[PRINCIPLE_PATH]:
            principle_change_planned = True
            blockers.extend(
                _validate_lease(
                    principle_lease,
                    operation_id=operation,
                    before_sha256=sha256_bytes(main_files[PRINCIPLE_PATH]),
                    after_sha256=sha256_bytes(principle.preview),
                    change_id=principle.change_id,
                )
            )
            previews.append(
                FilePreview(
                    PRINCIPLE_PATH,
                    "principle",
                    True,
                    sha256_bytes(main_files[PRINCIPLE_PATH]),
                    sha256_bytes(principle.preview),
                    principle.preview,
                )
            )

        progress = plan_progress_union(
            branch_base=base_files[PROGRESS_PATH],
            latest_main=main_files[PROGRESS_PATH],
            branch_candidate=candidate_files[PROGRESS_PATH],
            allow_divergent_main_history=progress_allow_divergent_main_history,
        )
        blockers.extend(_semantic_blockers(progress.blockers))
        progress_details = {
            "allow_divergent_main_history": progress_allow_divergent_main_history,
            "base_sha256": progress.base_sha256,
            "latest_main_sha256": progress.latest_main_sha256,
            "candidate_sha256": progress.candidate_sha256,
            "result_sha256": progress.result_sha256,
            "appended_event_identities": list(progress.appended_event_identities),
            "deduplicated_event_identities": list(progress.deduplicated_event_identities),
        }
        if progress.ready and progress.preview is not None and progress.preview != main_files[PROGRESS_PATH]:
            previews.append(
                FilePreview(
                    PROGRESS_PATH,
                    "progress",
                    True,
                    sha256_bytes(main_files[PROGRESS_PATH]),
                    sha256_bytes(progress.preview),
                    progress.preview,
                )
            )

        l0_preview, l0_blockers = _plan_l0(
            base_files[L0_PATH], main_files[L0_PATH], candidate_files[L0_PATH]
        )
        blockers.extend(l0_blockers)
        if l0_preview is not None and l0_preview != main_files[L0_PATH]:
            previews.append(
                FilePreview(
                    L0_PATH,
                    "l0",
                    True,
                    sha256_bytes(main_files[L0_PATH]),
                    sha256_bytes(l0_preview),
                    l0_preview,
                )
            )

        l1_paths = sorted(
            {
                path
                for files in (base_files, main_files, candidate_files)
                for path in files
                if L1_PATH_RE.fullmatch(path)
            }
        )
        for path in l1_paths:
            preview, l1_blockers = _plan_l1(
                path, base_files.get(path), main_files.get(path), candidate_files.get(path)
            )
            blockers.extend(l1_blockers)
            current = main_files.get(path)
            if preview is not None and preview != current:
                previews.append(
                    FilePreview(
                        path,
                        "l1",
                        current is not None,
                        sha256_bytes(current) if current is not None else None,
                        sha256_bytes(preview),
                        preview,
                    )
                )

    category_order = {"principle": 0, "progress": 1, "l0": 2, "l1": 3}
    previews.sort(key=lambda item: (category_order[item.category], item.path))
    lease_payload = (
        principle_lease.as_dict()
        if principle_change_planned and principle_lease is not None
        else None
    )
    observed_paths = sorted(
        set(main_files)
        | {
            path
            for path in candidate_files
            if L1_PATH_RE.fullmatch(path) is not None
        }
    )
    target_observations = [
        {
            "path": path,
            "exists": path in main_files,
            "sha256": sha256_bytes(main_files[path]) if path in main_files else None,
        }
        for path in observed_paths
    ]
    manifest: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "operation_id": operation,
        "project_root": str(root),
        "git_common_dir": str(common),
        "sources": {
            "branch_base": _snapshot_identity(branch_base),
            "latest_main": _snapshot_identity(latest_main),
            "branch_candidate": _snapshot_identity(branch_candidate),
        },
        "principle": principle_details,
        "principle_lease": lease_payload,
        "principle_lease_sha256": sha256_bytes(_canonical_json(lease_payload)) if lease_payload else None,
        "progress": progress_details,
        "target_observations": target_observations,
        "files": [item.manifest_dict() for item in previews],
        "exclusions": list(EXCLUSIONS),
    }
    digest = sha256_bytes(_canonical_json(manifest))
    return GovernanceReconcilePlan(
        operation_id=operation,
        project_root=str(root),
        git_common_dir=str(common),
        plan_digest=digest,
        manifest=manifest,
        previews=tuple(previews),
        blockers=tuple(blockers),
    )


def plan_reconciliation_from_roots(
    *,
    operation_id: str,
    branch_base_root: Path | str,
    latest_main_root: Path | str,
    branch_candidate_root: Path | str,
    principle_approval: PrincipleApproval | None = None,
    principle_lease: GlobalPrincipleLease | None = None,
) -> GovernanceReconcilePlan:
    main_root = Path(latest_main_root).absolute().resolve()
    return plan_reconciliation(
        project_root=main_root,
        git_common_dir=resolve_git_common_dir(main_root),
        operation_id=operation_id,
        branch_base=read_snapshot_from_root(branch_base_root, source_id=f"base:{Path(branch_base_root).resolve()}"),
        latest_main=read_snapshot_from_root(main_root, source_id=f"main:{main_root}"),
        branch_candidate=read_snapshot_from_root(
            branch_candidate_root, source_id=f"candidate:{Path(branch_candidate_root).resolve()}"
        ),
        principle_approval=principle_approval,
        principle_lease=principle_lease,
    )


def _registry_root(common_dir: Path) -> Path:
    return common_dir.joinpath(*REGISTRY_PARTS)


def journal_path(common_dir: Path | str, operation_id: str) -> Path:
    return _registry_root(Path(common_dir)) / "operations" / f"{_validate_operation_id(operation_id)}.json"


def principle_lease_path(common_dir: Path | str) -> Path:
    return _registry_root(Path(common_dir)) / "principle-lease.json"


def _lock_path(common_dir: Path, name: str) -> Path:
    return _registry_root(common_dir) / "locks" / f"{name}.lock"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes_replace(
    path: Path,
    raw: bytes,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _atomic_json_replace(path: Path, value: Mapping[str, object]) -> None:
    _atomic_bytes_replace(path, _canonical_json(value) + b"\n")


@contextlib.contextmanager
def _file_lock(path: Path, *, timeout_seconds: float = 30.0):
    path.parent.mkdir(parents=True, exist_ok=True)
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
                    raise ReconcileError(f"timed out waiting for reconcile lock: {path}") from exc
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


def _validated_lease_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "lease_id",
        "operation_id",
        "holder",
        "generation",
        "before_sha256",
        "after_sha256",
        "approval_change_id",
    }:
        raise ReconcileError("global principle lease fields are invalid")
    if value.get("schema_version") != LEASE_SCHEMA:
        raise ReconcileError("global principle lease schema is invalid")
    lease_id = value.get("lease_id")
    operation_id = value.get("operation_id")
    holder = value.get("holder")
    generation = value.get("generation")
    if not isinstance(lease_id, str) or LEASE_ID_RE.fullmatch(lease_id) is None:
        raise ReconcileError("global principle lease ID is invalid")
    _validate_operation_id(str(operation_id))
    if not isinstance(holder, str) or not holder.strip() or not isinstance(generation, int) or generation < 1:
        raise ReconcileError("global principle lease holder/generation is invalid")
    for key in ("before_sha256", "after_sha256"):
        item = value.get(key)
        if not isinstance(item, str) or DIGEST_RE.fullmatch(item) is None:
            raise ReconcileError(f"global principle lease {key} is invalid")
    if not isinstance(value.get("approval_change_id"), str) or not str(value["approval_change_id"]).strip():
        raise ReconcileError("global principle lease approval change ID is invalid")
    return dict(value)


def acquire_global_principle_lease(
    common_dir: Path | str,
    lease: GlobalPrincipleLease,
) -> bool:
    """Create one global exact-content lease; an unrelated lease is never replaced."""

    common = Path(common_dir).absolute().resolve()
    if not common.is_dir() or not (common / "HEAD").is_file() or not (common / "objects").is_dir():
        raise ReconcileError(f"global principle lease target is not a Git common directory: {common}")
    payload = _validated_lease_payload(lease.as_dict())
    path = principle_lease_path(common)
    with _file_lock(_lock_path(common, "global-principle-lease")):
        if path.exists():
            existing = _read_json_file(path, MAX_JOURNAL_BYTES)
            if existing == payload:
                return False
            raise ReconcileError("a different global principle lease is already active")
        _atomic_json_replace(path, payload)
        return True


def _read_json_file(path: Path, limit: int) -> dict[str, object]:
    try:
        size = path.stat().st_size
        if size > limit:
            raise ReconcileError(f"JSON state exceeds safe size: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcileError(f"cannot read durable JSON state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReconcileError(f"durable JSON state must be an object: {path}")
    return value


def _load_and_match_lease(plan: GovernanceReconcilePlan) -> None:
    expected = plan.manifest.get("principle_lease")
    if expected is None:
        if any(item.category == "principle" for item in plan.previews):
            raise ReconcileError("accepted principle change lacks its global lease")
        return
    path = principle_lease_path(plan.git_common_dir)
    if not path.is_file():
        raise ReconcileError("accepted global principle lease is missing")
    actual = _validated_lease_payload(_read_json_file(path, MAX_JOURNAL_BYTES))
    if actual != expected:
        raise ReconcileError("active global principle lease differs from the accepted plan")
    expected_hash = plan.manifest.get("principle_lease_sha256")
    if expected_hash != sha256_bytes(_canonical_json(actual)):
        raise ReconcileError("active global principle lease hash differs from the accepted plan")


def _new_journal(plan: GovernanceReconcilePlan) -> dict[str, object]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schema_version": JOURNAL_SCHEMA,
        "operation_id": plan.operation_id,
        "plan_digest": plan.plan_digest,
        "project_root": plan.project_root,
        "phase": "PLANNED",
        "created_at": now,
        "updated_at": now,
        "manifest": plan.manifest,
        "completed_paths": [],
        "history": [{"phase": "PLANNED", "at": now}],
        "error": None,
    }


def _validate_journal(value: object, source: Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReconcileError(f"reconcile journal is not an object: {source}")
    required = {
        "schema_version",
        "operation_id",
        "plan_digest",
        "project_root",
        "phase",
        "created_at",
        "updated_at",
        "manifest",
        "completed_paths",
        "history",
        "error",
    }
    if set(value) != required or value.get("schema_version") != JOURNAL_SCHEMA:
        raise ReconcileError(f"reconcile journal schema/fields are invalid: {source}")
    _validate_operation_id(str(value.get("operation_id")))
    digest = value.get("plan_digest")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
        raise ReconcileError(f"reconcile journal digest is invalid: {source}")
    if value.get("phase") not in {"PLANNED", "APPLYING", "APPLIED", "FAILED_NEEDS_RECONCILE"}:
        raise ReconcileError(f"reconcile journal phase is invalid: {source}")
    if not isinstance(value.get("manifest"), dict):
        raise ReconcileError(f"reconcile journal manifest is invalid: {source}")
    for key in ("completed_paths", "history"):
        if not isinstance(value.get(key), list):
            raise ReconcileError(f"reconcile journal {key} is invalid: {source}")
    return dict(value)


def _previews_from_manifest(manifest: Mapping[str, object]) -> tuple[FilePreview, ...]:
    if manifest.get("schema_version") != PLAN_SCHEMA:
        raise ReconcileError("durable reconcile manifest schema is invalid")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > MAX_SNAPSHOT_FILES:
        raise ReconcileError("durable reconcile manifest files are invalid")
    previews: list[FilePreview] = []
    seen: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "category",
            "before_exists",
            "before_sha256",
            "after_sha256",
            "content_base64",
        }:
            raise ReconcileError("durable reconcile file preview fields are invalid")
        path = _canonical_governance_path(raw["path"])
        if path in seen:
            raise ReconcileError(f"durable reconcile file preview is duplicated: {path}")
        seen.add(path)
        category = raw.get("category")
        before_exists = raw.get("before_exists")
        before_hash = raw.get("before_sha256")
        after_hash = raw.get("after_sha256")
        if category not in {"principle", "progress", "l0", "l1"}:
            raise ReconcileError(f"durable reconcile file category is invalid: {path}")
        expected_category = (
            "principle"
            if path == PRINCIPLE_PATH
            else "progress"
            if path == PROGRESS_PATH
            else "l0"
            if path == L0_PATH
            else "l1"
        )
        if category != expected_category:
            raise ReconcileError(f"durable reconcile file category/path binding is invalid: {path}")
        if not isinstance(before_exists, bool):
            raise ReconcileError(f"durable reconcile before_exists is invalid: {path}")
        if before_exists:
            if not isinstance(before_hash, str) or DIGEST_RE.fullmatch(before_hash) is None:
                raise ReconcileError(f"durable reconcile before hash is invalid: {path}")
        elif before_hash is not None:
            raise ReconcileError(f"new durable reconcile file unexpectedly has a before hash: {path}")
        if not isinstance(after_hash, str) or DIGEST_RE.fullmatch(after_hash) is None:
            raise ReconcileError(f"durable reconcile after hash is invalid: {path}")
        encoded = raw.get("content_base64")
        if not isinstance(encoded, str):
            raise ReconcileError(f"durable reconcile content encoding is invalid: {path}")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ReconcileError(f"durable reconcile content is not canonical base64: {path}") from exc
        if len(content) > MAX_FILE_BYTES or sha256_bytes(content) != after_hash:
            raise ReconcileError(f"durable reconcile content does not match its accepted hash: {path}")
        previews.append(
            FilePreview(path, str(category), before_exists, before_hash, after_hash, content)
        )
    return tuple(previews)


def _target_observations_from_manifest(
    manifest: Mapping[str, object],
) -> tuple[tuple[str, bool, str | None], ...]:
    raw_values = manifest.get("target_observations")
    if not isinstance(raw_values, list) or len(raw_values) > MAX_SNAPSHOT_FILES:
        raise ReconcileError("durable reconcile target observations are invalid")
    result: list[tuple[str, bool, str | None]] = []
    seen: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, dict) or set(raw) != {"path", "exists", "sha256"}:
            raise ReconcileError("durable reconcile target observation fields are invalid")
        path = _canonical_governance_path(raw["path"])
        if path in seen:
            raise ReconcileError(f"durable reconcile target observation is duplicated: {path}")
        seen.add(path)
        exists = raw.get("exists")
        digest = raw.get("sha256")
        if not isinstance(exists, bool):
            raise ReconcileError(f"durable reconcile target existence is invalid: {path}")
        if exists:
            if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
                raise ReconcileError(f"durable reconcile target hash is invalid: {path}")
        elif digest is not None:
            raise ReconcileError(f"missing durable reconcile target unexpectedly has a hash: {path}")
        result.append((path, exists, digest))
    return tuple(result)


def _validate_manifest_bindings(
    manifest: Mapping[str, object],
    previews: Sequence[FilePreview],
) -> None:
    observations = {
        path: (exists, digest)
        for path, exists, digest in _target_observations_from_manifest(manifest)
    }
    for preview in previews:
        observed = observations.get(preview.path)
        if observed is None:
            raise ReconcileError(f"accepted preview lacks its target observation: {preview.path}")
        if observed != (preview.before_exists, preview.before_sha256):
            raise ReconcileError(f"accepted preview differs from its target observation: {preview.path}")
        if preview.before_exists and preview.before_sha256 == preview.after_sha256:
            raise ReconcileError(f"accepted preview is not a material change: {preview.path}")
    principle_previews = [item for item in previews if item.category == "principle"]
    lease = manifest.get("principle_lease")
    details = manifest.get("principle")
    if principle_previews:
        if len(principle_previews) != 1 or not isinstance(details, dict):
            raise ReconcileError("accepted principle preview authority is invalid")
        if (
            details.get("action") != "APPLY_APPROVED_EXACT_CHANGE"
            or not isinstance(details.get("change_id"), str)
            or not str(details["change_id"]).strip()
            or not isinstance(details.get("evidence_ref"), str)
            or not str(details["evidence_ref"]).strip()
        ):
            raise ReconcileError("accepted principle preview lacks exact approval evidence")
        if not isinstance(lease, dict):
            raise ReconcileError("accepted principle preview lacks a global lease")
        validated = _validated_lease_payload(lease)
        preview = principle_previews[0]
        if (
            validated["before_sha256"] != preview.before_sha256
            or validated["after_sha256"] != preview.after_sha256
            or validated["approval_change_id"] != details["change_id"]
        ):
            raise ReconcileError("accepted principle preview, approval, and lease are not exact-bound")
        if manifest.get("principle_lease_sha256") != sha256_bytes(_canonical_json(validated)):
            raise ReconcileError("accepted principle lease hash is invalid")
    elif lease is not None:
        raise ReconcileError("accepted plan contains a principle lease without a principle change")


def load_reconciliation_plan(
    common_dir: Path | str,
    operation_id: str,
) -> GovernanceReconcilePlan:
    """Rehydrate an accepted plan from its durable journal after process restart."""

    common = Path(common_dir).absolute().resolve()
    path = journal_path(common, operation_id)
    journal = _validate_journal(_read_json_file(path, MAX_JOURNAL_BYTES), path)
    manifest = journal["manifest"]
    assert isinstance(manifest, dict)
    digest = str(journal["plan_digest"])
    if sha256_bytes(_canonical_json(manifest)) != digest:
        raise ReconcileError("durable reconcile manifest no longer matches its plan digest")
    if manifest.get("operation_id") != journal["operation_id"]:
        raise ReconcileError("durable reconcile manifest operation differs from its journal")
    if manifest.get("project_root") != journal["project_root"]:
        raise ReconcileError("durable reconcile manifest root differs from its journal")
    if manifest.get("git_common_dir") != str(common):
        raise ReconcileError("durable reconcile manifest common directory differs from its journal")
    root = Path(str(journal["project_root"])).absolute().resolve()
    if resolve_git_common_dir(root) != common:
        raise ReconcileError("durable reconcile common directory no longer belongs to its project")
    previews = _previews_from_manifest(manifest)
    _validate_manifest_bindings(manifest, previews)
    return GovernanceReconcilePlan(
        operation_id=str(journal["operation_id"]),
        project_root=str(root),
        git_common_dir=str(common),
        plan_digest=digest,
        manifest=manifest,
        previews=previews,
        blockers=(),
    )


def _advance_journal(
    path: Path,
    journal: dict[str, object],
    phase: str,
    *,
    completed_paths: Sequence[str] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    updated = dict(journal)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    updated["phase"] = phase
    updated["updated_at"] = now
    updated["error"] = error
    if completed_paths is not None:
        updated["completed_paths"] = list(completed_paths)
    history = list(updated["history"])
    if not history or history[-1].get("phase") != phase:
        history.append({"phase": phase, "at": now, **({"error": error} if error else {})})
    updated["history"] = history
    _atomic_json_replace(path, updated)
    return updated


def _current_file_state(root: Path, preview: FilePreview) -> str:
    target = root / Path(preview.path)
    _ensure_root_path_safe(root, target)
    if not target.exists():
        return "before" if not preview.before_exists else "drift"
    if not target.is_file():
        return "drift"
    current_hash = sha256_bytes(target.read_bytes())
    if current_hash == preview.after_sha256:
        return "after"
    if preview.before_exists and current_hash == preview.before_sha256:
        return "before"
    return "drift"


def _verify_target_observations(root: Path, plan: GovernanceReconcilePlan) -> None:
    preview_paths = {item.path for item in plan.previews}
    for path, expected_exists, expected_hash in _target_observations_from_manifest(plan.manifest):
        if path in preview_paths:
            # Changed targets accept either exact before or exact after bytes;
            # their state machine is checked immediately after this pass.
            continue
        target = root / Path(path)
        _ensure_root_path_safe(root, target)
        if not target.exists():
            if expected_exists:
                raise ReconcileError(f"target observation drift: {path} is now missing")
            continue
        if not expected_exists or not target.is_file():
            raise ReconcileError(f"target observation drift: unexpected target state for {path}")
        if sha256_bytes(target.read_bytes()) != expected_hash:
            raise ReconcileError(f"target observation drift: {path} changed after planning")


def _compare_and_replace(root: Path, preview: FilePreview) -> None:
    target = root / Path(preview.path)
    _ensure_root_path_safe(root, target)
    if not target.parent.is_dir():
        raise ReconcileError(f"target parent directory is missing: {preview.path}")
    state = _current_file_state(root, preview)
    if state == "after":
        return
    if state != "before":
        raise ReconcileError(f"target changed after planning: {preview.path}")
    def exact_before_cas() -> None:
        if _current_file_state(root, preview) != "before":
            raise ReconcileError(f"target changed during atomic preparation: {preview.path}")

    _atomic_bytes_replace(target, preview.content, before_replace=exact_before_cas)
    if sha256_bytes(target.read_bytes()) != preview.after_sha256:
        raise ReconcileError(f"atomic write verification failed: {preview.path}")


def apply_reconciliation(
    plan: GovernanceReconcilePlan,
    *,
    accept_plan_digest: str,
    fault_injector: Callable[[str, str | None], None] | None = None,
) -> ApplyResult:
    """Apply an accepted plan with durable, resumable per-file CAS semantics."""

    if not isinstance(plan, GovernanceReconcilePlan):
        raise TypeError("plan must be GovernanceReconcilePlan")
    if plan.blockers:
        raise ReconcileError("governance reconcile plan is blocked")
    if accept_plan_digest != plan.plan_digest:
        raise ReconcileError("accepted plan digest does not match the reviewed plan")
    if sha256_bytes(_canonical_json(plan.manifest)) != plan.plan_digest:
        raise ReconcileError("in-memory reconcile manifest no longer matches its digest")
    root = Path(plan.project_root).absolute().resolve()
    common = Path(plan.git_common_dir).absolute().resolve()
    if resolve_git_common_dir(root) != common:
        raise ReconcileError("accepted reconcile common directory no longer belongs to the project")
    if (
        plan.manifest.get("operation_id") != plan.operation_id
        or plan.manifest.get("project_root") != plan.project_root
        or plan.manifest.get("git_common_dir") != plan.git_common_dir
    ):
        raise ReconcileError("in-memory reconcile plan identity differs from its accepted manifest")
    if plan.previews != _previews_from_manifest(plan.manifest):
        raise ReconcileError("in-memory reconcile previews differ from the accepted manifest")
    _target_observations_from_manifest(plan.manifest)
    path = journal_path(common, plan.operation_id)
    global_lock = _lock_path(common, "governance-apply")
    operation_lock = _lock_path(common, plan.operation_id)
    with _file_lock(global_lock), _file_lock(operation_lock):
        resumed = path.exists()
        if resumed:
            journal = _validate_journal(_read_json_file(path, MAX_JOURNAL_BYTES), path)
            if (
                journal["operation_id"] != plan.operation_id
                or journal["plan_digest"] != plan.plan_digest
                or journal["project_root"] != plan.project_root
                or journal["manifest"] != plan.manifest
            ):
                raise ReconcileError("durable operation journal differs from the accepted plan")
            if journal["phase"] == "FAILED_NEEDS_RECONCILE":
                raise ReconcileError(f"operation requires manual reconcile: {journal.get('error')}")
        else:
            journal = _new_journal(plan)
            _atomic_json_replace(path, journal)
        try:
            if fault_injector is not None:
                fault_injector("after_journal", None)
            _load_and_match_lease(plan)
            _verify_target_observations(root, plan)
            completed: list[str] = []
            pending: list[FilePreview] = []
            for preview in plan.previews:
                state = _current_file_state(root, preview)
                if state == "after":
                    completed.append(preview.path)
                elif state == "before":
                    pending.append(preview)
                else:
                    raise ReconcileError(f"target drift violates accepted before hash: {preview.path}")
            if journal["phase"] == "APPLIED":
                if pending:
                    raise ReconcileError("APPLIED journal has target files that no longer match its result")
                return ApplyResult(
                    plan.operation_id,
                    plan.plan_digest,
                    plan.project_root,
                    "APPLIED",
                    str(path),
                    tuple(item.path for item in plan.previews),
                    True,
                )
            journal = _advance_journal(path, journal, "APPLYING", completed_paths=completed)
            for preview in pending:
                if preview.category == "principle":
                    _load_and_match_lease(plan)
                _compare_and_replace(root, preview)
                if fault_injector is not None:
                    fault_injector("after_replace_before_journal", preview.path)
                completed.append(preview.path)
                journal = _advance_journal(path, journal, "APPLYING", completed_paths=completed)
                if fault_injector is not None:
                    fault_injector("after_file_journal", preview.path)
            for preview in plan.previews:
                if _current_file_state(root, preview) != "after":
                    raise ReconcileError(f"final output verification failed: {preview.path}")
            journal = _advance_journal(path, journal, "APPLIED", completed_paths=completed)
            return ApplyResult(
                plan.operation_id,
                plan.plan_digest,
                plan.project_root,
                str(journal["phase"]),
                str(path),
                tuple(item.path for item in plan.previews),
                resumed,
            )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            with contextlib.suppress(Exception):
                _advance_journal(
                    path,
                    journal,
                    "FAILED_NEEDS_RECONCILE",
                    completed_paths=list(journal.get("completed_paths", [])),
                    error=message,
                )
            if isinstance(exc, ReconcileError):
                raise
            raise ReconcileError(message) from exc


__all__ = [
    "ApplyResult",
    "Blocker",
    "EXCLUSIONS",
    "FilePreview",
    "GlobalPrincipleLease",
    "GovernanceReconcilePlan",
    "GovernanceSnapshot",
    "PrincipleApproval",
    "ReconcileError",
    "SimulatedCrash",
    "SnapshotFile",
    "acquire_global_principle_lease",
    "apply_reconciliation",
    "journal_path",
    "load_reconciliation_plan",
    "plan_reconciliation",
    "plan_reconciliation_from_roots",
    "principle_lease_path",
    "read_snapshot_from_root",
    "resolve_git_common_dir",
    "sha256_bytes",
]
