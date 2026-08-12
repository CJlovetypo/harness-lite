#!/usr/bin/env python3
"""Initialize, extend, and validate a lightweight project governance Harness."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import unquote


SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = SKILL_ROOT / "assets" / "templates"

OWNER_MARKER = "<!-- managed-by: harness-lite v1 -->"
LEGACY_OWNER_MARKER = "<!-- managed-by: init-project-harness v1 -->"
OWNER_MARKERS = (OWNER_MARKER, LEGACY_OWNER_MARKER)
AGENTS_START = "<!-- project-harness:start v1 -->"
AGENTS_END = "<!-- project-harness:end -->"
FOCUS_START = "<!-- project-harness:focus:start -->"
FOCUS_END = "<!-- project-harness:focus:end -->"
ITERATIONS_START = "<!-- project-harness:iterations:start -->"
ITERATIONS_END = "<!-- project-harness:iterations:end -->"
PROGRESS_INDEX_START = "<!-- project-harness:progress-index:start -->"
PROGRESS_INDEX_END = "<!-- project-harness:progress-index:end -->"

PUBLIC_OPERATION_SCHEMA_V1 = "harness-lite.operation/v1"
OPERATION_PLAN_SCHEMA_V1 = PUBLIC_OPERATION_SCHEMA_V1
OPERATION_JOURNAL_SCHEMA_V1 = "harness-lite.operation-journal.v1"
STATUS_SCHEMA_V1 = PUBLIC_OPERATION_SCHEMA_V1
RESERVATION_RESULT_SCHEMA_V1 = PUBLIC_OPERATION_SCHEMA_V1
ALLOCATION_METADATA_SCHEMA_V1 = "harness-lite.allocation-metadata.v1"
OPERATION_ID_PATTERN = re.compile(r"OP-[0-9a-f]{32}")
PLAN_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_OPERATION_JOURNAL_BYTES = 1024 * 1024
MAX_ALLOCATION_METADATA_BYTES = 256 * 1024
MAX_GOVERNANCE_BLOB_BYTES = 2 * 1024 * 1024
MAX_GOVERNANCE_FILE_COUNT = 4096
MAX_GOVERNANCE_TOTAL_BYTES = 32 * 1024 * 1024
RESERVATION_MAX_ATTEMPTS = 1024
JOURNAL_PHASES = (
    "PLANNED",
    "RESERVED",
    "BRANCH_READY",
    "WORKTREE_READY",
    "GOVERNANCE_READY",
    "RUNTIME_READY",
    "VALIDATED",
    "READY",
    "FAILED_NEEDS_RECONCILE",
)
V2_REF_ROOT = "refs/project-harness/v2"

PRD_STATUSES = {
    "草案",
    "待批准",
    "已批准",
    "实施中",
    "待验收",
    "已验收",
    "已取代",
    "已取消",
}
SPEC_STATUSES = {
    "受 PRD 阻塞",
    "草案",
    "待批准",
    "已批准",
    "实施中",
    "已完成",
    "已取代",
    "已取消",
}
DEVIATION_STATUSES = {
    "开放",
    "待处置",
    "已修复",
    "基线已重批",
    "已接受残余",
    "已转后续迭代",
    "已关闭",
}
UNRESOLVED_DEVIATION_STATUSES = {"开放", "待处置"}

PRD_TEMPLATE_PLACEHOLDERS = (
    "说明当前事实、用户/业务问题、触发背景，以及不采取行动的代价。不要在这里预设技术实现。",
    "说明本轮希望获得的可观察结果。",
    "用产品行为、约束或必须交付的结果描述需求。",
    "给出可观察、可验证、能映射到需求的完成条件。",
    "明确本轮不解决的相邻问题，防止范围静默扩大。",
    "记录产品、安全、兼容、时间、成本或合规约束；不要写代码步骤。",
    "列出会改变范围、验收、约束或重要取舍且仍需用户决定的问题；先 grill 用户并解决，或由用户明确移出本轮范围并记录影响与下一道门禁，再把 PRD 提交批准。",
)
SPEC_TEMPLATE_PLACEHOLDERS = (
    "需求较小且明确时，可在 PRD 同轮起草本节并把 SPEC 状态改为 `草案`；需求明确但不小时，等待 PRD 获批后再起草；存在决策性歧义时保持 `受 PRD 阻塞`，先 grill 用户完善 PRD。不得在 SPEC 中新增 PRD 未授权的产品范围，未获批准的联合草案不授权实施。",
    "列出会创建或修改的责任路径、公共接口、输入输出、Schema 与不变量。",
    "把实施拆成可验证切片或工作包；每个切片说明依赖、输出与停止条件。",
    "说明向后兼容、数据迁移、部署顺序和用户资产保护。",
    "说明失败时如何安全恢复，不改写历史或丢弃用户数据。",
    "列出主要技术/交付风险。实现前已知会偏离批准基线的变化，先修订并重新批准受影响的 PRD/SPEC；deviation 只在实现完成后记录 as-built 事实差异。",
    "按风险定义单元、集成、端到端、静态或人工验证，并逐项映射验收 ID。",
)

BASELINE_MAX_FILE_BYTES = 50 * 1024 * 1024
SENSITIVE_BASENAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".authinfo",
    "auth.json",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pfx", ".kdbx"}
SAFE_ENV_SUFFIXES = {".example", ".sample", ".template"}
SECRET_PATTERNS = (
    ("private key material", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(rb"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("GitHub token", re.compile(rb"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}")),
    ("npm auth token", re.compile(rb"(?im)^\s*(?://[^=\r\n]+/)?_authToken\s*=\s*\S+")),
)


class HarnessError(RuntimeError):
    """Raised when a command cannot continue without risking user content."""


@dataclass(frozen=True)
class Document:
    text: str
    raw: bytes
    bom: bool
    newline: str


@dataclass(frozen=True)
class Operation:
    path: Path
    new_raw: bytes
    old_raw: bytes | None

    @property
    def action(self) -> str:
        return "CREATE" if self.old_raw is None else "UPDATE"


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class BaselineFile:
    relative: str
    size: int
    sha256: str


@dataclass(frozen=True)
class DeviationEntry:
    identity: str
    status: str | None
    body: str


@dataclass(frozen=True)
class BlockingReason:
    code: str
    message: str


@dataclass(frozen=True)
class OperationPlan:
    operation_id: str
    project_root: str
    git_common_dir: str
    title: str
    base_commit: str
    base_branch: str
    governance_ref: str
    governance_commit: str
    governance_snapshot: dict[str, object]
    observed_next_iteration: str
    plan_digest: str
    manifest: dict[str, object]
    reservation: dict[str, object]
    warnings: tuple[str, ...] = ()
    blocking_reasons: tuple[BlockingReason, ...] = ()
    next_gate: str = "reserve-iteration"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": OPERATION_PLAN_SCHEMA_V1,
            "command": "reserve-iteration",
            "action_level": "silent",
            "pushed": False,
            "project_root": self.project_root,
            "operation_id": self.operation_id,
            "title": self.title,
            "phase": "blocked" if self.blocking_reasons else "planned",
            "git_common_dir": self.git_common_dir,
            "plan_digest": self.plan_digest,
            "reservation": self.reservation,
            "warnings": list(self.warnings),
            "blocking_reasons": [asdict(reason) for reason in self.blocking_reasons],
            "next_gate": self.next_gate,
            "exclusions": [
                "no worktree",
                "no branch",
                "no governance bundle",
                "no progress update",
                "no commit",
                "no push",
            ],
        }


@dataclass(frozen=True)
class OperationJournal:
    operation_id: str
    plan_digest: str
    action: str
    phase: str
    project_root: str
    title: str
    base_commit: str
    base_branch: str
    governance_ref: str
    governance_commit: str
    principle_sha256: str
    created_at: str
    updated_at: str
    manifest: dict[str, object]
    expected_refs: tuple[str, ...] = ()
    iteration: str | None = None
    allocation_object: str | None = None
    created_refs: tuple[str, ...] = ()
    attempts: tuple[dict[str, object], ...] = ()
    history: tuple[dict[str, object], ...] = ()
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": OPERATION_JOURNAL_SCHEMA_V1,
            "operation_id": self.operation_id,
            "plan_digest": self.plan_digest,
            "action": self.action,
            "phase": self.phase,
            "project_root": self.project_root,
            "title": self.title,
            "base_commit": self.base_commit,
            "base_branch": self.base_branch,
            "governance_ref": self.governance_ref,
            "governance_commit": self.governance_commit,
            "principle_sha256": self.principle_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "manifest": self.manifest,
            "expected_refs": list(self.expected_refs),
            "iteration": self.iteration,
            "allocation_object": self.allocation_object,
            "created_refs": list(self.created_refs),
            "attempts": list(self.attempts),
            "history": list(self.history),
            "error": self.error,
        }


@dataclass(frozen=True)
class IterationRefState:
    number: str
    allocation_ref: str | None = None
    allocation_object: str | None = None
    allocation_metadata: dict[str, object] | None = None
    base_ref: str | None = None
    base_commit: str | None = None
    base_format: str | None = None
    base_branch: str | None = None
    candidates: tuple[dict[str, str], ...] = ()
    integrated_ref: str | None = None
    integrated_object: str | None = None
    final_ref: str | None = None
    final_object: str | None = None
    bundle_present: bool = False
    issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class StatusSnapshot:
    project_root: str
    git_common_dir: str
    head: str
    branch: str | None
    iterations: tuple[IterationRefState, ...]
    worktrees: tuple[dict[str, object], ...]
    journals: tuple[dict[str, object], ...]
    warnings: tuple[str, ...] = ()
    blocking_reasons: tuple[BlockingReason, ...] = ()
    next_gate: str = "ready"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": STATUS_SCHEMA_V1,
            "command": "status",
            "action_level": "silent",
            "pushed": False,
            "project_root": self.project_root,
            "git_common_dir": self.git_common_dir,
            "head": self.head,
            "branch": self.branch,
            "iterations": [iteration.as_dict() for iteration in self.iterations],
            "worktrees": list(self.worktrees),
            "journals": list(self.journals),
            "warnings": list(self.warnings),
            "blocking_reasons": [asdict(reason) for reason in self.blocking_reasons],
            "next_gate": self.next_gate,
        }


class ValidationReport:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.issues: list[Issue] = []

    def add(self, severity: str, code: str, path: Path | str, message: str) -> None:
        try:
            shown = str(Path(path).resolve().relative_to(self.root))
        except (TypeError, ValueError, OSError):
            shown = str(path)
        self.issues.append(Issue(severity, code, shown, message))

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    def as_dict(self) -> dict[str, object]:
        return {
            "project_root": str(self.root),
            "valid": not self.errors,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [issue.__dict__ for issue in self.issues],
        }


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def schema_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def new_operation_id() -> str:
    return f"OP-{uuid.uuid4().hex}"


def validate_operation_id(value: str) -> str:
    operation_id = value.strip()
    if not OPERATION_ID_PATTERN.fullmatch(operation_id):
        raise HarnessError("Operation ID must use canonical OP- plus 32 lowercase hexadecimal characters")
    return operation_id


def operation_intent(
    *,
    operation_id: str,
    project_root: Path,
    title: str,
    base_commit: str,
    base_ref: str,
    governance_ref: str,
    governance_commit: str,
    governance_snapshot: Mapping[str, object],
    observed_next_iteration: str,
) -> dict[str, object]:
    number = normalize_iteration_number(observed_next_iteration)
    return {
        "schema_version": OPERATION_PLAN_SCHEMA_V1,
        "operation_id": operation_id,
        "action": "reserve-iteration",
        "project_root": str(project_root),
        "title": title,
        "base_commit": base_commit,
        "base_ref": base_ref,
        "governance_ref": governance_ref,
        "governance_commit": governance_commit,
        "governance_snapshot": dict(governance_snapshot),
        "reservation_policy": {
            "strategy": "next-monotonic-v2-cas",
            "collision_policy": "advance-to-current-max-plus-one",
            "max_attempts": RESERVATION_MAX_ATTEMPTS,
            "observed_next_iteration": number,
            "observed_allocation_ref": v2_allocation_ref(number),
            "observed_base_ref": v2_iteration_base_ref(number),
            "ref_namespace": V2_REF_ROOT,
        },
        "exclusions": [
            "no worktree",
            "no branch",
            "no governance bundle",
            "no progress update",
            "no commit",
            "no push",
        ],
    }


def has_owner_marker(text: str) -> bool:
    return any(marker in text for marker in OWNER_MARKERS)


def read_document(path: Path) -> Document:
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    payload = raw[3:] if bom else raw
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError(f"Refusing to modify non-UTF-8 file: {path}") from exc
    newline = "\r\n" if text.count("\r\n") > text.count("\n") / 2 else "\n"
    return Document(text=text, raw=raw, bom=bom, newline=newline)


def encode_document(text: str, *, bom: bool = False) -> bytes:
    raw = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + raw) if bom else raw


def normalize_newlines(text: str, newline: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def load_template(name: str, values: dict[str, str]) -> str:
    path = TEMPLATE_ROOT / name
    if not path.is_file():
        raise HarnessError(f"Missing bundled template: {path}")
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", text)))
    if unresolved:
        raise HarnessError(f"Unresolved template values in {name}: {', '.join(unresolved)}")
    return text


def resolve_project_root(value: str) -> Path:
    requested = Path(value).expanduser().absolute()
    current = requested
    while current != Path(current.anchor):
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise HarnessError(
                f"Project root path traverses a symbolic link or junction; use its resolved target instead: {current}"
            )
        if current.parent == current:
            break
        current = current.parent
    root = requested.resolve()
    if not root.exists() or not root.is_dir():
        raise HarnessError(f"Project root is not an existing directory: {root}")
    anchor = Path(root.anchor).resolve() if root.anchor else None
    if anchor is not None and root == anchor:
        raise HarnessError(f"Refusing to initialize a filesystem root: {root}")
    return root


def ensure_inside_root(path: Path, root: Path) -> None:
    root = root.resolve()
    try:
        path.absolute().relative_to(root)
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise HarnessError(f"Path resolves outside project root: {path}") from exc

    current = path
    while current != root:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise HarnessError(f"Refusing to modify through a symbolic link or junction: {current}")
        if current.parent == current:
            raise HarnessError(f"Could not prove path containment beneath project root: {path}")
        current = current.parent


def validate_label(value: str, label: str, *, max_length: int = 160) -> str:
    result = value.strip()
    if not result:
        raise HarnessError(f"{label} must not be empty")
    if len(result) > max_length:
        raise HarnessError(f"{label} is too long (max {max_length} characters)")
    if any(ord(char) < 32 and char not in "\t" for char in result):
        raise HarnessError(f"{label} contains control characters")
    if "\n" in result or "\r" in result:
        raise HarnessError(f"{label} must be one line")
    return result


def git_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(key, None)
    # Read-only commands such as status must not refresh and rewrite an index.
    # Mandatory locks used by update-ref/commit remain available.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def require_git() -> str:
    git = shutil.which("git")
    if not git:
        raise HarnessError("Git is required for this Harness workflow but was not found on PATH")
    return git


def decode_output(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace").strip()


def run_git(
    git: str,
    root: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
    safe_directory: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    command = [git]
    if safe_directory:
        command.extend(["-c", f"safe.directory={root}"])
    command.extend(["-C", str(root), *arguments])
    result = subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=git_environment(),
    )
    if check and result.returncode != 0:
        detail = decode_output(result.stderr) or decode_output(result.stdout) or f"exit {result.returncode}"
        raise HarnessError(f"Git command failed ({' '.join(arguments)}): {detail}")
    return result


def git_path_list(result: subprocess.CompletedProcess[bytes]) -> list[str]:
    if result.returncode != 0:
        detail = decode_output(result.stderr) or decode_output(result.stdout) or f"exit {result.returncode}"
        raise HarnessError(f"Git path query failed: {detail}")
    return sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def resolve_existing_git_root(root: Path, git: str) -> Path | None:
    result = run_git(
        git,
        root,
        ["rev-parse", "--show-toplevel"],
        check=False,
        safe_directory=False,
    )
    if result.returncode == 0:
        discovered = Path(decode_output(result.stdout)).resolve()
        if discovered != root:
            raise HarnessError(
                f"Project root is inside a different Git repository ({discovered}); "
                "rerun against that repository root instead of creating nested governance"
            )
        return discovered

    marker = root / ".git"
    if marker.exists() or marker.is_symlink():
        detail = decode_output(result.stderr) or "Git could not resolve the repository"
        raise HarnessError(f"Existing .git metadata is unusable at {root}: {detail}")
    for parent in root.parents:
        parent_marker = parent / ".git"
        if parent_marker.exists() or parent_marker.is_symlink():
            raise HarnessError(
                f"Project root is nested inside an existing Git repository ({parent}); "
                "rerun against the repository root"
            )
    return None


def resolve_git_common_dir(git: str, root: Path) -> Path:
    result = run_git(
        git,
        root,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=False,
    )
    if result.returncode != 0:
        # Git before --path-format support still returns a cwd-relative path.
        result = run_git(git, root, ["rev-parse", "--git-common-dir"])
    shown = decode_output(result.stdout)
    if not shown:
        raise HarnessError("Git returned an empty common directory")
    candidate = Path(shown)
    if not candidate.is_absolute():
        candidate = root / candidate
    common_dir = candidate.resolve()
    if not common_dir.is_dir():
        raise HarnessError(f"Git common directory is not a directory: {common_dir}")
    return common_dir


def v2_allocation_ref(number: str) -> str:
    normalized = normalize_iteration_number(number)
    return f"{V2_REF_ROOT}/allocations/{normalized}"


def v2_iteration_base_ref(number: str) -> str:
    normalized = normalize_iteration_number(number)
    return f"{V2_REF_ROOT}/iterations/{normalized}/base"


def v2_candidate_ref(number: str, generation: str) -> str:
    normalized = normalize_iteration_number(number)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", generation):
        raise HarnessError(f"Invalid candidate generation: {generation}")
    return f"{V2_REF_ROOT}/iterations/{normalized}/candidates/{generation}"


def v2_integrated_ref(number: str) -> str:
    return f"{V2_REF_ROOT}/iterations/{normalize_iteration_number(number)}/integrated"


def v2_final_ref(number: str) -> str:
    return f"{V2_REF_ROOT}/iterations/{normalize_iteration_number(number)}/final"


def is_allowed_base_ref(reference: str) -> bool:
    dependency_ref = re.fullmatch(
        rf"{re.escape(V2_REF_ROOT)}/iterations/[0-9]{{3,}}/(?:candidates/[a-z0-9][a-z0-9._-]*|integrated|final)",
        reference,
    )
    return reference == "refs/heads/main" or dependency_ref is not None


def resolve_explicit_base_ref(git: str, root: Path, value: str) -> tuple[str, str]:
    reference = value.strip()
    if not reference.startswith("refs/"):
        raise HarnessError("Base ref must be an explicit full ref beginning with refs/")
    run_git(git, root, ["check-ref-format", reference])
    if not is_allowed_base_ref(reference):
        raise HarnessError(
            "Base ref must be refs/heads/main or a declared v2 candidate/integrated/final ref"
        )
    result = run_git(
        git,
        root,
        ["rev-parse", "--verify", f"{reference}^{{commit}}"],
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError(f"Base ref does not resolve to a committed snapshot: {reference}")
    commit = decode_output(result.stdout)
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise HarnessError(f"Base ref resolved to an invalid commit object: {reference}")
    return reference, commit


def read_committed_blob(
    git: str,
    root: Path,
    commit: str,
    relative_path: str,
) -> tuple[str, bytes]:
    object_name = decode_output(
        run_git(git, root, ["rev-parse", "--verify", f"{commit}:{relative_path}"], check=False).stdout
    )
    if not re.fullmatch(r"[0-9a-f]{40,64}", object_name):
        raise HarnessError(f"Committed governance path is missing: {relative_path}")
    object_type = decode_output(run_git(git, root, ["cat-file", "-t", object_name]).stdout)
    if object_type != "blob":
        raise HarnessError(f"Committed governance path is not a file: {relative_path}")
    size_text = decode_output(run_git(git, root, ["cat-file", "-s", object_name]).stdout)
    try:
        size = int(size_text)
    except ValueError as exc:
        raise HarnessError(f"Committed governance file has invalid size: {relative_path}") from exc
    if size > MAX_GOVERNANCE_BLOB_BYTES:
        raise HarnessError(f"Committed governance file exceeds safe size: {relative_path}")
    raw = run_git(git, root, ["cat-file", "-p", object_name]).stdout
    if len(raw) != size:
        raise HarnessError(f"Committed governance file size changed while reading: {relative_path}")
    return object_name, raw


def read_committed_governance_entries(
    git: str,
    root: Path,
    commit: str,
) -> dict[str, tuple[str, str, bytes]]:
    result = run_git(
        git,
        root,
        ["ls-tree", "-r", "-z", "--full-tree", commit, "--", "AGENTS.md", "harness"],
    )
    entries: dict[str, tuple[str, str, bytes]] = {}
    total_bytes = 0
    for raw_record in result.stdout.split(b"\0"):
        if not raw_record:
            continue
        try:
            raw_header, raw_path = raw_record.split(b"\t", 1)
            raw_mode, raw_type, raw_object = raw_header.split(b" ", 2)
            relative = raw_path.decode("utf-8", errors="strict")
            mode = raw_mode.decode("ascii", errors="strict")
            object_type = raw_type.decode("ascii", errors="strict")
            object_name = raw_object.decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise HarnessError("Could not parse the committed governance tree") from exc
        if relative != "AGENTS.md" and not relative.startswith("harness/"):
            continue
        if (
            relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise HarnessError(f"Committed governance path is unsafe: {relative}")
        if relative in entries:
            raise HarnessError(f"Committed governance path is duplicated: {relative}")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise HarnessError(f"Committed governance path is not a regular file: {relative}")
        if not re.fullmatch(r"[0-9a-f]{40,64}", object_name):
            raise HarnessError(f"Committed governance path has an invalid object ID: {relative}")
        size_text = decode_output(run_git(git, root, ["cat-file", "-s", object_name]).stdout)
        try:
            size = int(size_text)
        except ValueError as exc:
            raise HarnessError(f"Committed governance file has invalid size: {relative}") from exc
        if size > MAX_GOVERNANCE_BLOB_BYTES:
            raise HarnessError(f"Committed governance file exceeds safe size: {relative}")
        total_bytes += size
        if total_bytes > MAX_GOVERNANCE_TOTAL_BYTES:
            raise HarnessError("Committed governance tree exceeds the total safe size")
        raw = run_git(git, root, ["cat-file", "-p", object_name]).stdout
        if len(raw) != size:
            raise HarnessError(f"Committed governance file size changed while reading: {relative}")
        entries[relative] = (mode, object_name, raw)
        if len(entries) > MAX_GOVERNANCE_FILE_COUNT:
            raise HarnessError("Committed governance tree contains too many files")
    return entries


def committed_governance_snapshot(
    git: str,
    root: Path,
    governance_ref: str,
) -> tuple[str, str, dict[str, object]]:
    reference = governance_ref.strip()
    if reference != "refs/heads/main":
        raise HarnessError("Global governance ref must be the canonical refs/heads/main")
    _, commit = resolve_explicit_base_ref(git, root, reference)
    tree = decode_output(run_git(git, root, ["rev-parse", "--verify", f"{commit}^{{tree}}" ]).stdout)
    if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
        raise HarnessError("Governance commit has an invalid tree object")
    required = (
        "AGENTS.md",
        "harness/README.md",
        "harness/principle.md",
        "harness/progress.md",
    )
    entries = read_committed_governance_entries(git, root, commit)
    blobs: dict[str, str] = {}
    contents: dict[str, bytes] = {}
    for relative in required:
        entry = entries.get(relative)
        if entry is None:
            raise HarnessError(f"Committed governance path is missing: {relative}")
        _, object_name, raw = entry
        blobs[relative] = object_name
        contents[relative] = raw
    try:
        agents_text = contents["AGENTS.md"].decode("utf-8-sig")
        harness_texts = {
            relative: contents[relative].decode("utf-8-sig")
            for relative in required
            if relative != "AGENTS.md"
        }
    except UnicodeDecodeError as exc:
        raise HarnessError("Committed governance files must be UTF-8") from exc
    if (
        agents_text.count(AGENTS_START) != 1
        or agents_text.count(AGENTS_END) != 1
        or agents_text.index(AGENTS_START) >= agents_text.index(AGENTS_END)
    ):
        raise HarnessError("Committed AGENTS.md lacks one valid Harness managed block")
    for relative, text_value in harness_texts.items():
        if not has_owner_marker(text_value):
            raise HarnessError(f"Committed governance ownership marker is missing: {relative}")
    validation = collect_committed_governance_validation(git, root, commit, entries)
    if validation.errors:
        preview = "; ".join(
            f"{issue.code} ({issue.path}): {issue.message}"
            for issue in validation.errors[:5]
        )
        if len(validation.errors) > 5:
            preview += f"; and {len(validation.errors) - 5} more"
        raise HarnessError(f"Committed governance snapshot failed validation: {preview}")
    principle_raw = contents["harness/principle.md"]
    snapshot: dict[str, object] = {
        "schema_version": "harness-lite.governance-snapshot/v1",
        "ref": reference,
        "commit": commit,
        "tree": tree,
        "blobs": blobs,
        "principle_sha256": hashlib.sha256(principle_raw).hexdigest(),
    }
    return reference, commit, snapshot


def git_ref_records(git: str, root: Path) -> dict[str, tuple[str, str]]:
    result = run_git(
        git,
        root,
        [
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)",
            "refs/project-harness/",
        ],
    )
    records: dict[str, tuple[str, str]] = {}
    for raw_line in result.stdout.splitlines():
        raw_line = raw_line.rstrip(b"\r")
        if not raw_line:
            continue
        parts = raw_line.split(b"\0")
        if len(parts) != 3:
            raise HarnessError("Could not parse project-harness Git ref inventory")
        reference = parts[0].decode("utf-8", errors="strict")
        object_name = parts[1].decode("ascii", errors="strict")
        object_type = parts[2].decode("ascii", errors="strict")
        records[reference] = (object_name, object_type)
    return records


def read_json_blob(git: str, root: Path, object_name: str) -> dict[str, object]:
    object_type = decode_output(run_git(git, root, ["cat-file", "-t", object_name]).stdout)
    if object_type != "blob":
        raise HarnessError(f"Expected allocation metadata blob, found {object_type or 'unknown'}: {object_name}")
    size_text = decode_output(run_git(git, root, ["cat-file", "-s", object_name]).stdout)
    try:
        size = int(size_text)
    except ValueError as exc:
        raise HarnessError(f"Allocation metadata has an invalid object size: {object_name}") from exc
    if size > MAX_ALLOCATION_METADATA_BYTES:
        raise HarnessError(f"Allocation metadata exceeds the safe size limit: {object_name}")
    raw = run_git(git, root, ["cat-file", "-p", object_name]).stdout
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Allocation metadata is not valid UTF-8 JSON: {object_name}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"Allocation metadata must be a JSON object: {object_name}")
    return value


def read_allocation_metadata(git: str, root: Path, object_name: str) -> dict[str, object]:
    value = read_json_blob(git, root, object_name)
    expected_fields = {
        "schema_version",
        "operation_id",
        "plan_digest",
        "iteration",
        "base_commit",
        "base_branch",
        "governance_ref",
        "governance_commit",
        "governance_tree",
        "principle_sha256",
        "title",
    }
    if set(value) != expected_fields:
        raise HarnessError(f"Allocation metadata fields do not match the v1 schema: {object_name}")
    if value.get("schema_version") != ALLOCATION_METADATA_SCHEMA_V1:
        raise HarnessError(f"Unsupported allocation metadata schema: {value.get('schema_version')!r}")
    operation_id = value.get("operation_id")
    if not isinstance(operation_id, str):
        raise HarnessError("Allocation metadata lacks operation_id")
    validate_operation_id(operation_id)
    plan_digest = value.get("plan_digest")
    if not isinstance(plan_digest, str) or not PLAN_DIGEST_PATTERN.fullmatch(plan_digest):
        raise HarnessError("Allocation metadata lacks a valid plan digest")
    number = value.get("iteration")
    if not isinstance(number, str) or normalize_iteration_number(number) != number:
        raise HarnessError("Allocation metadata lacks a canonical iteration number")
    base_commit = value.get("base_commit")
    if not isinstance(base_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_commit):
        raise HarnessError("Allocation metadata lacks a valid base commit")
    base_branch = value.get("base_branch")
    if not isinstance(base_branch, str) or not is_allowed_base_ref(base_branch):
        raise HarnessError("Allocation metadata lacks an explicit base ref")
    if value.get("governance_ref") != "refs/heads/main":
        raise HarnessError("Allocation metadata lacks the canonical governance ref")
    for key in ("governance_commit", "governance_tree"):
        item = value.get(key)
        if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{40,64}", item):
            raise HarnessError(f"Allocation metadata lacks a valid {key}")
    principle_sha256 = value.get("principle_sha256")
    if not isinstance(principle_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", principle_sha256):
        raise HarnessError("Allocation metadata lacks a valid principle hash")
    title = value.get("title")
    if not isinstance(title, str):
        raise HarnessError("Allocation metadata lacks a title")
    if validate_label(title, "allocation title") != title:
        raise HarnessError("Allocation metadata title is not canonical")
    return value


def discover_iteration_numbers(root: Path, records: Mapping[str, tuple[str, str]]) -> list[int]:
    numbers = set(find_existing_numbers(root / "harness" / "iterations"))
    patterns = (
        re.compile(r"^refs/project-harness/iterations/(\d{3,})/"),
        re.compile(r"^refs/project-harness/v2/allocations/(\d{3,})$"),
        re.compile(r"^refs/project-harness/v2/iterations/(\d{3,})/"),
    )
    for reference in records:
        for pattern in patterns:
            match = pattern.match(reference)
            if match:
                numbers.add(int(match.group(1)))
                break
    return sorted(numbers)


def discover_committed_iteration_numbers(
    git: str,
    root: Path,
    commit: str,
    records: Mapping[str, tuple[str, str]],
) -> list[int]:
    numbers: set[int] = set()
    patterns = (
        re.compile(r"^refs/project-harness/iterations/(\d{3,})/"),
        re.compile(r"^refs/project-harness/v2/allocations/(\d{3,})$"),
        re.compile(r"^refs/project-harness/v2/iterations/(\d{3,})/"),
    )
    for reference in records:
        for pattern in patterns:
            match = pattern.match(reference)
            if match:
                numbers.add(int(match.group(1)))
                break
    tree = run_git(
        git,
        root,
        ["ls-tree", "-r", "-z", "--name-only", commit, "--", "harness/iterations"],
    )
    for raw_path in tree.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", errors="strict")
        match = re.match(r"^harness/iterations/(\d{3,})/", relative)
        if match:
            numbers.add(int(match.group(1)))
    return sorted(numbers)


def read_iteration_base_compat(
    git: str,
    root: Path,
    number: str,
    records: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, str] | None:
    normalized = normalize_iteration_number(number)
    inventory = dict(records) if records is not None else git_ref_records(git, root)
    v2_reference = v2_iteration_base_ref(normalized)
    v2_value = inventory.get(v2_reference)
    legacy_prefix = f"refs/project-harness/iterations/{normalized}/base/"
    legacy = [(reference, value) for reference, value in inventory.items() if reference.startswith(legacy_prefix)]
    if v2_value is not None:
        object_name, object_type = v2_value
        if object_type != "commit":
            raise HarnessError(f"V2 base ref does not point to a commit: {v2_reference}")
        result = {
            "format": "v2",
            "reference": v2_reference,
            "commit": object_name,
            "branch": "",
        }
        if legacy:
            legacy_commits = {value[0] for _, value in legacy}
            if len(legacy) != 1 or legacy_commits != {object_name}:
                raise HarnessError(f"Iteration {normalized} has conflicting legacy and v2 base refs")
            result["format"] = "legacy+v2"
            result["branch"] = legacy[0][0][len(legacy_prefix) :]
        return result
    if not legacy:
        return None
    if len(legacy) != 1:
        raise HarnessError(
            f"PRD-{normalized} must have exactly one legacy base ref beneath {legacy_prefix}; found {len(legacy)}"
        )
    reference, (object_name, object_type) = legacy[0]
    if object_type != "commit":
        raise HarnessError(f"Legacy base ref does not point to a commit: {reference}")
    branch_ref = reference[len(legacy_prefix) :]
    if not branch_ref.startswith("refs/heads/"):
        raise HarnessError(f"Legacy base ref encodes an invalid branch: {reference}")
    return {
        "format": "legacy",
        "reference": reference,
        "commit": object_name,
        "branch": branch_ref,
    }


def parse_worktree_porcelain(raw: bytes) -> list[dict[str, object]]:
    worktrees: list[dict[str, object]] = []
    for raw_record in raw.split(b"\0\0"):
        if not raw_record:
            continue
        record: dict[str, object] = {}
        for raw_field in raw_record.split(b"\0"):
            if not raw_field:
                continue
            field = raw_field.decode("utf-8", errors="replace")
            key, separator, value = field.partition(" ")
            if separator:
                record[key] = value
            else:
                record[key] = True
        if "worktree" in record:
            worktrees.append(record)
    return worktrees


def list_worktree_states(git: str, root: Path, *, all_worktrees: bool) -> list[dict[str, object]]:
    result = run_git(git, root, ["worktree", "list", "--porcelain", "-z"])
    states = parse_worktree_porcelain(result.stdout)
    current = root.resolve()
    selected: list[dict[str, object]] = []
    for state in states:
        raw_path = state.get("worktree")
        if not isinstance(raw_path, str):
            continue
        worktree_path = Path(raw_path).resolve()
        if not all_worktrees and worktree_path != current:
            continue
        status = run_git(
            git,
            worktree_path,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=False,
        )
        rendered = dict(state)
        rendered["worktree"] = str(worktree_path)
        if status.returncode == 0:
            changes = [
                item.decode("utf-8", errors="surrogateescape")
                for item in status.stdout.split(b"\0")
                if item
            ]
            rendered["dirty"] = bool(changes)
            rendered["changes"] = changes
        else:
            rendered["dirty"] = None
            rendered["status_error"] = decode_output(status.stderr) or f"exit {status.returncode}"
        selected.append(rendered)
    return selected


def ensure_git_identity(git: str, repository: Path) -> None:
    for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        result = run_git(
            git,
            repository,
            ["var", variable],
            check=False,
            safe_directory=False,
        )
        if result.returncode != 0:
            detail = decode_output(result.stderr) or "user.name/user.email are not configured"
            raise HarnessError(
                f"Git identity is unavailable ({variable}); configure it before committing: {detail}"
            )


def temporary_git_command(
    git: str,
    git_dir: Path,
    work_tree: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [git, f"--git-dir={git_dir}", f"--work-tree={work_tree}", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=git_environment(),
    )
    if check and result.returncode != 0:
        detail = decode_output(result.stderr) or decode_output(result.stdout) or f"exit {result.returncode}"
        raise HarnessError(f"Git preview failed ({' '.join(arguments)}): {detail}")
    return result


def sensitive_file_reason(path: Path, content: bytes) -> str | None:
    lowered = path.name.lower()
    if lowered == ".env" or (
        lowered.startswith(".env.") and path.suffix.lower() not in SAFE_ENV_SUFFIXES
    ):
        return "environment secrets file"
    if lowered in SENSITIVE_BASENAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
        return "sensitive credential/key filename"
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(content):
            return label
    return None


def inspect_baseline_files(
    root: Path,
    relative_paths: Iterable[str],
    operation_content: dict[str, bytes],
) -> list[BaselineFile]:
    entries: list[BaselineFile] = []
    unsafe: list[str] = []
    for relative in sorted(set(relative_paths)):
        path = root / Path(relative)
        try:
            ensure_inside_root(path, root)
        except HarnessError as exc:
            unsafe.append(f"{relative} ({exc})")
            continue
        if path.is_symlink():
            unsafe.append(f"{relative} (symbolic link requires explicit user review)")
            continue
        if path.is_dir():
            unsafe.append(f"{relative} (embedded directory/repository entry)")
            continue
        content = operation_content.get(relative)
        if content is None:
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise HarnessError(f"Cannot inspect baseline file {path}: {exc}") from exc
            if size > BASELINE_MAX_FILE_BYTES:
                unsafe.append(
                    f"{relative} ({size} bytes exceeds {BASELINE_MAX_FILE_BYTES}-byte baseline limit)"
                )
                continue
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise HarnessError(f"Cannot read baseline file {path}: {exc}") from exc
        else:
            size = len(content)
            if size > BASELINE_MAX_FILE_BYTES:
                unsafe.append(
                    f"{relative} ({size} bytes exceeds {BASELINE_MAX_FILE_BYTES}-byte baseline limit)"
                )
                continue
        reason = sensitive_file_reason(path, content)
        if reason:
            unsafe.append(f"{relative} ({reason})")
            continue
        entries.append(
            BaselineFile(
                relative=relative,
                size=size,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    if unsafe:
        raise HarnessError(
            "Baseline contains files that require user review or ignore rules; no files were written: "
            + "; ".join(unsafe)
        )
    return entries


def build_new_repository_baseline(
    root: Path,
    operations: Sequence[Operation],
    git: str,
) -> list[BaselineFile]:
    with tempfile.TemporaryDirectory(prefix="project-harness-git-preview-") as temporary:
        preview_root = Path(temporary)
        run_git(git, preview_root, ["init", "-b", "main"], safe_directory=False)
        ensure_git_identity(git, preview_root)
        git_dir = preview_root / ".git"
        existing = git_path_list(
            temporary_git_command(
                git,
                git_dir,
                root,
                ["ls-files", "--others", "--exclude-standard", "-z"],
            )
        )
        operation_content: dict[str, bytes] = {}
        for operation in operations:
            relative = operation.path.resolve().relative_to(root).as_posix()
            ignored = temporary_git_command(
                git,
                git_dir,
                root,
                ["check-ignore", "--no-index", "--quiet", "--", relative],
                check=False,
            )
            if ignored.returncode == 0:
                raise HarnessError(
                    f"Governance path is ignored by Git; update .gitignore manually before initialization: {relative}"
                )
            if ignored.returncode != 1:
                detail = decode_output(ignored.stderr) or f"exit {ignored.returncode}"
                raise HarnessError(f"Could not verify Git ignore rules for {relative}: {detail}")
            operation_content[relative] = operation.new_raw
        return inspect_baseline_files(root, [*existing, *operation_content], operation_content)


def print_baseline_plan(entries: Sequence[BaselineFile], *, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"{mode} Git bootstrap: git init -b main")
    print(f"{mode} baseline files: {len(entries)}")
    for entry in entries:
        print(
            f"BASELINE {entry.size:10d} {entry.sha256} "
            f"{json.dumps(entry.relative, ensure_ascii=False)}"
        )


def baseline_manifest_digest(root: Path, entries: Sequence[BaselineFile]) -> str:
    manifest = {
        "schema": "project-harness-bootstrap-baseline-v1",
        "project_root": str(root),
        "files": [
            {
                "path": entry.relative,
                "size": entry.size,
                "sha256": entry.sha256,
            }
            for entry in sorted(entries, key=lambda item: item.relative)
        ],
    }
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def baseline_plan_token(planned_at: datetime, digest: str) -> str:
    if planned_at.tzinfo is None:
        raise HarnessError("Baseline plan timestamp must include a timezone")
    timestamp = planned_at.isoformat(timespec="seconds")
    return f"v1:{timestamp}:{digest}"


def parse_baseline_plan_token(value: str) -> tuple[datetime, str]:
    token = value.strip()
    try:
        version, remainder = token.split(":", 1)
        timestamp, digest = remainder.rsplit(":", 1)
        planned_at = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError) as exc:
        raise HarnessError(
            "Invalid --accept-baseline-plan token; copy the exact BASELINE_PLAN_TOKEN from init --dry-run"
        ) from exc
    if version != "v1" or planned_at.tzinfo is None or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HarnessError(
            "Invalid --accept-baseline-plan token; copy the exact BASELINE_PLAN_TOKEN from init --dry-run"
        )
    canonical = baseline_plan_token(planned_at, digest)
    if canonical != token:
        raise HarnessError(
            "Non-canonical --accept-baseline-plan token; copy it unchanged from init --dry-run"
        )
    return planned_at, digest


def git_untracked_files(git: str, root: Path) -> list[str]:
    return git_path_list(
        run_git(git, root, ["ls-files", "--others", "--exclude-standard", "-z"])
    )


def git_changed_files(git: str, root: Path, *, cached: bool) -> list[str]:
    arguments = ["diff"]
    if cached:
        arguments.append("--cached")
    arguments.extend(["--name-only", "-z"])
    return git_path_list(run_git(git, root, arguments))


def git_index_changes_including_intent(git: str, root: Path) -> list[str]:
    return git_path_list(
        run_git(
            git,
            root,
            ["diff", "--cached", "--ita-visible-in-index", "--name-only", "-z"],
        )
    )


def stage_exact_paths(git: str, root: Path, relative_paths: Sequence[str]) -> None:
    payload = b"".join(path.encode("utf-8", errors="surrogateescape") + b"\0" for path in relative_paths)
    run_git(
        git,
        root,
        ["--literal-pathspecs", "add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul"],
        input_bytes=payload,
    )


def print_staged_summary(git: str, root: Path) -> None:
    stats = run_git(git, root, ["diff", "--cached", "--stat", "--no-renames"])
    print("STAGED_PATHS")
    for path in git_changed_files(git, root, cached=True):
        print(json.dumps(path, ensure_ascii=False))
    print("STAGED_STAT")
    print(decode_output(stats.stdout) or "(empty)")


def inspect_staged_blobs(
    git: str,
    root: Path,
    relative_paths: Sequence[str],
    *,
    allow_deletions: bool,
) -> tuple[list[BaselineFile], list[str]]:
    expected = set(relative_paths)
    entries: list[BaselineFile] = []
    present: set[str] = set()
    unsafe: list[str] = []
    index = run_git(git, root, ["ls-files", "--stage", "-z"])
    for record in index.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HarnessError("Could not parse the staged Git index safely") from exc
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        if relative not in expected:
            continue
        present.add(relative)
        if stage != "0":
            unsafe.append(f"{relative} (unmerged index stage {stage})")
            continue
        if mode not in {"100644", "100755"}:
            unsafe.append(f"{relative} (unsupported Git mode {mode}; links/submodules require manual review)")
            continue
        raw_size = decode_output(run_git(git, root, ["cat-file", "-s", object_id]).stdout)
        try:
            blob_size = int(raw_size)
        except ValueError as exc:
            raise HarnessError(f"Could not determine staged blob size for {relative}: {raw_size!r}") from exc
        if blob_size > BASELINE_MAX_FILE_BYTES:
            unsafe.append(
                f"{relative} ({blob_size} bytes exceeds {BASELINE_MAX_FILE_BYTES}-byte commit limit)"
            )
            continue
        content = run_git(git, root, ["cat-file", "blob", object_id]).stdout
        if len(content) != blob_size:
            raise HarnessError(
                f"Staged blob size changed while inspecting {relative}: expected {blob_size}, read {len(content)}"
            )
        reason = sensitive_file_reason(Path(relative), content)
        if reason:
            unsafe.append(f"{relative} ({reason} after Git clean filters)")
            continue
        entries.append(
            BaselineFile(
                relative=relative,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )

    deletions = sorted(expected - present)
    if deletions and not allow_deletions:
        unsafe.extend(f"{relative} (missing from staged tree)" for relative in deletions)
    if unsafe:
        raise HarnessError("Staged commit content requires manual review: " + "; ".join(unsafe))
    for entry in sorted(entries, key=lambda item: item.relative):
        print(
            f"STAGED_BLOB {entry.size:10d} {entry.sha256} "
            f"{json.dumps(entry.relative, ensure_ascii=False)}"
        )
    for relative in deletions:
        print(f"STAGED_DELETE {json.dumps(relative, ensure_ascii=False)}")
    return sorted(entries, key=lambda item: item.relative), deletions


def append_operation(operations: list[Operation], path: Path, new_raw: bytes) -> None:
    old_raw = path.read_bytes() if path.exists() else None
    if old_raw == new_raw:
        return
    operations.append(Operation(path=path, new_raw=new_raw, old_raw=old_raw))


def print_plan(root: Path, operations: Sequence[Operation], *, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"{mode} project root: {root}")
    if not operations:
        print("NO-CHANGES")
        return
    for operation in operations:
        print(f"{operation.action:6} {operation.path.relative_to(root)}")


def apply_operations(root: Path, operations: Sequence[Operation]) -> None:
    for operation in operations:
        ensure_inside_root(operation.path, root)
        operation.path.parent.mkdir(parents=True, exist_ok=True)

    for operation in operations:
        ensure_inside_root(operation.path, root)
        current = operation.path.read_bytes() if operation.path.exists() else None
        if current != operation.old_raw:
            raise HarnessError(f"File changed after planning; refusing to overwrite: {operation.path}")

    staged: list[tuple[Operation, Path]] = []
    try:
        for operation in operations:
            ensure_inside_root(operation.path, root)
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=operation.path.parent,
                prefix=f".{operation.path.name}.",
                suffix=".tmp",
            )
            try:
                handle.write(operation.new_raw)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            staged.append((operation, Path(handle.name)))

        for operation, temporary in staged:
            ensure_inside_root(operation.path, root)
            os.replace(temporary, operation.path)
    finally:
        for _, temporary in staged:
            if temporary.exists():
                temporary.unlink()


def missing_operation_directories(operations: Sequence[Operation], root: Path) -> set[Path]:
    missing: set[Path] = set()
    for operation in operations:
        parent = operation.path.parent
        while parent != root:
            if not parent.exists():
                missing.add(parent)
            parent = parent.parent
    return missing


def rollback_operations(
    operations: Sequence[Operation],
    root: Path,
    created_directories: Iterable[Path] = (),
) -> list[str]:
    errors: list[str] = []
    for operation in reversed(operations):
        try:
            ensure_inside_root(operation.path, root)
            current = operation.path.read_bytes() if operation.path.exists() else None
            if current == operation.old_raw:
                continue
            if current != operation.new_raw:
                errors.append(f"changed after apply: {operation.path}")
                continue
            if operation.old_raw is None:
                operation.path.unlink()
            else:
                operation.path.write_bytes(operation.old_raw)
        except (HarnessError, OSError) as exc:
            errors.append(f"{operation.path}: {exc}")

    for directory in sorted(set(created_directories), key=lambda item: len(item.parts), reverse=True):
        try:
            ensure_inside_root(directory, root)
            directory.rmdir()
        except HarnessError as exc:
            errors.append(f"{directory}: {exc}")
        except OSError:
            pass
    return errors


def remove_created_git_metadata(root: Path) -> str | None:
    marker = root / ".git"
    try:
        marker.resolve(strict=False).relative_to(root)
    except ValueError:
        return f"refusing to remove unexpected Git metadata path: {marker}"
    try:
        if marker.is_symlink() or marker.is_file():
            marker.unlink()
        elif marker.is_dir():
            def make_writable_and_retry(function: object, path: str, _: object) -> None:
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                function(path)  # type: ignore[operator]

            shutil.rmtree(marker, onerror=make_writable_and_retry)
    except OSError as exc:
        return f"could not remove created Git metadata {marker}: {exc}"
    return None


def assert_governance_not_ignored(root: Path, paths: Iterable[Path]) -> None:
    git = shutil.which("git")
    if not git:
        return
    repository = run_git(
        git,
        root,
        ["rev-parse", "--show-toplevel"],
        check=False,
        safe_directory=False,
    )
    if repository.returncode != 0 or Path(decode_output(repository.stdout)).resolve() != root:
        return
    ignored: list[str] = []
    for path in paths:
        ensure_inside_root(path, root)
        relative = path.resolve(strict=False).relative_to(root).as_posix()
        result = run_git(
            git,
            root,
            ["check-ignore", "--no-index", "--quiet", "--", relative],
            check=False,
        )
        if result.returncode == 0:
            ignored.append(relative)
        elif result.returncode != 1:
            detail = decode_output(result.stderr) or f"exit {result.returncode}"
            raise HarnessError(f"Could not verify Git ignore rules for {relative}: {detail}")
    if ignored:
        raise HarnessError(
            "Governance paths are ignored by Git; update repository-specific ignore rules "
            "manually before initialization: " + ", ".join(sorted(set(ignored)))
        )


def section_body(text: str, start: str, end: str) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise HarnessError(f"Expected exactly one managed section: {start} ... {end}")
    left = text.index(start) + len(start)
    right = text.index(end, left)
    return text[left:right]


def replace_section(text: str, start: str, end: str, body: str, newline: str) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise HarnessError(f"Expected exactly one managed section: {start} ... {end}")
    left = text.index(start) + len(start)
    right = text.index(end, left)
    normalized = normalize_newlines(body.strip("\r\n"), newline)
    replacement = newline + normalized + newline if normalized else newline
    return text[:left] + replacement + text[right:]


def strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def find_existing_numbers(iterations_dir: Path) -> list[int]:
    numbers: list[int] = []
    if not iterations_dir.exists():
        return numbers
    for child in iterations_dir.iterdir():
        if child.is_dir() and re.fullmatch(r"\d{3,}", child.name):
            numbers.append(int(child.name))
    return sorted(numbers)


def next_session_id(progress_text: str, now: datetime) -> str:
    date = now.strftime("%Y%m%d")
    matches = [int(value) for value in re.findall(rf"S-{date}-(\d{{2,}})", progress_text)]
    sequence = (max(matches) + 1) if matches else 1
    return f"S-{date}-{sequence:02d}"


def assert_fresh_or_managed(root: Path) -> None:
    harness = root / "harness"
    ensure_inside_root(harness, root)
    if not harness.exists():
        return
    if not harness.is_dir():
        raise HarnessError(f"Expected a directory but found a file: {harness}")
    readme = harness / "README.md"
    if readme.exists():
        ensure_inside_root(readme, root)
        if not has_owner_marker(read_document(readme).text):
            raise HarnessError(
                "Existing harness/README.md is not owned by harness-lite; "
                "inspect and adopt it manually instead of mixing governance systems"
            )
        return
    entries = list(harness.iterdir())
    if entries:
        raise HarnessError(
            "Existing harness/ contains content but no managed README; inspect it before initialization"
        )


def build_init_operations(root: Path, project_name: str, now: datetime) -> list[Operation]:
    assert_fresh_or_managed(root)
    if now.tzinfo is None:
        raise HarnessError("Harness initialization requires a timezone-aware plan timestamp")
    harness = root / "harness"
    values = {
        "PROJECT_NAME": project_name,
        "DATE": now.date().isoformat(),
        "TIMESTAMP": now.isoformat(timespec="seconds"),
        "SESSION_ID": f"S-{now.strftime('%Y%m%d')}-01",
    }
    operations: list[Operation] = []

    managed_templates = {
        harness / "README.md": "harness-readme.md.tmpl",
        harness / "principle.md": "principle.md.tmpl",
        harness / "progress.md": "progress.md.tmpl",
    }
    for path, template_name in managed_templates.items():
        ensure_inside_root(path, root)
        rendered = load_template(template_name, values)
        if not path.exists() or path.stat().st_size == 0:
            append_operation(operations, path, rendered.encode("utf-8"))
        else:
            document = read_document(path)
            if not has_owner_marker(document.text):
                raise HarnessError(f"Managed Harness file lost its ownership marker: {path}")

    keep = harness / "iterations" / ".gitkeep"
    ensure_inside_root(keep, root)
    if not keep.exists():
        append_operation(operations, keep, b"")

    agents = root / "AGENTS.md"
    ensure_inside_root(agents, root)
    block = load_template("root-agents-block.md.tmpl", {})
    if not agents.exists() or agents.stat().st_size == 0:
        content = "# Project Instructions\n\n" + block
        append_operation(operations, agents, content.encode("utf-8"))
    else:
        document = read_document(agents)
        starts = document.text.count(AGENTS_START)
        ends = document.text.count(AGENTS_END)
        if starts == 1 and ends == 1 and document.text.index(AGENTS_START) < document.text.index(AGENTS_END):
            pass
        elif starts == 0 and ends == 0:
            lowered = document.text.lower()
            if "harness" in lowered and "prd" in lowered and "spec" in lowered:
                raise HarnessError(
                    "Existing AGENTS.md appears to contain unmarked Harness/PRD/SPEC rules; "
                    "integrate the managed block manually to avoid conflicting control planes"
                )
            newline = document.newline
            separator = "" if document.text.endswith(("\n", "\r")) else newline
            separator += newline
            addition = normalize_newlines(block, newline)
            new_text = document.text + separator + addition
            append_operation(operations, agents, encode_document(new_text, bom=document.bom))
        else:
            raise HarnessError("Malformed or duplicated project-harness markers in AGENTS.md")

    assert_governance_not_ignored(
        root,
        [
            root / "AGENTS.md",
            harness / "README.md",
            harness / "principle.md",
            harness / "progress.md",
            keep,
        ],
    )
    return operations


def parse_status(text: str, label: str) -> str | None:
    match = re.search(rf"^- {re.escape(label)}：`([^`]+)`\s*$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_table_row(text: str, number: str) -> list[str] | None:
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", cells[0]):
            return cells
    return None


def deviation_entries(text: str) -> list[DeviationEntry]:
    clean = strip_html_comments(text)
    headings = list(
        re.finditer(
            r"^### (DEV-(\d{3,})-\d{3,})：\S.*$",
            clean,
            flags=re.MULTILINE,
        )
    )
    result: list[DeviationEntry] = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(clean)
        block = clean[match.end() : end]
        status_match = re.search(r"^- 状态：`?([^`\r\n]+)`?\s*$", block, flags=re.MULTILINE)
        result.append(
            DeviationEntry(
                identity=match.group(1),
                status=status_match.group(1).strip() if status_match else None,
                body=block,
            )
        )
    return result


def malformed_deviation_headings(text: str) -> list[str]:
    clean = strip_html_comments(text)
    visible = re.findall(r"^[ \t]{0,3}#{1,6}[ \t]+DEV-[^\r\n]*\r?$", clean, flags=re.MULTILINE)
    canonical = re.compile(r"^### DEV-\d{3,}-\d{3,}：\S.*$")
    return [heading.strip() for heading in visible if not canonical.fullmatch(heading)]


def bullet_value(text: str, label: str) -> str | None:
    match = re.search(rf"^- {re.escape(label)}：[ \t]*(.*?)[ \t]*$", text, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("`").strip()


def meaningful_value(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip()
    if not normalized or normalized.startswith("<"):
        return False
    return not any(
        normalized.startswith(marker)
        for marker in ("尚无", "待定义", "待处置", "无证据")
    )


def explicit_user_acceptance_evidence(value: str | None) -> bool:
    if not meaningful_value(value):
        return False
    normalized = value.strip().casefold()  # type: ignore[union-attr]
    if re.search(
        r"(?:尚未|未曾|未明确|没有|拒绝|不(?:予|同意|接受|批准|通过|验收)|"
        r"待(?:用户|验收|确认)|等待|可能|也许|拟|计划|默认|推断|假定)",
        normalized,
    ):
        return False
    if re.search(
        r"\b(?:not|never|without|rejected|declined|pending|awaiting|maybe|may|might|possibly|"
        r"planned|assumed|inferred)\b|\bnot yet\b|\bno explicit\b",
        normalized,
    ):
        return False
    actor = "用户" in normalized or re.search(r"\buser\b", normalized)
    acceptance = re.search(
        r"验收.{0,8}(?:通过|完成|接受|确认)|(?:通过|接受|确认).{0,8}验收|"
        r"\bacceptance\b.{0,16}\bpass(?:ed)?\b|"
        r"\baccept(?:ed|s)?\b.{0,24}\b(?:completed|implemented|delivered|iteration|result)\b",
        normalized,
    )
    return bool(actor and acceptance)


def explicit_user_baseline_approval(value: str | None, identity: str) -> bool:
    if not meaningful_value(value):
        return False
    normalized = value.strip().casefold()  # type: ignore[union-attr]
    if re.search(
        r"(?:尚未|未曾|未明确|未(?:予|同意|批准|确认)|没有|拒绝|不(?:予|同意|批准|确认)|"
        r"待(?:用户|批准|确认)|等待|可能|也许|拟|计划|默认|推断|假定)",
        normalized,
    ):
        return False
    if re.search(
        r"\b(?:not|never|without|rejected|declined|pending|awaiting|maybe|may|might|possibly|"
        r"planned|assumed|inferred)\b|\bnot yet\b|\bno explicit\b",
        normalized,
    ):
        return False
    actor = "用户" in normalized or re.search(r"\buser\b", normalized)
    approval = re.search(
        r"批准|同意.{0,8}(?:基线|规格|prd|spec)|确认.{0,8}(?:批准|基线|规格)|"
        r"\bapprov(?:e|ed|al)\b|\bconfirm(?:ed|s)?\b.{0,16}\b(?:baseline|specification)\b",
        normalized,
    )
    return bool(actor and approval and identity.casefold() in normalized)


def explicit_user_implementation_authorization(value: str | None) -> bool:
    if not meaningful_value(value):
        return False
    normalized = value.strip().casefold()  # type: ignore[union-attr]
    if re.search(
        r"(?:尚未|未曾|未明确|未(?:予|同意|授权|批准|允许|要求|指示|实施|实现|开发|执行)|"
        r"没有|拒绝|不(?:予|同意|授权|批准|允许|要求|指示|实施|实现|开发|执行)|"
        r"待(?:用户|批准|授权|确认|实施)|等待|可能|也许|拟|计划|默认|推断|假定)",
        normalized,
    ):
        return False
    if re.search(
        r"\b(?:not|never|without|rejected|declined|pending|awaiting|maybe|may|might|possibly|"
        r"planned|assumed|inferred)\b|\bnot yet\b|\bno explicit\b",
        normalized,
    ):
        return False
    actor = "用户" in normalized or re.search(r"\buser\b", normalized)
    authorization = re.search(
        r"(?:授权|允许|要求|指示|批准).{0,12}(?:开始|实施|实现|开发|执行)|"
        r"(?:开始|实施|实现|开发|执行).{0,12}(?:获准|授权|允许|要求|批准)|"
        r"\bauthori[sz](?:e|ed|ation)\b.{0,32}\b(?:begin|start|proceed|implement(?:ation)?|execute|develop(?:ment)?)\b|"
        r"\b(?:begin|start|proceed|implement(?:ation)?|execute|develop(?:ment)?)\b.{0,32}\bauthori[sz](?:e|ed|ation)\b|"
        r"\bgo ahead\b.{0,24}\b(?:implement(?:ation)?|execute|develop(?:ment)?)\b",
        normalized,
    )
    return bool(actor and authorization)


def meaningful_verification_evidence(value: str | None) -> bool:
    if not meaningful_value(value):
        return False
    normalized = value.strip().casefold()  # type: ignore[union-attr]
    if len(normalized) < 8:
        return False
    if re.search(
        r"(?:尚待|尚未|未运行|未执行|未完成|等待|待验证|验证计划|测试计划|"
        r"没有.{0,8}(?:测试|验证)|无.{0,8}(?:测试|验证))",
        normalized,
    ):
        return False
    if re.search(
        r"\b(?:not run|not executed|not completed|pending|awaiting|planned|no tests?|no verification)\b",
        normalized,
    ):
        return False
    return bool(
        re.search(
            r"测试|验证|检查|验收|通过|失败|命令|日志|报告|截图|\btest(?:ed|s)?\b|\bverif(?:y|ied|ication)\b|"
            r"\bcheck(?:ed|s)?\b|\bpass(?:ed)?\b|\bfail(?:ed)?\b|\bprov(?:e|es|ed)\b|\blog\b|\breport\b",
            normalized,
        )
    )


def meaningful_timestamp(value: str | None) -> bool:
    if not meaningful_value(value):
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][^\s]+)?", value.strip()))  # type: ignore[union-attr]


def validate_markers(report: ValidationReport, path: Path, text: str, start: str, end: str) -> None:
    if text.count(start) != 1 or text.count(end) != 1:
        report.add("error", "managed-markers", path, f"Expected one {start} and one {end}")
    elif text.index(start) > text.index(end):
        report.add("error", "managed-markers", path, "Managed section end appears before start")


def validate_local_links(report: ValidationReport, harness: Path) -> None:
    link_pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    markdown_files: list[Path] = []
    for current, directory_names, file_names in os.walk(harness, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for directory_name in directory_names:
            candidate = current_path / directory_name
            try:
                ensure_inside_root(candidate, report.root)
            except HarnessError as exc:
                report.add("error", "unsafe-path", candidate, str(exc))
                continue
            safe_directories.append(directory_name)
        directory_names[:] = safe_directories
        for file_name in file_names:
            if not file_name.lower().endswith(".md"):
                continue
            candidate = current_path / file_name
            try:
                ensure_inside_root(candidate, report.root)
            except HarnessError as exc:
                report.add("error", "unsafe-path", candidate, str(exc))
                continue
            markdown_files.append(candidate)

    for markdown in markdown_files:
        try:
            text = read_document(markdown).text
        except HarnessError as exc:
            report.add("error", "encoding", markdown, str(exc))
            continue
        for raw_target in link_pattern.findall(strip_html_comments(text)):
            target = raw_target.strip().strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            candidate = (markdown.parent / target).resolve()
            try:
                candidate.relative_to(harness.resolve())
            except ValueError:
                report.add("warning", "external-local-link", markdown, f"Link leaves harness/: {raw_target}")
                continue
            if not candidate.exists():
                report.add("error", "broken-link", markdown, f"Missing local link target: {raw_target}")


def validate_git_tracking(report: ValidationReport, root: Path) -> None:
    git = shutil.which("git")
    if not git:
        code = "git-unavailable" if (root / ".git").exists() else "no-git"
        report.add("warning", code, root, "Git executable was not found; version traceability cannot be verified")
        return
    discovery = run_git(
        git,
        root,
        ["rev-parse", "--show-toplevel"],
        check=False,
        safe_directory=False,
    )
    if discovery.returncode != 0:
        report.add("warning", "no-git", root, "No usable Git repository found; run Harness initialization to create one")
        return
    discovered = Path(decode_output(discovery.stdout)).resolve()
    if discovered != root:
        report.add("error", "nested-project-root", root, f"Project root is inside Git repository {discovered}")
        return
    head = run_git(git, root, ["rev-parse", "--verify", "HEAD"], check=False)
    if head.returncode != 0:
        report.add(
            "warning",
            "git-unborn",
            root,
            "Git exists but has no baseline commit; new iterations are blocked until the user authorizes one",
        )
    for relative in ("AGENTS.md", "harness/README.md", "harness/principle.md", "harness/progress.md"):
        result = run_git(
            git,
            root,
            ["check-ignore", "--no-index", "--quiet", "--", relative],
            check=False,
        )
        if result.returncode == 0:
            report.add("error", "git-ignored", root / relative, "Governance file is ignored by Git")
            continue
        if result.returncode != 1:
            report.add(
                "error",
                "git-ignore-check",
                root / relative,
                decode_output(result.stderr) or f"Git check-ignore exited {result.returncode}",
            )
            continue
        tracked = run_git(
            git,
            root,
            ["ls-files", "--error-unmatch", "--", relative],
            check=False,
        )
        if tracked.returncode != 0:
            report.add("warning", "git-untracked", root / relative, "Governance file is not yet tracked by Git")


def collect_validation(root: Path) -> ValidationReport:
    report = ValidationReport(root)
    harness = root / "harness"
    agents = root / "AGENTS.md"
    anchor_git = shutil.which("git")
    exact_git_repository = False
    if anchor_git:
        discovery = run_git(
            anchor_git,
            root,
            ["rev-parse", "--show-toplevel"],
            check=False,
            safe_directory=False,
        )
        exact_git_repository = (
            discovery.returncode == 0
            and Path(decode_output(discovery.stdout)).resolve() == root
        )

    try:
        ensure_inside_root(harness, root)
    except HarnessError as exc:
        report.add("error", "unsafe-path", harness, str(exc))
        validate_git_tracking(report, root)
        return report
    if not harness.is_dir():
        report.add("error", "missing-harness", harness, "Missing harness/ directory")
        validate_git_tracking(report, root)
        return report
    ensure_paths = [agents, harness / "README.md", harness / "principle.md", harness / "progress.md"]
    unsafe_paths: set[Path] = set()
    for path in ensure_paths:
        try:
            ensure_inside_root(path, root)
        except HarnessError as exc:
            report.add("error", "unsafe-path", path, str(exc))
            unsafe_paths.add(path)

    required = [harness / "README.md", harness / "principle.md", harness / "progress.md"]
    documents: dict[Path, Document] = {}
    for path in required:
        if path in unsafe_paths:
            continue
        if not path.is_file():
            report.add("error", "missing-global", path, "Missing required Harness document")
            continue
        try:
            documents[path] = read_document(path)
        except HarnessError as exc:
            report.add("error", "encoding", path, str(exc))
            continue
        if not has_owner_marker(documents[path].text):
            report.add("error", "owner-marker", path, "Missing harness-lite ownership marker")

    if agents in unsafe_paths:
        pass
    elif not agents.is_file():
        report.add("error", "missing-agents", agents, "Missing root AGENTS.md control file")
    else:
        try:
            agents_text = read_document(agents).text
            validate_markers(report, agents, agents_text, AGENTS_START, AGENTS_END)
        except HarnessError as exc:
            report.add("error", "encoding", agents, str(exc))

    root_readme = documents.get(harness / "README.md")
    progress = documents.get(harness / "progress.md")
    if root_readme:
        validate_markers(report, harness / "README.md", root_readme.text, FOCUS_START, FOCUS_END)
        validate_markers(report, harness / "README.md", root_readme.text, ITERATIONS_START, ITERATIONS_END)
    if progress:
        validate_markers(report, harness / "progress.md", progress.text, PROGRESS_INDEX_START, PROGRESS_INDEX_END)

    for pattern in ("prd-*.md", "spec-*.md"):
        for path in harness.glob(pattern):
            report.add("error", "flat-governance-file", path, "Numbered PRD/SPEC must live in iterations/NNN/")
    legacy_deviation = harness / "deviation.md"
    if legacy_deviation.exists():
        report.add("error", "flat-deviation", legacy_deviation, "Use one deviation-NNN.md per iteration")

    iterations = harness / "iterations"
    try:
        ensure_inside_root(iterations, root)
    except HarnessError as exc:
        report.add("error", "unsafe-path", iterations, str(exc))
        validate_git_tracking(report, root)
        return report
    if not iterations.is_dir():
        report.add("error", "missing-iterations", iterations, "Missing harness/iterations/ directory")
        validate_local_links(report, harness)
        validate_git_tracking(report, root)
        return report

    numbers: list[str] = []
    all_deviation_ids: dict[str, Path] = {}
    for child in sorted(iterations.iterdir(), key=lambda item: item.name):
        try:
            ensure_inside_root(child, root)
        except HarnessError as exc:
            report.add("error", "unsafe-path", child, str(exc))
            continue
        if child.name == ".gitkeep":
            if not child.is_file():
                report.add("error", "invalid-gitkeep", child, "iterations/.gitkeep must be a regular file")
            continue
        if not child.is_dir() or not re.fullmatch(r"\d{3,}", child.name):
            report.add("error", "invalid-iteration-entry", child, "Iteration entries must be NNN directories")
            continue
        number = child.name
        numbers.append(number)
        expected_names = {"README.md", f"prd-{number}.md", f"spec-{number}.md", f"deviation-{number}.md"}
        actual_entries = list(child.iterdir())
        unsafe_bundle = False
        for entry in actual_entries:
            try:
                ensure_inside_root(entry, root)
            except HarnessError as exc:
                report.add("error", "unsafe-path", entry, str(exc))
                unsafe_bundle = True
        actual_names = {path.name for path in actual_entries}
        for missing in sorted(expected_names - actual_names):
            report.add("error", "incomplete-bundle", child / missing, "Missing iteration bundle file")
        for extra in sorted(actual_names - expected_names):
            report.add("error", "extra-bundle-file", child / extra, "Iteration directory must contain exactly the four governance files")
        if expected_names - actual_names or unsafe_bundle:
            continue

        paths = {
            "readme": child / "README.md",
            "prd": child / f"prd-{number}.md",
            "spec": child / f"spec-{number}.md",
            "deviation": child / f"deviation-{number}.md",
        }
        invalid_bundle_file = False
        for path in paths.values():
            try:
                ensure_inside_root(path, root)
            except HarnessError as exc:
                report.add("error", "unsafe-path", path, str(exc))
                invalid_bundle_file = True
                continue
            if not path.is_file():
                report.add("error", "invalid-bundle-file", path, "Iteration governance entries must be regular files")
                invalid_bundle_file = True
        if invalid_bundle_file:
            continue
        try:
            texts = {key: read_document(path).text for key, path in paths.items()}
        except HarnessError as exc:
            report.add("error", "encoding", child, str(exc))
            continue
        for key, path in paths.items():
            if not has_owner_marker(texts[key]):
                report.add("error", "owner-marker", path, "Missing harness-lite ownership marker")

        if f"PRD-{number}" not in texts["prd"]:
            report.add("error", "prd-id", paths["prd"], f"Missing PRD-{number} identity")
        git_baseline = bullet_value(texts["prd"], "Git 基线")
        if not git_baseline or not re.fullmatch(r"[0-9a-fA-F]{40,64}", git_baseline):
            report.add("error", "prd-git-baseline", paths["prd"], "Missing or invalid immutable Git baseline")
        git_branch = bullet_value(texts["prd"], "Git 分支")
        if not git_branch or not re.fullmatch(r"refs/heads/[^\s`]+", git_branch):
            report.add("error", "prd-git-branch", paths["prd"], "Missing or invalid immutable Git branch")
        if exact_git_repository and anchor_git:
            try:
                _, anchored_commit, anchored_branch = read_iteration_base_anchor(
                    anchor_git,
                    root,
                    number,
                )
            except HarnessError as exc:
                report.add("error", "iteration-base-anchor", paths["prd"], str(exc))
            else:
                if git_baseline and git_baseline.lower() != anchored_commit.lower():
                    report.add(
                        "error",
                        "iteration-base-anchor-drift",
                        paths["prd"],
                        f"PRD Git baseline {git_baseline} differs from immutable anchor {anchored_commit}",
                    )
                if git_branch != anchored_branch:
                    report.add(
                        "error",
                        "iteration-branch-anchor-drift",
                        paths["prd"],
                        f"PRD Git branch {git_branch!r} differs from immutable anchor {anchored_branch!r}",
                    )
            for bundle_path in paths.values():
                relative = bundle_path.relative_to(root).as_posix()
                ignored = run_git(
                    anchor_git,
                    root,
                    ["check-ignore", "--no-index", "--quiet", "--", relative],
                    check=False,
                )
                if ignored.returncode == 0:
                    report.add(
                        "error",
                        "git-ignored-iteration-file",
                        bundle_path,
                        "Iteration governance file is ignored by Git",
                    )
                elif ignored.returncode != 1:
                    report.add(
                        "error",
                        "git-ignore-check",
                        bundle_path,
                        decode_output(ignored.stderr) or f"Git check-ignore exited {ignored.returncode}",
                    )
        if f"SPEC-{number}" not in texts["spec"]:
            report.add("error", "spec-id", paths["spec"], f"Missing SPEC-{number} identity")
        if f"PRD-{number}" not in texts["deviation"] or f"SPEC-{number}" not in texts["deviation"]:
            report.add("error", "deviation-owner", paths["deviation"], "Deviation ledger does not identify its same-number PRD/SPEC")

        prd_status = parse_status(texts["prd"], "状态")
        spec_status = parse_status(texts["spec"], "状态")
        l1_prd_status = parse_status(texts["readme"], "PRD 状态")
        l1_spec_status = parse_status(texts["readme"], "SPEC 状态")
        l1_open = parse_status(texts["readme"], "开放偏差")
        if prd_status not in PRD_STATUSES:
            report.add("error", "prd-status", paths["prd"], f"Invalid or missing PRD status: {prd_status}")
        if spec_status not in SPEC_STATUSES:
            report.add("error", "spec-status", paths["spec"], f"Invalid or missing SPEC status: {spec_status}")
        if spec_status in {"实施中", "已完成"} and not explicit_user_implementation_authorization(
            bullet_value(texts["spec"], "实施授权")
        ):
            report.add("error", "implementation-authorization", paths["spec"], f"SPEC {spec_status} lacks explicit implementation authorization")
        if prd_status in {"已批准", "实施中", "待验收", "已验收"}:
            if not explicit_user_baseline_approval(bullet_value(texts["prd"], "批准依据"), f"PRD-{number}"):
                report.add(
                    "error",
                    "prd-approval-evidence",
                    paths["prd"],
                    f"PRD {prd_status} lacks explicit user approval of the product baseline",
                )
            clean_prd = strip_html_comments(texts["prd"])
            remaining = [fragment for fragment in PRD_TEMPLATE_PLACEHOLDERS if fragment in clean_prd]
            if "待定义" in clean_prd or remaining:
                report.add(
                    "error",
                    "prd-template-placeholder",
                    paths["prd"],
                    "Approved/implemented PRD still contains bundled template placeholders",
                )
        if spec_status in {"待批准", "已批准", "实施中", "已完成"}:
            approved_baseline = bullet_value(texts["spec"], "当前批准基线")
            if not meaningful_value(approved_baseline) or f"PRD-{number}" not in (approved_baseline or ""):
                report.add(
                    "error",
                    "spec-approved-baseline",
                    paths["spec"],
                    f"SPEC {spec_status} must identify the approved PRD-{number} baseline",
                )
        if spec_status in {"已批准", "实施中", "已完成"}:
            if not explicit_user_baseline_approval(bullet_value(texts["spec"], "批准依据"), f"SPEC-{number}"):
                report.add(
                    "error",
                    "spec-approval-evidence",
                    paths["spec"],
                    f"SPEC {spec_status} lacks explicit user approval of the implementation baseline",
                )
        if spec_status in {"实施中", "已完成"}:
            clean_spec = strip_html_comments(texts["spec"])
            remaining = [fragment for fragment in SPEC_TEMPLATE_PLACEHOLDERS if fragment in clean_spec]
            if "待定义" in clean_spec or remaining:
                report.add(
                    "error",
                    "spec-template-placeholder",
                    paths["spec"],
                    "Implemented SPEC still contains bundled template placeholders",
                )
        if "PRD 当前状态" in texts["spec"]:
            report.add("error", "duplicate-status-owner", paths["spec"], "SPEC must not duplicate PRD status; derive it from the PRD")
        if l1_prd_status != prd_status:
            report.add("error", "l1-prd-drift", paths["readme"], f"L1 PRD status {l1_prd_status!r} differs from PRD {prd_status!r}")
        if l1_spec_status != spec_status:
            report.add("error", "l1-spec-drift", paths["readme"], f"L1 SPEC status {l1_spec_status!r} differs from SPEC {spec_status!r}")

        for heading in malformed_deviation_headings(texts["deviation"]):
            report.add(
                "error",
                "malformed-deviation-heading",
                paths["deviation"],
                f"Deviation heading must use canonical '### DEV-NNN-SSS：title' syntax: {heading}",
            )
        entries = deviation_entries(texts["deviation"])
        if entries and spec_status != "已完成":
            report.add(
                "error",
                "deviation-before-completion",
                paths["deviation"],
                "As-built deviations may be recorded only after the SPEC implementation is completed",
            )
        open_count = 0
        for entry in entries:
            deviation_id = entry.identity
            status = entry.status
            if not deviation_id.startswith(f"DEV-{number}-"):
                report.add("error", "deviation-prefix", paths["deviation"], f"Wrong iteration prefix: {deviation_id}")
            if deviation_id in all_deviation_ids:
                report.add("error", "duplicate-deviation", paths["deviation"], f"Duplicate {deviation_id}; first seen in {all_deviation_ids[deviation_id]}")
            all_deviation_ids[deviation_id] = paths["deviation"]
            if status is None:
                report.add("error", "deviation-status", paths["deviation"], f"Missing status for {deviation_id}")
            elif status not in DEVIATION_STATUSES:
                report.add("error", "deviation-status", paths["deviation"], f"Invalid status {status!r} for {deviation_id}")
            elif status in UNRESOLVED_DEVIATION_STATUSES:
                open_count += 1
            required_facts = (
                ("原批准内容", "deviation-baseline", "the original approved promise"),
                ("as-built 事实", "deviation-as-built", "an as-built fact"),
                ("原因", "deviation-cause", "a factual cause or current investigation result"),
                ("影响", "deviation-impact", "impact analysis"),
                ("验收影响", "deviation-acceptance-impact", "acceptance impact"),
            )
            if not meaningful_timestamp(bullet_value(entry.body, "发现时间")):
                report.add("error", "deviation-discovered-at", paths["deviation"], f"{deviation_id} lacks an ISO discovery date/time")
            association = bullet_value(entry.body, "关联需求/验收")
            if not (
                meaningful_value(association)
                and re.search(rf"(?<![A-Z0-9])R-{re.escape(number)}-\d+(?!\d)", association or "")
                and re.search(rf"(?<![A-Z0-9])AC-{re.escape(number)}-\d+(?!\d)", association or "")
            ):
                report.add(
                    "error",
                    "deviation-requirement-reference",
                    paths["deviation"],
                    f"{deviation_id} must cite same-iteration R-{number}-... and AC-{number}-... IDs",
                )
            spec_reference = bullet_value(entry.body, "SPEC 章节")
            if not (
                meaningful_value(spec_reference)
                and ("§" in (spec_reference or "") or f"SPEC-{number}" in (spec_reference or ""))
            ):
                report.add(
                    "error",
                    "deviation-spec-reference",
                    paths["deviation"],
                    f"{deviation_id} must cite an exact SPEC-{number} section",
                )
            for label, code, description in required_facts:
                if not meaningful_value(bullet_value(entry.body, label)):
                    report.add("error", code, paths["deviation"], f"{deviation_id} lacks {description}")
            if status and status not in UNRESOLVED_DEVIATION_STATUSES:
                if not meaningful_value(bullet_value(entry.body, "明确处置")):
                    report.add("error", "deviation-disposition", paths["deviation"], f"{deviation_id} lacks an explicit disposition")
                if not meaningful_value(bullet_value(entry.body, "处置依据")):
                    report.add("error", "deviation-disposition-evidence", paths["deviation"], f"{deviation_id} lacks disposition evidence")
                if not meaningful_verification_evidence(bullet_value(entry.body, "验证")):
                    report.add("error", "deviation-verification", paths["deviation"], f"{deviation_id} lacks concrete verification evidence")
                if not meaningful_timestamp(bullet_value(entry.body, "关闭或转交时间")):
                    report.add("error", "deviation-closure", paths["deviation"], f"{deviation_id} lacks an ISO closure or transfer date/time")
                disposition_evidence = bullet_value(entry.body, "处置依据")
                if status == "基线已重批" and not (
                    explicit_user_baseline_approval(disposition_evidence, f"PRD-{number}")
                    or explicit_user_baseline_approval(disposition_evidence, f"SPEC-{number}")
                ):
                    report.add(
                        "error",
                        "deviation-reapproval-evidence",
                        paths["deviation"],
                        f"{deviation_id} claims a reapproved baseline without explicit user approval evidence",
                    )
                if status == "已接受残余" and not explicit_user_acceptance_evidence(
                    disposition_evidence
                ):
                    report.add(
                        "error",
                        "deviation-residual-acceptance",
                        paths["deviation"],
                        f"{deviation_id} claims accepted residual behavior without explicit user acceptance evidence",
                    )
                if status == "已转后续迭代":
                    transfer_text = " ".join(
                        filter(
                            None,
                            (
                                bullet_value(entry.body, "明确处置"),
                                disposition_evidence,
                            ),
                        )
                    )
                    targets = re.findall(r"(?<![A-Z0-9])PRD-(\d{3,})(?!\d)", transfer_text)
                    if not targets or all(target == number for target in targets):
                        report.add(
                            "error",
                            "deviation-transfer-target",
                            paths["deviation"],
                            f"{deviation_id} must identify a different receiving PRD-NNN iteration",
                        )
        declared_match = re.search(r"当前开放偏差：`(\d+)`", texts["deviation"])
        declared_open = int(declared_match.group(1)) if declared_match else None
        if declared_open != open_count:
            report.add(
                "error",
                "deviation-open-count-drift",
                paths["deviation"],
                f"Ledger summary says {declared_open!r} open deviations; entries have {open_count}",
            )
        if l1_open != str(open_count):
            report.add("error", "l1-deviation-drift", paths["readme"], f"L1 says {l1_open!r} open deviations; ledger has {open_count}")
        if prd_status in {"待验收", "已验收"}:
            if spec_status != "已完成":
                report.add("error", "acceptance-spec-status", paths["spec"], f"PRD {prd_status} requires SPEC status 已完成")
            if open_count:
                report.add("error", "acceptance-open-deviation", paths["deviation"], f"PRD {prd_status} cannot have unresolved as-built deviations")
        if prd_status == "已验收" and not explicit_user_acceptance_evidence(
            bullet_value(texts["prd"], "验收依据")
        ):
            report.add("error", "acceptance-evidence", paths["prd"], "Accepted PRD lacks explicit user acceptance evidence")

        if root_readme:
            registry_matches = 0
            for line in section_body(root_readme.text, ITERATIONS_START, ITERATIONS_END).splitlines():
                if not line.lstrip().startswith("|"):
                    continue
                first_cell = line.strip().strip("|").split("|", 1)[0]
                if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", first_cell):
                    registry_matches += 1
            if registry_matches != 1:
                report.add("error", "l0-registration-count", harness / "README.md", f"Expected one registry row for {number}; found {registry_matches}")
            row = parse_table_row(root_readme.text, number)
            if row is None or len(row) < 8:
                report.add("error", "l0-registration", harness / "README.md", f"Missing or malformed registry row for {number}")
            else:
                if row[2] != prd_status:
                    report.add("error", "l0-prd-drift", harness / "README.md", f"Registry PRD status for {number} differs from PRD")
                if row[3] != spec_status:
                    report.add("error", "l0-spec-drift", harness / "README.md", f"Registry SPEC status for {number} differs from SPEC")
                if row[4] != str(open_count):
                    report.add("error", "l0-deviation-drift", harness / "README.md", f"Registry open deviation count for {number} differs from ledger")
        if progress:
            row = parse_table_row(section_body(progress.text, PROGRESS_INDEX_START, PROGRESS_INDEX_END), number)
            if row is None or len(row) < 4:
                report.add("error", "progress-index", harness / "progress.md", f"Missing or malformed progress index row for {number}")
            elif row[1] != prd_status or row[2] != spec_status:
                report.add("error", "progress-index-drift", harness / "progress.md", f"Progress index status for {number} differs from authoritative PRD/SPEC")

    if not numbers:
        report.add("warning", "no-iterations", iterations, "Harness is initialized but has no product iteration yet")

    if progress:
        clean_progress = strip_html_comments(progress.text)
        event_pairs: dict[tuple[str, str], int] = {}
        for session_id, event_type in re.findall(
            r"^## (S-\d{8}-\d{2,}) / (OPEN|DECISION|CHECKPOINT|MERGE|CLOSE) /",
            clean_progress,
            flags=re.MULTILINE,
        ):
            pair = (session_id, event_type)
            event_pairs[pair] = event_pairs.get(pair, 0) + 1
        for pair, count in event_pairs.items():
            if count > 1:
                report.add("error", "duplicate-event", harness / "progress.md", f"Duplicate event {pair[0]} / {pair[1]}")

    validate_local_links(report, harness)
    validate_git_tracking(report, root)
    return report


def collect_committed_governance_validation(
    git: str,
    root: Path,
    commit: str,
    entries: Mapping[str, tuple[str, str, bytes]],
) -> ValidationReport:
    """Validate the authoritative governance tree without checking it out or writing temp state."""
    report = ValidationReport(root)
    texts: dict[str, str] = {}
    for relative, (_, _, raw) in entries.items():
        if not relative.lower().endswith(".md"):
            continue
        try:
            texts[relative] = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            report.add("error", "encoding", root / relative, "Committed governance Markdown must be UTF-8")

    required = (
        "AGENTS.md",
        "harness/README.md",
        "harness/principle.md",
        "harness/progress.md",
    )
    for relative in required:
        if relative not in entries:
            report.add("error", "missing-global", root / relative, "Missing required committed governance file")
    for relative in required[1:]:
        text_value = texts.get(relative)
        if text_value is not None and not has_owner_marker(text_value):
            report.add("error", "owner-marker", root / relative, "Missing harness-lite ownership marker")

    agents_text = texts.get("AGENTS.md")
    if agents_text is not None:
        validate_markers(report, root / "AGENTS.md", agents_text, AGENTS_START, AGENTS_END)
    root_readme_text = texts.get("harness/README.md")
    if root_readme_text is not None:
        validate_markers(report, root / "harness/README.md", root_readme_text, FOCUS_START, FOCUS_END)
        validate_markers(report, root / "harness/README.md", root_readme_text, ITERATIONS_START, ITERATIONS_END)
    progress_text = texts.get("harness/progress.md")
    if progress_text is not None:
        validate_markers(
            report,
            root / "harness/progress.md",
            progress_text,
            PROGRESS_INDEX_START,
            PROGRESS_INDEX_END,
        )

    for relative in entries:
        name = relative.removeprefix("harness/")
        if "/" not in name and re.fullmatch(r"(?:prd|spec)-.*\.md", name):
            report.add(
                "error",
                "flat-governance-file",
                root / relative,
                "Numbered PRD/SPEC must live in iterations/NNN/",
            )
    if "harness/deviation.md" in entries:
        report.add(
            "error",
            "flat-deviation",
            root / "harness/deviation.md",
            "Use one deviation-NNN.md per iteration",
        )

    iteration_prefix = "harness/iterations/"
    iteration_members: dict[str, set[str]] = {}
    found_iterations_root = False
    for relative in entries:
        if not relative.startswith(iteration_prefix):
            continue
        found_iterations_root = True
        remainder = relative[len(iteration_prefix) :]
        if remainder == ".gitkeep":
            continue
        if "/" not in remainder:
            report.add(
                "error",
                "invalid-iteration-entry",
                root / relative,
                "Iteration entries must be NNN directories",
            )
            continue
        number, member = remainder.split("/", 1)
        if not re.fullmatch(r"\d{3,}", number):
            report.add(
                "error",
                "invalid-iteration-entry",
                root / iteration_prefix / number,
                "Iteration entries must be NNN directories",
            )
            continue
        iteration_members.setdefault(number, set()).add(member)
    if not found_iterations_root:
        report.add(
            "error",
            "missing-iterations",
            root / "harness/iterations",
            "Missing committed harness/iterations/ tree",
        )

    all_deviation_ids: dict[str, Path] = {}
    for number in sorted(iteration_members):
        child = root / "harness/iterations" / number
        expected_names = {
            "README.md",
            f"prd-{number}.md",
            f"spec-{number}.md",
            f"deviation-{number}.md",
        }
        actual_names = iteration_members[number]
        for missing in sorted(expected_names - actual_names):
            report.add("error", "incomplete-bundle", child / missing, "Missing iteration bundle file")
        for extra in sorted(actual_names - expected_names):
            report.add(
                "error",
                "extra-bundle-file",
                child / extra,
                "Iteration directory must contain exactly the four governance files",
            )
        if expected_names != actual_names:
            continue
        relative_paths = {
            "readme": f"harness/iterations/{number}/README.md",
            "prd": f"harness/iterations/{number}/prd-{number}.md",
            "spec": f"harness/iterations/{number}/spec-{number}.md",
            "deviation": f"harness/iterations/{number}/deviation-{number}.md",
        }
        paths = {key: root / relative for key, relative in relative_paths.items()}
        bundle_texts = {key: texts.get(relative) for key, relative in relative_paths.items()}
        if any(value is None for value in bundle_texts.values()):
            for key, value in bundle_texts.items():
                if value is None:
                    report.add("error", "encoding", paths[key], "Committed bundle file is not valid UTF-8 Markdown")
            continue
        typed_texts = {key: str(value) for key, value in bundle_texts.items()}
        for key, path in paths.items():
            if not has_owner_marker(typed_texts[key]):
                report.add("error", "owner-marker", path, "Missing harness-lite ownership marker")

        if f"PRD-{number}" not in typed_texts["prd"]:
            report.add("error", "prd-id", paths["prd"], f"Missing PRD-{number} identity")
        git_baseline = bullet_value(typed_texts["prd"], "Git 基线")
        if not git_baseline or not re.fullmatch(r"[0-9a-fA-F]{40,64}", git_baseline):
            report.add("error", "prd-git-baseline", paths["prd"], "Missing or invalid immutable Git baseline")
        git_branch = bullet_value(typed_texts["prd"], "Git 分支")
        if not git_branch or not re.fullmatch(r"refs/heads/[^\s`]+", git_branch):
            report.add("error", "prd-git-branch", paths["prd"], "Missing or invalid immutable Git branch")
        try:
            _, anchored_commit, anchored_branch = read_iteration_base_anchor(git, root, number)
        except HarnessError as exc:
            report.add("error", "iteration-base-anchor", paths["prd"], str(exc))
        else:
            if git_baseline and git_baseline.lower() != anchored_commit.lower():
                report.add(
                    "error",
                    "iteration-base-anchor-drift",
                    paths["prd"],
                    f"PRD Git baseline {git_baseline} differs from immutable anchor {anchored_commit}",
                )
            if git_branch != anchored_branch:
                report.add(
                    "error",
                    "iteration-branch-anchor-drift",
                    paths["prd"],
                    f"PRD Git branch {git_branch!r} differs from immutable anchor {anchored_branch!r}",
                )
        if f"SPEC-{number}" not in typed_texts["spec"]:
            report.add("error", "spec-id", paths["spec"], f"Missing SPEC-{number} identity")
        if f"PRD-{number}" not in typed_texts["deviation"] or f"SPEC-{number}" not in typed_texts["deviation"]:
            report.add(
                "error",
                "deviation-owner",
                paths["deviation"],
                "Deviation ledger does not identify its same-number PRD/SPEC",
            )

        prd_status = parse_status(typed_texts["prd"], "状态")
        spec_status = parse_status(typed_texts["spec"], "状态")
        l1_prd_status = parse_status(typed_texts["readme"], "PRD 状态")
        l1_spec_status = parse_status(typed_texts["readme"], "SPEC 状态")
        l1_open = parse_status(typed_texts["readme"], "开放偏差")
        if prd_status not in PRD_STATUSES:
            report.add("error", "prd-status", paths["prd"], f"Invalid or missing PRD status: {prd_status}")
        if spec_status not in SPEC_STATUSES:
            report.add("error", "spec-status", paths["spec"], f"Invalid or missing SPEC status: {spec_status}")
        if spec_status in {"实施中", "已完成"} and not explicit_user_implementation_authorization(
            bullet_value(typed_texts["spec"], "实施授权")
        ):
            report.add(
                "error",
                "implementation-authorization",
                paths["spec"],
                f"SPEC {spec_status} lacks explicit implementation authorization",
            )
        if prd_status in {"已批准", "实施中", "待验收", "已验收"}:
            if not explicit_user_baseline_approval(
                bullet_value(typed_texts["prd"], "批准依据"),
                f"PRD-{number}",
            ):
                report.add(
                    "error",
                    "prd-approval-evidence",
                    paths["prd"],
                    f"PRD {prd_status} lacks explicit user approval of the product baseline",
                )
            clean_prd = strip_html_comments(typed_texts["prd"])
            remaining = [fragment for fragment in PRD_TEMPLATE_PLACEHOLDERS if fragment in clean_prd]
            if "待定义" in clean_prd or remaining:
                report.add(
                    "error",
                    "prd-template-placeholder",
                    paths["prd"],
                    "Approved/implemented PRD still contains bundled template placeholders",
                )
        if spec_status in {"待批准", "已批准", "实施中", "已完成"}:
            approved_baseline = bullet_value(typed_texts["spec"], "当前批准基线")
            if not meaningful_value(approved_baseline) or f"PRD-{number}" not in (approved_baseline or ""):
                report.add(
                    "error",
                    "spec-approved-baseline",
                    paths["spec"],
                    f"SPEC {spec_status} must identify the approved PRD-{number} baseline",
                )
        if spec_status in {"已批准", "实施中", "已完成"} and not explicit_user_baseline_approval(
            bullet_value(typed_texts["spec"], "批准依据"),
            f"SPEC-{number}",
        ):
            report.add(
                "error",
                "spec-approval-evidence",
                paths["spec"],
                f"SPEC {spec_status} lacks explicit user approval of the implementation baseline",
            )
        if spec_status in {"实施中", "已完成"}:
            clean_spec = strip_html_comments(typed_texts["spec"])
            remaining = [fragment for fragment in SPEC_TEMPLATE_PLACEHOLDERS if fragment in clean_spec]
            if "待定义" in clean_spec or remaining:
                report.add(
                    "error",
                    "spec-template-placeholder",
                    paths["spec"],
                    "Implemented SPEC still contains bundled template placeholders",
                )
        if "PRD 当前状态" in typed_texts["spec"]:
            report.add(
                "error",
                "duplicate-status-owner",
                paths["spec"],
                "SPEC must not duplicate PRD status; derive it from the PRD",
            )
        if l1_prd_status != prd_status:
            report.add(
                "error",
                "l1-prd-drift",
                paths["readme"],
                f"L1 PRD status {l1_prd_status!r} differs from PRD {prd_status!r}",
            )
        if l1_spec_status != spec_status:
            report.add(
                "error",
                "l1-spec-drift",
                paths["readme"],
                f"L1 SPEC status {l1_spec_status!r} differs from SPEC {spec_status!r}",
            )

        for heading in malformed_deviation_headings(typed_texts["deviation"]):
            report.add(
                "error",
                "malformed-deviation-heading",
                paths["deviation"],
                f"Deviation heading must use canonical '### DEV-NNN-SSS：title' syntax: {heading}",
            )
        deviation_records = deviation_entries(typed_texts["deviation"])
        if deviation_records and spec_status != "已完成":
            report.add(
                "error",
                "deviation-before-completion",
                paths["deviation"],
                "As-built deviations may be recorded only after the SPEC implementation is completed",
            )
        open_count = 0
        for entry in deviation_records:
            deviation_id = entry.identity
            status = entry.status
            if not deviation_id.startswith(f"DEV-{number}-"):
                report.add("error", "deviation-prefix", paths["deviation"], f"Wrong iteration prefix: {deviation_id}")
            if deviation_id in all_deviation_ids:
                report.add(
                    "error",
                    "duplicate-deviation",
                    paths["deviation"],
                    f"Duplicate {deviation_id}; first seen in {all_deviation_ids[deviation_id]}",
                )
            all_deviation_ids[deviation_id] = paths["deviation"]
            if status is None:
                report.add("error", "deviation-status", paths["deviation"], f"Missing status for {deviation_id}")
            elif status not in DEVIATION_STATUSES:
                report.add(
                    "error",
                    "deviation-status",
                    paths["deviation"],
                    f"Invalid status {status!r} for {deviation_id}",
                )
            elif status in UNRESOLVED_DEVIATION_STATUSES:
                open_count += 1
            required_facts = (
                ("原批准内容", "deviation-baseline", "the original approved promise"),
                ("as-built 事实", "deviation-as-built", "an as-built fact"),
                ("原因", "deviation-cause", "a factual cause or current investigation result"),
                ("影响", "deviation-impact", "impact analysis"),
                ("验收影响", "deviation-acceptance-impact", "acceptance impact"),
            )
            if not meaningful_timestamp(bullet_value(entry.body, "发现时间")):
                report.add(
                    "error",
                    "deviation-discovered-at",
                    paths["deviation"],
                    f"{deviation_id} lacks an ISO discovery date/time",
                )
            association = bullet_value(entry.body, "关联需求/验收")
            if not (
                meaningful_value(association)
                and re.search(rf"(?<![A-Z0-9])R-{re.escape(number)}-\d+(?!\d)", association or "")
                and re.search(rf"(?<![A-Z0-9])AC-{re.escape(number)}-\d+(?!\d)", association or "")
            ):
                report.add(
                    "error",
                    "deviation-requirement-reference",
                    paths["deviation"],
                    f"{deviation_id} must cite same-iteration R-{number}-... and AC-{number}-... IDs",
                )
            spec_reference = bullet_value(entry.body, "SPEC 章节")
            if not (
                meaningful_value(spec_reference)
                and ("§" in (spec_reference or "") or f"SPEC-{number}" in (spec_reference or ""))
            ):
                report.add(
                    "error",
                    "deviation-spec-reference",
                    paths["deviation"],
                    f"{deviation_id} must cite an exact SPEC-{number} section",
                )
            for label, code, description in required_facts:
                if not meaningful_value(bullet_value(entry.body, label)):
                    report.add("error", code, paths["deviation"], f"{deviation_id} lacks {description}")
            if status and status not in UNRESOLVED_DEVIATION_STATUSES:
                if not meaningful_value(bullet_value(entry.body, "明确处置")):
                    report.add(
                        "error",
                        "deviation-disposition",
                        paths["deviation"],
                        f"{deviation_id} lacks an explicit disposition",
                    )
                disposition_evidence = bullet_value(entry.body, "处置依据")
                if not meaningful_value(disposition_evidence):
                    report.add(
                        "error",
                        "deviation-disposition-evidence",
                        paths["deviation"],
                        f"{deviation_id} lacks disposition evidence",
                    )
                if not meaningful_verification_evidence(bullet_value(entry.body, "验证")):
                    report.add(
                        "error",
                        "deviation-verification",
                        paths["deviation"],
                        f"{deviation_id} lacks concrete verification evidence",
                    )
                if not meaningful_timestamp(bullet_value(entry.body, "关闭或转交时间")):
                    report.add(
                        "error",
                        "deviation-closure",
                        paths["deviation"],
                        f"{deviation_id} lacks an ISO closure or transfer date/time",
                    )
                if status == "基线已重批" and not (
                    explicit_user_baseline_approval(disposition_evidence, f"PRD-{number}")
                    or explicit_user_baseline_approval(disposition_evidence, f"SPEC-{number}")
                ):
                    report.add(
                        "error",
                        "deviation-reapproval-evidence",
                        paths["deviation"],
                        f"{deviation_id} claims a reapproved baseline without explicit user approval evidence",
                    )
                if status == "已接受残余" and not explicit_user_acceptance_evidence(disposition_evidence):
                    report.add(
                        "error",
                        "deviation-residual-acceptance",
                        paths["deviation"],
                        f"{deviation_id} claims accepted residual behavior without explicit user acceptance evidence",
                    )
                if status == "已转后续迭代":
                    transfer_text = " ".join(
                        filter(
                            None,
                            (
                                bullet_value(entry.body, "明确处置"),
                                disposition_evidence,
                            ),
                        )
                    )
                    targets = re.findall(r"(?<![A-Z0-9])PRD-(\d{3,})(?!\d)", transfer_text)
                    if not targets or all(target == number for target in targets):
                        report.add(
                            "error",
                            "deviation-transfer-target",
                            paths["deviation"],
                            f"{deviation_id} must identify a different receiving PRD-NNN iteration",
                        )
        declared_match = re.search(r"当前开放偏差：`(\d+)`", typed_texts["deviation"])
        declared_open = int(declared_match.group(1)) if declared_match else None
        if declared_open != open_count:
            report.add(
                "error",
                "deviation-open-count-drift",
                paths["deviation"],
                f"Ledger summary says {declared_open!r} open deviations; entries have {open_count}",
            )
        if l1_open != str(open_count):
            report.add(
                "error",
                "l1-deviation-drift",
                paths["readme"],
                f"L1 says {l1_open!r} open deviations; ledger has {open_count}",
            )
        if prd_status in {"待验收", "已验收"}:
            if spec_status != "已完成":
                report.add(
                    "error",
                    "acceptance-spec-status",
                    paths["spec"],
                    f"PRD {prd_status} requires SPEC status 已完成",
                )
            if open_count:
                report.add(
                    "error",
                    "acceptance-open-deviation",
                    paths["deviation"],
                    f"PRD {prd_status} cannot have unresolved as-built deviations",
                )
        if prd_status == "已验收" and not explicit_user_acceptance_evidence(
            bullet_value(typed_texts["prd"], "验收依据")
        ):
            report.add(
                "error",
                "acceptance-evidence",
                paths["prd"],
                "Accepted PRD lacks explicit user acceptance evidence",
            )

        if root_readme_text is not None:
            registry_matches = 0
            for line in section_body(root_readme_text, ITERATIONS_START, ITERATIONS_END).splitlines():
                if not line.lstrip().startswith("|"):
                    continue
                first_cell = line.strip().strip("|").split("|", 1)[0]
                if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", first_cell):
                    registry_matches += 1
            if registry_matches != 1:
                report.add(
                    "error",
                    "l0-registration-count",
                    root / "harness/README.md",
                    f"Expected one registry row for {number}; found {registry_matches}",
                )
            row = parse_table_row(root_readme_text, number)
            if row is None or len(row) < 8:
                report.add(
                    "error",
                    "l0-registration",
                    root / "harness/README.md",
                    f"Missing or malformed registry row for {number}",
                )
            else:
                if row[2] != prd_status:
                    report.add(
                        "error",
                        "l0-prd-drift",
                        root / "harness/README.md",
                        f"Registry PRD status for {number} differs from PRD",
                    )
                if row[3] != spec_status:
                    report.add(
                        "error",
                        "l0-spec-drift",
                        root / "harness/README.md",
                        f"Registry SPEC status for {number} differs from SPEC",
                    )
                if row[4] != str(open_count):
                    report.add(
                        "error",
                        "l0-deviation-drift",
                        root / "harness/README.md",
                        f"Registry open deviation count for {number} differs from ledger",
                    )
        if progress_text is not None:
            row = parse_table_row(section_body(progress_text, PROGRESS_INDEX_START, PROGRESS_INDEX_END), number)
            if row is None or len(row) < 4:
                report.add(
                    "error",
                    "progress-index",
                    root / "harness/progress.md",
                    f"Missing or malformed progress index row for {number}",
                )
            elif row[1] != prd_status or row[2] != spec_status:
                report.add(
                    "error",
                    "progress-index-drift",
                    root / "harness/progress.md",
                    f"Progress index status for {number} differs from authoritative PRD/SPEC",
                )

    if not iteration_members:
        report.add(
            "warning",
            "no-iterations",
            root / "harness/iterations",
            "Harness is initialized but has no product iteration yet",
        )
    if progress_text is not None:
        clean_progress = strip_html_comments(progress_text)
        event_pairs: dict[tuple[str, str], int] = {}
        for session_id, event_type in re.findall(
            r"^## (S-\d{8}-\d{2,}) / (OPEN|DECISION|CHECKPOINT|MERGE|CLOSE) /",
            clean_progress,
            flags=re.MULTILINE,
        ):
            pair = (session_id, event_type)
            event_pairs[pair] = event_pairs.get(pair, 0) + 1
        for pair, count in event_pairs.items():
            if count > 1:
                report.add(
                    "error",
                    "duplicate-event",
                    root / "harness/progress.md",
                    f"Duplicate event {pair[0]} / {pair[1]}",
                )

    markdown_paths = {relative for relative in texts if relative.startswith("harness/")}
    link_pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    for relative in sorted(markdown_paths):
        text_value = texts[relative]
        parent_parts = relative.split("/")[:-1]
        for raw_target in link_pattern.findall(strip_html_comments(text_value)):
            target = raw_target.strip().strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            parts = [] if target.startswith("/") else list(parent_parts)
            escaped = target.startswith("/")
            for part in target.replace("\\", "/").split("/"):
                if part in {"", "."}:
                    continue
                if part == "..":
                    if parts:
                        parts.pop()
                    else:
                        escaped = True
                    continue
                parts.append(part)
            normalized = "/".join(parts)
            if escaped or not normalized.startswith("harness/"):
                report.add(
                    "warning",
                    "external-local-link",
                    root / relative,
                    f"Link leaves harness/: {raw_target}",
                )
                continue
            target_exists = normalized in entries or any(
                candidate.startswith(normalized.rstrip("/") + "/") for candidate in entries
            )
            if not target_exists:
                report.add(
                    "error",
                    "broken-link",
                    root / relative,
                    f"Missing local link target: {raw_target}",
                )
    return report


def print_validation(report: ValidationReport, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return
    state = "VALID" if not report.errors else "INVALID"
    print(f"{state}: {report.root}")
    print(f"errors={len(report.errors)} warnings={len(report.warnings)}")
    for issue in report.issues:
        print(f"{issue.severity.upper():7} {issue.code:24} {issue.path}: {issue.message}")


def add_row(existing_body: str, row: str, newline: str) -> str:
    current = normalize_newlines(existing_body, newline).strip("\r\n")
    return row if not current else current + newline + row


def build_new_iteration_operations(
    root: Path,
    title: str,
    now: datetime,
    base_commit: str,
    base_branch: str,
) -> tuple[str, list[Operation]]:
    report = collect_validation(root)
    if report.errors:
        print_validation(report, as_json=False)
        raise HarnessError("Repair validation errors before allocating a new iteration")

    harness = root / "harness"
    iterations = harness / "iterations"
    existing = find_existing_numbers(iterations)
    next_number = (max(existing) + 1) if existing else 1
    number = f"{next_number:03d}"
    target = iterations / number
    if target.exists():
        raise HarnessError(f"Iteration target already exists: {target}")

    progress_path = harness / "progress.md"
    root_readme_path = harness / "README.md"
    progress = read_document(progress_path)
    root_readme = read_document(root_readme_path)
    session_id = next_session_id(progress.text, now)
    date = now.date().isoformat()
    timestamp = now.astimezone().isoformat(timespec="seconds")
    values = {
        "NUMBER": number,
        "TITLE": title,
        "DATE": date,
        "TIMESTAMP": timestamp,
        "SESSION_ID": session_id,
        "BASE_COMMIT": base_commit,
        "BASE_BRANCH": base_branch,
    }

    operations: list[Operation] = []
    bundle_templates = {
        target / "README.md": "iteration-readme.md.tmpl",
        target / f"prd-{number}.md": "prd.md.tmpl",
        target / f"spec-{number}.md": "spec.md.tmpl",
        target / f"deviation-{number}.md": "deviation.md.tmpl",
    }
    for path, template_name in bundle_templates.items():
        rendered = load_template(template_name, values)
        append_operation(operations, path, rendered.encode("utf-8"))

    table_title = title.replace("|", "\\|")
    registry_row = (
        f"| [{number}](iterations/{number}/README.md) | {table_title} | 草案 | 受 PRD 阻塞 | 0 | "
        f"已创建治理四件套 | 评估后选择 grill、联合起草或 PRD 先行 | [进入](iterations/{number}/README.md) |"
    )
    registry = add_row(
        section_body(root_readme.text, ITERATIONS_START, ITERATIONS_END),
        registry_row,
        root_readme.newline,
    )
    updated_readme = replace_section(
        root_readme.text,
        ITERATIONS_START,
        ITERATIONS_END,
        registry,
        root_readme.newline,
    )
    focus = (
        f"- 当前迭代：[{number}](iterations/{number}/README.md) — {title}。{root_readme.newline}"
        f"- 下一步：先解决决策性歧义；小且明确可同轮起草 PRD-{number}/SPEC-{number}，明确但不小则 PRD 先行，有歧义才 grill 用户；在批准前不进入实现。"
    )
    updated_readme = replace_section(updated_readme, FOCUS_START, FOCUS_END, focus, root_readme.newline)
    append_operation(
        operations,
        root_readme_path,
        encode_document(updated_readme, bom=root_readme.bom),
    )

    progress_row = f"| [{number}](iterations/{number}/README.md) | 草案 | 受 PRD 阻塞 | 评估后选择 grill、联合起草或 PRD 先行 |"
    progress_index = add_row(
        section_body(progress.text, PROGRESS_INDEX_START, PROGRESS_INDEX_END),
        progress_row,
        progress.newline,
    )
    updated_progress = replace_section(
        progress.text,
        PROGRESS_INDEX_START,
        PROGRESS_INDEX_END,
        progress_index,
        progress.newline,
    )
    event = f"""
