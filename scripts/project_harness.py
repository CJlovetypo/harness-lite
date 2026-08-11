#!/usr/bin/env python3
"""Initialize, extend, and validate a lightweight project governance Harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence
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
    return f"{int(value):03d}"


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
) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryDirectory(prefix="project-harness-empty-hooks-") as hooks:
        return run_git(
            git,
            root,
            ["-c", f"core.hooksPath={hooks}", "update-ref", *arguments],
            input_bytes=input_bytes,
        )


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


def command_new_iteration(args: argparse.Namespace) -> int:
    root = resolve_project_root(args.project_root)
    git = require_git()
    if resolve_existing_git_root(root, git) is None:
        raise HarnessError("No Git repository found; run Harness initialization before creating an iteration")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except HarnessError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