## {session_id} / OPEN / {timestamp}

- 关联：PRD-{number} / SPEC-{number}
- 会话背景：出现新的产品目标“{title}”。
- 用户目标：建立本轮产品范围、验收与实施基线。
- 决策与依据：分配下一单调编号 {number}，一次性创建同号四件套；先按清晰度与规模选择 grill、联合起草或 PRD 先行路径，当前仅为草案，不自动授权实施。
- 执行与变更：创建 `harness/iterations/{number}/` 并更新 L0 与全局索引。
- 验证证据：运行 `project_harness.py validate`。
- 关联偏差：无。
- 未决问题与下一步：先解决决策性歧义；小且明确可同轮起草 PRD-{number}/SPEC-{number}，明确但不小则 PRD 先行，有歧义才 grill 用户。
"""
    event = normalize_newlines(event.strip("\r\n"), progress.newline)
    separator = progress.newline if updated_progress.endswith(("\n", "\r")) else progress.newline * 2
    updated_progress = updated_progress + separator + event + progress.newline
    append_operation(
        operations,
        progress_path,
        encode_document(updated_progress, bom=progress.bom),
    )
    assert_governance_not_ignored(root, (operation.path for operation in operations))
    return number, operations


def normalize_iteration_number(value: str) -> str:
    if not re.fullmatch(r"\d{1,9}", value.strip()):
        raise HarnessError("Iteration number must contain decimal digits only")
    number = int(value)
    if number < 1:
        raise HarnessError("Iteration number must be greater than zero")
    return f"{number:03d}"


def repository_operation_markers(git: str, root: Path) -> list[Path]:
    markers: list[Path] = []
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "REBASE_HEAD",
        "rebase-apply",
        "rebase-merge",
    ):
        result = run_git(git, root, ["rev-parse", "--git-path", name])
        raw = decode_output(result.stdout)
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.exists():
            markers.append(candidate.resolve())
    return markers


def has_iteration_close_event(progress_text: str, number: str) -> bool:
    clean = strip_html_comments(progress_text)
    headings = list(
        re.finditer(
            r"^## S-\d{8}-\d{2,} / (OPEN|DECISION|CHECKPOINT|MERGE|CLOSE) /.*$",
            clean,
            flags=re.MULTILINE,
        )
    )
    for index, heading in enumerate(headings):
        if heading.group(1) != "CLOSE":
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(clean)
        block = clean[heading.start() : end]
        association = bullet_value(block, "关联")
        if (
            association
            and re.search(rf"(?<!\d)PRD-{re.escape(number)}(?!\d)", association)
            and meaningful_verification_evidence(bullet_value(block, "验证证据"))
        ):
            return True
    return False


def normalize_include(root: Path, value: str) -> tuple[str, bool]:
    raw = Path(value)
    candidate = raw if raw.is_absolute() else root / raw
    ensure_inside_root(candidate, root)
    resolved = candidate.resolve(strict=False)
    try:
        relative_path = resolved.relative_to(root)
    except ValueError as exc:
        raise HarnessError(f"Included path resolves outside the project root: {value}") from exc
    if not relative_path.parts or relative_path.parts[0].lower() == ".git":
        raise HarnessError(f"Refusing to include Git metadata: {value}")
    return relative_path.as_posix(), candidate.is_dir()


def path_matches_include(path: str, include: tuple[str, bool]) -> bool:
    relative, is_directory = include
    return path == relative or (is_directory and path.startswith(relative.rstrip("/") + "/"))


def unstage_exact_paths(git: str, root: Path, relative_paths: Sequence[str]) -> None:
    if not relative_paths:
        return
    payload = b"".join(path.encode("utf-8", errors="surrogateescape") + b"\0" for path in relative_paths)
    run_git(
        git,
        root,
        [
            "--literal-pathspecs",
            "restore",
            "--staged",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
        ],
        input_bytes=payload,
    )


def iteration_final_ref(number: str) -> str:
    return f"refs/project-harness/iterations/{number}/final"


def iteration_base_ref(number: str, branch_ref: str) -> str:
    if not branch_ref.startswith("refs/heads/"):
        raise HarnessError(f"Iteration baseline branch is not a local branch: {branch_ref}")
    return f"refs/project-harness/iterations/{number}/base/{branch_ref}"


def run_update_ref_without_hooks(
    git: str,
    root: Path,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryDirectory(prefix="project-harness-empty-hooks-") as hooks:
        return run_git(
            git,
            root,
            ["-c", f"core.hooksPath={hooks}", "update-ref", *arguments],
            input_bytes=input_bytes,
            check=check,
        )


def operation_journal_root(common_dir: Path) -> Path:
    return common_dir / "project-harness" / "journal" / "v1"


def operation_journal_path(common_dir: Path, operation_id: str) -> Path:
    validated = validate_operation_id(operation_id)
    return operation_journal_root(common_dir) / f"{validated}.json"


def operation_lock_path(common_dir: Path, operation_id: str) -> Path:
    validated = validate_operation_id(operation_id)
    return common_dir / "project-harness" / "locks" / "v1" / f"{validated}.lock"


def ensure_operational_path(path: Path, common_dir: Path) -> None:
    resolved_common = common_dir.resolve()
    try:
        path.absolute().relative_to(resolved_common)
        path.resolve(strict=False).relative_to(resolved_common)
    except ValueError as exc:
        raise HarnessError(f"Operational path resolves outside Git common directory: {path}") from exc
    current = path
    while current != resolved_common:
        if current.exists():
            is_junction = getattr(current, "is_junction", lambda: False)
            if current.is_symlink() or is_junction():
                raise HarnessError(f"Refusing to use operational path through a link or junction: {current}")
        if current.parent == current:
            raise HarnessError(f"Could not prove operational path containment: {path}")
        current = current.parent


@contextlib.contextmanager
def operation_lock(common_dir: Path, operation_id: str, *, timeout_seconds: float = 30.0):
    """Serialize one operation across processes; the OS releases the lock after a crash."""
    path = operation_lock_path(common_dir, operation_id)
    ensure_operational_path(path, common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_operational_path(path.parent, common_dir)
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
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
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise HarnessError(
                        f"Timed out waiting for operation lock: {operation_id}"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def operation_journal_from_dict(value: object, *, source: Path) -> OperationJournal:
    if not isinstance(value, dict):
        raise HarnessError(f"Operation journal must be a JSON object: {source}")
    if value.get("schema_version") != OPERATION_JOURNAL_SCHEMA_V1:
        raise HarnessError(f"Unsupported operation journal schema in {source}: {value.get('schema_version')!r}")
    allowed_fields = {
        "schema_version",
        "operation_id",
        "plan_digest",
        "action",
        "phase",
        "project_root",
        "title",
        "base_commit",
        "base_branch",
        "governance_ref",
        "governance_commit",
        "principle_sha256",
        "created_at",
        "updated_at",
        "manifest",
        "expected_refs",
        "iteration",
        "allocation_object",
        "created_refs",
        "attempts",
        "history",
        "error",
    }
    unknown_fields = sorted(set(value) - allowed_fields)
    if unknown_fields:
        raise HarnessError(f"Operation journal has unknown fields in {source}: {', '.join(unknown_fields)}")
    required_strings = (
        "operation_id",
        "plan_digest",
        "action",
        "phase",
        "project_root",
        "title",
        "base_commit",
        "base_branch",
        "governance_ref",
        "governance_commit",
        "principle_sha256",
        "created_at",
        "updated_at",
    )
    strings: dict[str, str] = {}
    for key in required_strings:
        item = value.get(key)
        if not isinstance(item, str):
            raise HarnessError(f"Operation journal field {key!r} must be a string: {source}")
        strings[key] = item
    validate_operation_id(strings["operation_id"])
    if not PLAN_DIGEST_PATTERN.fullmatch(strings["plan_digest"]):
        raise HarnessError(f"Operation journal has invalid plan digest: {source}")
    if not re.fullmatch(r"[0-9a-f]{40,64}", strings["base_commit"]):
        raise HarnessError(f"Operation journal has invalid base commit: {source}")
    if not re.fullmatch(r"[0-9a-f]{40,64}", strings["governance_commit"]):
        raise HarnessError(f"Operation journal has invalid governance commit: {source}")
    if not re.fullmatch(r"[0-9a-f]{64}", strings["principle_sha256"]):
        raise HarnessError(f"Operation journal has invalid principle hash: {source}")
    if validate_label(strings["title"], "operation title") != strings["title"]:
        raise HarnessError(f"Operation journal title is not canonical: {source}")
    if not Path(strings["project_root"]).is_absolute() or len(strings["project_root"]) > 4096:
        raise HarnessError(f"Operation journal project root is invalid: {source}")
    if strings["action"] != "reserve-iteration":
        raise HarnessError(f"Operation journal has unsupported action {strings['action']!r}: {source}")
    if strings["phase"] not in JOURNAL_PHASES:
        raise HarnessError(f"Operation journal has unknown phase {strings['phase']!r}: {source}")
    manifest = value.get("manifest")
    if not isinstance(manifest, dict):
        raise HarnessError(f"Operation journal manifest must be an object: {source}")
    expected_manifest_fields = {
        "schema_version",
        "operation_id",
        "action",
        "project_root",
        "title",
        "base_commit",
        "base_ref",
        "governance_ref",
        "governance_commit",
        "governance_snapshot",
        "reservation_policy",
        "exclusions",
    }
    if set(manifest) != expected_manifest_fields:
        raise HarnessError(f"Operation journal manifest fields do not match the v1 schema: {source}")
    if manifest.get("schema_version") != OPERATION_PLAN_SCHEMA_V1:
        raise HarnessError(f"Operation journal manifest schema is unsupported: {source}")
    if schema_digest(manifest) != strings["plan_digest"]:
        raise HarnessError(f"Operation journal manifest digest mismatch: {source}")
    manifest_matches = {
        "operation_id": strings["operation_id"],
        "action": strings["action"],
        "project_root": strings["project_root"],
        "title": strings["title"],
        "base_commit": strings["base_commit"],
        "base_ref": strings["base_branch"],
        "governance_ref": strings["governance_ref"],
        "governance_commit": strings["governance_commit"],
    }
    for key, expected in manifest_matches.items():
        if manifest.get(key) != expected:
            raise HarnessError(f"Operation journal manifest field {key!r} does not match: {source}")
    if not is_allowed_base_ref(strings["base_branch"]):
        raise HarnessError(f"Operation journal base ref is not allowed: {source}")
    if strings["governance_ref"] != "refs/heads/main":
        raise HarnessError(f"Operation journal governance ref is not canonical main: {source}")
    governance_snapshot = manifest.get("governance_snapshot")
    expected_snapshot_fields = {
        "schema_version",
        "ref",
        "commit",
        "tree",
        "blobs",
        "principle_sha256",
    }
    if not isinstance(governance_snapshot, dict) or set(governance_snapshot) != expected_snapshot_fields:
        raise HarnessError(f"Operation journal governance snapshot is invalid: {source}")
    if (
        governance_snapshot.get("schema_version") != "harness-lite.governance-snapshot/v1"
        or governance_snapshot.get("ref") != strings["governance_ref"]
        or governance_snapshot.get("commit") != strings["governance_commit"]
        or governance_snapshot.get("principle_sha256") != strings["principle_sha256"]
        or not re.fullmatch(r"[0-9a-f]{40,64}", str(governance_snapshot.get("tree", "")))
    ):
        raise HarnessError(f"Operation journal governance snapshot values are invalid: {source}")
    snapshot_blobs = governance_snapshot.get("blobs")
    required_snapshot_paths = {
        "AGENTS.md",
        "harness/README.md",
        "harness/principle.md",
        "harness/progress.md",
    }
    if (
        not isinstance(snapshot_blobs, dict)
        or set(snapshot_blobs) != required_snapshot_paths
        or not all(re.fullmatch(r"[0-9a-f]{40,64}", str(item)) for item in snapshot_blobs.values())
    ):
        raise HarnessError(f"Operation journal governance blob map is invalid: {source}")
    policy = manifest.get("reservation_policy")
    expected_policy_fields = {
        "strategy",
        "collision_policy",
        "max_attempts",
        "observed_next_iteration",
        "observed_allocation_ref",
        "observed_base_ref",
        "ref_namespace",
    }
    if not isinstance(policy, dict) or set(policy) != expected_policy_fields:
        raise HarnessError(f"Operation journal reservation policy is invalid: {source}")
    observed = policy.get("observed_next_iteration")
    if (
        policy.get("strategy") != "next-monotonic-v2-cas"
        or policy.get("collision_policy") != "advance-to-current-max-plus-one"
        or policy.get("max_attempts") != RESERVATION_MAX_ATTEMPTS
        or not isinstance(observed, str)
        or normalize_iteration_number(observed) != observed
        or policy.get("observed_allocation_ref") != v2_allocation_ref(observed)
        or policy.get("observed_base_ref") != v2_iteration_base_ref(observed)
        or policy.get("ref_namespace") != V2_REF_ROOT
    ):
        raise HarnessError(f"Operation journal reservation policy values are invalid: {source}")
    exclusions = manifest.get("exclusions")
    expected_exclusions = [
        "no worktree",
        "no branch",
        "no governance bundle",
        "no progress update",
        "no commit",
        "no push",
    ]
    if exclusions != expected_exclusions:
        raise HarnessError(f"Operation journal exclusions are invalid: {source}")
    iteration = value.get("iteration")
    if not isinstance(iteration, str) or normalize_iteration_number(iteration) != iteration:
        raise HarnessError(f"Operation journal has invalid iteration: {source}")
    allocation_object = value.get("allocation_object")
    if allocation_object is not None and (
        not isinstance(allocation_object, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", allocation_object)
    ):
        raise HarnessError(f"Operation journal has invalid allocation object: {source}")
    expected_refs = value.get("expected_refs")
    if not isinstance(expected_refs, list) or not expected_refs or not all(
        isinstance(item, str) and item.startswith(f"{V2_REF_ROOT}/") for item in expected_refs
    ):
        raise HarnessError(f"Operation journal expected_refs must be a non-empty v2 ref array: {source}")
    exact_expected_refs = [v2_allocation_ref(iteration), v2_iteration_base_ref(iteration)]
    if expected_refs != exact_expected_refs:
        raise HarnessError(f"Operation journal expected_refs do not match its iteration: {source}")
    created_refs = value.get("created_refs", [])
    attempts = value.get("attempts", [])
    history = value.get("history", [])
    if not isinstance(created_refs, list) or not all(isinstance(item, str) for item in created_refs):
        raise HarnessError(f"Operation journal created_refs must be a string array: {source}")
    if not isinstance(attempts, list) or not all(
        isinstance(item, dict)
        and set(item) <= {"iteration", "result", "owner_operation_id", "at"}
        for item in attempts
    ):
        raise HarnessError(f"Operation journal attempts must be an object array: {source}")
    if not isinstance(history, list) or not all(
        isinstance(item, dict) and set(item) <= {"phase", "at"} for item in history
    ):
        raise HarnessError(f"Operation journal history must be an object array: {source}")
    if len(attempts) > RESERVATION_MAX_ATTEMPTS or len(history) > RESERVATION_MAX_ATTEMPTS + 3:
        raise HarnessError(f"Operation journal event arrays exceed their safe limits: {source}")
    error = value.get("error")
    if error is not None and (not isinstance(error, str) or len(error) > 1000):
        raise HarnessError(f"Operation journal error must be a string or null: {source}")
    for attempt in attempts:
        attempt_iteration = attempt.get("iteration")
        if not isinstance(attempt_iteration, str) or normalize_iteration_number(attempt_iteration) != attempt_iteration:
            raise HarnessError(f"Operation journal attempt iteration is invalid: {source}")
        if attempt.get("result") != "conflict":
            raise HarnessError(f"Operation journal attempt result is invalid: {source}")
        owner = attempt.get("owner_operation_id")
        if owner is not None and (not isinstance(owner, str) or not OPERATION_ID_PATTERN.fullmatch(owner)):
            raise HarnessError(f"Operation journal attempt owner is invalid: {source}")
        if not isinstance(attempt.get("at"), str) or len(str(attempt["at"])) > 80:
            raise HarnessError(f"Operation journal attempt timestamp is invalid: {source}")
    if not history:
        raise HarnessError(f"Operation journal history must not be empty: {source}")
    for event in history:
        if event.get("phase") not in JOURNAL_PHASES:
            raise HarnessError(f"Operation journal history phase is invalid: {source}")
        if not isinstance(event.get("at"), str) or len(str(event["at"])) > 80:
            raise HarnessError(f"Operation journal history timestamp is invalid: {source}")
    if history[-1].get("phase") != strings["phase"]:
        raise HarnessError(f"Operation journal current phase does not match its history: {source}")
    if strings["phase"] in {"RESERVED", "READY"} and allocation_object is None:
        raise HarnessError(f"Operation journal reserved phase lacks its allocation object: {source}")
    if strings["phase"] == "PLANNED" and created_refs:
        raise HarnessError(f"Operation journal PLANNED phase must not claim created refs: {source}")
    if strings["phase"] in {"RESERVED", "READY"} and created_refs != expected_refs:
        raise HarnessError(f"Operation journal reserved refs do not match expected refs: {source}")
    if strings["phase"] == "READY" and created_refs != expected_refs:
        raise HarnessError(f"Operation journal READY refs do not match its expected refs: {source}")
    if strings["phase"] == "FAILED_NEEDS_RECONCILE" and not error:
        raise HarnessError(f"Operation journal failed phase lacks an error summary: {source}")
    if strings["phase"] != "FAILED_NEEDS_RECONCILE" and error is not None:
        raise HarnessError(f"Operation journal non-failed phase must not contain an error: {source}")
    return OperationJournal(
        operation_id=strings["operation_id"],
        plan_digest=strings["plan_digest"],
        action=strings["action"],
        phase=strings["phase"],
        project_root=strings["project_root"],
        title=strings["title"],
        base_commit=strings["base_commit"],
        base_branch=strings["base_branch"],
        governance_ref=strings["governance_ref"],
        governance_commit=strings["governance_commit"],
        principle_sha256=strings["principle_sha256"],
        created_at=strings["created_at"],
        updated_at=strings["updated_at"],
        manifest=dict(manifest),
        expected_refs=tuple(expected_refs),
        iteration=iteration,
        allocation_object=allocation_object,
        created_refs=tuple(created_refs),
        attempts=tuple(dict(item) for item in attempts),
        history=tuple(dict(item) for item in history),
        error=error,
    )


def load_operation_journal(common_dir: Path, operation_id: str) -> tuple[OperationJournal, bytes]:
    path = operation_journal_path(common_dir, operation_id)
    ensure_operational_path(path, common_dir)
    try:
        if path.stat().st_size > MAX_OPERATION_JOURNAL_BYTES:
            raise HarnessError(f"Operation journal exceeds the safe size limit and was preserved: {path}")
        raw = path.read_bytes()
    except OSError as exc:
        raise HarnessError(f"Could not read operation journal {path}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Operation journal is corrupt and was preserved unchanged: {path}") from exc
    record = operation_journal_from_dict(value, source=path)
    if record.operation_id != operation_id:
        raise HarnessError(f"Operation journal identity mismatch in {path}")
    return record, raw


def journal_raw(record: OperationJournal) -> bytes:
    return canonical_json_bytes(record.as_dict()) + b"\n"


def create_operation_journal(common_dir: Path, plan: OperationPlan) -> tuple[OperationJournal, bool]:
    path = operation_journal_path(common_dir, plan.operation_id)
    ensure_operational_path(path, common_dir)
    if path.exists():
        existing, _ = load_operation_journal(common_dir, plan.operation_id)
        if existing.plan_digest != plan.plan_digest:
            raise HarnessError(
                f"Operation {plan.operation_id} already exists with a different plan digest; refusing to reuse it"
            )
        return existing, False
    parent = path.parent
    ensure_operational_path(parent, common_dir)
    parent.mkdir(parents=True, exist_ok=True)
    ensure_operational_path(parent, common_dir)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    record = OperationJournal(
        operation_id=plan.operation_id,
        plan_digest=plan.plan_digest,
        action="reserve-iteration",
        phase="PLANNED",
        project_root=plan.project_root,
        title=plan.title,
        base_commit=plan.base_commit,
        base_branch=plan.base_branch,
        governance_ref=plan.governance_ref,
        governance_commit=plan.governance_commit,
        principle_sha256=str(plan.governance_snapshot["principle_sha256"]),
        created_at=now,
        updated_at=now,
        manifest=dict(plan.manifest),
        expected_refs=(
            v2_allocation_ref(plan.observed_next_iteration),
            v2_iteration_base_ref(plan.observed_next_iteration),
        ),
        iteration=plan.observed_next_iteration,
        history=({"phase": "PLANNED", "at": now},),
    )
    raw = journal_raw(record)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(handle.name)
    try:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    try:
        if path.exists():
            existing, _ = load_operation_journal(common_dir, plan.operation_id)
            if existing.plan_digest != plan.plan_digest:
                raise HarnessError(
                    f"Operation {plan.operation_id} was concurrently created with a different plan digest"
                )
            return existing, False
        # The per-operation OS lock serializes every cooperating creator. A
        # crash before replace leaves only a non-authoritative temp file; a
        # crash after replace leaves the complete fsynced journal.
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return record, True


def write_operation_journal(
    common_dir: Path,
    record: OperationJournal,
    *,
    expected_raw: bytes,
) -> OperationJournal:
    path = operation_journal_path(common_dir, record.operation_id)
    ensure_operational_path(path, common_dir)
    current = path.read_bytes() if path.exists() else None
    if current != expected_raw:
        raise HarnessError(f"Operation journal changed concurrently; refusing to overwrite: {path}")
    parent = path.parent
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(handle.name)
    try:
        handle.write(journal_raw(record))
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    try:
        current = path.read_bytes() if path.exists() else None
        if current != expected_raw:
            raise HarnessError(f"Operation journal changed while updating; refusing to overwrite: {path}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return record


def update_planned_journal(
    common_dir: Path,
    record: OperationJournal,
    *,
    iteration: str,
    allocation_object: str | None = None,
    attempt: Mapping[str, object] | None = None,
) -> OperationJournal:
    if record.phase != "PLANNED":
        raise HarnessError(f"Cannot update allocation candidate while operation is {record.phase}")
    current, raw = load_operation_journal(common_dir, record.operation_id)
    if current.plan_digest != record.plan_digest or current.phase != "PLANNED":
        raise HarnessError(f"Operation journal changed before allocation planning: {record.operation_id}")
    attempts = list(current.attempts)
    if attempt is not None:
        attempts.append(dict(attempt))
    number = normalize_iteration_number(iteration)
    next_allocation_object = allocation_object
    if allocation_object is None and current.iteration == number:
        next_allocation_object = current.allocation_object
    updated = replace(
        current,
        iteration=number,
        allocation_object=next_allocation_object,
        expected_refs=(v2_allocation_ref(number), v2_iteration_base_ref(number)),
        attempts=tuple(attempts),
        updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    return write_operation_journal(common_dir, updated, expected_raw=raw)


def advance_operation_journal(
    common_dir: Path,
    record: OperationJournal,
    target_phase: str,
    *,
    iteration: str | None = None,
    allocation_object: str | None = None,
    created_refs: Sequence[str] | None = None,
    error: str | None = None,
) -> OperationJournal:
    if target_phase not in JOURNAL_PHASES:
        raise HarnessError(f"Unknown operation journal phase: {target_phase}")
    current, raw = load_operation_journal(common_dir, record.operation_id)
    if current.plan_digest != record.plan_digest:
        raise HarnessError(f"Operation journal plan digest changed: {record.operation_id}")
    if current.phase == target_phase:
        return current
    if current.phase == "FAILED_NEEDS_RECONCILE" or current.phase == "READY":
        raise HarnessError(f"Operation {record.operation_id} cannot advance from terminal phase {current.phase}")
    if current.action == "reserve-iteration":
        allowed = {
            "PLANNED": {"RESERVED", "FAILED_NEEDS_RECONCILE"},
            "RESERVED": {"READY", "FAILED_NEEDS_RECONCILE"},
        }
        if target_phase not in allowed.get(current.phase, set()):
            raise HarnessError(f"Invalid reserve-iteration journal transition: {current.phase} -> {target_phase}")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    history = list(current.history)
    history.append({"phase": target_phase, "at": now})
    updated = replace(
        current,
        phase=target_phase,
        updated_at=now,
        iteration=normalize_iteration_number(iteration) if iteration is not None else current.iteration,
        allocation_object=allocation_object if allocation_object is not None else current.allocation_object,
        created_refs=tuple(created_refs) if created_refs is not None else current.created_refs,
        history=tuple(history),
        error=error,
    )
    return write_operation_journal(common_dir, updated, expected_raw=raw)


def list_operation_journals(common_dir: Path) -> tuple[list[dict[str, object]], list[BlockingReason]]:
    root = operation_journal_root(common_dir)
    ensure_operational_path(root, common_dir)
    if not root.exists():
        return [], []
    journals: list[dict[str, object]] = []
    blockers: list[BlockingReason] = []
    for path in sorted(root.glob("*.json")):
        operation_id = path.stem
        try:
            validate_operation_id(operation_id)
            record, _ = load_operation_journal(common_dir, operation_id)
        except HarnessError as exc:
            blockers.append(BlockingReason("corrupt-operation-journal", str(exc)))
            journals.append({"path": str(path), "corrupt": True, "error": str(exc)})
            continue
        value = {
            "schema_version": OPERATION_JOURNAL_SCHEMA_V1,
            "operation_id": record.operation_id,
            "plan_digest": record.plan_digest,
            "action": record.action,
            "phase": record.phase,
            "iteration": record.iteration,
            "allocation_object": record.allocation_object,
            "base_commit": record.base_commit,
            "base_branch": record.base_branch,
            "governance_ref": record.governance_ref,
            "governance_commit": record.governance_commit,
            "governance_tree": record.manifest["governance_snapshot"]["tree"],
            "principle_sha256": record.principle_sha256,
            "expected_refs": list(record.expected_refs),
            "created_refs": list(record.created_refs),
            "updated_at": record.updated_at,
            "error": record.error,
            "path": str(path),
            "corrupt": False,
        }
        journals.append(value)
    return journals, blockers


def build_allocation_metadata(plan: OperationPlan, number: str) -> dict[str, object]:
    normalized = normalize_iteration_number(number)
    return {
        "schema_version": ALLOCATION_METADATA_SCHEMA_V1,
        "operation_id": plan.operation_id,
        "plan_digest": plan.plan_digest,
        "iteration": normalized,
        "base_commit": plan.base_commit,
        "base_branch": plan.base_branch,
        "governance_ref": plan.governance_ref,
        "governance_commit": plan.governance_commit,
        "governance_tree": plan.governance_snapshot["tree"],
        "principle_sha256": plan.governance_snapshot["principle_sha256"],
        "title": plan.title,
    }


def hash_git_blob(git: str, root: Path, content: bytes, *, write: bool) -> str:
    arguments = ["hash-object"]
    if write:
        arguments.append("-w")
    arguments.append("--stdin")
    object_name = decode_output(run_git(git, root, arguments, input_bytes=content).stdout)
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", object_name):
        raise HarnessError(f"Git returned an invalid object ID for allocation metadata: {object_name!r}")
    return object_name


def try_create_v2_reservation_refs(
    git: str,
    root: Path,
    number: str,
    allocation_object: str,
    base_commit: str,
    source_base_ref: str,
    governance_ref: str,
    governance_commit: str,
) -> subprocess.CompletedProcess[bytes]:
    allocation_reference = v2_allocation_ref(number)
    base_reference = v2_iteration_base_ref(number)
    run_git(git, root, ["check-ref-format", allocation_reference])
    run_git(git, root, ["check-ref-format", base_reference])
    commands = ["start", f"verify {source_base_ref} {base_commit}"]
    if governance_ref != source_base_ref or governance_commit != base_commit:
        commands.append(f"verify {governance_ref} {governance_commit}")
    commands.extend(
        (
            f"create {allocation_reference} {allocation_object}",
            f"create {base_reference} {base_commit}",
            "prepare",
            "commit",
        )
    )
    transaction = ("\n".join(commands) + "\n").encode("ascii")
    return run_update_ref_without_hooks(
        git,
        root,
        ["-m", f"project-harness: reserve iteration {normalize_iteration_number(number)}", "--stdin"],
        input_bytes=transaction,
        check=False,
    )


def verify_owned_reservation(
    git: str,
    root: Path,
    plan: OperationPlan,
    number: str,
) -> tuple[bool, str | None, str | None]:
    records = git_ref_records(git, root)
    allocation_reference = v2_allocation_ref(number)
    base_reference = v2_iteration_base_ref(number)
    allocation_value = records.get(allocation_reference)
    base_value = records.get(base_reference)
    if allocation_value is None and base_value is None:
        return False, None, None
    if allocation_value is None or base_value is None:
        raise HarnessError(
            f"Incomplete v2 reservation for {number}; allocation and base refs must be created atomically"
        )
    allocation_object, allocation_type = allocation_value
    base_object, base_type = base_value
    if allocation_type != "blob" or base_type != "commit":
        raise HarnessError(f"V2 reservation {number} has invalid ref object types")
    metadata = read_allocation_metadata(git, root, allocation_object)
    owned = metadata.get("operation_id") == plan.operation_id and metadata.get("plan_digest") == plan.plan_digest
    if not owned:
        return False, allocation_object, str(metadata.get("operation_id") or "")
    if base_object != plan.base_commit or metadata.get("base_commit") != plan.base_commit:
        raise HarnessError(f"V2 reservation {number} owned by this operation has a different base")
    if metadata.get("iteration") != normalize_iteration_number(number):
        raise HarnessError(f"V2 reservation {number} metadata names a different iteration")
    if metadata.get("base_branch") != plan.base_branch:
        raise HarnessError(f"V2 reservation {number} metadata names a different source base ref")
    if metadata.get("title") != plan.title:
        raise HarnessError(f"V2 reservation {number} metadata names a different title")
    if (
        metadata.get("governance_ref") != plan.governance_ref
        or metadata.get("governance_commit") != plan.governance_commit
        or metadata.get("governance_tree") != plan.governance_snapshot.get("tree")
        or metadata.get("principle_sha256") != plan.governance_snapshot.get("principle_sha256")
    ):
        raise HarnessError(f"V2 reservation {number} metadata names a different governance snapshot")
    return bool(owned), allocation_object, str(metadata.get("operation_id") or "")


def build_iteration_ref_states(
    git: str,
    root: Path,
    records: Mapping[str, tuple[str, str]],
) -> list[IterationRefState]:
    states: list[IterationRefState] = []
    for raw_number in discover_iteration_numbers(root, records):
        number = f"{raw_number:03d}"
        issues: list[str] = []
        allocation_reference = v2_allocation_ref(number)
        allocation_value = records.get(allocation_reference)
        allocation_object: str | None = None
        allocation_metadata: dict[str, object] | None = None
        if allocation_value is not None:
            allocation_object = allocation_value[0]
            try:
                metadata = read_allocation_metadata(git, root, allocation_object)
            except HarnessError as exc:
                issues.append(str(exc))
            else:
                allocation_metadata = {
                    key: metadata[key]
                    for key in (
                        "schema_version",
                        "operation_id",
                        "plan_digest",
                        "iteration",
                        "base_commit",
                        "base_branch",
                        "governance_ref",
                        "governance_commit",
                        "governance_tree",
                        "principle_sha256",
                    )
                }
                if allocation_metadata.get("iteration") != number:
                    issues.append("allocation metadata iteration does not match its ref")

        base: dict[str, str] | None = None
        try:
            base = read_iteration_base_compat(git, root, number, records)
        except HarnessError as exc:
            issues.append(str(exc))
        if allocation_value is not None and (base is None or base.get("format") == "legacy"):
            issues.append("v2 allocation ref lacks its matching v2 base ref")
        if allocation_value is None and base is not None and base.get("format") in {"v2", "legacy+v2"}:
            issues.append("v2 base ref lacks its matching allocation ref")
        if allocation_metadata is not None and base is not None:
            metadata_base = allocation_metadata.get("base_commit")
            if base.get("commit") != metadata_base:
                issues.append("allocation metadata base differs from iteration base ref")

        candidate_prefix = f"{V2_REF_ROOT}/iterations/{number}/candidates/"
        candidates = tuple(
            {"generation": reference[len(candidate_prefix) :], "reference": reference, "object": value[0]}
            for reference, value in sorted(records.items())
            if reference.startswith(candidate_prefix)
        )
        integrated_reference = v2_integrated_ref(number)
        integrated_value = records.get(integrated_reference)
        v2_final_reference = v2_final_ref(number)
        v2_final_value = records.get(v2_final_reference)
        legacy_final_reference = iteration_final_ref(number)
        legacy_final_value = records.get(legacy_final_reference)
        final_reference: str | None = None
        final_object: str | None = None
        if v2_final_value is not None:
            final_reference = v2_final_reference
            final_object = v2_final_value[0]
        if legacy_final_value is not None:
            if final_object is not None and final_object != legacy_final_value[0]:
                issues.append("legacy and v2 final refs point to different objects")
            elif final_object is None:
                final_reference = legacy_final_reference
                final_object = legacy_final_value[0]

        base_branch = base.get("branch") or None if base else None
        if base_branch is None and allocation_metadata is not None:
            metadata_branch = allocation_metadata.get("base_branch")
            if isinstance(metadata_branch, str):
                base_branch = metadata_branch
        states.append(
            IterationRefState(
                number=number,
                allocation_ref=allocation_reference if allocation_value is not None else None,
                allocation_object=allocation_object,
                allocation_metadata=allocation_metadata,
                base_ref=base.get("reference") if base else None,
                base_commit=base.get("commit") if base else None,
                base_format=base.get("format") if base else None,
                base_branch=base_branch,
                candidates=candidates,
                integrated_ref=integrated_reference if integrated_value is not None else None,
                integrated_object=integrated_value[0] if integrated_value is not None else None,
                final_ref=final_reference,
                final_object=final_object,
                bundle_present=(root / "harness" / "iterations" / number).is_dir(),
                issues=tuple(issues),
            )
        )
    return states


def build_status_snapshot(root: Path, git: str, *, all_worktrees: bool) -> StatusSnapshot:
    if resolve_existing_git_root(root, git) is None:
        raise HarnessError("No Git repository found for status")
    common_dir = resolve_git_common_dir(git, root)
    head_result = run_git(git, root, ["rev-parse", "--verify", "HEAD"], check=False)
    if head_result.returncode != 0:
        raise HarnessError("The repository has no committed HEAD")
    head = decode_output(head_result.stdout)
    branch_result = run_git(git, root, ["symbolic-ref", "--quiet", "HEAD"], check=False)
    branch = decode_output(branch_result.stdout) if branch_result.returncode == 0 else None
    records = git_ref_records(git, root)
    iterations = build_iteration_ref_states(git, root, records)
    journals, journal_blockers = list_operation_journals(common_dir)
    warnings: list[str] = []
    blockers = list(journal_blockers)
    journal_by_operation = {
        str(value.get("operation_id")): value
        for value in journals
        if not value.get("corrupt") and isinstance(value.get("operation_id"), str)
    }
    for iteration in iterations:
        blockers.extend(
            BlockingReason("iteration-ref-inconsistent", f"PRD-{iteration.number}: {issue}")
            for issue in iteration.issues
        )
        metadata = iteration.allocation_metadata
        if metadata is not None:
            owner = metadata.get("operation_id")
            if isinstance(owner, str) and owner not in journal_by_operation:
                blockers.append(
                    BlockingReason(
                        "orphan-allocation-ref",
                        f"PRD-{iteration.number} allocation is owned by {owner}, but its journal is missing",
                    )
                )
            elif isinstance(owner, str):
                journal = journal_by_operation[owner]
                mismatches: list[str] = []
                if journal.get("iteration") != iteration.number:
                    mismatches.append("iteration")
                if journal.get("plan_digest") != metadata.get("plan_digest"):
                    mismatches.append("plan digest")
                if journal.get("allocation_object") != iteration.allocation_object:
                    mismatches.append("allocation object")
                if journal.get("base_commit") != iteration.base_commit:
                    mismatches.append("base commit")
                if journal.get("base_branch") != metadata.get("base_branch"):
                    mismatches.append("source base ref")
                if journal.get("governance_ref") != metadata.get("governance_ref"):
                    mismatches.append("governance ref")
                if journal.get("governance_commit") != metadata.get("governance_commit"):
                    mismatches.append("governance commit")
                if journal.get("governance_tree") != metadata.get("governance_tree"):
                    mismatches.append("governance tree")
                if journal.get("principle_sha256") != metadata.get("principle_sha256"):
                    mismatches.append("principle hash")
                if mismatches:
                    blockers.append(
                        BlockingReason(
                            "journal-ref-mismatch",
                            f"PRD-{iteration.number} journal differs from refs/metadata: {', '.join(mismatches)}",
                        )
                    )
    referenced_operations = {
        str(iteration.allocation_metadata.get("operation_id"))
        for iteration in iterations
        if iteration.allocation_metadata is not None
        and isinstance(iteration.allocation_metadata.get("operation_id"), str)
    }
    for operation_id, journal in journal_by_operation.items():
        phase = journal.get("phase")
        if phase in {"PLANNED", "RESERVED"}:
            blockers.append(
                BlockingReason(
                    "operation-incomplete",
                    f"Operation {operation_id} is {phase} and must be resumed or reconciled",
                )
            )
        elif phase == "FAILED_NEEDS_RECONCILE":
            blockers.append(
                BlockingReason(
                    "operation-failed-needs-reconcile",
                    f"Operation {operation_id} requires explicit reconciliation",
                )
            )
        if journal.get("phase") in {"RESERVED", "READY"} and operation_id not in referenced_operations:
            blockers.append(
                BlockingReason(
                    "orphan-operation-journal",
                    f"Operation {operation_id} is {journal.get('phase')} but owns no allocation ref",
                )
            )
        if phase == "READY" and tuple(journal.get("created_refs", [])) != tuple(
            journal.get("expected_refs", [])
        ):
            blockers.append(
                BlockingReason(
                    "operation-ref-manifest-mismatch",
                    f"Operation {operation_id} READY refs do not match its expected refs",
                )
            )
    for iteration in iterations:
        if (
            iteration.base_format == "legacy"
            and iteration.final_ref is None
            and iteration.base_commit is not None
            and iteration.base_commit != head
        ):
            warnings.append(
                f"PRD-{iteration.number}: legacy base differs from HEAD; authorized checkpoint or migration evidence may be required"
            )
    worktrees = list_worktree_states(git, root, all_worktrees=all_worktrees)
    next_gate = "reconcile" if blockers else "ready"
    return StatusSnapshot(
        project_root=str(root),
        git_common_dir=str(common_dir),
        head=head,
        branch=branch,
        iterations=tuple(iterations),
        worktrees=tuple(worktrees),
        journals=tuple(journals),
        warnings=tuple(warnings),
        blocking_reasons=tuple(blockers),
        next_gate=next_gate,
    )


def build_reserve_iteration_plan(
    root: Path,
    git: str,
    *,
    title: str,
    operation_id: str,
    base_ref: str,
    governance_ref: str,
) -> OperationPlan:
    validated_title = validate_label(title, "iteration title")
    validated_operation = validate_operation_id(operation_id)
    if resolve_existing_git_root(root, git) is None:
        raise HarnessError("No Git repository found; initialize Harness before reserving an iteration")
    common_dir = resolve_git_common_dir(git, root)
    blockers: list[BlockingReason] = []
    warnings: list[str] = []
    base_branch, base_commit = resolve_explicit_base_ref(git, root, base_ref)
    governance_branch, governance_commit, governance_snapshot = committed_governance_snapshot(
        git,
        root,
        governance_ref,
    )
    operational_status = build_status_snapshot(root, git, all_worktrees=False)
    for reason in operational_status.blocking_reasons:
        if reason.code == "operation-incomplete":
            warnings.append(reason.message + "; ref CAS will serialize allocation")
            continue
        blockers.append(reason)
    warnings.extend(operational_status.warnings)
    records = git_ref_records(git, root)
    existing = discover_committed_iteration_numbers(git, root, governance_commit, records)
    next_number = (max(existing) + 1) if existing else 1
    number = f"{next_number:03d}"
    intent = operation_intent(
        operation_id=validated_operation,
        project_root=root,
        title=validated_title,
        base_commit=base_commit,
        base_ref=base_branch,
        governance_ref=governance_branch,
        governance_commit=governance_commit,
        governance_snapshot=governance_snapshot,
        observed_next_iteration=number,
    )
    digest = schema_digest(intent)
    journal_path = operation_journal_path(common_dir, validated_operation)
    if journal_path.exists():
        try:
            existing_journal, _ = load_operation_journal(common_dir, validated_operation)
        except HarnessError:
            # list_operation_journals already supplied the structured corrupt-journal blocker.
            existing_journal = None
        if existing_journal is not None:
            if existing_journal.plan_digest != digest:
                blockers.append(
                    BlockingReason(
                        "operation-digest-mismatch",
                        f"Operation {validated_operation} already exists with a different plan digest",
                    )
                )
            elif existing_journal.action != "reserve-iteration":
                blockers.append(
                    BlockingReason(
                        "operation-action-mismatch",
                        f"Operation {validated_operation} is recorded for {existing_journal.action!r}",
                    )
                )
    reservation = {
        "strategy": "next-monotonic-v2-cas",
        "status": "planned",
        "observed_next_iteration": number,
        "allocation_ref": v2_allocation_ref(number),
        "base_ref": v2_iteration_base_ref(number),
        "base_commit": base_commit,
        "base_branch": base_branch,
        "source_base_ref": base_branch,
        "governance_ref": governance_branch,
        "governance_commit": governance_commit,
        "governance_tree": governance_snapshot["tree"],
        "principle_sha256": governance_snapshot["principle_sha256"],
        "journal_path": str(journal_path),
        "planned_ref_namespace": V2_REF_ROOT,
        "collision_policy": "advance-to-current-max-plus-one",
        "max_attempts": RESERVATION_MAX_ATTEMPTS,
    }
    return OperationPlan(
        operation_id=validated_operation,
        project_root=str(root),
        git_common_dir=str(common_dir),
        title=validated_title,
        base_commit=base_commit,
        base_branch=base_branch,
        governance_ref=governance_branch,
        governance_commit=governance_commit,
        governance_snapshot=governance_snapshot,
        observed_next_iteration=number,
        plan_digest=digest,
        manifest=intent,
        reservation=reservation,
        warnings=tuple(warnings),
        blocking_reasons=tuple(blockers),
        next_gate="blocked" if blockers else "reserve-iteration",
    )


def plan_from_operation_journal(
    root: Path,
    common_dir: Path,
    record: OperationJournal,
) -> OperationPlan:
    """Recover the accepted immutable plan instead of recomputing it from live HEAD."""
    if Path(record.project_root).resolve() != root.resolve():
        raise HarnessError(f"Operation {record.operation_id} belongs to a different project root")
    policy = record.manifest.get("reservation_policy")
    if not isinstance(policy, dict):
        raise HarnessError(f"Operation {record.operation_id} lacks its reservation policy")
    if policy.get("strategy") != "next-monotonic-v2-cas":
        raise HarnessError(f"Operation {record.operation_id} has an unsupported reservation strategy")
    if policy.get("collision_policy") != "advance-to-current-max-plus-one":
        raise HarnessError(f"Operation {record.operation_id} has an unsupported collision policy")
    if policy.get("max_attempts") != RESERVATION_MAX_ATTEMPTS:
        raise HarnessError(f"Operation {record.operation_id} has an unsupported retry bound")
    observed = policy.get("observed_next_iteration")
    if not isinstance(observed, str) or normalize_iteration_number(observed) != observed:
        raise HarnessError(f"Operation {record.operation_id} has an invalid observed iteration")
    if policy.get("observed_allocation_ref") != v2_allocation_ref(observed):
        raise HarnessError(f"Operation {record.operation_id} has an invalid observed allocation ref")
    if policy.get("observed_base_ref") != v2_iteration_base_ref(observed):
        raise HarnessError(f"Operation {record.operation_id} has an invalid observed base ref")
    if policy.get("ref_namespace") != V2_REF_ROOT:
        raise HarnessError(f"Operation {record.operation_id} has an invalid ref namespace")
    number = record.iteration or observed
    expected_refs = (v2_allocation_ref(number), v2_iteration_base_ref(number))
    if record.expected_refs != expected_refs:
        raise HarnessError(f"Operation {record.operation_id} journal expected refs do not match its iteration")
    reservation = {
        "strategy": policy["strategy"],
        "status": record.phase.lower(),
        "observed_next_iteration": observed,
        "allocation_ref": expected_refs[0],
        "base_ref": expected_refs[1],
        "base_commit": record.base_commit,
        "base_branch": record.base_branch,
        "source_base_ref": record.base_branch,
        "governance_ref": record.governance_ref,
        "governance_commit": record.governance_commit,
        "governance_tree": record.manifest["governance_snapshot"]["tree"],
        "principle_sha256": record.principle_sha256,
        "journal_path": str(operation_journal_path(common_dir, record.operation_id)),
        "planned_ref_namespace": V2_REF_ROOT,
        "collision_policy": policy["collision_policy"],
        "max_attempts": policy["max_attempts"],
    }
    return OperationPlan(
        operation_id=record.operation_id,
        project_root=record.project_root,
        git_common_dir=str(common_dir),
        title=record.title,
        base_commit=record.base_commit,
        base_branch=record.base_branch,
        governance_ref=record.governance_ref,
        governance_commit=record.governance_commit,
        governance_snapshot=dict(record.manifest["governance_snapshot"]),
        observed_next_iteration=observed,
        plan_digest=record.plan_digest,
        manifest=dict(record.manifest),
        reservation=reservation,
        next_gate="reconcile" if record.phase == "FAILED_NEEDS_RECONCILE" else "reserve-iteration",
    )


def reserve_iteration(plan: OperationPlan, git: str, root: Path) -> tuple[OperationJournal, bool]:
    if plan.blocking_reasons:
        detail = "; ".join(f"{reason.code}: {reason.message}" for reason in plan.blocking_reasons)
        raise HarnessError(f"Reservation plan is blocked: {detail}")
    common_dir = Path(plan.git_common_dir)
    journal, _ = create_operation_journal(common_dir, plan)
    if journal.phase == "FAILED_NEEDS_RECONCILE":
        raise HarnessError(f"Operation {plan.operation_id} requires reconcile: {journal.error or 'unknown error'}")
    if journal.phase in {"RESERVED", "READY"}:
        if journal.iteration is None:
            raise HarnessError(f"Operation {plan.operation_id} is {journal.phase} without an iteration")
        owned, allocation_object, owner = verify_owned_reservation(git, root, plan, journal.iteration)
        if not owned:
            raise HarnessError(
                f"Operation {plan.operation_id} journal claims PRD-{journal.iteration}, but refs belong to {owner or 'unknown'}"
            )
        if journal.allocation_object != allocation_object:
            raise HarnessError(
                f"Operation {plan.operation_id} journal allocation object does not match its ref"
            )
        if journal.phase == "RESERVED":
            journal = advance_operation_journal(common_dir, journal, "READY")
        return journal, False
    if journal.phase != "PLANNED":
        raise HarnessError(f"Unsupported reservation journal phase: {journal.phase}")

    candidate = journal.iteration or plan.observed_next_iteration
    created_refs_now = False
    for _ in range(RESERVATION_MAX_ATTEMPTS):
        candidate = normalize_iteration_number(candidate)
        if journal.iteration != candidate:
            journal = update_planned_journal(common_dir, journal, iteration=candidate)
        owned, existing_object, owner = verify_owned_reservation(git, root, plan, candidate)
        allocation_object: str | None = existing_object
        if not owned and existing_object is None:
            metadata = build_allocation_metadata(plan, candidate)
            metadata_raw = canonical_json_bytes(metadata)
            allocation_object = hash_git_blob(git, root, metadata_raw, write=False)
            if journal.allocation_object != allocation_object:
                journal = update_planned_journal(
                    common_dir,
                    journal,
                    iteration=candidate,
                    allocation_object=allocation_object,
                )
            written_object = hash_git_blob(git, root, metadata_raw, write=True)
            if written_object != allocation_object:
                raise HarnessError("Allocation object identity changed while writing the Git object")
            result = try_create_v2_reservation_refs(
                git,
                root,
                candidate,
                allocation_object,
                plan.base_commit,
                plan.base_branch,
                plan.governance_ref,
                plan.governance_commit,
            )
            if result.returncode == 0:
                owned = True
                owner = plan.operation_id
                created_refs_now = True
            else:
                owned, allocation_object, owner = verify_owned_reservation(git, root, plan, candidate)
                if not owned and allocation_object is None:
                    live_base = run_git(
                        git,
                        root,
                        ["rev-parse", "--verify", f"{plan.base_branch}^{{commit}}"],
                        check=False,
                    )
                    live_governance = run_git(
                        git,
                        root,
                        ["rev-parse", "--verify", f"{plan.governance_ref}^{{commit}}"],
                        check=False,
                    )
                    if live_base.returncode != 0 or decode_output(live_base.stdout) != plan.base_commit:
                        failed = advance_operation_journal(
                            common_dir,
                            journal,
                            "FAILED_NEEDS_RECONCILE",
                            error="Accepted source base ref changed before reservation CAS",
                        )
                        raise HarnessError(f"Operation {failed.operation_id} needs reconcile: {failed.error}")
                    if (
                        live_governance.returncode != 0
                        or decode_output(live_governance.stdout) != plan.governance_commit
                    ):
                        failed = advance_operation_journal(
                            common_dir,
                            journal,
                            "FAILED_NEEDS_RECONCILE",
                            error="Accepted governance ref changed before reservation CAS",
                        )
                        raise HarnessError(f"Operation {failed.operation_id} needs reconcile: {failed.error}")
                    failed = advance_operation_journal(
                        common_dir,
                        journal,
                        "FAILED_NEEDS_RECONCILE",
                        error="Git ref transaction failed without an observable reservation",
                    )
                    raise HarnessError(f"Operation {failed.operation_id} needs reconcile: {failed.error}")
        if owned:
            if allocation_object is None:
                raise HarnessError(f"Owned reservation {candidate} lacks an allocation metadata object")
            references = (v2_allocation_ref(candidate), v2_iteration_base_ref(candidate))
            journal = advance_operation_journal(
                common_dir,
                journal,
                "RESERVED",
                iteration=candidate,
                allocation_object=allocation_object,
                created_refs=references,
            )
            journal = advance_operation_journal(common_dir, journal, "READY")
            return journal, created_refs_now

        attempt = {
            "iteration": candidate,
            "result": "conflict",
            "owner_operation_id": owner,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        records = git_ref_records(git, root)
        existing_numbers = discover_iteration_numbers(root, records)
        next_number = (max(existing_numbers) + 1) if existing_numbers else int(candidate) + 1
        candidate = f"{next_number:03d}"
        journal = update_planned_journal(
            common_dir,
            journal,
            iteration=candidate,
            attempt=attempt,
        )
    failed = advance_operation_journal(
        common_dir,
        journal,
        "FAILED_NEEDS_RECONCILE",
        error="Allocation retry limit exceeded",
    )
    raise HarnessError(f"Operation {failed.operation_id} needs reconcile: {failed.error}")


def create_iteration_base_anchor(
    git: str,
    root: Path,
    number: str,
    base_commit: str,
    branch_ref: str,
) -> str:
    reference = iteration_base_ref(number, branch_ref)
    run_git(git, root, ["check-ref-format", reference])
    run_update_ref_without_hooks(
        git,
        root,
        ["-m", f"project-harness: anchor PRD-{number}", reference, base_commit, "0" * len(base_commit)],
    )
    return reference


def read_iteration_base_anchor(git: str, root: Path, number: str) -> tuple[str, str, str]:
    prefix = f"refs/project-harness/iterations/{number}/base/"
    result = run_git(
        git,
        root,
        ["for-each-ref", "--format=%(refname)%00%(objectname)", prefix],
    )
    anchors: list[tuple[str, str]] = []
    for raw_line in result.stdout.splitlines():
        raw_line = raw_line.rstrip(b"\r")
        if not raw_line:
            continue
        try:
            raw_reference, raw_object = raw_line.split(b"\0", 1)
        except ValueError as exc:
            raise HarnessError(f"Could not parse PRD-{number} base anchor") from exc
        anchors.append((raw_reference.decode("utf-8"), raw_object.decode("ascii")))
    if len(anchors) != 1:
        raise HarnessError(
            f"PRD-{number} must have exactly one immutable Git base anchor beneath {prefix}; "
            f"found {len(anchors)}"
        )
    reference, commit = anchors[0]
    branch_ref = reference[len(prefix) :]
    if not branch_ref.startswith("refs/heads/"):
        raise HarnessError(f"PRD-{number} base anchor encodes an invalid branch: {branch_ref}")
    commit_check = run_git(git, root, ["cat-file", "-e", f"{commit}^{{commit}}"], check=False)
    if commit_check.returncode != 0:
        raise HarnessError(f"PRD-{number} base anchor does not point to a commit: {commit}")
    return reference, commit, branch_ref


def validate_iteration_commit_gate(root: Path, git: str, number: str) -> tuple[Path, str, str]:
    report = collect_validation(root)
    if report.errors:
        print_validation(report, as_json=False)
        raise HarnessError("Repair Harness validation errors before committing an iteration")

    target = root / "harness" / "iterations" / number
    prd_path = target / f"prd-{number}.md"
    spec_path = target / f"spec-{number}.md"
    deviation_path = target / f"deviation-{number}.md"
    if not target.is_dir() or not prd_path.is_file() or not spec_path.is_file() or not deviation_path.is_file():
        raise HarnessError(f"Missing complete iteration bundle for PRD-{number}")

    prd_text = read_document(prd_path).text
    spec_text = read_document(spec_path).text
    deviation_text = read_document(deviation_path).text
    if parse_status(prd_text, "状态") != "已验收":
        raise HarnessError(f"PRD-{number} is not explicitly accepted (required status: 已验收)")
    if not explicit_user_acceptance_evidence(bullet_value(prd_text, "验收依据")):
        raise HarnessError(f"PRD-{number} lacks explicit user acceptance evidence")
    if parse_status(spec_text, "状态") != "已完成":
        raise HarnessError(f"SPEC-{number} must be 已完成 before the final iteration commit")
    if not explicit_user_implementation_authorization(bullet_value(spec_text, "实施授权")):
        raise HarnessError(f"SPEC-{number} lacks explicit implementation authorization")
    unresolved = [
        entry.identity
        for entry in deviation_entries(deviation_text)
        if entry.status in UNRESOLVED_DEVIATION_STATUSES
    ]
    if unresolved:
        raise HarnessError(f"PRD-{number} has unresolved as-built deviations: {', '.join(unresolved)}")
    progress_text = read_document(root / "harness" / "progress.md").text
    if not has_iteration_close_event(progress_text, number):
        raise HarnessError(f"PRD-{number} lacks a CLOSE event with final verification evidence")

    if repository_operation_markers(git, root):
        raise HarnessError("Git merge/rebase/cherry-pick/revert state is active; finish it before committing")
    final_ref = iteration_final_ref(number)
    marker = run_git(git, root, ["show-ref", "--verify", "--quiet", final_ref], check=False)
    if marker.returncode == 0:
        recorded = decode_output(run_git(git, root, ["rev-parse", final_ref]).stdout)
        raise HarnessError(f"PRD-{number} already has a recorded final commit: {recorded}")
    if marker.returncode != 1:
        raise HarnessError(
            f"Could not verify final-commit marker {final_ref}: "
            + (decode_output(marker.stderr) or f"exit {marker.returncode}")
        )
    head = run_git(git, root, ["rev-parse", "--verify", "HEAD"], check=False)
    if head.returncode != 0:
        raise HarnessError("The repository has no baseline commit; initialize or repair Git history first")
    current_head = decode_output(head.stdout)
    recorded_base = bullet_value(prd_text, "Git 基线")
    recorded_branch = bullet_value(prd_text, "Git 分支")
    _, base_commit, base_branch = read_iteration_base_anchor(git, root, number)
    if not recorded_base or not re.fullmatch(r"[0-9a-fA-F]{40,64}", recorded_base):
        raise HarnessError(f"PRD-{number} lacks a valid immutable Git baseline")
    if recorded_base.lower() != base_commit.lower() or recorded_branch != base_branch:
        raise HarnessError(
            f"PRD-{number} Git baseline metadata was changed after allocation; "
            f"document=({recorded_base}, {recorded_branch}) anchor=({base_commit}, {base_branch})"
        )
    if current_head.lower() != base_commit.lower():
        raise HarnessError(
            f"Git HEAD advanced after PRD-{number} was created ({base_commit} -> {current_head}); "
            "an intermediate commit exists, so the one-final-commit invariant cannot be satisfied"
        )
    branch = run_git(git, root, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    if branch.returncode != 0:
        raise HarnessError("Detached HEAD is not allowed for an iteration final commit")
    branch_ref = decode_output(run_git(git, root, ["symbolic-ref", "--quiet", "HEAD"]).stdout)
    if branch_ref != base_branch:
        raise HarnessError(
            f"PRD-{number} was created on {base_branch!r}, but HEAD is now attached to {branch_ref!r}"
        )
    ensure_git_identity(git, root)
    staged = git_index_changes_including_intent(git, root)
    if staged:
        raise HarnessError(f"Git index already contains staged changes or intent-to-add entries: {staged}")
    head_tree = decode_output(run_git(git, root, ["rev-parse", "HEAD^{tree}"]).stdout)
    index_tree = decode_output(run_git(git, root, ["write-tree"]).stdout)
    if index_tree != head_tree:
        raise HarnessError(
            "Git index differs from HEAD even though no ordinary staged diff is visible; "
            "remove intent-to-add or other hidden index state before finalization"
        )

    history = run_git(
        git,
        root,
        [
            "--literal-pathspecs",
            "rev-list",
            "--all",
            "--reflog",
            "--",
            f"harness/iterations/{number}",
        ],
    )
    if decode_output(history.stdout):
        raise HarnessError(
            f"PRD-{number} already appears in Git history; refusing an intermediate or second iteration commit"
        )
    title_match = re.search(rf"^# PRD-{re.escape(number)}：(.+)$", prd_text, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else f"iteration {number}"
    return target, title, base_commit


def assert_no_post_baseline_history(
    git: str,
    root: Path,
    number: str,
    base_commit: str,
    candidates: Sequence[str],
) -> None:
    touched: list[str] = []
    for path in candidates:
        history = run_git(
            git,
            root,
            [
                "--literal-pathspecs",
                "rev-list",
                "--all",
                "--reflog",
                "--not",
                base_commit,
                "--",
                path,
            ],
        )
        commit = decode_output(history.stdout).splitlines()
        if commit:
            touched.append(f"{path} ({commit[0]})")
    if touched:
        raise HarnessError(
            f"PRD-{number} candidate paths appear in commits outside its immutable baseline; "
            "an intermediate, reset, or prior final commit prevents one-commit finalization: "
            + ", ".join(touched)
        )


def plan_iteration_commit_paths(
    root: Path,
    git: str,
    number: str,
    include_values: Sequence[str],
) -> tuple[list[str], list[str]]:
    tracked_changes = git_changed_files(git, root, cached=False)
    untracked = git_untracked_files(git, root)
    dirty = sorted(set(tracked_changes) | set(untracked))
    includes = [normalize_include(root, value) for value in include_values]
    if any(relative == "harness" and is_directory for relative, is_directory in includes):
        raise HarnessError("Include shared governance files individually; '--include harness' is too broad")

    for relative, _ in includes:
        ignored = run_git(
            git,
            root,
            ["check-ignore", "--no-index", "--quiet", "--", relative],
            check=False,
        )
        if ignored.returncode == 0:
            raise HarnessError(f"Included path is ignored by Git; do not force-add it: {relative}")
        if ignored.returncode != 1:
            detail = decode_output(ignored.stderr) or f"exit {ignored.returncode}"
            raise HarnessError(f"Could not verify ignore rules for {relative}: {detail}")

    shared_controls = {
        "AGENTS.md",
        "harness/README.md",
        "harness/principle.md",
        "harness/progress.md",
        "harness/iterations/.gitkeep",
    }
    iteration_prefix = f"harness/iterations/{number}/"
    candidates: list[str] = []
    unrelated: list[str] = []
    forbidden_harness: list[str] = []
    omitted_controls: list[str] = []
    for path in dirty:
        if path.startswith(iteration_prefix):
            candidates.append(path)
        elif path in shared_controls:
            if any(path_matches_include(path, include) for include in includes):
                candidates.append(path)
            else:
                omitted_controls.append(path)
        elif path == "harness" or path.startswith("harness/"):
            forbidden_harness.append(path)
        elif any(path_matches_include(path, include) for include in includes):
            candidates.append(path)
        else:
            unrelated.append(path)
    if forbidden_harness:
        raise HarnessError(
            "Dirty Harness files outside the accepted iteration cannot be included: "
            + ", ".join(forbidden_harness)
        )
    if omitted_controls:
        raise HarnessError(
            "Dirty shared governance files require an explicit --include after confirming that every change "
            "belongs to the accepted iteration: " + ", ".join(omitted_controls)
        )
    for include in includes:
        if not any(path_matches_include(path, include) for path in dirty):
            raise HarnessError(f"Included path has no uncommitted change: {include[0]}")
    required_bundle = {
        f"{iteration_prefix}README.md",
        f"{iteration_prefix}prd-{number}.md",
        f"{iteration_prefix}spec-{number}.md",
        f"{iteration_prefix}deviation-{number}.md",
    }
    missing_bundle_candidates = sorted(required_bundle - set(candidates))
    if missing_bundle_candidates:
        raise HarnessError(
            f"The final PRD-{number} commit must contain all four uncommitted bundle files; missing: "
            + ", ".join(missing_bundle_candidates)
        )

    existing_candidates = [path for path in candidates if (root / Path(path)).is_file()]
    inspect_baseline_files(root, existing_candidates, {})
    return sorted(set(candidates)), unrelated


def print_iteration_commit_plan(
    root: Path,
    number: str,
    candidates: Sequence[str],
    unrelated: Sequence[str],
    *,
    dry_run: bool,
) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"{mode} final commit for PRD-{number}: {root}")
    for path in candidates:
        print(f"COMMIT_PATH {json.dumps(path, ensure_ascii=False)}")
    for path in unrelated:
        print(f"UNRELATED_UNSTAGED {json.dumps(path, ensure_ascii=False)}")


def commit_iteration(
    root: Path,
    git: str,
    number: str,
    base_commit: str,
    candidates: Sequence[str],
    message: str,
) -> str:
    staged_by_command = False
    created_commit: str | None = None
    try:
        stage_exact_paths(git, root, candidates)
        staged_by_command = True
        staged = git_changed_files(git, root, cached=True)
        if staged != sorted(candidates):
            raise HarnessError(f"Staged files differ from reviewed commit paths: {staged}")
        inspect_staged_blobs(git, root, candidates, allow_deletions=True)
        print_staged_summary(git, root)
        current_head = decode_output(run_git(git, root, ["rev-parse", "--verify", "HEAD"]).stdout)
        if current_head != base_commit:
            raise HarnessError(
                f"Git HEAD changed while preparing PRD-{number}: {base_commit} -> {current_head}"
            )
        branch_ref = decode_output(run_git(git, root, ["symbolic-ref", "--quiet", "HEAD"]).stdout)
        reviewed_tree = decode_output(run_git(git, root, ["write-tree"]).stdout)
        commit = decode_output(
            run_git(
                git,
                root,
                ["commit-tree", reviewed_tree, "-p", base_commit, "-F", "-"],
                input_bytes=(message + "\n").encode("utf-8"),
            ).stdout
        )
        final_ref = iteration_final_ref(number)
        transaction = (
            f"start\n"
            f"create {final_ref} {commit}\n"
            f"update {branch_ref} {commit} {base_commit}\n"
            f"prepare\n"
            f"commit\n"
        ).encode("ascii")
        run_update_ref_without_hooks(
            git,
            root,
            ["-m", f"project-harness: finalize PRD-{number}", "--stdin"],
            input_bytes=transaction,
        )
        created_commit = commit
        committed_parent = decode_output(run_git(git, root, ["rev-parse", f"{commit}^"]).stdout)
        if committed_parent != base_commit:
            raise HarnessError(
                f"Final commit parent differs from the immutable baseline: {base_commit} -> {committed_parent}"
            )
        committed_tree = decode_output(run_git(git, root, ["rev-parse", f"{commit}^{{tree}}"]).stdout)
        if committed_tree != reviewed_tree:
            raise HarnessError(
                f"Final commit tree differs from the reviewed staged tree: {reviewed_tree} -> {committed_tree}"
            )
        committed = git_path_list(
            run_git(
                git,
                root,
                ["diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", commit],
            )
        )
        if committed != sorted(candidates):
            raise HarnessError(
                f"Final commit tree differs from the reviewed path set; commit={commit} paths={committed}"
            )
        recorded = decode_output(run_git(git, root, ["rev-parse", final_ref]).stdout)
        if recorded != commit:
            raise HarnessError(f"Final marker {final_ref} does not point to commit {commit}")
        return commit
    except Exception as exc:
        if created_commit is not None:
            raise HarnessError(
                f"Commit {created_commit} was created, but the post-commit path audit failed: {exc}. "
                "Do not retry or rewrite history; inspect the commit and ask the user how to proceed."
            ) from exc
        if staged_by_command:
            try:
                unstage_exact_paths(git, root, candidates)
            except Exception as rollback_exc:
                raise HarnessError(
                    f"Iteration commit failed ({exc}); staged-index rollback also failed: {rollback_exc}"
                ) from exc
        if isinstance(exc, HarnessError):
            raise
        raise HarnessError(f"Iteration commit failed: {exc}") from exc


def initialize_new_repository(
    root: Path,
    operations: Sequence[Operation],
    baseline: Sequence[BaselineFile],
    git: str,
) -> tuple[ValidationReport, str]:
    expected = list(baseline)
    git_created = False
    created_directories = missing_operation_directories(operations, root)
    try:
        apply_operations(root, operations)
        git_marker = root / ".git"
        ensure_inside_root(git_marker, root)
        try:
            git_marker.mkdir()
        except FileExistsError as exc:
            raise HarnessError(
                "Git metadata appeared after the no-repository preview; refusing to initialize or remove it"
            ) from exc
        git_created = True
        run_git(git, root, ["init", "-b", "main", "--template="], safe_directory=False)
        branch = decode_output(run_git(git, root, ["symbolic-ref", "--short", "HEAD"]).stdout)
        if branch != "main":
            raise HarnessError(f"Git initialized an unexpected branch {branch!r}; expected 'main'")
        ensure_git_identity(git, root)

        actual_paths = git_untracked_files(git, root)
        actual = inspect_baseline_files(root, actual_paths, {})
        if actual != expected:
            expected_names = {entry.relative for entry in expected}
            actual_names = {entry.relative for entry in actual}
            added = sorted(actual_names - expected_names)
            missing = sorted(expected_names - actual_names)
            changed = sorted(
                entry.relative
                for entry in actual
                if entry.relative in expected_names
                and entry != next(item for item in expected if item.relative == entry.relative)
            )
            raise HarnessError(
                "Baseline changed after preview; refusing to commit"
                + (f"; added={added}" if added else "")
                + (f"; missing={missing}" if missing else "")
                + (f"; content-changed={changed}" if changed else "")
            )

        report = collect_validation(root)
        if report.errors:
            print_validation(report, as_json=False)
            raise HarnessError("Harness validation failed before the initial baseline commit")

        expected_paths = [entry.relative for entry in expected]
        stage_exact_paths(git, root, expected_paths)
        staged = git_changed_files(git, root, cached=True)
        if staged != sorted(expected_paths):
            raise HarnessError(
                f"Staged baseline differs from the reviewed file set; expected={sorted(expected_paths)} staged={staged}"
            )
        staged_entries, staged_deletions = inspect_staged_blobs(
            git,
            root,
            expected_paths,
            allow_deletions=False,
        )
        if staged_deletions or staged_entries != sorted(expected, key=lambda item: item.relative):
            raise HarnessError(
                "Git clean filters changed baseline bytes after the reviewed preview; "
                "adjust attributes/configuration or make the working-tree bytes match the intended commit"
            )
        print_staged_summary(git, root)
        reviewed_tree = decode_output(run_git(git, root, ["write-tree"]).stdout)
        with tempfile.TemporaryDirectory(prefix="project-harness-empty-hooks-") as hooks:
            run_git(
                git,
                root,
                [
                    "-c",
                    f"core.hooksPath={hooks}",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    "chore(harness): initialize project baseline",
                ],
            )
        commit = decode_output(run_git(git, root, ["rev-parse", "HEAD"]).stdout)
        committed_tree = decode_output(run_git(git, root, ["rev-parse", f"{commit}^{{tree}}"]).stdout)
        if committed_tree != reviewed_tree:
            raise HarnessError("Initial commit tree differs from the reviewed staged tree")
        committed_paths = git_path_list(
            run_git(
                git,
                root,
                ["diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", commit],
            )
        )
        if committed_paths != sorted(expected_paths):
            raise HarnessError(f"Initial commit paths differ from the reviewed baseline: {committed_paths}")
        status = run_git(
            git,
            root,
            ["status", "--porcelain=v1", "--untracked-files=all", "-z"],
        )
        if status.stdout:
            raise HarnessError("Repository is not clean after the initial baseline commit")
        return collect_validation(root), commit
    except Exception as exc:
        cleanup_errors: list[str] = []
        if git_created:
            git_error = remove_created_git_metadata(root)
            if git_error:
                cleanup_errors.append(git_error)
        cleanup_errors.extend(rollback_operations(operations, root, created_directories))
        if cleanup_errors:
            raise HarnessError(
                f"Initialization failed ({exc}); automatic rollback was incomplete: "
                + "; ".join(cleanup_errors)
            ) from exc
        if isinstance(exc, HarnessError):
            raise
        raise HarnessError(f"Initialization failed and was rolled back: {exc}") from exc


def command_init(args: argparse.Namespace) -> int:
    root = resolve_project_root(args.project_root)
    project_name = validate_label(args.project_name or root.name, "project name")
    git = require_git()
    existing_git_root = resolve_existing_git_root(root, git)
    accepted_plan: tuple[datetime, str] | None = None
    if args.accept_baseline_plan:
        accepted_plan = parse_baseline_plan_token(args.accept_baseline_plan)
    planned_at = accepted_plan[0] if accepted_plan else datetime.now().astimezone()
    operations = build_init_operations(root, project_name, planned_at)
    print_plan(root, operations, dry_run=args.dry_run)
    if existing_git_root is None:
        baseline = build_new_repository_baseline(root, operations, git)
        print_baseline_plan(baseline, dry_run=args.dry_run)
        digest = baseline_manifest_digest(root, baseline)
        token = baseline_plan_token(planned_at, digest)
        print(f"BASELINE_DIGEST {digest}")
        print(f"BASELINE_PLAN_TOKEN {token}")
        if args.dry_run:
            return 0
        if accepted_plan is None:
            raise HarnessError(
                "A no-Git bootstrap apply requires --accept-baseline-plan from a preceding init --dry-run; "
                "no files were written"
            )
        if accepted_plan[1] != digest:
            raise HarnessError(
                "The accepted baseline plan no longer matches the target files or rendered Harness; "
                "rerun init --dry-run and review the new BASELINE_PLAN_TOKEN. No files were written"
            )
        report, commit = initialize_new_repository(root, operations, baseline, git)
        print_validation(report, as_json=False)
        print(f"BASELINE_COMMIT {commit}")
        return 1 if report.errors else 0
    if accepted_plan is not None:
        raise HarnessError(
            "--accept-baseline-plan is only valid while bootstrapping a target that has no Git repository; "
            "the target now has Git, so no files were written"
        )
    if args.dry_run:
        return 0
    created_directories = missing_operation_directories(operations, root)
    try:
        apply_operations(root, operations)
        report = collect_validation(root)
        if report.errors:
            print_validation(report, as_json=False)
            raise HarnessError("Harness validation failed; initialization changes were rolled back")
    except Exception as exc:
        rollback_errors = rollback_operations(operations, root, created_directories)
        if rollback_errors:
            raise HarnessError(
                f"Existing-repository initialization failed ({exc}); rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, HarnessError):
            raise
        raise HarnessError(f"Existing-repository initialization failed and was rolled back: {exc}") from exc
    print_validation(report, as_json=False)
    return 0


def assert_existing_iterations_finalized(git: str, root: Path) -> None:
    iterations = root / "harness" / "iterations"
    existing = [f"{value:03d}" for value in find_existing_numbers(iterations)]
    if not existing:
        return
    missing: list[str] = []
    unreachable: list[str] = []
    for number in existing:
        final_ref = iteration_final_ref(number)
        marker = run_git(git, root, ["show-ref", "--verify", "--quiet", final_ref], check=False)
        if marker.returncode != 0:
            missing.append(number)
            continue
        final_commit = decode_output(run_git(git, root, ["rev-parse", final_ref]).stdout)
        ancestor = run_git(
            git,
            root,
            ["merge-base", "--is-ancestor", final_commit, "HEAD"],
            check=False,
        )
        if ancestor.returncode != 0:
            unreachable.append(f"{number} ({final_commit})")
    if missing or unreachable:
        details: list[str] = []
        if missing:
            details.append("missing final marker: " + ", ".join(missing))
        if unreachable:
            details.append("final commit not reachable from HEAD: " + ", ".join(unreachable))
        raise HarnessError(
            "Create iterations serially in this no-concurrent-lifecycle version; finish the existing iteration "
            "before allocating another (" + "; ".join(details) + ")"
        )

    dirty = sorted(set(git_changed_files(git, root, cached=False)) | set(git_untracked_files(git, root)))
    dirty_governance = [
        path for path in dirty if path == "AGENTS.md" or path == "harness" or path.startswith("harness/")
    ]
    if dirty_governance:
        raise HarnessError(
            "Resolve dirty governance left after finalized iterations before allocating another: "
            + ", ".join(dirty_governance)
        )


def assert_legacy_command_has_no_v2_identity(
    git: str,
    root: Path,
    *,
    number: str | None = None,
) -> None:
    prefix = V2_REF_ROOT if number is None else f"{V2_REF_ROOT}/iterations/{number}/"
    records = git_ref_records(git, root)
    conflicting = sorted(reference for reference in records if reference.startswith(prefix))
    if number is not None:
        allocation_reference = v2_allocation_ref(number)
        if allocation_reference in records:
            conflicting.insert(0, allocation_reference)
    if conflicting:
        scope = "the repository" if number is None else f"PRD-{number}"
        raise HarnessError(
            f"Legacy lifecycle command is not applicable to {scope} after v2 identity exists; "
            "use the v2 plan/reserve/workspace gates instead"
        )


def command_new_iteration(args: argparse.Namespace) -> int:
    root = resolve_project_root(args.project_root)
    git = require_git()
    if resolve_existing_git_root(root, git) is None:
        raise HarnessError("No Git repository found; run Harness initialization before creating an iteration")
    assert_legacy_command_has_no_v2_identity(git, root)
    head = run_git(git, root, ["rev-parse", "--verify", "HEAD"], check=False)
    if head.returncode != 0:
        raise HarnessError(
            "This existing Git repository has no baseline commit. Create and explicitly authorize a project baseline "
            "before starting an iteration; Harness initialization will not commit in an existing repository."
        )
    base_commit = decode_output(head.stdout)
    branch = run_git(git, root, ["symbolic-ref", "--quiet", "HEAD"], check=False)
    if branch.returncode != 0:
        raise HarnessError("Detached HEAD is not allowed when creating an iteration baseline")
    base_branch = decode_output(branch.stdout)
    title = validate_label(args.title, "iteration title")
    number, operations = build_new_iteration_operations(
        root,
        title,
        datetime.now().astimezone(),
        base_commit,
        base_branch,
    )
    assert_existing_iterations_finalized(git, root)
    print_plan(root, operations, dry_run=args.dry_run)
    planned_anchor = iteration_base_ref(number, base_branch)
    print(f"{'DRY-RUN' if args.dry_run else 'APPLY'} BASE_ANCHOR {planned_anchor} {base_commit}")
    if args.dry_run:
        return 0
    created_directories = missing_operation_directories(operations, root)
    anchor_ref: str | None = None
    try:
        apply_operations(root, operations)
        anchor_ref = create_iteration_base_anchor(
            git,
            root,
            number,
            base_commit,
            base_branch,
        )
        report = collect_validation(root)
        if report.errors:
            print_validation(report, as_json=False)
            raise HarnessError("New iteration failed validation and was rolled back")
    except Exception as exc:
        rollback_errors: list[str] = []
        if anchor_ref is not None:
            try:
                run_update_ref_without_hooks(git, root, ["-d", anchor_ref, base_commit])
            except Exception as anchor_exc:
                rollback_errors.append(f"could not remove base anchor {anchor_ref}: {anchor_exc}")
        rollback_errors.extend(rollback_operations(operations, root, created_directories))
        if rollback_errors:
            raise HarnessError(
                f"New iteration failed ({exc}); rollback was incomplete: " + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, HarnessError):
            raise
        raise HarnessError(f"New iteration failed and was rolled back: {exc}") from exc
    print_validation(report, as_json=False)
    return 0


def command_commit_iteration(args: argparse.Namespace) -> int:
    root = resolve_project_root(args.project_root)
    git = require_git()
    if resolve_existing_git_root(root, git) is None:
        raise HarnessError("No Git repository found; initialize the Harness before committing an iteration")
    number = normalize_iteration_number(args.number)
    assert_legacy_command_has_no_v2_identity(git, root, number=number)
    _, title, base_commit = validate_iteration_commit_gate(root, git, number)
    candidates, unrelated = plan_iteration_commit_paths(root, git, number, args.include or [])
    assert_no_post_baseline_history(git, root, number, base_commit, candidates)
    message = validate_label(
        args.message or f"feat: complete PRD-{number} — {title}",
        "commit message",
        max_length=240,
    )
    if f"PRD-{number}" not in message:
        raise HarnessError(f"Final commit message must include PRD-{number}")
    print_iteration_commit_plan(root, number, candidates, unrelated, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    commit = commit_iteration(root, git, number, base_commit, candidates, message)
    print(f"ITERATION_COMMIT {commit}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = resolve_project_root(args.project_root)
    report = collect_validation(root)
    print_validation(report, as_json=args.json)
    return 1 if report.errors else 0


def print_operation_json(value: Mapping[str, object]) -> None:
    print(canonical_json_bytes(dict(value)).decode("utf-8"))


def print_status_snapshot(snapshot: StatusSnapshot, *, as_json: bool) -> None:
    value = snapshot.as_dict()
    if as_json:
        print_operation_json(value)
        return
    print(f"STATUS {snapshot.next_gate.upper()}")
    print(f"PROJECT_ROOT {snapshot.project_root}")
    print(f"GIT_COMMON_DIR {snapshot.git_common_dir}")
    print(f"HEAD {snapshot.head}")
    print(f"BRANCH {snapshot.branch or '(detached)'}")
    for iteration in snapshot.iterations:
        phase = "allocated" if iteration.allocation_ref else "legacy"
        print(f"ITERATION {iteration.number} {phase} {iteration.base_commit or '(no-base)'}")
    for worktree in snapshot.worktrees:
        dirty = worktree.get("dirty")
        dirty_label = "unknown" if dirty is None else ("dirty" if dirty else "clean")
        print(f"WORKTREE {worktree.get('worktree', '(unknown)')} {dirty_label}")
    for warning in snapshot.warnings:
        print(f"WARNING {warning}")
    for reason in snapshot.blocking_reasons:
        print(f"BLOCKED {reason.code}: {reason.message}")


def command_status(args: argparse.Namespace) -> int:
    root = resolve_project_root(args.project_root)
    git = require_git()
    snapshot = build_status_snapshot(root, git, all_worktrees=args.all_worktrees)
    print_status_snapshot(snapshot, as_json=args.json)
    return 1 if snapshot.blocking_reasons else 0


def print_reservation_plan(plan: OperationPlan, *, as_json: bool) -> None:
    value = plan.as_dict()
    if as_json:
        print_operation_json(value)
        return
    print(f"PLAN {plan.operation_id} {value['phase']}")
    print(f"PROJECT_ROOT {plan.project_root}")
    print(f"PLAN_DIGEST {plan.plan_digest}")
    print(f"OBSERVED_ITERATION {plan.observed_next_iteration}")
    print(f"BASE {plan.base_commit} {plan.base_branch or '(detached)'}")
    print(f"ALLOCATION_REF {plan.reservation['allocation_ref']}")
    print(f"BASE_REF {plan.reservation['base_ref']}")
    for warning in plan.warnings:
        print(f"WARNING {warning}")
    for reason in plan.blocking_reasons:
        print(f"BLOCKED {reason.code}: {reason.message}")


def command_plan_reserve_iteration(args: argparse.Namespace) -> int:
    root = resolve_project_root(args.project_root)
    git = require_git()
    plan = build_reserve_iteration_plan(
        root,
        git,
        title=args.title,
        operation_id=new_operation_id(),
        base_ref=args.base_ref,
        governance_ref=args.governance_ref,
    )
    print_reservation_plan(plan, as_json=args.json)
    return 1 if plan.blocking_reasons else 0


def reservation_result(
    plan: OperationPlan,
    journal: OperationJournal | None,
    *,
    created_now: bool,
) -> dict[str, object]:
    if journal is None:
        phase = "blocked"
        reservation = dict(plan.reservation)
    else:
        if journal.iteration is None or journal.allocation_object is None:
            raise HarnessError(f"Operation {journal.operation_id} reached {journal.phase} without reservation data")
        phase = "succeeded" if journal.phase == "READY" else journal.phase.lower()
        reservation = {
            "status": "ready" if journal.phase == "READY" else journal.phase.lower(),
            "journal_phase": journal.phase,
            "iteration": journal.iteration,
            "allocation_ref": v2_allocation_ref(journal.iteration),
            "base_ref": v2_iteration_base_ref(journal.iteration),
            "allocation_object": journal.allocation_object,
            "base_commit": journal.base_commit,
            "base_branch": journal.base_branch or None,
            "governance_ref": journal.governance_ref,
            "governance_commit": journal.governance_commit,
            "governance_tree": journal.manifest["governance_snapshot"]["tree"],
            "principle_sha256": journal.principle_sha256,
            "journal_path": str(operation_journal_path(Path(plan.git_common_dir), plan.operation_id)),
            "created_now": created_now,
            "idempotent_replay": not created_now,
            "ref_namespace": V2_REF_ROOT,
        }
    return {
        "schema_version": RESERVATION_RESULT_SCHEMA_V1,
        "command": "reserve-iteration",
        "action_level": "notify",
        "pushed": False,
        "project_root": plan.project_root,
        "operation_id": plan.operation_id,
        "title": plan.title,
        "phase": phase,
        "plan_digest": plan.plan_digest,
        "reservation": reservation,
        "warnings": list(plan.warnings),
        "blocking_reasons": [asdict(reason) for reason in plan.blocking_reasons],
        "next_gate": "create-iteration-workspace" if phase == "succeeded" else "blocked",
        "exclusions": [
            "no worktree",
            "no branch",
            "no governance bundle",
            "no progress update",
            "no commit",
            "no push",
        ],
    }


def print_reservation_result(value: Mapping[str, object], *, as_json: bool) -> None:
    if as_json:
        print_operation_json(value)
        return
    print(f"RESERVATION {value['operation_id']} {value['phase']}")
    reservation = value.get("reservation")
    if isinstance(reservation, dict):
        if reservation.get("iteration"):
            print(f"ITERATION {reservation['iteration']}")
        print(f"ALLOCATION_REF {reservation.get('allocation_ref', '(none)')}")
        print(f"BASE_REF {reservation.get('base_ref', '(none)')}")
    for reason in value.get("blocking_reasons", []):
        if isinstance(reason, dict):
            print(f"BLOCKED {reason.get('code')}: {reason.get('message')}")


def command_reserve_iteration(args: argparse.Namespace) -> int:
    root = resolve_project_root(args.project_root)
    git = require_git()
    operation_id = validate_operation_id(args.operation_id)
    accepted_digest = args.accept_plan_digest.strip()
    if not PLAN_DIGEST_PATTERN.fullmatch(accepted_digest):
        raise HarnessError("Accepted plan digest must contain exactly 64 lowercase hexadecimal characters")
    common_dir = resolve_git_common_dir(git, root)
    with operation_lock(common_dir, operation_id):
        journal_path = operation_journal_path(common_dir, operation_id)
        if journal_path.exists():
            try:
                existing, _ = load_operation_journal(common_dir, operation_id)
            except HarnessError:
                plan = build_reserve_iteration_plan(
                    root,
                    git,
                    title=args.title,
                    operation_id=operation_id,
                    base_ref=args.base_ref,
                    governance_ref=args.governance_ref,
                )
                value = reservation_result(plan, None, created_now=False)
                print_reservation_result(value, as_json=args.json)
                return 1
            plan = plan_from_operation_journal(root, common_dir, existing)
            reasons: list[BlockingReason] = []
            if accepted_digest != existing.plan_digest:
                reasons.append(
                    BlockingReason(
                        "accepted-plan-digest-mismatch",
                        "The supplied accepted plan digest does not match the durable operation manifest",
                    )
                )
            if args.title.strip() != existing.title:
                reasons.append(
                    BlockingReason(
                        "operation-title-mismatch",
                        "The supplied title does not match the durable operation manifest",
                    )
                )
            if args.base_ref.strip() != existing.base_branch:
                reasons.append(
                    BlockingReason(
                        "operation-base-ref-mismatch",
                        "The supplied base ref does not match the durable operation manifest",
                    )
                )
            if args.governance_ref.strip() != existing.governance_ref:
                reasons.append(
                    BlockingReason(
                        "operation-governance-ref-mismatch",
                        "The supplied governance ref does not match the durable operation manifest",
                    )
                )
            if reasons:
                plan = replace(plan, blocking_reasons=tuple(reasons), next_gate="use-accepted-plan")
        else:
            plan = build_reserve_iteration_plan(
                root,
                git,
                title=args.title,
                operation_id=operation_id,
                base_ref=args.base_ref,
                governance_ref=args.governance_ref,
            )
            if accepted_digest != plan.plan_digest:
                plan = replace(
                    plan,
                    blocking_reasons=plan.blocking_reasons
                    + (
                        BlockingReason(
                            "accepted-plan-digest-mismatch",
                            "Repository state or the requested manifest changed after planning; run plan again",
                        ),
                    ),
                    next_gate="plan-reserve-iteration",
                )
        if plan.blocking_reasons:
            value = reservation_result(plan, None, created_now=False)
            print_reservation_result(value, as_json=args.json)
            return 1
        base_object = run_git(
            git,
            root,
            ["cat-file", "-e", f"{plan.base_commit}^{{commit}}"],
            check=False,
        )
        if base_object.returncode != 0:
            raise HarnessError("The accepted base commit is no longer available; reconcile before retrying")
        governance_object = run_git(
            git,
            root,
            ["cat-file", "-e", f"{plan.governance_commit}^{{commit}}"],
            check=False,
        )
        if governance_object.returncode != 0:
            raise HarnessError("The accepted governance commit is no longer available; reconcile before retrying")
        journal, created_now = reserve_iteration(plan, git, root)
    value = reservation_result(plan, journal, created_now=created_now)
    print_reservation_result(value, as_json=args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Initialize and validate a lightweight PRD/SPEC project Harness. "
            "For a target without Git, init creates main and one reviewed baseline commit; "
            "when Git already exists, init writes Harness files but never commits them."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize Harness; bootstrap Git+baseline only when the target has no Git",
        description=(
            "Initialize or idempotently repair global Harness scaffolding. A target without Git receives "
            "git init -b main plus one reviewed baseline commit. An existing Git repository receives no commit."
        ),
    )
    init_parser.add_argument("--project-root", required=True, help="Absolute or relative target project directory")
    init_parser.add_argument("--project-name", help="Human-readable project name; defaults to directory name")
    init_parser.add_argument("--dry-run", action="store_true", help="Print the mutation plan without writing")
    init_parser.add_argument(
        "--accept-baseline-plan",
        help=(
            "For a no-Git apply, the exact BASELINE_PLAN_TOKEN emitted by a preceding init --dry-run; "
            "binds the reviewed file manifest before any write"
        ),
    )
    init_parser.set_defaults(func=command_init)

    new_parser = subparsers.add_parser("new-iteration", help="Allocate and create the next complete iteration bundle")
    new_parser.add_argument("--project-root", required=True, help="Target project directory")
    new_parser.add_argument("--title", required=True, help="One-line iteration title")
    new_parser.add_argument("--dry-run", action="store_true", help="Print the mutation plan without writing")
    new_parser.set_defaults(func=command_new_iteration)

    commit_parser = subparsers.add_parser(
        "commit-iteration",
        help="Create the one final iteration commit after explicit user acceptance",
    )
    commit_parser.add_argument("--project-root", required=True, help="Target Git repository root")
    commit_parser.add_argument("--number", required=True, help="Accepted iteration number, for example 001")
    commit_parser.add_argument(
        "--include",
        action="append",
        default=[],
        help=(
            "Explicit changed implementation/test or shared control path to include; "
            "repeat for multiple paths"
        ),
    )
    commit_parser.add_argument("--message", help="Commit subject; must include PRD-NNN")
    commit_parser.add_argument("--dry-run", action="store_true", help="Preview exact commit paths without staging")
    commit_parser.set_defaults(func=command_commit_iteration)

    validate_parser = subparsers.add_parser("validate", help="Validate Harness structure and derived status routing")
    validate_parser.add_argument("--project-root", required=True, help="Target project directory")
    validate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate_parser.set_defaults(func=command_validate)

    status_parser = subparsers.add_parser(
        "status",
        help="Inspect Harness refs, operation journals, and local worktree state without writing",
    )
    status_parser.add_argument("--project-root", required=True, help="Target Git repository root")
    status_parser.add_argument(
        "--all-worktrees",
        action="store_true",
        help="Include every linked worktree that shares the repository common directory",
    )
    status_parser.add_argument("--json", action="store_true", help="Emit canonical machine-readable JSON")
    status_parser.set_defaults(func=command_status)

    plan_parser = subparsers.add_parser("plan", help="Compute a mutation manifest without writing")
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command", required=True)
    plan_reserve_parser = plan_subparsers.add_parser(
        "reserve-iteration",
        help="Plan the next monotonic iteration reservation without creating journal, objects, or refs",
    )
    plan_reserve_parser.add_argument("--project-root", required=True, help="Target Git repository root")
    plan_reserve_parser.add_argument("--title", required=True, help="One-line iteration title")
    plan_reserve_parser.add_argument(
        "--base-ref",
        required=True,
        help="Explicit committed integration or dependency ref; never inferred from the caller cwd",
    )
    plan_reserve_parser.add_argument(
        "--governance-ref",
        required=True,
        help="Explicit canonical governance ref; global principle authority defaults to refs/heads/main",
    )
    plan_reserve_parser.add_argument("--json", action="store_true", help="Emit canonical machine-readable JSON")
    plan_reserve_parser.set_defaults(func=command_plan_reserve_iteration)

    reserve_parser = subparsers.add_parser(
        "reserve-iteration",
        help="Atomically reserve the next monotonic v2 iteration refs using a planned operation ID",
    )
    reserve_parser.add_argument("--project-root", required=True, help="Target Git repository root")
    reserve_parser.add_argument("--title", required=True, help="One-line iteration title from the plan")
    reserve_parser.add_argument("--operation-id", required=True, help="Canonical OP-... identifier emitted by plan")
    reserve_parser.add_argument("--base-ref", required=True, help="Exact source base ref emitted by plan")
    reserve_parser.add_argument(
        "--governance-ref",
        required=True,
        help="Exact canonical governance ref emitted by plan",
    )
    reserve_parser.add_argument(
        "--accept-plan-digest",
        required=True,
        help="Exact 64-character digest emitted by plan; apply refuses drift",
    )
    reserve_parser.add_argument("--json", action="store_true", help="Emit canonical machine-readable JSON")
    reserve_parser.set_defaults(func=command_reserve_iteration)
    return parser


def public_error_payload(args: argparse.Namespace, error: HarnessError) -> dict[str, object] | None:
    if not getattr(args, "json", False):
        return None
    raw_command = getattr(args, "command", "")
    if raw_command == "status":
        command = "status"
        action_level = "silent"
    elif raw_command == "plan" and getattr(args, "plan_command", "") == "reserve-iteration":
        command = "reserve-iteration"
        action_level = "silent"
    elif raw_command == "reserve-iteration":
        command = "reserve-iteration"
        action_level = "notify"
    else:
        return None
    message = str(error)
    message = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[redacted]@", message)
    message = re.sub(
        r"(?i)(token|password|passwd|secret|authorization)=([^&\s]+)",
        r"\1=[redacted]",
        message,
    )
    value: dict[str, object] = {
        "schema_version": PUBLIC_OPERATION_SCHEMA_V1,
        "command": command,
        "action_level": action_level,
        "pushed": False,
        "project_root": str(getattr(args, "project_root", "")),
        "phase": "blocked",
        "warnings": [],
        "blocking_reasons": [
            {
                "code": "harness-error",
                "message": message[:1000],
            }
        ],
        "next_gate": "fix-input-or-reconcile",
    }
    operation_id = getattr(args, "operation_id", None)
    if isinstance(operation_id, str) and OPERATION_ID_PATTERN.fullmatch(operation_id):
        value["operation_id"] = operation_id
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except HarnessError as exc:
        payload = public_error_payload(args, exc)
        if payload is not None:
            print_operation_json(payload)
            return 2
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
