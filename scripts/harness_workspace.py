#!/usr/bin/env python3
"""Safe Local/linked-worktree orchestration for Harness Lite.

This module is deliberately separate from ``project_harness.py`` so the
workspace lifecycle can be reviewed and tested without coupling it to the
legacy serial finalizer.  It never commits, pushes, merges, rebases, stashes,
resets, cleans, removes worktrees, or deletes branches.
"""

from __future__ import annotations

import argparse
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
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from .harness_candidate import (
        AcceptanceEvidence,
        CandidateEvidence,
        candidate_evidence_gate,
    )
except ImportError:  # pragma: no cover - direct script execution
    from harness_candidate import AcceptanceEvidence, CandidateEvidence, candidate_evidence_gate


PUBLIC_SCHEMA = "harness-lite.workspace-operation/v1"
PLAN_SCHEMA = "harness-lite.workspace-plan/v1"
JOURNAL_SCHEMA = "harness-lite.workspace-journal/v1"
LEASE_SCHEMA = "harness-lite.writer-lease/v2"
LEGACY_LEASE_SCHEMA = "harness-lite.writer-lease/v1"
TOPOLOGY_SCHEMA = "harness-lite.workspace-topology/v1"
ALLOCATION_SCHEMA = "harness-lite.allocation-metadata.v1"
DEPENDENCY_BINDING_SCHEMA = "harness-lite.dependency-candidate-binding/v1"
DEPENDENCY_REFRESH_PLAN_SCHEMA = "harness-lite.dependency-refresh-plan/v1"
DEPENDENCY_REFRESH_JOURNAL_SCHEMA = "harness-lite.dependency-refresh-journal/v1"
DEPENDENCY_REFRESH_RECEIPT_SCHEMA = "harness-lite.dependency-refresh-receipt/v1"
CANDIDATE_EVIDENCE_METADATA_SCHEMA = "harness-lite.candidate-evidence-metadata/v1"

OPERATION_ID_RE = re.compile(r"OP-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
OID_RE = re.compile(r"[0-9a-f]{40,64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")

MAX_JSON_BYTES = 1024 * 1024
MAX_LABEL = 200
MAX_PATH = 4096
MAX_BIND_SNAPSHOT_FILES = 200_000
MAX_BIND_SNAPSHOT_BYTES = 8 * 1024 * 1024 * 1024
REGISTRY_PARTS = ("project-harness", "workspace", "v1")
DEPENDENCY_BINDING_FIELDS = {
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
    "registry_digest",
}
EXCLUSIONS = (
    "no commit",
    "no push",
    "no merge",
    "no rebase",
    "no cherry-pick",
    "no stash",
    "no reset",
    "no clean",
    "no force",
    "no worktree removal",
    "no branch deletion",
    "no governance document write",
)


class WorkspaceError(RuntimeError):
    """Raised when workspace orchestration cannot prove a mutation safe."""


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class RepositoryContext:
    git: str
    project_root: Path
    common_dir: Path


@dataclass(frozen=True)
class WorkspacePlan:
    action: str
    manifest: dict[str, object]
    digest: str
    blockers: tuple[Blocker, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def operation_id(self) -> str:
        return str(self.manifest["operation_id"])

    @property
    def iteration(self) -> str:
        return str(self.manifest["iteration"])

    def as_dict(self) -> dict[str, object]:
        topology = str(self.manifest.get("execution_topology", "local"))
        before = self.action in {"activate-local", "create-worktree"}
        action_level = (
            "notify"
            if topology == "worktree" or self.action in {"release-writer", "bind-local-branch"}
            else "silent"
        )
        if before:
            notification = notification_before(self.manifest)
        elif self.action == "bind-local-branch":
            notification = bind_notification_before(self.manifest)
        else:
            notification = release_notification_before(self.manifest)
        return {
            "schema_version": PUBLIC_SCHEMA,
            "command": self.action,
            "action_level": action_level,
            "notification_phase": "before",
            "pushed": False,
            "project_root": self.manifest["project_root"],
            "git_common_dir": self.manifest["git_common_dir"],
            "operation_id": self.operation_id,
            "iteration": self.iteration,
            "phase": "blocked" if self.blockers else "planned",
            "plan_digest": self.digest,
            "notification": notification,
            "warnings": list(self.warnings),
            "blocking_reasons": [item.as_dict() for item in self.blockers],
            "next_gate": "blocked" if self.blockers else f"apply-{self.action}",
            "exclusions": list(EXCLUSIONS),
        }


@dataclass(frozen=True)
class DependencyRefreshPlan:
    manifest: dict[str, object]
    digest: str
    blockers: tuple[Blocker, ...] = ()

    @property
    def operation_id(self) -> str:
        return str(self.manifest["operation_id"])

    @property
    def iteration(self) -> str:
        return str(self.manifest["iteration"])

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PUBLIC_SCHEMA,
            "command": "refresh-dependencies",
            "action_level": "notify",
            "notification_phase": "before",
            "pushed": False,
            "project_root": self.manifest["project_root"],
            "git_common_dir": self.manifest["git_common_dir"],
            "operation_id": self.operation_id,
            "iteration": self.iteration,
            "phase": "blocked" if self.blockers else "planned",
            "plan_digest": self.digest,
            "notification": dependency_refresh_notification(self.manifest, phase="before"),
            "warnings": [],
            "blocking_reasons": [item.as_dict() for item in self.blockers],
            "next_gate": "blocked" if self.blockers else "apply-refresh-dependencies",
            "exclusions": list(EXCLUSIONS),
        }


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_operation_id(value: str) -> str:
    candidate = value.strip()
    if not OPERATION_ID_RE.fullmatch(candidate):
        raise WorkspaceError("operation_id must be OP- followed by 32 lowercase hexadecimal characters")
    return candidate


def new_operation_id() -> str:
    return f"OP-{uuid.uuid4().hex}"


def validate_iteration(value: str) -> str:
    candidate = value.strip()
    if not ITERATION_RE.fullmatch(candidate) or int(candidate) < 1:
        raise WorkspaceError("iteration must be a canonical zero-padded decimal identifier such as 001")
    canonical = f"{int(candidate):03d}"
    if candidate != canonical:
        raise WorkspaceError(f"iteration must be canonical: {canonical}")
    return candidate


def validate_label(value: str, label: str, *, max_length: int = MAX_LABEL) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > max_length:
        raise WorkspaceError(f"{label} must contain 1..{max_length} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        raise WorkspaceError(f"{label} contains a control character")
    return candidate


def validate_digest(value: str) -> str:
    candidate = value.strip()
    if not DIGEST_RE.fullmatch(candidate):
        raise WorkspaceError("accepted plan digest must contain 64 lowercase hexadecimal characters")
    return candidate


def validate_generation(value: int) -> int:
    if value < 1 or value > 2_147_483_647:
        raise WorkspaceError("lease generation must be between 1 and 2147483647")
    return value


def path_key(path: Path) -> str:
    rendered = os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))
    return rendered.rstrip("\\/")


def same_path(left: Path | str, right: Path | str) -> bool:
    return path_key(Path(left)) == path_key(Path(right))


def is_within(candidate: Path, parent: Path) -> bool:
    candidate_key = path_key(candidate)
    parent_key = path_key(parent)
    separator = os.sep
    return candidate_key == parent_key or candidate_key.startswith(parent_key + separator)


def is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or bool(is_junction())


def assert_existing_chain_has_no_links(path: Path, *, stop: Path | None = None) -> None:
    current = path
    stop_key = path_key(stop) if stop is not None else None
    while True:
        if current.exists() and is_link_or_junction(current):
            raise WorkspaceError(f"refusing a path through a symbolic link or junction: {current}")
        if stop_key is not None and path_key(current) == stop_key:
            return
        if current.parent == current:
            if stop_key is not None:
                raise WorkspaceError(f"path is not contained by the expected root: {path}")
            return
        current = current.parent


def git_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def run_git(
    context: RepositoryContext | None,
    cwd: Path,
    arguments: Sequence[str],
    *,
    git: str | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
    disable_hooks: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    executable = git or (context.git if context is not None else None)
    if not executable:
        raise WorkspaceError("Git executable was not resolved")
    command = [executable, "-C", str(cwd)]
    temporary_hooks: tempfile.TemporaryDirectory[str] | None = None
    if disable_hooks:
        temporary_hooks = tempfile.TemporaryDirectory(prefix="harness-workspace-empty-hooks-")
        command.extend(["-c", f"core.hooksPath={temporary_hooks.name}"])
    command.extend(arguments)
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment(),
            check=False,
        )
    finally:
        if temporary_hooks is not None:
            temporary_hooks.cleanup()
    if check and result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(error or f"Git command failed with exit {result.returncode}")
    return result


def decode_stdout(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="strict").strip()


def resolve_repository(project_root: str | Path) -> RepositoryContext:
    git = shutil.which("git")
    if not git:
        raise WorkspaceError("Git is required for workspace orchestration")
    supplied = Path(project_root).expanduser()
    if not supplied.is_absolute():
        supplied = (Path.cwd() / supplied).resolve()
    else:
        supplied = supplied.resolve()
    if not supplied.is_dir():
        raise WorkspaceError(f"project root is not an existing directory: {supplied}")
    top = run_git(None, supplied, ["rev-parse", "--show-toplevel"], git=git, check=False)
    if top.returncode != 0:
        raise WorkspaceError(f"no Git worktree found at project root: {supplied}")
    actual = Path(decode_stdout(top)).resolve()
    if not same_path(actual, supplied):
        raise WorkspaceError(
            f"project root must name the worktree root exactly; expected {actual}, received {supplied}"
        )
    bare = run_git(None, actual, ["rev-parse", "--is-bare-repository"], git=git)
    if decode_stdout(bare) != "false":
        raise WorkspaceError("bare repositories cannot host a Local workspace")
    common_result = run_git(None, actual, ["rev-parse", "--git-common-dir"], git=git)
    raw_common = Path(decode_stdout(common_result))
    if not raw_common.is_absolute():
        raw_common = actual / raw_common
    common_dir = raw_common.resolve()
    if not common_dir.is_dir():
        raise WorkspaceError(f"Git common directory is missing: {common_dir}")
    assert_existing_chain_has_no_links(common_dir)
    context = RepositoryContext(git=git, project_root=actual, common_dir=common_dir)
    worktrees = list_worktrees(context, include_status=False)
    if not worktrees or not same_path(str(worktrees[0]["path"]), actual):
        primary = worktrees[0]["path"] if worktrees else "(unknown)"
        raise WorkspaceError(
            f"workspace mutations must be coordinated from the primary checkout {primary}, not {actual}"
        )
    return context


def parse_worktree_porcelain(raw: bytes) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for raw_record in raw.split(b"\0\0"):
        if not raw_record:
            continue
        record: dict[str, object] = {}
        for raw_field in raw_record.split(b"\0"):
            if not raw_field:
                continue
            field = raw_field.decode("utf-8", errors="strict")
            key, separator, value = field.partition(" ")
            record[key] = value if separator else True
        if isinstance(record.get("worktree"), str):
            records.append(record)
    return records


def status_fingerprint(context: RepositoryContext, worktree: Path) -> dict[str, object]:
    result = run_git(
        context,
        worktree,
        [
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=none",
        ],
        check=False,
    )
    if result.returncode != 0:
        return {
            "readable": False,
            "sha256": None,
            "tracked": None,
            "untracked": None,
            "ignored": None,
        }
    tracked = 0
    untracked = 0
    ignored = 0
    for item in result.stdout.split(b"\0"):
        if item.startswith(b"? "):
            untracked += 1
        elif item.startswith(b"! "):
            ignored += 1
        elif item.startswith((b"1 ", b"2 ", b"u ")):
            tracked += 1
    return {
        "readable": True,
        "sha256": hashlib.sha256(result.stdout).hexdigest(),
        "tracked": tracked,
        "untracked": untracked,
        "ignored": ignored,
    }


def stable_file_digest(path: Path) -> tuple[int, str]:
    try:
        before = path.stat()
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        after = path.stat()
    except OSError as exc:
        raise WorkspaceError(f"could not snapshot local workspace file {path}: {exc}") from exc
    before_identity = (before.st_size, before.st_mtime_ns, getattr(before, "st_ino", None))
    after_identity = (after.st_size, after.st_mtime_ns, getattr(after, "st_ino", None))
    if before_identity != after_identity:
        raise WorkspaceError(f"local workspace file changed while it was being snapshotted: {path}")
    return before.st_size, hasher.hexdigest()


def local_file_snapshot(context: RepositoryContext, worktree: Path) -> dict[str, object]:
    visible = run_git(
        context,
        worktree,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    ).stdout
    ignored = run_git(
        context,
        worktree,
        ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"],
    ).stdout
    raw_paths = sorted({item for item in (*visible.split(b"\0"), *ignored.split(b"\0")) if item})
    if len(raw_paths) > MAX_BIND_SNAPSHOT_FILES:
        raise WorkspaceError(
            f"Local workspace contains more than {MAX_BIND_SNAPSHOT_FILES} files; exact branch-bind snapshot is blocked"
        )
    aggregate = hashlib.sha256()
    total_bytes = 0
    for raw_path in raw_paths:
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        candidate = worktree / Path(relative)
        resolved = candidate.resolve(strict=False)
        if not is_within(resolved, worktree):
            raise WorkspaceError(f"Local workspace path escapes through a link or junction: {relative!r}")
        aggregate.update(len(raw_path).to_bytes(8, "big"))
        aggregate.update(raw_path)
        if candidate.is_symlink():
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                raise WorkspaceError(f"could not snapshot local symlink {relative!r}: {exc}") from exc
            encoded = os.fsencode(target)
            aggregate.update(b"L")
            aggregate.update(len(encoded).to_bytes(8, "big"))
            aggregate.update(encoded)
        elif candidate.is_file():
            size, file_digest = stable_file_digest(candidate)
            total_bytes += size
            if total_bytes > MAX_BIND_SNAPSHOT_BYTES:
                raise WorkspaceError(
                    "Local workspace exceeds the exact branch-bind byte snapshot limit; no Git state was changed"
                )
            aggregate.update(b"F")
            aggregate.update(size.to_bytes(8, "big"))
            aggregate.update(bytes.fromhex(file_digest))
        elif candidate.is_dir():
            # A tracked gitlink/submodule is represented semantically by the
            # index fingerprint; the containing directory identity is enough
            # to prove this operation did not move it.
            aggregate.update(b"D")
        else:
            aggregate.update(b"M")
    return {
        "file_count": len(raw_paths),
        "total_bytes": total_bytes,
        "sha256": aggregate.hexdigest(),
    }


def local_index_snapshot(context: RepositoryContext, worktree: Path) -> dict[str, object]:
    entries = run_git(context, worktree, ["ls-files", "--stage", "-z"]).stdout
    raw_index_path = Path(decode_stdout(run_git(context, worktree, ["rev-parse", "--git-path", "index"])))
    index_path = raw_index_path if raw_index_path.is_absolute() else worktree / raw_index_path
    index_path = index_path.resolve(strict=False)
    if not is_within(index_path, context.common_dir):
        raise WorkspaceError(f"Local workspace index escapes the Git common directory: {index_path}")
    if index_path.exists():
        size, index_digest = stable_file_digest(index_path)
    else:
        size, index_digest = 0, hashlib.sha256(b"").hexdigest()
    return {
        "path": str(index_path),
        "size": size,
        "sha256": index_digest,
        "entries_sha256": hashlib.sha256(entries).hexdigest(),
    }


def local_binding_snapshot(context: RepositoryContext) -> dict[str, object]:
    worktrees = list_worktrees(context, include_status=True)
    if not worktrees or not worktrees[0].get("primary"):
        raise WorkspaceError("primary checkout identity is unavailable")
    primary = worktrees[0]
    status = primary.get("status")
    if not isinstance(status, Mapping) or status.get("readable") is not True:
        raise WorkspaceError("primary checkout status is unreadable; Local branch binding is blocked")
    return {
        "path": str(context.project_root),
        "head_oid": primary.get("head_oid"),
        "branch_ref": primary.get("branch_ref"),
        "status": dict(status),
        "index": local_index_snapshot(context, context.project_root),
        "files": local_file_snapshot(context, context.project_root),
    }


def binding_preservation(
    expected: Mapping[str, object],
    actual: Mapping[str, object],
) -> dict[str, bool]:
    return {
        "workspace_path_unchanged": expected.get("path") == actual.get("path"),
        "head_commit_unchanged": expected.get("head_oid") == actual.get("head_oid"),
        "status_fingerprint_unchanged": expected.get("status") == actual.get("status"),
        "index_bytes_unchanged": expected.get("index") == actual.get("index"),
        "worktree_bytes_unchanged": expected.get("files") == actual.get("files"),
    }


def list_worktrees(context: RepositoryContext, *, include_status: bool = True) -> list[dict[str, object]]:
    result = run_git(context, context.project_root, ["worktree", "list", "--porcelain", "-z"])
    parsed = parse_worktree_porcelain(result.stdout)
    rendered: list[dict[str, object]] = []
    for index, record in enumerate(parsed):
        raw_path = record.get("worktree")
        if not isinstance(raw_path, str):
            continue
        path = Path(raw_path).resolve()
        entry: dict[str, object] = {
            "path": str(path),
            "head_oid": record.get("HEAD"),
            "branch_ref": record.get("branch") if isinstance(record.get("branch"), str) else None,
            "detached": bool(record.get("detached", False)),
            "locked": bool(record.get("locked", False)),
            "prunable": bool(record.get("prunable", False)),
            "primary": index == 0,
        }
        if include_status and path.is_dir():
            entry["status"] = status_fingerprint(context, path)
        rendered.append(entry)
    return rendered


def compact_worktree_snapshot(worktrees: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for entry in worktrees:
        status = entry.get("status")
        projected_status = dict(status) if isinstance(status, Mapping) else None
        result.append(
            {
                "path": entry.get("path"),
                "head_oid": entry.get("head_oid"),
                "branch_ref": entry.get("branch_ref"),
                "detached": bool(entry.get("detached", False)),
                "locked": bool(entry.get("locked", False)),
                "prunable": bool(entry.get("prunable", False)),
                "primary": bool(entry.get("primary", False)),
                "status": projected_status,
            }
        )
    return sorted(result, key=lambda item: path_key(Path(str(item["path"]))))


def ref_oid(context: RepositoryContext, reference: str) -> str | None:
    result = run_git(
        context,
        context.project_root,
        ["show-ref", "--verify", "--hash", reference],
        check=False,
    )
    if result.returncode != 0:
        return None
    value = decode_stdout(result)
    if not OID_RE.fullmatch(value):
        raise WorkspaceError(f"Git returned an invalid object ID for {reference}")
    return value


def ensure_commit(context: RepositoryContext, object_name: str) -> str:
    result = run_git(
        context,
        context.project_root,
        ["rev-parse", "--verify", f"{object_name}^{{commit}}"],
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError(f"object is not an available commit: {object_name}")
    commit = decode_stdout(result)
    if not OID_RE.fullmatch(commit):
        raise WorkspaceError(f"Git returned an invalid commit ID for {object_name}")
    return commit


def commit_tree(context: RepositoryContext, commit: str) -> str:
    value = decode_stdout(
        run_git(context, context.project_root, ["rev-parse", "--verify", f"{commit}^{{tree}}"])
    )
    if not OID_RE.fullmatch(value):
        raise WorkspaceError(f"Git returned an invalid tree ID for commit {commit}")
    return value


def _dependency_generation(value: object) -> str:
    candidate = str(value).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", candidate):
        raise WorkspaceError("dependency candidate generation is invalid")
    return candidate


def normalize_dependency_binding(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != DEPENDENCY_BINDING_FIELDS:
        raise WorkspaceError("dependency candidate binding fields do not match the v1 schema")
    normalized = {field: str(value[field]) for field in DEPENDENCY_BINDING_FIELDS}
    if normalized["schema_version"] != DEPENDENCY_BINDING_SCHEMA:
        raise WorkspaceError("dependency candidate binding schema is unsupported")
    number = validate_iteration(normalized["iteration"])
    generation = _dependency_generation(normalized["generation"])
    expected_candidate = f"refs/project-harness/v2/iterations/{number}/candidates/{generation}"
    expected_evidence = f"refs/project-harness/v2/iterations/{number}/candidate-evidence/{generation}"
    if normalized["candidate_ref"] != expected_candidate:
        raise WorkspaceError("dependency candidate ref does not match its iteration/generation")
    if normalized["candidate_evidence_ref"] != expected_evidence:
        raise WorkspaceError("dependency candidate evidence ref does not match its iteration/generation")
    for field in ("candidate_commit", "candidate_tree", "candidate_evidence_blob"):
        if not OID_RE.fullmatch(normalized[field]):
            raise WorkspaceError(f"dependency binding {field} is not a full Git object ID")
    for field in (
        "candidate_evidence_digest",
        "candidate_evidence_metadata_digest",
        "registration_digest",
        "registry_digest",
    ):
        if not DIGEST_RE.fullmatch(normalized[field]):
            raise WorkspaceError(f"dependency binding {field} is not a SHA-256 digest")
    return normalized


def normalize_dependency_bindings(values: Sequence[Mapping[str, object]] | None) -> tuple[dict[str, str], ...]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values or ():
        binding = normalize_dependency_binding(raw)
        number = binding["iteration"]
        if number in seen:
            raise WorkspaceError(f"dependency candidate binding is duplicated: PRD-{number}")
        seen.add(number)
        normalized.append(binding)
    return tuple(normalized)


def dependency_bindings_digest(values: Sequence[Mapping[str, object]]) -> str:
    return digest([dict(item) for item in values])


def dependency_registry_snapshot(context: RepositoryContext, iteration: str) -> dict[str, object]:
    number = validate_iteration(iteration)
    prefixes = (
        f"refs/project-harness/v2/iterations/{number}/candidates/",
        f"refs/project-harness/v2/iterations/{number}/candidate-evidence/",
    )
    result = run_git(
        context,
        context.project_root,
        ["for-each-ref", "--format=%(refname)%00%(objectname)", *prefixes],
    )
    entries: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        try:
            raw_ref, raw_oid = line.split(b"\0", 1)
            reference = raw_ref.decode("utf-8", errors="strict")
            oid = raw_oid.decode("ascii", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise WorkspaceError("Git returned a malformed dependency candidate ref") from exc
        if not reference.startswith(prefixes) or not OID_RE.fullmatch(oid):
            raise WorkspaceError("Git returned an invalid dependency candidate ref identity")
        entries.append({"ref": reference, "oid": oid})
    entries.sort(key=lambda item: item["ref"])
    return {"iteration": number, "refs": entries, "digest": digest(entries)}


def _candidate_evidence_from_mapping(value: object) -> CandidateEvidence:
    if not isinstance(value, Mapping):
        raise WorkspaceError("dependency candidate core evidence is not an object")
    expected = {
        "schema_version",
        "iteration",
        "generation",
        "base_commit",
        "candidate_commit",
        "candidate_tree",
        "principle_sha256",
        "included_paths",
        "acceptance_ids",
        "acceptance_evidence",
        "verification_ids",
        "evidence_digest",
        "verified",
        "blockers",
    }
    if set(value) != expected:
        raise WorkspaceError("dependency candidate core evidence fields are invalid")
    acceptance_raw = value.get("acceptance_evidence")
    if not isinstance(acceptance_raw, list):
        raise WorkspaceError("dependency candidate acceptance evidence is invalid")
    acceptance: list[AcceptanceEvidence] = []
    for item in acceptance_raw:
        if not isinstance(item, Mapping) or set(item) != {
            "acceptance_id",
            "evidence_ids",
            "verification_ids",
        }:
            raise WorkspaceError("dependency candidate acceptance receipt is invalid")
        evidence_ids = item.get("evidence_ids")
        verification_ids = item.get("verification_ids")
        if (
            not isinstance(evidence_ids, list)
            or not all(isinstance(entry, str) for entry in evidence_ids)
            or not isinstance(verification_ids, list)
            or not all(isinstance(entry, str) for entry in verification_ids)
        ):
            raise WorkspaceError("dependency candidate acceptance receipt IDs are invalid")
        acceptance.append(
            AcceptanceEvidence(
                acceptance_id=str(item.get("acceptance_id", "")),
                evidence_ids=tuple(evidence_ids),
                verification_ids=tuple(verification_ids),
            )
        )
    sequences: dict[str, tuple[str, ...]] = {}
    for field in ("included_paths", "acceptance_ids", "verification_ids", "blockers"):
        raw = value.get(field)
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise WorkspaceError(f"dependency candidate core evidence {field} is invalid")
        sequences[field] = tuple(raw)
    candidate = CandidateEvidence(
        schema_version=str(value.get("schema_version", "")),
        iteration=str(value.get("iteration", "")),
        generation=str(value.get("generation", "")),
        base_commit=str(value.get("base_commit", "")),
        candidate_commit=str(value.get("candidate_commit", "")),
        candidate_tree=str(value.get("candidate_tree", "")),
        principle_sha256=str(value.get("principle_sha256", "")),
        included_paths=sequences["included_paths"],
        acceptance_ids=sequences["acceptance_ids"],
        acceptance_evidence=tuple(acceptance),
        verification_ids=sequences["verification_ids"],
        evidence_digest=str(value.get("evidence_digest", "")),
        verified=value.get("verified") is True,
        blockers=sequences["blockers"],
    )
    gate = candidate_evidence_gate(candidate)
    if not gate.allowed:
        raise WorkspaceError("dependency candidate core evidence is not verified: " + ", ".join(gate.blockers))
    return candidate


def dependency_binding_live_blockers(
    context: RepositoryContext,
    binding: Mapping[str, object],
    *,
    check_registry: bool = True,
) -> list[Blocker]:
    try:
        normalized = normalize_dependency_binding(binding)
    except WorkspaceError as exc:
        return [Blocker("dependency-binding-invalid", str(exc))]
    number = normalized["iteration"]
    blockers: list[Blocker] = []
    if ref_oid(context, normalized["candidate_ref"]) != normalized["candidate_commit"]:
        blockers.append(
            Blocker("dependency-candidate-ref-drift", f"PRD-{number} candidate ref identity changed")
        )
    try:
        if commit_tree(context, normalized["candidate_commit"]) != normalized["candidate_tree"]:
            blockers.append(
                Blocker("dependency-candidate-tree-drift", f"PRD-{number} candidate tree identity changed")
            )
    except WorkspaceError as exc:
        blockers.append(Blocker("dependency-candidate-object-invalid", str(exc)))
    if ref_oid(context, normalized["candidate_evidence_ref"]) != normalized["candidate_evidence_blob"]:
        blockers.append(
            Blocker("dependency-candidate-evidence-ref-drift", f"PRD-{number} evidence ref identity changed")
        )
    metadata: object | None = None
    object_type = run_git(
        context,
        context.project_root,
        ["cat-file", "-t", normalized["candidate_evidence_blob"]],
        check=False,
    )
    if object_type.returncode != 0 or decode_stdout(object_type) != "blob":
        blockers.append(
            Blocker("dependency-candidate-evidence-object-invalid", f"PRD-{number} evidence object is not a blob")
        )
    else:
        raw = run_git(
            context,
            context.project_root,
            ["cat-file", "blob", normalized["candidate_evidence_blob"]],
        ).stdout
        if not raw or len(raw) > MAX_JSON_BYTES:
            blockers.append(
                Blocker("dependency-candidate-evidence-size", f"PRD-{number} evidence blob size is invalid")
            )
        else:
            try:
                metadata = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                blockers.append(
                    Blocker("dependency-candidate-evidence-json", f"PRD-{number} evidence blob is invalid JSON")
                )
    candidate: CandidateEvidence | None = None
    if isinstance(metadata, dict):
        supplied_digest = metadata.get("metadata_digest")
        metadata_payload = dict(metadata)
        metadata_payload.pop("metadata_digest", None)
        expected_metadata_digest = digest(metadata_payload)
        identity_matches = (
            metadata.get("schema_version") == CANDIDATE_EVIDENCE_METADATA_SCHEMA
            and metadata.get("iteration") == number
            and metadata.get("generation") == normalized["generation"]
            and metadata.get("candidate_ref") == normalized["candidate_ref"]
            and metadata.get("candidate_evidence_ref") == normalized["candidate_evidence_ref"]
            and metadata.get("seal_commit") == normalized["candidate_commit"]
            and metadata.get("seal_tree") == normalized["candidate_tree"]
            and supplied_digest == normalized["candidate_evidence_metadata_digest"]
            and supplied_digest == expected_metadata_digest
        )
        if not identity_matches:
            blockers.append(
                Blocker("dependency-candidate-evidence-identity", f"PRD-{number} evidence metadata differs")
            )
        try:
            candidate = _candidate_evidence_from_mapping(metadata.get("candidate_evidence"))
        except WorkspaceError as exc:
            blockers.append(Blocker("dependency-candidate-evidence-core", str(exc)))
        else:
            if (
                candidate.iteration != number
                or candidate.generation != normalized["generation"]
                or candidate.candidate_commit != normalized["candidate_commit"]
                or candidate.candidate_tree != normalized["candidate_tree"]
                or candidate.evidence_digest != normalized["candidate_evidence_digest"]
            ):
                blockers.append(
                    Blocker("dependency-candidate-evidence-core-identity", f"PRD-{number} core evidence differs")
                )
    elif metadata is not None:
        blockers.append(
            Blocker("dependency-candidate-evidence-json", f"PRD-{number} evidence blob is not an object")
        )
    if candidate is not None:
        try:
            try:
                from . import harness_train as train
            except ImportError:  # pragma: no cover - direct script execution
                import harness_train as train
            registered, registration_blockers = train.load_registered_candidate(
                context.project_root,
                iteration=number,
                generation=normalized["generation"],
                current_principle_sha256=candidate.principle_sha256,
            )
        except (ImportError, AttributeError, RuntimeError, ValueError) as exc:
            blockers.append(
                Blocker("dependency-candidate-registration-unreadable", f"PRD-{number}: {exc}")
            )
        else:
            blockers.extend(
                Blocker(
                    "dependency-candidate-registration-invalid",
                    f"PRD-{number}: {item.code}: {item.message}",
                )
                for item in registration_blockers
            )
            if registered is None:
                blockers.append(
                    Blocker(
                        "dependency-candidate-registration-missing",
                        f"PRD-{number} has no exact registered stable candidate",
                    )
                )
            elif (
                registered.candidate_ref != normalized["candidate_ref"]
                or registered.candidate_commit != normalized["candidate_commit"]
                or registered.candidate_tree != normalized["candidate_tree"]
                or registered.candidate_evidence_ref != normalized["candidate_evidence_ref"]
                or registered.candidate_evidence_blob != normalized["candidate_evidence_blob"]
                or registered.candidate_evidence.evidence_digest != normalized["candidate_evidence_digest"]
                or registered.candidate_evidence_metadata_digest
                != normalized["candidate_evidence_metadata_digest"]
                or registered.registration_digest != normalized["registration_digest"]
            ):
                blockers.append(
                    Blocker(
                        "dependency-candidate-registration-identity",
                        f"PRD-{number} registered candidate differs from the bound identity",
                    )
                )
    if check_registry:
        try:
            registry = dependency_registry_snapshot(context, number)
        except WorkspaceError as exc:
            blockers.append(Blocker("dependency-candidate-registry-unreadable", str(exc)))
        else:
            if registry["digest"] != normalized["registry_digest"]:
                blockers.append(
                    Blocker(
                        "dependency-baseline-stale",
                        f"PRD-{number} candidate generation/ref/evidence registry changed; refresh is required",
                    )
                )
    return blockers


def dependency_order_blockers(
    context: RepositoryContext,
    bindings: Sequence[Mapping[str, object]],
) -> list[Blocker]:
    blockers: list[Blocker] = []
    for binding in bindings:
        blockers.extend(dependency_binding_live_blockers(context, binding))
    for previous, current in zip(bindings, bindings[1:]):
        if not is_ancestor(context, str(previous["candidate_commit"]), str(current["candidate_commit"])):
            blockers.append(
                Blocker(
                    "dependency-order-not-stacked",
                    f"PRD-{previous['iteration']} candidate is not an ancestor of declared next dependency PRD-{current['iteration']}",
                )
            )
    return blockers


def _normalize_refresh_commands(values: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping) or set(value) != {"evidence_id", "argv"}:
            raise WorkspaceError("dependency refresh verification command fields are invalid")
        evidence_id = validate_label(str(value.get("evidence_id", "")), "verification evidence ID")
        argv = value.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv or not all(
            isinstance(item, str) and item and "\x00" not in item for item in argv
        ):
            raise WorkspaceError(f"dependency refresh verification command is invalid: {evidence_id}")
        if evidence_id in seen:
            raise WorkspaceError(f"dependency refresh verification evidence ID is duplicated: {evidence_id}")
        seen.add(evidence_id)
        normalized.append({"evidence_id": evidence_id, "argv": list(argv)})
    if not normalized:
        raise WorkspaceError("dependency refresh requires at least one executable verification command")
    return tuple(normalized)


def dependency_refresh_root(context: RepositoryContext) -> Path:
    return registry_root(context) / "dependency-refresh"


def dependency_refresh_journal_path(context: RepositoryContext, operation_id: str) -> Path:
    path = dependency_refresh_root(context) / "journal" / f"refresh-{validate_operation_id(operation_id)}.json"
    assert_operational_path(context, path)
    return path


def dependency_refresh_receipt_path(context: RepositoryContext, operation_id: str) -> Path:
    path = dependency_refresh_root(context) / "receipts" / f"refresh-{validate_operation_id(operation_id)}.json"
    assert_operational_path(context, path)
    return path


def _worktree_head_and_tree(context: RepositoryContext, target: Path) -> tuple[str, str]:
    head = ensure_commit(context, decode_stdout(run_git(context, target, ["rev-parse", "HEAD"])))
    tree = commit_tree(context, head)
    return head, tree


def dependency_refresh_notification(manifest: Mapping[str, object], *, phase: str) -> dict[str, object]:
    return {
        "prd": f"PRD-{manifest['iteration']}",
        "reason_code": "dependency-baseline-stale-refresh",
        "reason": "replace only the explicitly reviewed dependency candidate binding after the workspace already contains and revalidates that candidate",
        "phase": phase,
        "worktree_path": manifest.get("worktree_path"),
        "branch_ref": manifest.get("branch_ref"),
        "head_commit": manifest.get("worktree_head"),
        "before_bindings": manifest.get("before_bindings"),
        "after_bindings": manifest.get("after_bindings"),
        "verification_commands": manifest.get("verification_commands"),
        "will_switch_branch": False,
        "will_move_worktree": False,
        "will_commit": False,
        "will_stash": False,
        "remote": {"involved": False, "pushed": False, "force": False},
    }


def build_dependency_refresh_plan(
    project_root: str | Path,
    *,
    iteration: str,
    owner: str,
    lease_generation: int,
    worktree_path: str | Path,
    branch_ref: str,
    base_commit: str,
    dependency_bindings: Sequence[Mapping[str, object]],
    verification_commands: Sequence[Mapping[str, object]],
    operation_id: str | None = None,
) -> DependencyRefreshPlan:
    context = resolve_repository(project_root)
    number = validate_iteration(iteration)
    operation = validate_operation_id(operation_id) if operation_id else new_operation_id()
    target = Path(worktree_path).expanduser()
    if not target.is_absolute():
        raise WorkspaceError("dependency refresh worktree path must be absolute")
    target = target.resolve(strict=False)
    branch = validate_branch_ref(context, branch_ref)
    generation = validate_generation(lease_generation)
    owner_value = validate_label(owner, "writer owner")
    if not OID_RE.fullmatch(base_commit):
        raise WorkspaceError("dependency refresh base commit is invalid")
    after_bindings = normalize_dependency_bindings(dependency_bindings)
    commands = _normalize_refresh_commands(verification_commands)
    blockers: list[Blocker] = dependency_order_blockers(context, after_bindings)
    lease = load_lease(context, number)
    if lease is None:
        raise WorkspaceError(f"PRD-{number} has no active writer lease")
    guard_blockers, actual = guard_lease(
        context,
        lease,
        owner=owner_value,
        generation=generation,
        worktree_path=target,
        branch_ref=branch,
        base_commit=base_commit,
    )
    blockers.extend(item for item in guard_blockers if not item.code.startswith("dependency-"))
    before_bindings = normalize_dependency_bindings(lease.get("dependency_bindings", []))
    if not before_bindings:
        blockers.append(Blocker("dependency-refresh-independent", "an independent workspace has no dependency baseline"))
    if tuple(item["iteration"] for item in before_bindings) != tuple(
        item["iteration"] for item in after_bindings
    ):
        blockers.append(
            Blocker(
                "dependency-refresh-scope-change",
                "refresh cannot add, remove, or reorder PRD dependencies; revise the approved PRD instead",
            )
        )
    if [dict(item) for item in before_bindings] == [dict(item) for item in after_bindings]:
        blockers.append(Blocker("dependency-refresh-no-change", "dependency baseline is already exact"))
    if not isinstance(actual, Mapping) or actual.get("present") is not True:
        blockers.append(Blocker("dependency-refresh-worktree-missing", "writer worktree is not registered"))
        head, tree = base_commit, commit_tree(context, base_commit)
    else:
        head = str(actual.get("head_oid", ""))
        if not OID_RE.fullmatch(head):
            blockers.append(Blocker("dependency-refresh-head-invalid", "writer HEAD is unavailable"))
            head = base_commit
        tree = commit_tree(context, head)
    for binding in after_bindings:
        if not is_ancestor(context, binding["candidate_commit"], head):
            blockers.append(
                Blocker(
                    "dependency-refresh-candidate-not-contained",
                    f"writer HEAD does not contain PRD-{binding['iteration']} candidate {binding['generation']}",
                )
            )
    status = status_fingerprint(context, target)
    if status.get("readable") is not True or any(
        int(status.get(field, 0) or 0) for field in ("tracked", "untracked")
    ):
        blockers.append(
            Blocker(
                "dependency-refresh-worktree-dirty",
                "dependency refresh requires a clean tracked/untracked worktree for exact revalidation",
            )
        )
    before_lease = lease_projection(lease)
    after_lease = dict(before_lease)
    after_lease["generation"] = generation + 1
    after_lease["implementation_ref"] = after_bindings[-1]["candidate_ref"] if after_bindings else "refs/heads/main"
    after_lease["implementation_commit"] = (
        after_bindings[-1]["candidate_commit"] if after_bindings else lease["implementation_commit"]
    )
    after_lease["dependency_bindings"] = [dict(item) for item in after_bindings]
    after_lease["dependency_bindings_digest"] = dependency_bindings_digest(after_bindings)
    after_lease["dependency_refresh_generation"] = int(lease.get("dependency_refresh_generation", 0)) + 1
    manifest: dict[str, object] = {
        "schema_version": DEPENDENCY_REFRESH_PLAN_SCHEMA,
        "operation_id": operation,
        "project_root": str(context.project_root),
        "git_common_dir": str(context.common_dir),
        "iteration": number,
        "owner": owner_value,
        "lease_generation": generation,
        "worktree_path": str(target),
        "branch_ref": branch,
        "base_commit": base_commit,
        "worktree_head": head,
        "worktree_tree": tree,
        "status_fingerprint": status,
        "before_bindings": [dict(item) for item in before_bindings],
        "after_bindings": [dict(item) for item in after_bindings],
        "before_lease": before_lease,
        "before_lease_digest": digest(lease),
        "after_lease": after_lease,
        "verification_commands": [dict(item) for item in commands],
    }
    return DependencyRefreshPlan(
        manifest=manifest,
        digest=digest(manifest),
        blockers=tuple(dict.fromkeys(blockers)),
    )


def read_json_blob(context: RepositoryContext, object_name: str) -> dict[str, object]:
    object_type = decode_stdout(run_git(context, context.project_root, ["cat-file", "-t", object_name]))
    if object_type != "blob":
        raise WorkspaceError(f"allocation ref must point to a blob, found {object_type}")
    size_text = decode_stdout(run_git(context, context.project_root, ["cat-file", "-s", object_name]))
    try:
        size = int(size_text)
    except ValueError as exc:
        raise WorkspaceError("Git returned an invalid allocation metadata size") from exc
    if size < 2 or size > MAX_JSON_BYTES:
        raise WorkspaceError("allocation metadata exceeds the safe size limit")
    raw = run_git(context, context.project_root, ["cat-file", "-p", object_name]).stdout
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("allocation metadata is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkspaceError("allocation metadata must be a JSON object")
    return value


def load_allocation(
    context: RepositoryContext,
    iteration: str,
    base_ref: str,
) -> tuple[str, str, dict[str, object]]:
    expected_base_ref = f"refs/project-harness/v2/iterations/{iteration}/base"
    if base_ref != expected_base_ref:
        raise WorkspaceError(
            f"workspace base must use the immutable v2 anchor {expected_base_ref}, not {base_ref}"
        )
    base_oid = ref_oid(context, expected_base_ref)
    if base_oid is None:
        raise WorkspaceError(f"missing immutable iteration base ref: {expected_base_ref}")
    base_commit = ensure_commit(context, base_oid)
    allocation_ref = f"refs/project-harness/v2/allocations/{iteration}"
    allocation_object = ref_oid(context, allocation_ref)
    if allocation_object is None:
        raise WorkspaceError(f"missing iteration allocation ref: {allocation_ref}")
    metadata = read_json_blob(context, allocation_object)
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
    if set(metadata) != expected_fields or metadata.get("schema_version") != ALLOCATION_SCHEMA:
        raise WorkspaceError("allocation metadata does not match harness-lite.allocation-metadata.v1")
    if metadata.get("iteration") != iteration or metadata.get("base_commit") != base_commit:
        raise WorkspaceError("allocation metadata and immutable base ref disagree")
    if metadata.get("governance_ref") != "refs/heads/main":
        raise WorkspaceError("allocation metadata does not bind canonical main governance")
    if not isinstance(metadata.get("principle_sha256"), str) or not DIGEST_RE.fullmatch(
        str(metadata["principle_sha256"])
    ):
        raise WorkspaceError("allocation metadata has an invalid principle hash")
    for field in ("operation_id", "plan_digest"):
        value = metadata.get(field)
        pattern = OPERATION_ID_RE if field == "operation_id" else DIGEST_RE
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise WorkspaceError(f"allocation metadata has an invalid {field}")
    return allocation_ref, allocation_object, metadata


def validate_branch_ref(context: RepositoryContext, reference: str) -> str:
    candidate = validate_label(reference, "branch ref", max_length=255)
    if not candidate.startswith("refs/heads/"):
        raise WorkspaceError("branch ref must be an explicit refs/heads/... reference")
    result = run_git(context, context.project_root, ["check-ref-format", candidate], check=False)
    if result.returncode != 0:
        raise WorkspaceError(f"invalid branch ref: {candidate}")
    return candidate


def registry_root(context: RepositoryContext) -> Path:
    return context.common_dir.joinpath(*REGISTRY_PARTS)


def assert_operational_path(context: RepositoryContext, path: Path) -> None:
    root = registry_root(context)
    if not is_within(path, root):
        raise WorkspaceError(f"operational path escapes the workspace registry: {path}")
    assert_existing_chain_has_no_links(path, stop=context.common_dir)


def operation_path(context: RepositoryContext, operation_id: str) -> Path:
    return registry_root(context) / "operations" / f"{validate_operation_id(operation_id)}.json"


def lease_path(context: RepositoryContext, iteration: str) -> Path:
    return registry_root(context) / "leases" / "iterations" / f"{validate_iteration(iteration)}.json"


def topology_path(context: RepositoryContext) -> Path:
    return registry_root(context) / "topology.json"


def lock_path(context: RepositoryContext) -> Path:
    return registry_root(context) / "coordinator.lock"


def archive_lease_path(context: RepositoryContext, lease: Mapping[str, object]) -> Path:
    return (
        registry_root(context)
        / "archives"
        / "leases"
        / f"{lease['iteration']}-g{lease['generation']}-{lease['operation_id']}.json"
    )


def read_json_file(context: RepositoryContext, path: Path, *, label: str) -> dict[str, object]:
    assert_operational_path(context, path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise WorkspaceError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.st_size < 2 or stat.st_size > MAX_JSON_BYTES:
        raise WorkspaceError(f"{label} exceeds the safe size limit: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"{label} is corrupt and was preserved: {path}") from exc
    if not isinstance(value, dict):
        raise WorkspaceError(f"{label} must be a JSON object: {path}")
    return value


def atomic_write_json(context: RepositoryContext, path: Path, value: Mapping[str, object]) -> None:
    assert_operational_path(context, path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    assert_operational_path(context, parent)
    raw = canonical_json(dict(value)) + b"\n"
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def exclusive_write_json(context: RepositoryContext, path: Path, value: Mapping[str, object]) -> None:
    assert_operational_path(context, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_operational_path(context, path.parent)
    raw = canonical_json(dict(value)) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def coordinator_lock(context: RepositoryContext, *, timeout_seconds: float = 30.0):
    path = lock_path(context)
    assert_operational_path(context, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_operational_path(context, path.parent)
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
                    raise WorkspaceError("timed out waiting for the workspace coordinator lock") from exc
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


LEGACY_LEASE_FIELDS = {
    "schema_version",
    "scope",
    "state",
    "iteration",
    "operation_id",
    "owner",
    "generation",
    "execution_topology",
    "expected_root",
    "worktree_path",
    "branch_ref",
    "base_ref",
    "base_commit",
    "principle_sha256",
    "runtime_namespace",
    "acquired_at",
    "heartbeat",
}

LEASE_FIELDS = LEGACY_LEASE_FIELDS | {
    "implementation_ref",
    "implementation_commit",
    "dependency_bindings",
    "dependency_bindings_digest",
    "dependency_refresh_generation",
}


def validate_lease(value: object, *, source: Path) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) not in {frozenset(LEASE_FIELDS), frozenset(LEGACY_LEASE_FIELDS)}:
        raise WorkspaceError(f"writer lease fields do not match the v1 schema: {source}")
    schema = value.get("schema_version")
    if schema not in {LEASE_SCHEMA, LEGACY_LEASE_SCHEMA} or value.get("scope") != "iteration-writer":
        raise WorkspaceError(f"unsupported writer lease schema: {source}")
    if schema == LEASE_SCHEMA and set(value) != LEASE_FIELDS:
        raise WorkspaceError(f"writer lease v2 fields are incomplete: {source}")
    if schema == LEGACY_LEASE_SCHEMA and set(value) != LEGACY_LEASE_FIELDS:
        raise WorkspaceError(f"writer lease v1 fields are invalid: {source}")
    if value.get("state") != "active":
        raise WorkspaceError(f"active lease registry contains a non-active lease: {source}")
    validate_iteration(str(value.get("iteration", "")))
    validate_operation_id(str(value.get("operation_id", "")))
    validate_label(str(value.get("owner", "")), "lease owner")
    generation = value.get("generation")
    if not isinstance(generation, int):
        raise WorkspaceError(f"writer lease generation is invalid: {source}")
    validate_generation(generation)
    if value.get("execution_topology") not in {"local", "worktree"}:
        raise WorkspaceError(f"writer lease topology is invalid: {source}")
    for field in ("expected_root", "worktree_path"):
        item = value.get(field)
        if not isinstance(item, str) or not Path(item).is_absolute() or len(item) > MAX_PATH:
            raise WorkspaceError(f"writer lease {field} is invalid: {source}")
    branch = value.get("branch_ref")
    if not isinstance(branch, str) or not branch.startswith("refs/heads/"):
        raise WorkspaceError(f"writer lease branch_ref is invalid: {source}")
    expected_base = f"refs/project-harness/v2/iterations/{value['iteration']}/base"
    if value.get("base_ref") != expected_base:
        raise WorkspaceError(f"writer lease base_ref is invalid: {source}")
    if not isinstance(value.get("base_commit"), str) or not OID_RE.fullmatch(str(value["base_commit"])):
        raise WorkspaceError(f"writer lease base_commit is invalid: {source}")
    if not isinstance(value.get("principle_sha256"), str) or not DIGEST_RE.fullmatch(
        str(value["principle_sha256"])
    ):
        raise WorkspaceError(f"writer lease principle hash is invalid: {source}")
    validate_label(str(value.get("runtime_namespace", "")), "runtime namespace")
    for field in ("acquired_at", "heartbeat"):
        validate_label(str(value.get(field, "")), field, max_length=80)
    normalized = dict(value)
    if schema == LEGACY_LEASE_SCHEMA:
        normalized.update(
            {
                "schema_version": LEASE_SCHEMA,
                "implementation_ref": "refs/heads/main",
                "implementation_commit": value["base_commit"],
                "dependency_bindings": [],
                "dependency_bindings_digest": dependency_bindings_digest(()),
                "dependency_refresh_generation": 0,
            }
        )
        return normalized
    implementation_ref = value.get("implementation_ref")
    implementation_commit = value.get("implementation_commit")
    if not isinstance(implementation_ref, str) or not (
        implementation_ref == "refs/heads/main"
        or implementation_ref.startswith("refs/project-harness/v2/iterations/")
    ):
        raise WorkspaceError(f"writer lease implementation_ref is invalid: {source}")
    if not isinstance(implementation_commit, str) or not OID_RE.fullmatch(implementation_commit):
        raise WorkspaceError(f"writer lease implementation_commit is invalid: {source}")
    raw_dependencies = value.get("dependency_bindings")
    if not isinstance(raw_dependencies, list):
        raise WorkspaceError(f"writer lease dependency_bindings are invalid: {source}")
    dependencies = normalize_dependency_bindings(raw_dependencies)
    if value.get("dependency_bindings_digest") != dependency_bindings_digest(dependencies):
        raise WorkspaceError(f"writer lease dependency binding digest differs: {source}")
    refresh_generation = value.get("dependency_refresh_generation")
    if not isinstance(refresh_generation, int) or refresh_generation < 0 or refresh_generation > 2_147_483_647:
        raise WorkspaceError(f"writer lease dependency refresh generation is invalid: {source}")
    if dependencies:
        last = dependencies[-1]
        if implementation_ref != last["candidate_ref"] or implementation_commit != last["candidate_commit"]:
            # The original start remains immutable after an explicit refresh.
            # A refreshed binding can therefore differ only once generation > 0.
            if refresh_generation == 0:
                raise WorkspaceError(f"writer lease implementation start differs from dependency baseline: {source}")
    elif implementation_ref != "refs/heads/main":
        raise WorkspaceError(f"independent writer lease implementation ref must be main: {source}")
    normalized["dependency_bindings"] = [dict(item) for item in dependencies]
    return normalized


def load_lease(context: RepositoryContext, iteration: str) -> dict[str, object] | None:
    path = lease_path(context, iteration)
    if not path.exists():
        return None
    return validate_lease(read_json_file(context, path, label="writer lease"), source=path)


def load_active_leases(context: RepositoryContext) -> tuple[list[dict[str, object]], list[Blocker]]:
    directory = registry_root(context) / "leases" / "iterations"
    if not directory.exists():
        return [], []
    assert_operational_path(context, directory)
    leases: list[dict[str, object]] = []
    blockers: list[Blocker] = []
    for path in sorted(directory.glob("*.json")):
        try:
            lease = validate_lease(read_json_file(context, path, label="writer lease"), source=path)
            if path.name != f"{lease['iteration']}.json":
                raise WorkspaceError(f"writer lease filename does not match its iteration: {path}")
            leases.append(lease)
        except WorkspaceError as exc:
            blockers.append(Blocker("corrupt-writer-lease", str(exc)))
    seen: set[str] = set()
    for lease in leases:
        number = str(lease["iteration"])
        if number in seen:
            blockers.append(Blocker("duplicate-writer-lease", f"iteration {number} has duplicate leases"))
        seen.add(number)
    return leases, blockers


TOPOLOGY_FIELDS = {"schema_version", "epoch", "phase", "active_count", "high_watermark", "updated_at"}


def load_topology_state(context: RepositoryContext) -> dict[str, object] | None:
    path = topology_path(context)
    if not path.exists():
        return None
    value = read_json_file(context, path, label="workspace topology")
    if set(value) != TOPOLOGY_FIELDS or value.get("schema_version") != TOPOLOGY_SCHEMA:
        raise WorkspaceError(f"workspace topology fields do not match the v1 schema: {path}")
    if value.get("phase") not in {"IDLE", "SINGLE_LOCAL", "PARALLEL", "DRAINING"}:
        raise WorkspaceError(f"workspace topology phase is invalid: {path}")
    for field in ("epoch", "active_count", "high_watermark"):
        if not isinstance(value.get(field), int) or int(value[field]) < 0:
            raise WorkspaceError(f"workspace topology {field} is invalid: {path}")
    return dict(value)


def derive_topology(
    leases: Sequence[Mapping[str, object]],
    previous: Mapping[str, object] | None,
) -> dict[str, object]:
    count = len(leases)
    previous_count = int(previous.get("active_count", 0)) if previous else 0
    previous_high = int(previous.get("high_watermark", 0)) if previous else 0
    epoch = int(previous.get("epoch", 0)) if previous else 0
    if count == 0:
        phase = "IDLE"
        high = 0
        if previous_count:
            epoch += 1
    elif count >= 2:
        phase = "PARALLEL"
        high = max(previous_high, count)
    else:
        survivor_is_worktree = leases[0].get("execution_topology") == "worktree"
        if previous_high >= 2 or survivor_is_worktree:
            phase = "DRAINING"
            high = max(previous_high, 2 if survivor_is_worktree else 1)
        else:
            phase = "SINGLE_LOCAL"
            high = 1
    return {
        "schema_version": TOPOLOGY_SCHEMA,
        "epoch": epoch,
        "phase": phase,
        "active_count": count,
        "high_watermark": high,
        "updated_at": now_iso(),
    }


def write_topology(context: RepositoryContext) -> dict[str, object]:
    leases, blockers = load_active_leases(context)
    if blockers:
        raise WorkspaceError(blockers[0].message)
    previous = load_topology_state(context)
    state = derive_topology(leases, previous)
    atomic_write_json(context, topology_path(context), state)
    return state


def runtime_namespace(context: RepositoryContext, iteration: str) -> str:
    repository = hashlib.sha256(path_key(context.common_dir).encode("utf-8")).hexdigest()[:10]
    return f"hl-{repository}-prd-{iteration}"


def validate_worktree_target(
    context: RepositoryContext,
    raw_path: str | Path,
    existing_worktrees: Sequence[Mapping[str, object]],
) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceError("linked worktree path must be absolute")
    if len(str(candidate)) > MAX_PATH or any(ord(char) < 32 for char in str(candidate)):
        raise WorkspaceError("linked worktree path is invalid")
    candidate = candidate.resolve(strict=False)
    if candidate.exists():
        raise WorkspaceError(f"linked worktree target already exists: {candidate}")
    parent = candidate.parent
    if not parent.is_dir():
        raise WorkspaceError(f"linked worktree parent must already exist: {parent}")
    assert_existing_chain_has_no_links(parent)
    for existing in existing_worktrees:
        existing_path = Path(str(existing["path"]))
        if is_within(candidate, existing_path) or is_within(existing_path, candidate):
            raise WorkspaceError(
                f"linked worktree must be outside every existing checkout: {candidate} conflicts with {existing_path}"
            )
    return candidate


def operation_markers(context: RepositoryContext, worktree: Path) -> list[str]:
    markers: list[str] = []
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "REBASE_HEAD",
        "rebase-apply",
        "rebase-merge",
    ):
        result = run_git(context, worktree, ["rev-parse", "--git-path", name], check=False)
        if result.returncode != 0:
            continue
        raw = Path(decode_stdout(result))
        marker = raw if raw.is_absolute() else worktree / raw
        if marker.exists():
            markers.append(name)
    return markers


def is_ancestor(context: RepositoryContext, ancestor: str, descendant: str) -> bool:
    result = run_git(
        context,
        context.project_root,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
    )
    return result.returncode == 0


def lease_projection(lease: Mapping[str, object]) -> dict[str, object]:
    return {
        "iteration": lease.get("iteration"),
        "operation_id": lease.get("operation_id"),
        "owner": lease.get("owner"),
        "generation": lease.get("generation"),
        "execution_topology": lease.get("execution_topology"),
        "expected_root": lease.get("expected_root"),
        "worktree_path": lease.get("worktree_path"),
        "branch_ref": lease.get("branch_ref"),
        "base_ref": lease.get("base_ref"),
        "base_commit": lease.get("base_commit"),
        "implementation_ref": lease.get("implementation_ref"),
        "implementation_commit": lease.get("implementation_commit"),
        "dependency_bindings": [
            dict(item) for item in lease.get("dependency_bindings", []) if isinstance(item, Mapping)
        ],
        "dependency_bindings_digest": lease.get("dependency_bindings_digest"),
        "dependency_refresh_generation": lease.get("dependency_refresh_generation"),
        "principle_sha256": lease.get("principle_sha256"),
        "runtime_namespace": lease.get("runtime_namespace"),
        "heartbeat": lease.get("heartbeat"),
    }


LEASE_PROJECTION_FIELDS = {
    "iteration",
    "operation_id",
    "owner",
    "generation",
    "execution_topology",
    "expected_root",
    "worktree_path",
    "branch_ref",
    "base_ref",
    "base_commit",
    "implementation_ref",
    "implementation_commit",
    "dependency_bindings",
    "dependency_bindings_digest",
    "dependency_refresh_generation",
    "principle_sha256",
    "runtime_namespace",
    "heartbeat",
}


def release_precondition_lease(manifest: Mapping[str, object]) -> dict[str, object]:
    preconditions = manifest.get("preconditions")
    if not isinstance(preconditions, Mapping):
        raise WorkspaceError("release manifest lacks preconditions")
    lease = preconditions.get("writer_lease")
    if not isinstance(lease, dict) or set(lease) != LEASE_PROJECTION_FIELDS:
        raise WorkspaceError("release manifest lacks the exact writer lease projection")
    return dict(lease)


def expected_release_archive(context: RepositoryContext, manifest: Mapping[str, object]) -> Path:
    lease = release_precondition_lease(manifest)
    return (
        registry_root(context)
        / "archives"
        / "leases"
        / f"{lease['iteration']}-g{lease['generation']}-{lease['operation_id']}.json"
    )


def verify_release_result(
    context: RepositoryContext,
    manifest: Mapping[str, object],
) -> list[Blocker]:
    expected = release_precondition_lease(manifest)
    number = str(expected["iteration"])
    blockers: list[Blocker] = []
    if load_lease(context, number) is not None:
        blockers.append(Blocker("writer-lease-still-active", "released operation still has an active writer lease"))
    archive = expected_release_archive(context, manifest)
    if not archive.exists():
        blockers.append(Blocker("lease-archive-missing", "released writer lease archive is missing"))
    else:
        try:
            archived = validate_lease(read_json_file(context, archive, label="archived writer lease"), source=archive)
            if lease_projection(archived) != expected:
                blockers.append(Blocker("lease-archive-mismatch", "writer lease archive differs from the accepted plan"))
        except WorkspaceError as exc:
            blockers.append(Blocker("lease-archive-corrupt", str(exc)))
    target = Path(str(expected["worktree_path"])).resolve(strict=False)
    worktree = next(
        (item for item in list_worktrees(context, include_status=False) if same_path(str(item["path"]), target)),
        None,
    )
    if worktree is None:
        blockers.append(Blocker("released-worktree-missing", "release must not remove the owned worktree"))
    else:
        if worktree.get("branch_ref") != expected["branch_ref"]:
            blockers.append(Blocker("released-branch-mismatch", "release must not change the owned branch"))
        head = worktree.get("head_oid")
        if not isinstance(head, str) or not is_ancestor(context, str(expected["base_commit"]), head):
            blockers.append(Blocker("released-base-diverged", "released worktree no longer descends from its base"))
    return blockers


def guard_lease(
    context: RepositoryContext,
    lease: Mapping[str, object],
    *,
    owner: str | None = None,
    generation: int | None = None,
    worktree_path: Path | None = None,
    branch_ref: str | None = None,
    base_commit: str | None = None,
) -> tuple[list[Blocker], dict[str, object]]:
    blockers: list[Blocker] = []
    expected_path = Path(str(lease["worktree_path"])).resolve(strict=False)
    requested_path = worktree_path.resolve(strict=False) if worktree_path is not None else expected_path
    if not same_path(str(lease["expected_root"]), context.project_root):
        blockers.append(Blocker("lease-root-mismatch", "writer lease belongs to a different project root"))
    if owner is not None and owner != lease["owner"]:
        blockers.append(Blocker("lease-owner-mismatch", "requested writer owner does not hold the lease"))
    if generation is not None and generation != lease["generation"]:
        blockers.append(Blocker("lease-generation-mismatch", "requested writer lease generation is stale"))
    if not same_path(requested_path, expected_path):
        blockers.append(Blocker("lease-path-mismatch", "requested path does not match the writer lease"))
    if branch_ref is not None and branch_ref != lease["branch_ref"]:
        blockers.append(Blocker("lease-branch-mismatch", "requested branch does not match the writer lease"))
    if base_commit is not None and base_commit != lease["base_commit"]:
        blockers.append(Blocker("lease-base-mismatch", "requested base commit does not match the writer lease"))
    current_base = ref_oid(context, str(lease["base_ref"]))
    if current_base != lease["base_commit"]:
        blockers.append(Blocker("base-anchor-drift", "immutable iteration base ref no longer matches the lease"))
    raw_bindings = lease.get("dependency_bindings", [])
    if not isinstance(raw_bindings, list):
        blockers.append(Blocker("dependency-binding-invalid", "writer lease dependency bindings are unreadable"))
        bindings: tuple[dict[str, str], ...] = ()
    else:
        try:
            bindings = normalize_dependency_bindings(raw_bindings)
        except WorkspaceError as exc:
            blockers.append(Blocker("dependency-binding-invalid", str(exc)))
            bindings = ()
    dependency_blockers = dependency_order_blockers(context, bindings)
    blockers.extend(dependency_blockers)
    if dependency_blockers and not any(item.code == "dependency-baseline-stale" for item in dependency_blockers):
        blockers.append(
            Blocker(
                "dependency-baseline-stale",
                "one or more exact dependency candidate identities changed; explicit refresh/revalidation is required",
            )
        )
    worktrees = list_worktrees(context, include_status=True)
    actual = next((item for item in worktrees if same_path(str(item["path"]), expected_path)), None)
    if actual is None:
        blockers.append(Blocker("lease-worktree-missing", "writer lease worktree is not registered with Git"))
        actual_projection: dict[str, object] = {"path": str(expected_path), "present": False}
    else:
        actual_projection = dict(actual)
        actual_projection["present"] = True
        if actual.get("branch_ref") != lease["branch_ref"]:
            blockers.append(Blocker("actual-branch-mismatch", "worktree is attached to a different branch"))
        head = actual.get("head_oid")
        if not isinstance(head, str) or not OID_RE.fullmatch(head):
            blockers.append(Blocker("actual-head-invalid", "worktree HEAD identity is unavailable"))
        elif not is_ancestor(context, str(lease["base_commit"]), head):
            blockers.append(Blocker("actual-base-diverged", "worktree HEAD does not descend from its immutable base"))
        else:
            implementation_commit = str(lease.get("implementation_commit", lease["base_commit"]))
            if not is_ancestor(context, implementation_commit, head):
                blockers.append(
                    Blocker(
                        "actual-implementation-start-diverged",
                        "worktree HEAD no longer descends from its exact implementation start",
                    )
                )
        markers = operation_markers(context, expected_path) if expected_path.is_dir() else []
        if markers:
            blockers.append(
                Blocker("git-operation-in-progress", f"worktree has an in-progress Git operation: {', '.join(markers)}")
            )
            actual_projection["operation_markers"] = markers
    return blockers, actual_projection


def source_snapshot_matches(
    before: Sequence[Mapping[str, object]],
    after: Sequence[Mapping[str, object]],
) -> tuple[bool, list[str]]:
    after_by_path = {path_key(Path(str(item["path"]))): item for item in after}
    changed: list[str] = []
    for expected in before:
        key = path_key(Path(str(expected["path"])))
        actual = after_by_path.get(key)
        if actual is None:
            changed.append(str(expected["path"]))
            continue
        for field in ("head_oid", "branch_ref", "detached", "locked", "prunable", "status"):
            if expected.get(field) != actual.get(field):
                changed.append(str(expected["path"]))
                break
    return not changed, changed


def build_activation_plan(
    project_root: str | Path,
    *,
    iteration: str,
    execution_topology: str,
    base_ref: str,
    branch_ref: str,
    worktree_path: str | Path,
    owner: str,
    lease_generation: int,
    dependency_bindings: Sequence[Mapping[str, object]] = (),
    operation_id: str | None = None,
) -> WorkspacePlan:
    context = resolve_repository(project_root)
    number = validate_iteration(iteration)
    topology = execution_topology.strip().lower()
    if topology not in {"local", "worktree"}:
        raise WorkspaceError("execution topology must be local or worktree")
    owner_value = validate_label(owner, "writer owner")
    generation = validate_generation(lease_generation)
    operation = validate_operation_id(operation_id) if operation_id else new_operation_id()
    branch = validate_branch_ref(context, branch_ref)
    allocation_ref, allocation_object, metadata = load_allocation(context, number, base_ref)
    base_commit = str(metadata["base_commit"])
    dependencies = normalize_dependency_bindings(dependency_bindings)
    if dependencies:
        implementation_ref = dependencies[-1]["candidate_ref"]
        implementation_commit = dependencies[-1]["candidate_commit"]
    else:
        implementation_ref = "refs/heads/main"
        implementation_commit = ref_oid(context, implementation_ref)
        if implementation_commit is None:
            raise WorkspaceError("canonical main implementation start ref is missing")
    existing_worktrees = list_worktrees(context, include_status=True)
    active_leases, lease_errors = load_active_leases(context)
    blockers: list[Blocker] = list(lease_errors)
    dependency_blockers = dependency_order_blockers(context, dependencies)
    blockers.extend(dependency_blockers)
    if dependency_blockers:
        blockers.append(
            Blocker(
                "dependency-baseline-stale",
                "the exact stable dependency candidate changed before activation",
            )
        )
    # A new writer must not use a broken existing writer as proof that the
    # repository is safely parallel.  Validate every active lease before any
    # branch, path, or journal can be created.
    for active_lease in active_leases:
        active_blockers, _ = guard_lease(context, active_lease)
        blockers.extend(active_blockers)
    _, operation_blockers = load_journals(context)
    blockers.extend(operation_blockers)
    existing_lease = next((item for item in active_leases if item["iteration"] == number), None)
    if existing_lease is not None:
        blockers.append(
            Blocker(
                "writer-lease-held",
                f"PRD-{number} already has writer {existing_lease['owner']} generation {existing_lease['generation']}",
            )
        )
    other_branches = {str(item["branch_ref"]) for item in active_leases}
    other_paths = {path_key(Path(str(item["worktree_path"]))) for item in active_leases}
    if branch in other_branches:
        blockers.append(Blocker("branch-already-leased", f"branch is already owned by another writer: {branch}"))

    target: Path
    if topology == "local":
        target = Path(worktree_path).expanduser()
        if not target.is_absolute():
            raise WorkspaceError("Local workspace path must be absolute")
        target = target.resolve(strict=False)
        if not same_path(target, context.project_root):
            blockers.append(Blocker("local-path-mismatch", "Local workspace must remain at the primary checkout"))
        if active_leases:
            blockers.append(Blocker("local-requires-idle", "Local activation is allowed only when no writer is active"))
        if dependencies:
            blockers.append(
                Blocker(
                    "stacked-requires-worktree",
                    "an implementation that consumes a stable dependency candidate must use a linked worktree",
                )
            )
        primary = existing_worktrees[0]
        if primary.get("branch_ref") != branch:
            blockers.append(Blocker("local-branch-mismatch", "primary checkout is attached to a different branch"))
        if primary.get("head_oid") != implementation_commit:
            blockers.append(
                Blocker(
                    "local-implementation-start-mismatch",
                    "primary checkout HEAD does not equal the accepted latest-main implementation start",
                )
            )
        primary_status = primary.get("status")
        if not isinstance(primary_status, Mapping) or not primary_status.get("readable"):
            blockers.append(Blocker("local-status-unreadable", "primary checkout status could not be read"))
        elif any(int(primary_status.get(key, 0) or 0) for key in ("tracked", "untracked", "ignored")):
            blockers.append(
                Blocker(
                    "local-dirty-unowned",
                    "primary checkout is not clean, so existing files cannot be silently assigned to this PRD",
                )
            )
    else:
        if not active_leases and not dependencies:
            blockers.append(
                Blocker(
                    "worktree-requires-existing-writer",
                    "the first active PRD must use Local; a linked worktree is added only for the second or later writer",
                )
            )
        try:
            target = validate_worktree_target(context, worktree_path, existing_worktrees)
        except WorkspaceError as exc:
            target = Path(worktree_path).expanduser().resolve(strict=False)
            blockers.append(Blocker("unsafe-worktree-path", str(exc)))
        if path_key(target) in other_paths:
            blockers.append(Blocker("path-already-leased", f"workspace path is already leased: {target}"))
        if branch == "refs/heads/main":
            blockers.append(Blocker("worktree-main-branch", "a linked PRD worktree cannot claim refs/heads/main"))
        existing_branch = ref_oid(context, branch)
        if existing_branch is not None:
            blockers.append(Blocker("branch-already-exists", f"planned PRD branch already exists: {branch}"))
    if not is_ancestor(context, base_commit, implementation_commit):
        blockers.append(
            Blocker(
                "implementation-start-not-descendant",
                "latest main no longer descends from the immutable PRD creation baseline",
            )
        )

    journal = operation_path(context, operation)
    if journal.exists():
        blockers.append(Blocker("operation-id-already-used", f"workspace operation already exists: {operation}"))
    manifest: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "action": "activate-local" if topology == "local" else "create-worktree",
        "operation_id": operation,
        "project_root": str(context.project_root),
        "git_common_dir": str(context.common_dir),
        "iteration": number,
        "owner": owner_value,
        "lease_generation": generation,
        "execution_topology": topology,
        "base": {
            "ref": base_ref,
            "commit": base_commit,
            "implementation_ref": implementation_ref,
            "implementation_commit": implementation_commit,
            "dependency_bindings": [dict(item) for item in dependencies],
            "dependency_bindings_digest": dependency_bindings_digest(dependencies),
            "allocation_ref": allocation_ref,
            "allocation_object": allocation_object,
            "principle_sha256": metadata["principle_sha256"],
        },
        "branch": {"ref": branch, "create": topology == "worktree"},
        "worktree": {"path": str(target), "create": topology == "worktree"},
        "runtime_namespace": runtime_namespace(context, number),
        "preconditions": {
            "existing_worktrees": compact_worktree_snapshot(existing_worktrees),
            "active_writer_leases": [lease_projection(item) for item in active_leases],
            "strategy": "add-only" if topology == "worktree" else "stay-local",
        },
    }
    return WorkspacePlan(
        action=str(manifest["action"]),
        manifest=manifest,
        digest=digest(manifest),
        blockers=tuple(blockers),
    )


def build_release_plan(
    project_root: str | Path,
    *,
    iteration: str,
    owner: str,
    lease_generation: int,
    worktree_path: str | Path,
    branch_ref: str,
    base_commit: str,
    operation_id: str | None = None,
) -> WorkspacePlan:
    context = resolve_repository(project_root)
    number = validate_iteration(iteration)
    owner_value = validate_label(owner, "writer owner")
    generation = validate_generation(lease_generation)
    operation = validate_operation_id(operation_id) if operation_id else new_operation_id()
    branch = validate_branch_ref(context, branch_ref)
    if not OID_RE.fullmatch(base_commit):
        raise WorkspaceError("base commit must be a full hexadecimal Git object ID")
    target = Path(worktree_path).expanduser()
    if not target.is_absolute():
        raise WorkspaceError("worktree path must be absolute")
    target = target.resolve(strict=False)
    lease = load_lease(context, number)
    blockers: list[Blocker] = []
    if lease is None:
        blockers.append(Blocker("writer-lease-missing", f"PRD-{number} has no active writer lease"))
        lease_projection_value: dict[str, object] | None = None
    else:
        lease_projection_value = lease_projection(lease)
        guard_blockers, _ = guard_lease(
            context,
            lease,
            owner=owner_value,
            generation=generation,
            worktree_path=target,
            branch_ref=branch,
            base_commit=base_commit,
        )
        blockers.extend(guard_blockers)
    if operation_path(context, operation).exists():
        blockers.append(Blocker("operation-id-already-used", f"workspace operation already exists: {operation}"))
    manifest: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "action": "release-writer",
        "operation_id": operation,
        "project_root": str(context.project_root),
        "git_common_dir": str(context.common_dir),
        "iteration": number,
        "owner": owner_value,
        "lease_generation": generation,
        "execution_topology": lease.get("execution_topology") if lease else "unknown",
        "base": {"ref": lease.get("base_ref") if lease else None, "commit": base_commit},
        "branch": {"ref": branch, "create": False},
        "worktree": {"path": str(target), "create": False},
        "runtime_namespace": lease.get("runtime_namespace") if lease else None,
        "preconditions": {
            "writer_lease": lease_projection_value,
            "existing_worktrees": compact_worktree_snapshot(list_worktrees(context, include_status=True)),
            "strategy": "release-lease-only-no-migration",
        },
    }
    return WorkspacePlan(
        action="release-writer",
        manifest=manifest,
        digest=digest(manifest),
        blockers=tuple(blockers),
    )


def build_bind_local_branch_plan(
    project_root: str | Path,
    *,
    iteration: str,
    owner: str,
    lease_generation: int,
    worktree_path: str | Path,
    base_commit: str,
    new_branch_ref: str,
    operation_id: str | None = None,
) -> WorkspacePlan:
    context = resolve_repository(project_root)
    number = validate_iteration(iteration)
    owner_value = validate_label(owner, "writer owner")
    generation = validate_generation(lease_generation)
    if generation == 2_147_483_647:
        raise WorkspaceError("writer lease generation cannot be advanced beyond 2147483647")
    operation = validate_operation_id(operation_id) if operation_id else new_operation_id()
    new_branch = validate_branch_ref(context, new_branch_ref)
    if new_branch == "refs/heads/main":
        raise WorkspaceError("the Local release branch must differ from refs/heads/main")
    if not OID_RE.fullmatch(base_commit):
        raise WorkspaceError("base commit must be a full hexadecimal Git object ID")
    target = Path(worktree_path).expanduser()
    if not target.is_absolute():
        raise WorkspaceError("Local worktree path must be absolute")
    target = target.resolve(strict=False)
    blockers: list[Blocker] = []
    if not same_path(target, context.project_root):
        blockers.append(Blocker("local-path-mismatch", "Local branch binding must stay in the primary checkout"))
    leases, lease_errors = load_active_leases(context)
    blockers.extend(lease_errors)
    _, operation_blockers = load_journals(context)
    blockers.extend(operation_blockers)
    lease = next((item for item in leases if item.get("iteration") == number), None)
    if lease is None:
        raise WorkspaceError(f"PRD-{number} has no active Local writer lease")
    blockers_from_guard, actual = guard_lease(
        context,
        lease,
        owner=owner_value,
        generation=generation,
        worktree_path=target,
        branch_ref="refs/heads/main",
        base_commit=base_commit,
    )
    blockers.extend(blockers_from_guard)
    if lease.get("execution_topology") != "local":
        blockers.append(Blocker("not-local-writer", "only the primary Local writer can release main in place"))
    if lease.get("branch_ref") != "refs/heads/main":
        blockers.append(Blocker("main-already-released", "Local writer is no longer attached to refs/heads/main"))
    other_worktree_leases = [
        item
        for item in leases
        if item.get("iteration") != number and item.get("execution_topology") == "worktree"
    ]
    if not other_worktree_leases:
        blockers.append(
            Blocker(
                "main-release-not-required",
                "main is released only when another active worktree PRD must integrate first",
            )
        )
    for other in other_worktree_leases:
        other_blockers, _ = guard_lease(context, other)
        blockers.extend(other_blockers)
    base_ref = str(lease["base_ref"])
    try:
        _, _, metadata = load_allocation(context, number, base_ref)
        if metadata.get("base_commit") != base_commit:
            blockers.append(Blocker("allocation-base-mismatch", "Local allocation differs from the requested base"))
    except WorkspaceError as exc:
        blockers.append(Blocker("allocation-invalid", str(exc)))
    if ref_oid(context, new_branch) is not None:
        blockers.append(Blocker("branch-already-exists", f"Local release branch already exists: {new_branch}"))
    if any(item.get("branch_ref") == new_branch for item in leases):
        blockers.append(Blocker("branch-already-leased", f"Local release branch is already leased: {new_branch}"))
    if operation_path(context, operation).exists():
        blockers.append(Blocker("operation-id-already-used", f"workspace operation already exists: {operation}"))
    source_snapshot = local_binding_snapshot(context)
    binding_commit = source_snapshot.get("head_oid")
    if not isinstance(binding_commit, str) or not OID_RE.fullmatch(binding_commit):
        blockers.append(Blocker("local-head-invalid", "Local HEAD identity is unavailable for branch binding"))
        binding_commit = base_commit
    elif not is_ancestor(context, base_commit, binding_commit):
        blockers.append(Blocker("local-base-diverged", "Local HEAD no longer descends from its immutable baseline"))
    if ref_oid(context, "refs/heads/main") != binding_commit:
        blockers.append(Blocker("main-ref-drift", "refs/heads/main no longer equals the Local committed HEAD"))
    if actual.get("head_oid") != binding_commit:
        blockers.append(Blocker("local-head-drift", "Local HEAD differs from the committed main identity"))
    lease_before = lease_projection(lease)
    lease_after = dict(lease_before)
    lease_after["operation_id"] = operation
    lease_after["generation"] = generation + 1
    lease_after["branch_ref"] = new_branch
    manifest: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "action": "bind-local-branch",
        "operation_id": operation,
        "project_root": str(context.project_root),
        "git_common_dir": str(context.common_dir),
        "iteration": number,
        "owner": owner_value,
        "lease_generation": generation,
        "execution_topology": "local",
        "base": {"ref": base_ref, "commit": base_commit},
        "branch": {"from_ref": "refs/heads/main", "to_ref": new_branch, "create": True},
        "worktree": {"path": str(context.project_root), "create": False},
        "runtime_namespace": lease["runtime_namespace"],
        "preconditions": {
            "writer_lease_before": lease_before,
            "writer_lease_after": lease_after,
            "existing_worktrees": compact_worktree_snapshot(list_worktrees(context, include_status=True)),
            "source_snapshot": source_snapshot,
            "strategy": "bind-in-place-release-main",
        },
    }
    return WorkspacePlan(
        action="bind-local-branch",
        manifest=manifest,
        digest=digest(manifest),
        blockers=tuple(blockers),
    )


def notification_before(manifest: Mapping[str, object]) -> dict[str, object]:
    topology = str(manifest["execution_topology"])
    base = dict(manifest["base"])  # type: ignore[arg-type]
    branch = dict(manifest["branch"])  # type: ignore[arg-type]
    worktree = dict(manifest["worktree"])  # type: ignore[arg-type]
    preconditions = dict(manifest["preconditions"])  # type: ignore[arg-type]
    existing = preconditions.get("existing_worktrees", [])
    dependency_bindings = base.get("dependency_bindings")
    stacked = isinstance(dependency_bindings, list) and bool(dependency_bindings)
    return {
        "prd": f"PRD-{manifest['iteration']}",
        "reason_code": (
            "stable-dependency-stacked-worktree"
            if stacked
            else "parallel-prd-lazy-worktree"
            if topology == "worktree"
            else "single-active-prd-local"
        ),
        "reason": (
            "this PRD consumes exact stable dependency candidates, so it receives a stacked linked worktree"
            if stacked
            else "another writable PRD is already active, so this PRD receives an independent linked worktree"
            if topology == "worktree"
            else "this is the only writable PRD, so it stays in the primary checkout"
        ),
        "base": {
            "ref": base.get("ref"),
            "commit": base.get("commit"),
            "implementation_ref": base.get("implementation_ref"),
            "implementation_commit": base.get("implementation_commit"),
            "dependency_bindings": base.get("dependency_bindings"),
            "dependency_bindings_digest": base.get("dependency_bindings_digest"),
        },
        "branch": {"ref": branch.get("ref"), "will_create": bool(branch.get("create"))},
        "worktree": {"path": worktree.get("path"), "will_create": bool(worktree.get("create"))},
        "effect_on_existing_prds": {
            "strategy": preconditions.get("strategy"),
            "existing_paths": [item.get("path") for item in existing if isinstance(item, Mapping)],
            "moved": False,
            "committed": False,
            "stashed": False,
            "files_copied": False,
        },
        "remote": {"involved": False, "pushed": False, "force": False},
        "runtime_namespace": manifest.get("runtime_namespace"),
        "writer_lease": {
            "owner": manifest.get("owner"),
            "generation": manifest.get("lease_generation"),
            "scope": "iteration-writer",
        },
    }


def bind_notification_before(manifest: Mapping[str, object]) -> dict[str, object]:
    branch = manifest["branch"]
    base = manifest["base"]
    worktree = manifest["worktree"]
    preconditions = manifest["preconditions"]
    if not all(isinstance(item, Mapping) for item in (branch, base, worktree, preconditions)):
        raise WorkspaceError("Local branch-bind manifest is structurally invalid")
    assert isinstance(branch, Mapping)
    assert isinstance(base, Mapping)
    assert isinstance(worktree, Mapping)
    assert isinstance(preconditions, Mapping)
    snapshot = preconditions.get("source_snapshot")
    lease_after = preconditions.get("writer_lease_after")
    return {
        "prd": f"PRD-{manifest['iteration']}",
        "reason_code": "main-release-for-earlier-integration",
        "reason": "another PRD must integrate first, so the dirty Local PRD is bound to its own branch in place",
        "base": {"ref": base.get("ref"), "commit": base.get("commit")},
        "branch": {
            "from_ref": branch.get("from_ref"),
            "to_ref": branch.get("to_ref"),
            "will_create": True,
        },
        "worktree": {"path": worktree.get("path"), "will_move": False},
        "source_snapshot": snapshot,
        "writer_lease": {
            "owner": manifest.get("owner"),
            "generation_before": manifest.get("lease_generation"),
            "generation_after": lease_after.get("generation") if isinstance(lease_after, Mapping) else None,
        },
        "effect_on_local_prd": {
            "workspace_path_unchanged": True,
            "cwd_unchanged": True,
            "worktree_bytes_will_change": False,
            "index_will_change": False,
            "commit_will_be_created": False,
            "stash_will_be_created": False,
            "files_will_move": False,
        },
        "main_release": {"required": True, "main_ref_will_move": False},
        "remote": {"involved": False, "pushed": False, "force": False},
    }


def release_notification_before(manifest: Mapping[str, object]) -> dict[str, object]:
    return {
        "prd": f"PRD-{manifest['iteration']}",
        "reason_code": "writer-lifecycle-finished",
        "reason": "release the exact writer lease while leaving every surviving workspace in place",
        "branch": dict(manifest["branch"]),  # type: ignore[arg-type]
        "worktree": dict(manifest["worktree"]),  # type: ignore[arg-type]
        "survivor_policy": "stay-in-place",
        "will_remove_worktree": False,
        "will_delete_branch": False,
        "will_migrate_survivor": False,
        "remote": {"involved": False, "pushed": False, "force": False},
    }


JOURNAL_FIELDS = {
    "schema_version",
    "operation_id",
    "plan_digest",
    "action",
    "phase",
    "project_root",
    "git_common_dir",
    "iteration",
    "owner",
    "lease_generation",
    "created_at",
    "updated_at",
    "manifest",
    "created_objects",
    "history",
    "error",
}
JOURNAL_PHASES = {
    "PLANNED",
    "LEASED",
    "BRANCH_READY",
    "WORKTREE_READY",
    "LOCAL_BRANCH_BOUND",
    "RELEASED",
    "READY",
    "FAILED_NEEDS_RECONCILE",
}


def validate_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema_version") != PLAN_SCHEMA:
        raise WorkspaceError("workspace operation manifest has an unsupported schema")
    required = {
        "schema_version",
        "action",
        "operation_id",
        "project_root",
        "git_common_dir",
        "iteration",
        "owner",
        "lease_generation",
        "execution_topology",
        "base",
        "branch",
        "worktree",
        "runtime_namespace",
        "preconditions",
    }
    if set(value) != required:
        raise WorkspaceError("workspace operation manifest fields do not match the v1 schema")
    if value.get("action") not in {
        "activate-local",
        "create-worktree",
        "release-writer",
        "bind-local-branch",
    }:
        raise WorkspaceError("workspace operation manifest action is invalid")
    validate_operation_id(str(value.get("operation_id", "")))
    validate_iteration(str(value.get("iteration", "")))
    validate_label(str(value.get("owner", "")), "writer owner")
    generation = value.get("lease_generation")
    if not isinstance(generation, int):
        raise WorkspaceError("workspace operation manifest lease generation is invalid")
    validate_generation(generation)
    if value.get("execution_topology") not in {"local", "worktree", "unknown"}:
        raise WorkspaceError("workspace operation manifest topology is invalid")
    for field in ("base", "branch", "worktree", "preconditions"):
        if not isinstance(value.get(field), dict):
            raise WorkspaceError(f"workspace operation manifest {field} must be an object")
    action = str(value["action"])
    base = value["base"]
    branch = value["branch"]
    worktree = value["worktree"]
    preconditions = value["preconditions"]
    assert isinstance(base, dict)
    assert isinstance(branch, dict)
    assert isinstance(worktree, dict)
    assert isinstance(preconditions, dict)
    expected_base_fields = (
        {"ref", "commit"}
        if action in {"release-writer", "bind-local-branch"}
        else {
            "ref",
            "commit",
            "implementation_ref",
            "implementation_commit",
            "dependency_bindings",
            "dependency_bindings_digest",
            "allocation_ref",
            "allocation_object",
            "principle_sha256",
        }
    )
    if action == "release-writer":
        expected_precondition_fields = {"writer_lease", "existing_worktrees", "strategy"}
    elif action == "bind-local-branch":
        expected_precondition_fields = {
            "writer_lease_before",
            "writer_lease_after",
            "existing_worktrees",
            "source_snapshot",
            "strategy",
        }
    else:
        expected_precondition_fields = {"existing_worktrees", "active_writer_leases", "strategy"}
    if set(base) != expected_base_fields:
        raise WorkspaceError("workspace operation manifest base fields do not match its action")
    if action == "bind-local-branch":
        if set(branch) != {"from_ref", "to_ref", "create"}:
            raise WorkspaceError("Local branch-bind manifest branch fields are invalid")
        if branch.get("from_ref") != "refs/heads/main":
            raise WorkspaceError("Local branch-bind manifest must release refs/heads/main")
        target_ref = branch.get("to_ref")
        if not isinstance(target_ref, str) or not target_ref.startswith("refs/heads/"):
            raise WorkspaceError("Local branch-bind target ref is invalid")
    elif set(branch) != {"ref", "create"}:
        raise WorkspaceError("workspace operation manifest branch fields are invalid")
    if set(worktree) != {"path", "create"}:
        raise WorkspaceError("workspace operation manifest worktree fields are invalid")
    if set(preconditions) != expected_precondition_fields:
        raise WorkspaceError("workspace operation manifest precondition fields do not match its action")
    base_ref = base.get("ref")
    base_commit = base.get("commit")
    if not isinstance(base_ref, str) or not base_ref.startswith("refs/project-harness/v2/iterations/"):
        raise WorkspaceError("workspace operation manifest base ref is invalid")
    if not isinstance(base_commit, str) or not OID_RE.fullmatch(base_commit):
        raise WorkspaceError("workspace operation manifest base commit is invalid")
    branch_ref = branch.get("to_ref") if action == "bind-local-branch" else branch.get("ref")
    if not isinstance(branch_ref, str) or not branch_ref.startswith("refs/heads/"):
        raise WorkspaceError("workspace operation manifest branch ref is invalid")
    raw_path = worktree.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or len(raw_path) > MAX_PATH:
        raise WorkspaceError("workspace operation manifest worktree path is invalid")
    expected_branch_create = action in {"create-worktree", "bind-local-branch"}
    expected_worktree_create = action == "create-worktree"
    if branch.get("create") is not expected_branch_create or worktree.get("create") is not expected_worktree_create:
        raise WorkspaceError("workspace operation manifest create flags do not match its action")
    if not isinstance(preconditions.get("existing_worktrees"), list):
        raise WorkspaceError("workspace operation manifest worktree snapshot is invalid")
    if action not in {"release-writer", "bind-local-branch"}:
        if not isinstance(preconditions.get("active_writer_leases"), list):
            raise WorkspaceError("workspace operation manifest lease snapshot is invalid")
        for field in ("allocation_ref", "allocation_object"):
            item = base.get(field)
            if not isinstance(item, str) or not item:
                raise WorkspaceError(f"workspace operation manifest {field} is invalid")
        if not isinstance(base.get("principle_sha256"), str) or not DIGEST_RE.fullmatch(
            str(base["principle_sha256"])
        ):
            raise WorkspaceError("workspace operation manifest principle hash is invalid")
        raw_dependencies = base.get("dependency_bindings")
        if not isinstance(raw_dependencies, list):
            raise WorkspaceError("workspace operation manifest dependency bindings are invalid")
        dependencies = normalize_dependency_bindings(raw_dependencies)
        if base.get("dependency_bindings_digest") != dependency_bindings_digest(dependencies):
            raise WorkspaceError("workspace operation manifest dependency binding digest differs")
        expected_implementation_ref = dependencies[-1]["candidate_ref"] if dependencies else "refs/heads/main"
        expected_implementation_commit = dependencies[-1]["candidate_commit"] if dependencies else None
        if base.get("implementation_ref") != expected_implementation_ref:
            raise WorkspaceError("workspace operation manifest implementation start ref is invalid")
        if not isinstance(base.get("implementation_commit"), str) or not OID_RE.fullmatch(
            str(base["implementation_commit"])
        ):
            raise WorkspaceError("workspace operation manifest implementation start commit is invalid")
        if expected_implementation_commit is not None and base.get("implementation_commit") != expected_implementation_commit:
            raise WorkspaceError("workspace operation manifest implementation start differs from dependency candidate")
        if not isinstance(value.get("runtime_namespace"), str):
            raise WorkspaceError("workspace operation manifest runtime namespace is invalid")
    elif action == "release-writer":
        writer_lease = preconditions.get("writer_lease")
        if not isinstance(writer_lease, dict) or set(writer_lease) != LEASE_PROJECTION_FIELDS:
            raise WorkspaceError("release manifest lacks its exact writer lease precondition")
    else:
        before_lease = preconditions.get("writer_lease_before")
        after_lease = preconditions.get("writer_lease_after")
        if not isinstance(before_lease, dict) or set(before_lease) != LEASE_PROJECTION_FIELDS:
            raise WorkspaceError("Local branch-bind manifest lacks its before lease")
        if not isinstance(after_lease, dict) or set(after_lease) != LEASE_PROJECTION_FIELDS:
            raise WorkspaceError("Local branch-bind manifest lacks its after lease")
        if not isinstance(preconditions.get("source_snapshot"), dict):
            raise WorkspaceError("Local branch-bind manifest lacks its source snapshot")
        assert isinstance(before_lease, dict)
        assert isinstance(after_lease, dict)
        expected_after = dict(before_lease)
        expected_after["operation_id"] = value["operation_id"]
        expected_after["generation"] = int(value["lease_generation"]) + 1
        expected_after["branch_ref"] = branch["to_ref"]
        if after_lease != expected_after:
            raise WorkspaceError("Local branch-bind lease transition does not match its operation identity")
        source_snapshot = preconditions["source_snapshot"]
        assert isinstance(source_snapshot, dict)
        if set(source_snapshot) != {"path", "head_oid", "branch_ref", "status", "index", "files"}:
            raise WorkspaceError("Local branch-bind source snapshot fields are invalid")
        if source_snapshot.get("path") != value["project_root"]:
            raise WorkspaceError("Local branch-bind source snapshot belongs to a different root")
        if (
            not isinstance(source_snapshot.get("head_oid"), str)
            or not OID_RE.fullmatch(str(source_snapshot.get("head_oid")))
            or source_snapshot.get("branch_ref") != "refs/heads/main"
        ):
            raise WorkspaceError("Local branch-bind source snapshot has the wrong HEAD identity")
        status = source_snapshot.get("status")
        index = source_snapshot.get("index")
        files = source_snapshot.get("files")
        if not isinstance(status, dict) or set(status) != {
            "readable",
            "sha256",
            "tracked",
            "untracked",
            "ignored",
        }:
            raise WorkspaceError("Local branch-bind status snapshot is invalid")
        if not isinstance(index, dict) or set(index) != {"path", "size", "sha256", "entries_sha256"}:
            raise WorkspaceError("Local branch-bind index snapshot is invalid")
        if not isinstance(files, dict) or set(files) != {"file_count", "total_bytes", "sha256"}:
            raise WorkspaceError("Local branch-bind byte snapshot is invalid")
        for snapshot_digest in (status.get("sha256"), index.get("sha256"), index.get("entries_sha256"), files.get("sha256")):
            if not isinstance(snapshot_digest, str) or not DIGEST_RE.fullmatch(snapshot_digest):
                raise WorkspaceError("Local branch-bind snapshot contains an invalid digest")
        if not isinstance(value.get("runtime_namespace"), str):
            raise WorkspaceError("Local branch-bind manifest runtime namespace is invalid")
    return dict(value)


def validate_journal(value: object, *, source: Path) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != JOURNAL_FIELDS:
        raise WorkspaceError(f"workspace journal fields do not match the v1 schema: {source}")
    if value.get("schema_version") != JOURNAL_SCHEMA:
        raise WorkspaceError(f"unsupported workspace journal schema: {source}")
    operation = validate_operation_id(str(value.get("operation_id", "")))
    plan_digest = str(value.get("plan_digest", ""))
    validate_digest(plan_digest)
    manifest = validate_manifest(value.get("manifest"))
    if digest(manifest) != plan_digest or manifest["operation_id"] != operation:
        raise WorkspaceError(f"workspace journal manifest identity or digest mismatch: {source}")
    if value.get("action") != manifest["action"] or value.get("phase") not in JOURNAL_PHASES:
        raise WorkspaceError(f"workspace journal action or phase is invalid: {source}")
    if value.get("project_root") != manifest["project_root"] or value.get("git_common_dir") != manifest["git_common_dir"]:
        raise WorkspaceError(f"workspace journal repository identity mismatch: {source}")
    if value.get("iteration") != manifest["iteration"] or value.get("owner") != manifest["owner"]:
        raise WorkspaceError(f"workspace journal writer identity mismatch: {source}")
    if value.get("lease_generation") != manifest["lease_generation"]:
        raise WorkspaceError(f"workspace journal lease generation mismatch: {source}")
    if not isinstance(value.get("created_objects"), dict) or not isinstance(value.get("history"), list):
        raise WorkspaceError(f"workspace journal mutable fields are invalid: {source}")
    created_objects = value["created_objects"]
    history = value["history"]
    assert isinstance(created_objects, dict)
    assert isinstance(history, list)
    if not set(created_objects).issubset(
        {
            "writer_lease",
            "branch_ref",
            "worktree_path",
            "lease_archive",
            "local_branch_ref",
            "head_symref",
            "writer_lease_generation",
            "source_snapshot_after",
            "preservation",
        }
    ):
        raise WorkspaceError(f"workspace journal created-object fields are invalid: {source}")
    if len(history) < 1 or len(history) > 128:
        raise WorkspaceError(f"workspace journal history length is invalid: {source}")
    for entry in history:
        if not isinstance(entry, dict) or set(entry) != {"phase", "at"}:
            raise WorkspaceError(f"workspace journal history entry is invalid: {source}")
        if entry.get("phase") not in JOURNAL_PHASES or not isinstance(entry.get("at"), str):
            raise WorkspaceError(f"workspace journal history value is invalid: {source}")
    if value.get("action") == "bind-local-branch" and value.get("phase") == "READY":
        preservation = created_objects.get("preservation")
        expected_preservation = {
            "workspace_path_unchanged",
            "head_commit_unchanged",
            "status_fingerprint_unchanged",
            "index_bytes_unchanged",
            "worktree_bytes_unchanged",
        }
        if (
            not isinstance(preservation, dict)
            or set(preservation) != expected_preservation
            or not all(item is True for item in preservation.values())
        ):
            raise WorkspaceError(f"ready Local branch-bind journal lacks complete preservation evidence: {source}")
    error = value.get("error")
    if value.get("phase") == "FAILED_NEEDS_RECONCILE" and not isinstance(error, str):
        raise WorkspaceError(f"failed workspace journal lacks an error: {source}")
    if value.get("phase") != "FAILED_NEEDS_RECONCILE" and error is not None:
        raise WorkspaceError(f"non-failed workspace journal contains an error: {source}")
    return dict(value)


def load_journal(context: RepositoryContext, operation_id: str) -> dict[str, object] | None:
    path = operation_path(context, operation_id)
    if not path.exists():
        return None
    return validate_journal(read_json_file(context, path, label="workspace journal"), source=path)


def create_journal(context: RepositoryContext, plan: WorkspacePlan) -> dict[str, object]:
    timestamp = now_iso()
    journal: dict[str, object] = {
        "schema_version": JOURNAL_SCHEMA,
        "operation_id": plan.operation_id,
        "plan_digest": plan.digest,
        "action": plan.action,
        "phase": "PLANNED",
        "project_root": plan.manifest["project_root"],
        "git_common_dir": plan.manifest["git_common_dir"],
        "iteration": plan.manifest["iteration"],
        "owner": plan.manifest["owner"],
        "lease_generation": plan.manifest["lease_generation"],
        "created_at": timestamp,
        "updated_at": timestamp,
        "manifest": plan.manifest,
        "created_objects": {},
        "history": [{"phase": "PLANNED", "at": timestamp}],
        "error": None,
    }
    exclusive_write_json(context, operation_path(context, plan.operation_id), journal)
    return journal


def advance_journal(
    context: RepositoryContext,
    journal: Mapping[str, object],
    phase: str,
    *,
    created_objects: Mapping[str, object] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    if phase not in JOURNAL_PHASES:
        raise WorkspaceError(f"invalid workspace journal phase: {phase}")
    updated = dict(journal)
    timestamp = now_iso()
    updated["phase"] = phase
    updated["updated_at"] = timestamp
    current_objects = dict(updated.get("created_objects", {}))
    if created_objects:
        current_objects.update(created_objects)
    updated["created_objects"] = current_objects
    history = list(updated.get("history", []))
    history.append({"phase": phase, "at": timestamp})
    updated["history"] = history
    updated["error"] = error if phase == "FAILED_NEEDS_RECONCILE" else None
    atomic_write_json(context, operation_path(context, str(updated["operation_id"])), updated)
    return updated


def lease_from_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    base = manifest["base"]
    branch = manifest["branch"]
    worktree = manifest["worktree"]
    if not isinstance(base, Mapping) or not isinstance(branch, Mapping) or not isinstance(worktree, Mapping):
        raise WorkspaceError("accepted workspace manifest is structurally invalid")
    timestamp = now_iso()
    return {
        "schema_version": LEASE_SCHEMA,
        "scope": "iteration-writer",
        "state": "active",
        "iteration": manifest["iteration"],
        "operation_id": manifest["operation_id"],
        "owner": manifest["owner"],
        "generation": manifest["lease_generation"],
        "execution_topology": manifest["execution_topology"],
        "expected_root": manifest["project_root"],
        "worktree_path": worktree["path"],
        "branch_ref": branch["ref"],
        "base_ref": base["ref"],
        "base_commit": base["commit"],
        "implementation_ref": base["implementation_ref"],
        "implementation_commit": base["implementation_commit"],
        "dependency_bindings": [dict(item) for item in base["dependency_bindings"]],
        "dependency_bindings_digest": base["dependency_bindings_digest"],
        "dependency_refresh_generation": 0,
        "principle_sha256": base["principle_sha256"],
        "runtime_namespace": manifest["runtime_namespace"],
        "acquired_at": timestamp,
        "heartbeat": timestamp,
    }


def acquire_matching_lease(
    context: RepositoryContext,
    manifest: Mapping[str, object],
) -> tuple[dict[str, object], bool]:
    number = str(manifest["iteration"])
    expected = lease_from_manifest(manifest)
    existing = load_lease(context, number)
    if existing is not None:
        immutable_fields = LEASE_FIELDS - {"acquired_at", "heartbeat"}
        if any(existing.get(field) != expected.get(field) for field in immutable_fields):
            raise WorkspaceError(f"PRD-{number} writer lease is held by a different operation or identity")
        return existing, False
    exclusive_write_json(context, lease_path(context, number), expected)
    return expected, True


def create_branch(
    context: RepositoryContext,
    *,
    branch_ref: str,
    base_ref: str,
    base_commit: str,
    operation_id: str,
) -> bool:
    existing = ref_oid(context, branch_ref)
    if existing is not None:
        if existing != base_commit:
            raise WorkspaceError(f"branch exists at a different commit: {branch_ref}")
        return False
    commands = (
        "start\n"
        f"verify {base_ref} {base_commit}\n"
        f"create {branch_ref} {base_commit}\n"
        "prepare\n"
        "commit\n"
    ).encode("ascii")
    result = run_git(
        context,
        context.project_root,
        ["update-ref", "-m", f"harness-workspace: {operation_id}", "--stdin"],
        input_bytes=commands,
        check=False,
        disable_hooks=True,
    )
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(error or "atomic branch/base transaction failed")
    return True


def current_symbolic_head(context: RepositoryContext, worktree: Path) -> str | None:
    result = run_git(context, worktree, ["symbolic-ref", "-q", "HEAD"], check=False)
    if result.returncode != 0:
        return None
    value = decode_stdout(result)
    return value if value.startswith("refs/heads/") else None


def bind_local_head_transaction(
    context: RepositoryContext,
    *,
    base_ref: str,
    base_commit: str,
    binding_commit: str,
    new_branch_ref: str,
    operation_id: str,
) -> bool:
    current_head = current_symbolic_head(context, context.project_root)
    new_branch_oid = ref_oid(context, new_branch_ref)
    main_oid = ref_oid(context, "refs/heads/main")
    if current_head == new_branch_ref:
        if new_branch_oid != binding_commit or main_oid != binding_commit:
            raise WorkspaceError("Local branch is bound but its branch/main identity differs from the accepted plan")
        return False
    if current_head != "refs/heads/main":
        raise WorkspaceError("primary checkout is no longer attached to refs/heads/main")
    if main_oid != binding_commit:
        raise WorkspaceError("refs/heads/main changed before Local branch binding")
    branch_created = False
    if new_branch_oid is None:
        # Git rejects a single transaction that both verifies main and updates
        # HEAD away from main because HEAD currently dereferences to that ref.
        # Keep the authoritative part atomic: immutable base + main CAS +
        # branch create.  The following symbolic-ref write is idempotently
        # recoverable and never checks out files or refreshes the index.
        commands = (
            "start\n"
            f"verify {base_ref} {base_commit}\n"
            f"verify refs/heads/main {binding_commit}\n"
            f"create {new_branch_ref} {binding_commit}\n"
            "prepare\n"
            "commit\n"
        ).encode("ascii")
        result = run_git(
            context,
            context.project_root,
            ["update-ref", "-m", f"harness-workspace: release main {operation_id}", "--stdin"],
            input_bytes=commands,
            check=False,
            disable_hooks=True,
        )
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise WorkspaceError(error or "Local branch/base CAS transaction failed")
        branch_created = True
    elif new_branch_oid != binding_commit:
        raise WorkspaceError("Local release branch appeared at a different commit")
    if current_symbolic_head(context, context.project_root) != "refs/heads/main":
        raise WorkspaceError("primary HEAD changed after Local branch creation; branch was preserved for reconcile")
    if ref_oid(context, "refs/heads/main") != binding_commit or ref_oid(context, new_branch_ref) != binding_commit:
        raise WorkspaceError("main or Local branch changed before the symbolic HEAD bind")
    result = run_git(
        context,
        context.project_root,
        ["symbolic-ref", "-m", f"harness-workspace: release main {operation_id}", "HEAD", new_branch_ref],
        check=False,
        disable_hooks=True,
    )
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(error or "Local symbolic HEAD bind failed")
    if current_symbolic_head(context, context.project_root) != new_branch_ref:
        raise WorkspaceError("atomic transaction completed without the expected Local HEAD symref")
    if ref_oid(context, new_branch_ref) != binding_commit or ref_oid(context, "refs/heads/main") != binding_commit:
        raise WorkspaceError("atomic transaction completed with an unexpected branch identity")
    return True


def transition_bound_lease(
    context: RepositoryContext,
    manifest: Mapping[str, object],
) -> tuple[dict[str, object], bool]:
    preconditions = manifest.get("preconditions")
    if not isinstance(preconditions, Mapping):
        raise WorkspaceError("Local branch-bind manifest lacks lease preconditions")
    before = preconditions.get("writer_lease_before")
    after = preconditions.get("writer_lease_after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise WorkspaceError("Local branch-bind manifest lease transition is invalid")
    number = str(manifest["iteration"])
    lease = load_lease(context, number)
    if lease is None:
        raise WorkspaceError("Local writer lease disappeared during branch binding")
    projected = lease_projection(lease)
    if projected == dict(after):
        return lease, False
    if projected != dict(before):
        raise WorkspaceError("Local writer lease changed after the accepted branch-bind plan")
    updated = dict(lease)
    updated["operation_id"] = after["operation_id"]
    updated["generation"] = after["generation"]
    updated["branch_ref"] = after["branch_ref"]
    # Preserve acquired_at/heartbeat: branch binding changes routing identity,
    # not lease age.  The generation bump invalidates every stale writer card.
    atomic_write_json(context, lease_path(context, number), updated)
    return validate_lease(updated, source=lease_path(context, number)), True


def verify_bound_local_result(
    context: RepositoryContext,
    manifest: Mapping[str, object],
    *,
    check_source_preservation: bool,
) -> tuple[list[Blocker], dict[str, bool], dict[str, object] | None]:
    blockers: list[Blocker] = []
    base = manifest.get("base")
    branch = manifest.get("branch")
    preconditions = manifest.get("preconditions")
    if not isinstance(base, Mapping) or not isinstance(branch, Mapping) or not isinstance(preconditions, Mapping):
        raise WorkspaceError("Local branch-bind manifest is structurally invalid")
    base_ref = str(base["ref"])
    base_commit = str(base["commit"])
    source_snapshot = preconditions.get("source_snapshot")
    binding_commit = source_snapshot.get("head_oid") if isinstance(source_snapshot, Mapping) else None
    new_branch = str(branch["to_ref"])
    if ref_oid(context, base_ref) != base_commit:
        blockers.append(Blocker("base-anchor-drift", "immutable iteration base changed during Local branch binding"))
    if not isinstance(binding_commit, str) or not OID_RE.fullmatch(binding_commit):
        blockers.append(Blocker("binding-commit-invalid", "accepted Local binding commit is invalid"))
        binding_commit = base_commit
    if ref_oid(context, "refs/heads/main") != binding_commit:
        blockers.append(Blocker("main-ref-drift", "refs/heads/main changed during Local branch binding"))
    if ref_oid(context, new_branch) != binding_commit:
        blockers.append(Blocker("bound-branch-mismatch", "Local release branch does not equal the accepted Local HEAD"))
    if current_symbolic_head(context, context.project_root) != new_branch:
        blockers.append(Blocker("bound-head-mismatch", "primary checkout HEAD is not bound to the Local PRD branch"))
    after_lease = preconditions.get("writer_lease_after")
    active = load_lease(context, str(manifest["iteration"]))
    if active is None or not isinstance(after_lease, Mapping) or lease_projection(active) != dict(after_lease):
        blockers.append(Blocker("bound-lease-mismatch", "writer lease was not advanced to the bound branch identity"))
    actual_snapshot: dict[str, object] | None = None
    preservation = {
        "workspace_path_unchanged": False,
        "head_commit_unchanged": False,
        "status_fingerprint_unchanged": False,
        "index_bytes_unchanged": False,
        "worktree_bytes_unchanged": False,
    }
    if check_source_preservation:
        expected_snapshot = preconditions.get("source_snapshot")
        if not isinstance(expected_snapshot, Mapping):
            blockers.append(Blocker("source-snapshot-missing", "accepted Local source snapshot is missing"))
        else:
            actual_snapshot = local_binding_snapshot(context)
            preservation = binding_preservation(expected_snapshot, actual_snapshot)
            failed = [name for name, preserved in preservation.items() if not preserved]
            if failed:
                blockers.append(
                    Blocker(
                        "local-source-changed",
                        "Local branch binding did not preserve: " + ", ".join(failed),
                    )
                )
    return blockers, preservation, actual_snapshot


def matching_worktree(
    context: RepositoryContext,
    path: Path,
    branch_ref: str,
    base_commit: str,
) -> dict[str, object] | None:
    matches = [item for item in list_worktrees(context, include_status=True) if same_path(str(item["path"]), path)]
    if not matches:
        return None
    item = matches[0]
    if item.get("branch_ref") != branch_ref or item.get("head_oid") != base_commit:
        raise WorkspaceError(f"worktree target is registered with a different branch or HEAD: {path}")
    return item


def add_worktree(
    context: RepositoryContext,
    *,
    path: Path,
    branch_ref: str,
    base_commit: str,
) -> bool:
    existing = matching_worktree(context, path, branch_ref, base_commit)
    if existing is not None:
        return False
    if path.exists():
        raise WorkspaceError(f"worktree target exists but is not a matching registered worktree: {path}")
    # ``git worktree add`` treats a fully-qualified ref as a generic
    # commit-ish and can therefore create a detached checkout.  The ref has
    # already passed ``check-ref-format``; use its unambiguous local-branch
    # shorthand so the linked checkout is attached to that exact branch.
    branch_shorthand = branch_ref[len("refs/heads/") :]
    result = run_git(
        context,
        context.project_root,
        ["worktree", "add", str(path), branch_shorthand],
        check=False,
        disable_hooks=True,
    )
    if result.returncode != 0:
        # A process can be interrupted after Git registers the worktree but before
        # it returns.  Matching Git state is safe to adopt; everything else stays.
        if matching_worktree(context, path, branch_ref, base_commit) is not None:
            return True
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(error or "Git could not create the linked worktree")
    if matching_worktree(context, path, branch_ref, base_commit) is None:
        raise WorkspaceError("Git reported worktree creation success but the expected worktree is absent")
    return True


def manifest_matches_arguments(
    manifest: Mapping[str, object],
    *,
    project_root: Path,
    iteration: str,
    owner: str,
    lease_generation: int,
    execution_topology: str | None = None,
    base_ref: str | None = None,
    branch_ref: str | None = None,
    worktree_path: Path | None = None,
    dependency_bindings: Sequence[Mapping[str, object]] | None = None,
) -> list[Blocker]:
    blockers: list[Blocker] = []
    base = manifest.get("base")
    branch = manifest.get("branch")
    worktree = manifest.get("worktree")
    if not same_path(str(manifest.get("project_root")), project_root):
        blockers.append(Blocker("operation-root-mismatch", "request project root differs from durable manifest"))
    if manifest.get("iteration") != iteration:
        blockers.append(Blocker("operation-iteration-mismatch", "request iteration differs from durable manifest"))
    if manifest.get("owner") != owner:
        blockers.append(Blocker("operation-owner-mismatch", "request owner differs from durable manifest"))
    if manifest.get("lease_generation") != lease_generation:
        blockers.append(Blocker("operation-generation-mismatch", "request generation differs from durable manifest"))
    if execution_topology is not None and manifest.get("execution_topology") != execution_topology:
        blockers.append(Blocker("operation-topology-mismatch", "request topology differs from durable manifest"))
    if base_ref is not None and (not isinstance(base, Mapping) or base.get("ref") != base_ref):
        blockers.append(Blocker("operation-base-mismatch", "request base differs from durable manifest"))
    if branch_ref is not None and (not isinstance(branch, Mapping) or branch.get("ref") != branch_ref):
        blockers.append(Blocker("operation-branch-mismatch", "request branch differs from durable manifest"))
    if worktree_path is not None and (
        not isinstance(worktree, Mapping) or not same_path(str(worktree.get("path")), worktree_path)
    ):
        blockers.append(Blocker("operation-path-mismatch", "request path differs from durable manifest"))
    if dependency_bindings is not None:
        try:
            requested = normalize_dependency_bindings(dependency_bindings)
        except WorkspaceError as exc:
            blockers.append(Blocker("operation-dependency-binding-invalid", str(exc)))
        else:
            accepted_dependencies = base.get("dependency_bindings") if isinstance(base, Mapping) else None
            if not isinstance(accepted_dependencies, list) or [dict(item) for item in requested] != accepted_dependencies:
                blockers.append(
                    Blocker(
                        "operation-dependency-mismatch",
                        "requested dependency candidates differ from the durable manifest",
                    )
                )
    return blockers


def result_payload(
    plan: WorkspacePlan,
    *,
    journal: Mapping[str, object] | None,
    created_now: bool,
    topology: Mapping[str, object] | None,
    blockers: Sequence[Blocker] = (),
) -> dict[str, object]:
    phase = "blocked" if blockers else "succeeded"
    manifest = plan.manifest
    worktree = manifest["worktree"]
    branch = manifest["branch"]
    base = manifest["base"]
    if not isinstance(worktree, Mapping) or not isinstance(branch, Mapping) or not isinstance(base, Mapping):
        raise WorkspaceError("workspace manifest is structurally invalid")
    if plan.action == "bind-local-branch":
        created_objects = journal.get("created_objects") if journal else None
        evidence = dict(created_objects) if isinstance(created_objects, Mapping) else {}
        preconditions = manifest.get("preconditions")
        lease_after = preconditions.get("writer_lease_after") if isinstance(preconditions, Mapping) else None
        source_snapshot = preconditions.get("source_snapshot") if isinstance(preconditions, Mapping) else None
        notification = {
            "prd": f"PRD-{plan.iteration}",
            "reason_code": "main-release-for-earlier-integration",
            "actual_path": worktree.get("path"),
            "branch_from_ref": branch.get("from_ref"),
            "actual_branch_ref": branch.get("to_ref"),
            "actual_head": source_snapshot.get("head_oid") if isinstance(source_snapshot, Mapping) else None,
            "branch_created": not blockers,
            "main_released": not blockers,
            "main_ref_moved": False,
            "writer_lease": {
                "owner": manifest.get("owner"),
                "generation_before": manifest.get("lease_generation"),
                "generation_after": lease_after.get("generation") if isinstance(lease_after, Mapping) else None,
            },
            "preservation": evidence.get("preservation"),
            "effect_on_local_prd": {
                "workspace_path_unchanged": True,
                "cwd_unchanged": True,
                "committed": False,
                "stashed": False,
                "files_moved": False,
                "worktree_moved": False,
            },
            "remote": {"involved": False, "pushed": False, "force": False},
        }
    elif plan.action == "release-writer":
        notification: dict[str, object] = {
            "prd": f"PRD-{plan.iteration}",
            "actual_path": worktree.get("path"),
            "actual_branch_ref": branch.get("ref"),
            "writer_lease_released": not blockers,
            "survivor_policy": "stay-in-place",
            "worktree_removed": False,
            "branch_deleted": False,
            "survivor_migrated": False,
            "remote": {"involved": False, "pushed": False, "force": False},
        }
    else:
        raw_dependencies = base.get("dependency_bindings")
        stacked = isinstance(raw_dependencies, list) and bool(raw_dependencies)
        notification = {
            "prd": f"PRD-{plan.iteration}",
            "reason_code": (
                "stable-dependency-stacked-worktree"
                if stacked
                else "parallel-prd-lazy-worktree"
                if manifest["execution_topology"] == "worktree"
                else "single-active-prd-local"
            ),
            "actual_path": worktree.get("path"),
            "actual_branch_ref": branch.get("ref"),
            "actual_head": base.get("implementation_commit", base.get("commit")),
            "runtime_namespace": manifest.get("runtime_namespace"),
            "dependency_bindings": raw_dependencies,
            "writer_lease": {
                "owner": manifest.get("owner"),
                "generation": manifest.get("lease_generation"),
                "active": not blockers,
            },
            "effect_on_existing_prds": {
                "strategy": "add-only" if manifest["execution_topology"] == "worktree" else "stay-local",
                "moved": False,
                "committed": False,
                "stashed": False,
                "files_copied": False,
                "source_state_preserved": not blockers,
            },
            "remote": {"involved": False, "pushed": False, "force": False},
        }
    return {
        "schema_version": PUBLIC_SCHEMA,
        "command": plan.action,
        "action_level": (
            "notify"
            if manifest.get("execution_topology") == "worktree"
            or plan.action in {"release-writer", "bind-local-branch"}
            else "silent"
        ),
        "notification_phase": "after",
        "pushed": False,
        "project_root": manifest["project_root"],
        "git_common_dir": manifest["git_common_dir"],
        "operation_id": plan.operation_id,
        "iteration": plan.iteration,
        "phase": phase,
        "plan_digest": plan.digest,
        "journal_phase": journal.get("phase") if journal else None,
        "created_now": created_now if not blockers else False,
        "idempotent_replay": (not created_now) if not blockers else False,
        "notification": notification,
        "topology": dict(topology) if topology is not None else None,
        "warnings": list(plan.warnings),
        "blocking_reasons": [item.as_dict() for item in blockers],
        "next_gate": "blocked" if blockers else "workspace-ready",
        "exclusions": list(EXCLUSIONS),
    }


def plan_from_journal(journal: Mapping[str, object]) -> WorkspacePlan:
    manifest = validate_manifest(journal["manifest"])
    return WorkspacePlan(
        action=str(journal["action"]),
        manifest=manifest,
        digest=str(journal["plan_digest"]),
    )


def apply_activation(
    project_root: str | Path,
    *,
    iteration: str,
    execution_topology: str,
    base_ref: str,
    branch_ref: str,
    worktree_path: str | Path,
    owner: str,
    lease_generation: int,
    operation_id: str,
    accepted_plan_digest: str,
    dependency_bindings: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    context = resolve_repository(project_root)
    number = validate_iteration(iteration)
    topology_name = execution_topology.strip().lower()
    if topology_name not in {"local", "worktree"}:
        raise WorkspaceError("execution topology must be local or worktree")
    owner_value = validate_label(owner, "writer owner")
    generation = validate_generation(lease_generation)
    operation = validate_operation_id(operation_id)
    accepted = validate_digest(accepted_plan_digest)
    branch = validate_branch_ref(context, branch_ref)
    target = Path(worktree_path).expanduser()
    if not target.is_absolute():
        raise WorkspaceError("worktree path must be absolute")
    target = target.resolve(strict=False)

    with coordinator_lock(context):
        journal = load_journal(context, operation)
        if journal is not None:
            plan = plan_from_journal(journal)
            blockers = manifest_matches_arguments(
                plan.manifest,
                project_root=context.project_root,
                iteration=number,
                owner=owner_value,
                lease_generation=generation,
                execution_topology=topology_name,
                base_ref=base_ref,
                branch_ref=branch,
                worktree_path=target,
                dependency_bindings=dependency_bindings,
            )
            if accepted != plan.digest:
                blockers.append(
                    Blocker("accepted-plan-digest-mismatch", "accepted digest differs from the durable workspace plan")
                )
            if blockers:
                return result_payload(plan, journal=journal, created_now=False, topology=None, blockers=blockers)
            if journal["phase"] == "READY":
                lease = load_lease(context, number)
                if lease is None:
                    blockers = [Blocker("writer-lease-missing", "ready operation lost its writer lease")]
                else:
                    blockers, _ = guard_lease(
                        context,
                        lease,
                        owner=owner_value,
                        generation=generation,
                        worktree_path=target,
                        branch_ref=branch,
                        base_commit=str(plan.manifest["base"]["commit"]),  # type: ignore[index]
                    )
                current_topology = derive_topology(load_active_leases(context)[0], load_topology_state(context))
                return result_payload(
                    plan,
                    journal=journal,
                    created_now=False,
                    topology=current_topology,
                    blockers=blockers,
                )
        else:
            plan = build_activation_plan(
                context.project_root,
                iteration=number,
                execution_topology=topology_name,
                base_ref=base_ref,
                branch_ref=branch,
                worktree_path=target,
                owner=owner_value,
                lease_generation=generation,
                dependency_bindings=dependency_bindings,
                operation_id=operation,
            )
            blockers = list(plan.blockers)
            if accepted != plan.digest:
                blockers.append(
                    Blocker(
                        "accepted-plan-digest-mismatch",
                        "workspace state or request changed after planning; create a new plan",
                    )
                )
            if blockers:
                return result_payload(plan, journal=None, created_now=False, topology=None, blockers=blockers)
            journal = create_journal(context, plan)

        created_now = False
        try:
            accepted_base = plan.manifest.get("base")
            if not isinstance(accepted_base, Mapping):
                raise WorkspaceError("accepted workspace manifest lacks its implementation start")
            raw_dependencies = accepted_base.get("dependency_bindings")
            if not isinstance(raw_dependencies, list):
                raise WorkspaceError("accepted workspace manifest lacks exact dependency bindings")
            dependencies = normalize_dependency_bindings(raw_dependencies)
            live_dependency_blockers = dependency_order_blockers(context, dependencies)
            if live_dependency_blockers:
                raise WorkspaceError(
                    "dependency baseline changed after planning: "
                    + "; ".join(item.code for item in live_dependency_blockers)
                )
            implementation_ref = str(accepted_base.get("implementation_ref"))
            implementation_commit = str(accepted_base.get("implementation_commit"))
            if ref_oid(context, implementation_ref) != implementation_commit:
                raise WorkspaceError("latest-main implementation start changed after the accepted workspace plan")
            if not is_ancestor(context, str(accepted_base["commit"]), implementation_commit):
                raise WorkspaceError("implementation start no longer descends from the immutable PRD baseline")
            if topology_name == "local":
                local_worktrees = list_worktrees(context, include_status=True)
                primary = local_worktrees[0] if local_worktrees else None
                if (
                    not isinstance(primary, Mapping)
                    or primary.get("branch_ref") != branch
                    or primary.get("head_oid") != implementation_commit
                ):
                    raise WorkspaceError("Local checkout changed after the accepted implementation start plan")
            lease, lease_created = acquire_matching_lease(context, plan.manifest)
            created_now = created_now or lease_created
            journal = advance_journal(
                context,
                journal,
                "LEASED",
                created_objects={"writer_lease": str(lease_path(context, number))},
            )
            write_topology(context)
            if topology_name == "worktree":
                base = plan.manifest["base"]
                branch_manifest = plan.manifest["branch"]
                worktree_manifest = plan.manifest["worktree"]
                if not isinstance(base, Mapping) or not isinstance(branch_manifest, Mapping) or not isinstance(
                    worktree_manifest, Mapping
                ):
                    raise WorkspaceError("accepted workspace manifest is structurally invalid")
                branch_created = create_branch(
                    context,
                    branch_ref=str(branch_manifest["ref"]),
                    base_ref=str(base["implementation_ref"]),
                    base_commit=str(base["implementation_commit"]),
                    operation_id=operation,
                )
                created_now = created_now or branch_created
                journal = advance_journal(
                    context,
                    journal,
                    "BRANCH_READY",
                    created_objects={"branch_ref": str(branch_manifest["ref"])},
                )
                worktree_created = add_worktree(
                    context,
                    path=Path(str(worktree_manifest["path"])),
                    branch_ref=str(branch_manifest["ref"]),
                    base_commit=str(base["implementation_commit"]),
                )
                created_now = created_now or worktree_created
                journal = advance_journal(
                    context,
                    journal,
                    "WORKTREE_READY",
                    created_objects={"worktree_path": str(worktree_manifest["path"])},
                )
                before = plan.manifest["preconditions"]
                if not isinstance(before, Mapping) or not isinstance(before.get("existing_worktrees"), list):
                    raise WorkspaceError("accepted workspace manifest lacks its source snapshot")
                after = compact_worktree_snapshot(list_worktrees(context, include_status=True))
                preserved, changed = source_snapshot_matches(before["existing_worktrees"], after)
                if not preserved:
                    raise WorkspaceError(
                        "an existing workspace changed during add-only creation and was preserved for reconcile: "
                        + ", ".join(changed)
                    )
            blockers, _ = guard_lease(
                context,
                lease,
                owner=owner_value,
                generation=generation,
                worktree_path=target,
                branch_ref=branch,
                base_commit=str(plan.manifest["base"]["commit"]),  # type: ignore[index]
            )
            if blockers:
                raise WorkspaceError("; ".join(item.message for item in blockers))
            journal = advance_journal(context, journal, "READY")
            current_topology = write_topology(context)
            return result_payload(
                plan,
                journal=journal,
                created_now=created_now,
                topology=current_topology,
            )
        except WorkspaceError as exc:
            journal = advance_journal(
                context,
                journal,
                "FAILED_NEEDS_RECONCILE",
                error=str(exc)[:1000],
            )
            return result_payload(
                plan,
                journal=journal,
                created_now=False,
                topology=derive_topology(load_active_leases(context)[0], load_topology_state(context)),
                blockers=(Blocker("workspace-needs-reconcile", str(exc)),),
            )


def bind_arguments_match_manifest(
    manifest: Mapping[str, object],
    *,
    project_root: Path,
    iteration: str,
    owner: str,
    lease_generation: int,
    worktree_path: Path,
    base_commit: str,
    new_branch_ref: str,
) -> list[Blocker]:
    blockers: list[Blocker] = []
    if manifest.get("action") != "bind-local-branch":
        blockers.append(Blocker("operation-action-mismatch", "operation is not a Local branch-bind plan"))
    if not same_path(str(manifest.get("project_root")), project_root):
        blockers.append(Blocker("operation-root-mismatch", "request project root differs from durable manifest"))
    if manifest.get("iteration") != iteration:
        blockers.append(Blocker("operation-iteration-mismatch", "request iteration differs from durable manifest"))
    if manifest.get("owner") != owner:
        blockers.append(Blocker("operation-owner-mismatch", "request owner differs from durable manifest"))
    if manifest.get("lease_generation") != lease_generation:
        blockers.append(Blocker("operation-generation-mismatch", "request generation differs from durable manifest"))
    branch = manifest.get("branch")
    if not isinstance(branch, Mapping) or branch.get("to_ref") != new_branch_ref:
        blockers.append(Blocker("operation-branch-mismatch", "request branch differs from durable manifest"))
    worktree = manifest.get("worktree")
    if not isinstance(worktree, Mapping) or not same_path(str(worktree.get("path")), worktree_path):
        blockers.append(Blocker("operation-path-mismatch", "request path differs from durable manifest"))
    base = manifest.get("base")
    if not isinstance(base, Mapping) or base.get("commit") != base_commit:
        blockers.append(Blocker("operation-base-mismatch", "request base differs from durable manifest"))
    return blockers


def apply_bind_local_branch(
    project_root: str | Path,
    *,
    iteration: str,
    owner: str,
    lease_generation: int,
    worktree_path: str | Path,
    base_commit: str,
    new_branch_ref: str,
    operation_id: str,
    accepted_plan_digest: str,
) -> dict[str, object]:
    context = resolve_repository(project_root)
    number = validate_iteration(iteration)
    owner_value = validate_label(owner, "writer owner")
    generation = validate_generation(lease_generation)
    operation = validate_operation_id(operation_id)
    accepted = validate_digest(accepted_plan_digest)
    new_branch = validate_branch_ref(context, new_branch_ref)
    if new_branch == "refs/heads/main":
        raise WorkspaceError("the Local release branch must differ from refs/heads/main")
    if not OID_RE.fullmatch(base_commit):
        raise WorkspaceError("base commit must be a full hexadecimal Git object ID")
    target = Path(worktree_path).expanduser()
    if not target.is_absolute():
        raise WorkspaceError("Local worktree path must be absolute")
    target = target.resolve(strict=False)
    with coordinator_lock(context):
        journal = load_journal(context, operation)
        if journal is not None:
            plan = plan_from_journal(journal)
            blockers = bind_arguments_match_manifest(
                plan.manifest,
                project_root=context.project_root,
                iteration=number,
                owner=owner_value,
                lease_generation=generation,
                worktree_path=target,
                base_commit=base_commit,
                new_branch_ref=new_branch,
            )
            if accepted != plan.digest:
                blockers.append(
                    Blocker("accepted-plan-digest-mismatch", "accepted digest differs from the durable workspace plan")
                )
            if blockers:
                return result_payload(plan, journal=journal, created_now=False, topology=None, blockers=blockers)
            if journal["phase"] == "READY":
                result_blockers, _, _ = verify_bound_local_result(
                    context,
                    plan.manifest,
                    check_source_preservation=False,
                )
                current_topology = derive_topology(load_active_leases(context)[0], load_topology_state(context))
                return result_payload(
                    plan,
                    journal=journal,
                    created_now=False,
                    topology=current_topology,
                    blockers=result_blockers,
                )
        else:
            plan = build_bind_local_branch_plan(
                context.project_root,
                iteration=number,
                owner=owner_value,
                lease_generation=generation,
                worktree_path=target,
                base_commit=base_commit,
                new_branch_ref=new_branch,
                operation_id=operation,
            )
            blockers = list(plan.blockers)
            if accepted != plan.digest:
                blockers.append(
                    Blocker(
                        "accepted-plan-digest-mismatch",
                        "workspace state or request changed after planning; create a new Local branch-bind plan",
                    )
                )
            if blockers:
                return result_payload(plan, journal=None, created_now=False, topology=None, blockers=blockers)
            journal = create_journal(context, plan)
        created_now = False
        try:
            base = plan.manifest.get("base")
            branch = plan.manifest.get("branch")
            preconditions = plan.manifest.get("preconditions")
            if not isinstance(base, Mapping) or not isinstance(branch, Mapping) or not isinstance(
                preconditions, Mapping
            ):
                raise WorkspaceError("accepted Local branch-bind plan is structurally invalid")
            expected_source = preconditions.get("source_snapshot")
            if not isinstance(expected_source, Mapping):
                raise WorkspaceError("accepted Local branch-bind plan lacks its source snapshot")
            live_source = local_binding_snapshot(context)
            pre_preservation = binding_preservation(expected_source, live_source)
            changed_before = [name for name, preserved in pre_preservation.items() if not preserved]
            if changed_before:
                raise WorkspaceError(
                    "Local source changed before the branch-bind transaction: " + ", ".join(changed_before)
                )
            branch_created = bind_local_head_transaction(
                context,
                base_ref=str(base["ref"]),
                base_commit=str(base["commit"]),
                binding_commit=str(expected_source["head_oid"]),
                new_branch_ref=str(branch["to_ref"]),
                operation_id=operation,
            )
            created_now = created_now or branch_created
            _, lease_changed = transition_bound_lease(context, plan.manifest)
            created_now = created_now or lease_changed
            result_blockers, preservation, actual_snapshot = verify_bound_local_result(
                context,
                plan.manifest,
                check_source_preservation=True,
            )
            if result_blockers:
                raise WorkspaceError("; ".join(item.message for item in result_blockers))
            journal = advance_journal(
                context,
                journal,
                "LOCAL_BRANCH_BOUND",
                created_objects={
                    "local_branch_ref": str(branch["to_ref"]),
                    "head_symref": str(branch["to_ref"]),
                    "writer_lease_generation": generation + 1,
                    "source_snapshot_after": actual_snapshot,
                    "preservation": preservation,
                },
            )
            journal = advance_journal(context, journal, "READY")
            current_topology = derive_topology(load_active_leases(context)[0], load_topology_state(context))
            return result_payload(
                plan,
                journal=journal,
                created_now=created_now,
                topology=current_topology,
            )
        except WorkspaceError as exc:
            journal = advance_journal(
                context,
                journal,
                "FAILED_NEEDS_RECONCILE",
                error=str(exc)[:1000],
            )
            return result_payload(
                plan,
                journal=journal,
                created_now=False,
                topology=derive_topology(load_active_leases(context)[0], load_topology_state(context)),
                blockers=(Blocker("workspace-needs-reconcile", str(exc)),),
            )


def _read_dependency_refresh_json(
    context: RepositoryContext,
    path: Path,
    *,
    label: str,
) -> dict[str, object] | None:
    if not path.exists():
        return None
    return read_json_file(context, path, label=label)


def _dependency_refresh_result(
    plan: DependencyRefreshPlan,
    *,
    phase: str,
    blockers: Sequence[Blocker],
    receipts: Sequence[Mapping[str, object]] = (),
    receipt_digest: str | None = None,
    idempotent: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": PUBLIC_SCHEMA,
        "command": "refresh-dependencies",
        "action_level": "notify",
        "notification_phase": "after",
        "pushed": False,
        "project_root": plan.manifest["project_root"],
        "git_common_dir": plan.manifest["git_common_dir"],
        "operation_id": plan.operation_id,
        "iteration": plan.iteration,
        "phase": "blocked" if blockers else phase,
        "plan_digest": plan.digest,
        "notification": dependency_refresh_notification(plan.manifest, phase="after"),
        "verification_receipts": [dict(item) for item in receipts],
        "dependency_refresh_receipt_digest": receipt_digest,
        "idempotent_replay": idempotent,
        "blocking_reasons": [item.as_dict() for item in blockers],
        "next_gate": "blocked" if blockers else "workspace-revalidated-candidate-allowed",
        "exclusions": list(EXCLUSIONS),
    }


def _run_dependency_refresh_commands(
    context: RepositoryContext,
    plan: DependencyRefreshPlan,
) -> tuple[list[dict[str, object]], list[Blocker]]:
    target = Path(str(plan.manifest["worktree_path"]))
    receipts: list[dict[str, object]] = []
    blockers: list[Blocker] = []
    commands = plan.manifest.get("verification_commands")
    if not isinstance(commands, list):
        raise WorkspaceError("dependency refresh plan verification commands are invalid")
    for command in commands:
        if not isinstance(command, Mapping):
            raise WorkspaceError("dependency refresh plan verification command is invalid")
        evidence_id = str(command.get("evidence_id", ""))
        argv = command.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise WorkspaceError(f"dependency refresh argv is invalid: {evidence_id}")
        try:
            result = subprocess.run(
                argv,
                cwd=target,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=git_environment(),
                timeout=3600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            blockers.append(Blocker("dependency-refresh-verification-error", f"{evidence_id}: {exc}"))
            continue
        if len(result.stdout) > 8 * 1024 * 1024 or len(result.stderr) > 8 * 1024 * 1024:
            blockers.append(
                Blocker("dependency-refresh-verification-output", f"{evidence_id}: output exceeded safety limit")
            )
            continue
        receipt_payload: dict[str, object] = {
            "evidence_id": evidence_id,
            "argv": list(argv),
            "exit_code": result.returncode,
            "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
            "worktree_head": plan.manifest["worktree_head"],
            "worktree_tree": plan.manifest["worktree_tree"],
            "dependency_bindings_digest": dependency_bindings_digest(
                plan.manifest["after_bindings"]  # type: ignore[arg-type]
            ),
        }
        receipt_payload["receipt_digest"] = digest(receipt_payload)
        receipts.append(receipt_payload)
        if result.returncode != 0:
            blockers.append(
                Blocker(
                    "dependency-refresh-verification-failed",
                    f"{evidence_id} exited with {result.returncode}",
                )
            )
    return receipts, blockers


def apply_dependency_refresh(
    plan: DependencyRefreshPlan,
    *,
    accepted_plan_digest: str,
) -> dict[str, object]:
    if not isinstance(plan, DependencyRefreshPlan):
        raise WorkspaceError("dependency refresh requires a DependencyRefreshPlan")
    accepted = validate_digest(accepted_plan_digest)
    if accepted != plan.digest or plan.digest != digest(plan.manifest):
        return _dependency_refresh_result(
            plan,
            phase="blocked",
            blockers=(Blocker("accepted-plan-digest-mismatch", "dependency refresh plan was not accepted exactly"),),
        )
    if plan.blockers:
        return _dependency_refresh_result(plan, phase="blocked", blockers=plan.blockers)
    context = resolve_repository(str(plan.manifest["project_root"]))
    journal_path = dependency_refresh_journal_path(context, plan.operation_id)
    receipt_path = dependency_refresh_receipt_path(context, plan.operation_id)
    with coordinator_lock(context):
        journal = _read_dependency_refresh_json(
            context,
            journal_path,
            label="dependency refresh journal",
        )
        current = load_lease(context, plan.iteration)
        if current is None:
            return _dependency_refresh_result(
                plan,
                phase="blocked",
                blockers=(Blocker("writer-lease-missing", "writer lease disappeared before dependency refresh"),),
            )
        before_digest = str(plan.manifest["before_lease_digest"])
        after_projection = plan.manifest.get("after_lease")
        if not isinstance(after_projection, Mapping):
            raise WorkspaceError("dependency refresh after lease is invalid")
        current_projection = lease_projection(current)
        current_digest = digest(current)
        already_applied = current_projection == dict(after_projection)
        if journal is not None:
            if (
                journal.get("schema_version") != DEPENDENCY_REFRESH_JOURNAL_SCHEMA
                or journal.get("operation_id") != plan.operation_id
                or journal.get("plan_digest") != plan.digest
                or journal.get("manifest") != plan.manifest
            ):
                return _dependency_refresh_result(
                    plan,
                    phase="blocked",
                    blockers=(Blocker("dependency-refresh-journal-mismatch", "refresh operation identity differs"),),
                )
            if journal.get("status") == "complete" and already_applied:
                receipt = _read_dependency_refresh_json(
                    context,
                    receipt_path,
                    label="dependency refresh receipt",
                )
                if receipt is None or receipt.get("receipt_digest") != digest(
                    {key: value for key, value in receipt.items() if key != "receipt_digest"}
                ):
                    return _dependency_refresh_result(
                        plan,
                        phase="blocked",
                        blockers=(Blocker("dependency-refresh-receipt-invalid", "refresh receipt is missing or changed"),),
                    )
                return _dependency_refresh_result(
                    plan,
                    phase="succeeded",
                    blockers=(),
                    receipts=receipt.get("verification_receipts", []),  # type: ignore[arg-type]
                    receipt_digest=str(receipt["receipt_digest"]),
                    idempotent=True,
                )
        elif current_digest != before_digest:
            return _dependency_refresh_result(
                plan,
                phase="blocked",
                blockers=(Blocker("dependency-refresh-lease-cas", "writer lease changed after planning"),),
            )
        if journal is None:
            journal = {
                "schema_version": DEPENDENCY_REFRESH_JOURNAL_SCHEMA,
                "operation_id": plan.operation_id,
                "plan_digest": plan.digest,
                "manifest": plan.manifest,
                "status": "planned",
                "verification_receipts": [],
                "receipt_digest": None,
            }
            exclusive_write_json(context, journal_path, journal)
        if already_applied:
            receipts_raw = journal.get("verification_receipts")
            if not isinstance(receipts_raw, list) or not receipts_raw:
                return _dependency_refresh_result(
                    plan,
                    phase="blocked",
                    blockers=(
                        Blocker(
                            "dependency-refresh-reconcile",
                            "lease was refreshed without durable verification receipts",
                        ),
                    ),
                )
            receipts = [dict(item) for item in receipts_raw if isinstance(item, Mapping)]
        else:
            head, tree = _worktree_head_and_tree(context, Path(str(plan.manifest["worktree_path"])))
            status = status_fingerprint(context, Path(str(plan.manifest["worktree_path"])))
            if (
                head != plan.manifest["worktree_head"]
                or tree != plan.manifest["worktree_tree"]
                or status != plan.manifest["status_fingerprint"]
            ):
                return _dependency_refresh_result(
                    plan,
                    phase="blocked",
                    blockers=(Blocker("dependency-refresh-worktree-drift", "worktree changed after planning"),),
                )
            bindings = normalize_dependency_bindings(plan.manifest["after_bindings"])  # type: ignore[arg-type]
            live = dependency_order_blockers(context, bindings)
            if live:
                return _dependency_refresh_result(plan, phase="blocked", blockers=live)
            receipts, verification_blockers = _run_dependency_refresh_commands(context, plan)
            post_head, post_tree = _worktree_head_and_tree(
                context,
                Path(str(plan.manifest["worktree_path"])),
            )
            post_status = status_fingerprint(context, Path(str(plan.manifest["worktree_path"])))
            if (
                post_head != plan.manifest["worktree_head"]
                or post_tree != plan.manifest["worktree_tree"]
                or post_status != plan.manifest["status_fingerprint"]
            ):
                verification_blockers.append(
                    Blocker("dependency-refresh-verification-mutated", "verification changed the writer worktree")
                )
            journal["verification_receipts"] = receipts
            journal["status"] = "verified" if not verification_blockers else "failed-needs-reconcile"
            atomic_write_json(context, journal_path, journal)
            if verification_blockers:
                return _dependency_refresh_result(
                    plan,
                    phase="blocked",
                    blockers=verification_blockers,
                    receipts=receipts,
                )
            live_current = load_lease(context, plan.iteration)
            if live_current is None or digest(live_current) != before_digest:
                return _dependency_refresh_result(
                    plan,
                    phase="blocked",
                    blockers=(Blocker("dependency-refresh-lease-cas", "writer lease changed during verification"),),
                    receipts=receipts,
                )
            updated = dict(live_current)
            for field, value in after_projection.items():
                if field in updated:
                    updated[field] = value
            atomic_write_json(context, lease_path(context, plan.iteration), updated)
            validate_lease(updated, source=lease_path(context, plan.iteration))
        receipt_payload: dict[str, object] = {
            "schema_version": DEPENDENCY_REFRESH_RECEIPT_SCHEMA,
            "operation_id": plan.operation_id,
            "plan_digest": plan.digest,
            "iteration": plan.iteration,
            "before_lease_digest": before_digest,
            "after_lease_digest": digest(load_lease(context, plan.iteration)),
            "worktree_head": plan.manifest["worktree_head"],
            "worktree_tree": plan.manifest["worktree_tree"],
            "before_bindings_digest": dependency_bindings_digest(
                plan.manifest["before_bindings"]  # type: ignore[arg-type]
            ),
            "after_bindings_digest": dependency_bindings_digest(
                plan.manifest["after_bindings"]  # type: ignore[arg-type]
            ),
            "verification_receipts": receipts,
            "pushed": False,
        }
        receipt_payload["receipt_digest"] = digest(receipt_payload)
        if not receipt_path.exists():
            exclusive_write_json(context, receipt_path, receipt_payload)
        else:
            existing_receipt = read_json_file(context, receipt_path, label="dependency refresh receipt")
            if existing_receipt != receipt_payload:
                return _dependency_refresh_result(
                    plan,
                    phase="blocked",
                    blockers=(Blocker("dependency-refresh-receipt-collision", "refresh receipt differs"),),
                )
        journal["status"] = "complete"
        journal["receipt_digest"] = receipt_payload["receipt_digest"]
        atomic_write_json(context, journal_path, journal)
        write_topology(context)
        return _dependency_refresh_result(
            plan,
            phase="succeeded",
            blockers=(),
            receipts=receipts,
            receipt_digest=str(receipt_payload["receipt_digest"]),
        )


def apply_release(
    project_root: str | Path,
    *,
    iteration: str,
    owner: str,
    lease_generation: int,
    worktree_path: str | Path,
    branch_ref: str,
    base_commit: str,
    operation_id: str,
    accepted_plan_digest: str,
) -> dict[str, object]:
    context = resolve_repository(project_root)
    number = validate_iteration(iteration)
    owner_value = validate_label(owner, "writer owner")
    generation = validate_generation(lease_generation)
    operation = validate_operation_id(operation_id)
    accepted = validate_digest(accepted_plan_digest)
    branch = validate_branch_ref(context, branch_ref)
    if not OID_RE.fullmatch(base_commit):
        raise WorkspaceError("base commit must be a full hexadecimal Git object ID")
    target = Path(worktree_path).expanduser()
    if not target.is_absolute():
        raise WorkspaceError("worktree path must be absolute")
    target = target.resolve(strict=False)
    with coordinator_lock(context):
        journal = load_journal(context, operation)
        if journal is not None:
            plan = plan_from_journal(journal)
            blockers = manifest_matches_arguments(
                plan.manifest,
                project_root=context.project_root,
                iteration=number,
                owner=owner_value,
                lease_generation=generation,
                branch_ref=branch,
                worktree_path=target,
            )
            base = plan.manifest.get("base")
            if not isinstance(base, Mapping) or base.get("commit") != base_commit:
                blockers.append(Blocker("operation-base-mismatch", "request base differs from durable manifest"))
            if accepted != plan.digest:
                blockers.append(
                    Blocker("accepted-plan-digest-mismatch", "accepted digest differs from durable workspace plan")
                )
            if blockers:
                return result_payload(plan, journal=journal, created_now=False, topology=None, blockers=blockers)
            if journal["phase"] == "READY":
                blockers = verify_release_result(context, plan.manifest)
                current = derive_topology(load_active_leases(context)[0], load_topology_state(context))
                return result_payload(
                    plan,
                    journal=journal,
                    created_now=False,
                    topology=current,
                    blockers=blockers,
                )
        else:
            plan = build_release_plan(
                context.project_root,
                iteration=number,
                owner=owner_value,
                lease_generation=generation,
                worktree_path=target,
                branch_ref=branch,
                base_commit=base_commit,
                operation_id=operation,
            )
            blockers = list(plan.blockers)
            if accepted != plan.digest:
                blockers.append(
                    Blocker(
                        "accepted-plan-digest-mismatch",
                        "workspace state or request changed after planning; create a new plan",
                    )
                )
            if blockers:
                return result_payload(plan, journal=None, created_now=False, topology=None, blockers=blockers)
            journal = create_journal(context, plan)

        lease = load_lease(context, number)
        if lease is None:
            # A crash can occur after the atomic archive but before journal READY.
            release_blockers = verify_release_result(context, plan.manifest)
            if release_blockers:
                error = WorkspaceError("; ".join(item.message for item in release_blockers))
                journal = advance_journal(
                    context,
                    journal,
                    "FAILED_NEEDS_RECONCILE",
                    error=str(error),
                )
                return result_payload(
                    plan,
                    journal=journal,
                    created_now=False,
                    topology=derive_topology(load_active_leases(context)[0], load_topology_state(context)),
                    blockers=(Blocker("workspace-needs-reconcile", str(error)),),
                )
            released_now = False
        else:
            blockers, _ = guard_lease(
                context,
                lease,
                owner=owner_value,
                generation=generation,
                worktree_path=target,
                branch_ref=branch,
                base_commit=base_commit,
            )
            if blockers:
                journal = advance_journal(
                    context,
                    journal,
                    "FAILED_NEEDS_RECONCILE",
                    error="; ".join(item.message for item in blockers),
                )
                return result_payload(
                    plan,
                    journal=journal,
                    created_now=False,
                    topology=derive_topology(load_active_leases(context)[0], load_topology_state(context)),
                    blockers=blockers,
                )
            source = lease_path(context, number)
            destination = archive_lease_path(context, lease)
            assert_operational_path(context, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            assert_operational_path(context, destination.parent)
            if destination.exists():
                raise WorkspaceError(f"writer lease archive already exists unexpectedly: {destination}")
            os.replace(source, destination)
            released_now = True
            journal = advance_journal(
                context,
                journal,
                "RELEASED",
                created_objects={"lease_archive": str(destination)},
            )
        release_blockers = verify_release_result(context, plan.manifest)
        if release_blockers:
            journal = advance_journal(
                context,
                journal,
                "FAILED_NEEDS_RECONCILE",
                error="; ".join(item.message for item in release_blockers),
            )
            return result_payload(
                plan,
                journal=journal,
                created_now=False,
                topology=derive_topology(load_active_leases(context)[0], load_topology_state(context)),
                blockers=release_blockers,
            )
        current_topology = write_topology(context)
        journal = advance_journal(context, journal, "READY")
        return result_payload(
            plan,
            journal=journal,
            created_now=released_now,
            topology=current_topology,
        )


def guard_payload(
    context: RepositoryContext,
    *,
    iteration: str,
    owner: str,
    lease_generation: int,
    worktree_path: str | Path,
    branch_ref: str,
    base_commit: str,
) -> dict[str, object]:
    number = validate_iteration(iteration)
    owner_value = validate_label(owner, "writer owner")
    generation = validate_generation(lease_generation)
    branch = validate_branch_ref(context, branch_ref)
    if not OID_RE.fullmatch(base_commit):
        raise WorkspaceError("base commit must be a full hexadecimal Git object ID")
    target = Path(worktree_path).expanduser()
    if not target.is_absolute():
        raise WorkspaceError("worktree path must be absolute")
    target = target.resolve(strict=False)
    lease = load_lease(context, number)
    if lease is None:
        blockers = [Blocker("writer-lease-missing", f"PRD-{number} has no active writer lease")]
        actual: dict[str, object] | None = None
        expected: dict[str, object] | None = None
    else:
        blockers, actual = guard_lease(
            context,
            lease,
            owner=owner_value,
            generation=generation,
            worktree_path=target,
            branch_ref=branch,
            base_commit=base_commit,
        )
        expected = lease_projection(lease)
    return {
        "schema_version": PUBLIC_SCHEMA,
        "command": "guard-workspace",
        "action_level": "silent",
        "pushed": False,
        "project_root": str(context.project_root),
        "git_common_dir": str(context.common_dir),
        "iteration": number,
        "phase": "blocked" if blockers else "valid",
        "expected": expected,
        "actual": actual,
        "blocking_reasons": [item.as_dict() for item in blockers],
        "next_gate": "blocked" if blockers else "mutation-may-proceed",
        "exclusions": list(EXCLUSIONS),
    }


def journal_projection(journal: Mapping[str, object]) -> dict[str, object]:
    created = journal.get("created_objects")
    return {
        "operation_id": journal.get("operation_id"),
        "plan_digest": journal.get("plan_digest"),
        "action": journal.get("action"),
        "phase": journal.get("phase"),
        "iteration": journal.get("iteration"),
        "owner": journal.get("owner"),
        "lease_generation": journal.get("lease_generation"),
        "updated_at": journal.get("updated_at"),
        "created_objects": dict(created) if isinstance(created, Mapping) else {},
        "error": journal.get("error"),
    }


def load_journals(context: RepositoryContext) -> tuple[list[dict[str, object]], list[Blocker]]:
    directory = registry_root(context) / "operations"
    if not directory.exists():
        return [], []
    assert_operational_path(context, directory)
    journals: list[dict[str, object]] = []
    blockers: list[Blocker] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = validate_journal(read_json_file(context, path, label="workspace journal"), source=path)
            journals.append(value)
            if value["phase"] != "READY":
                blockers.append(
                    Blocker(
                        "workspace-operation-incomplete",
                        f"operation {value['operation_id']} is {value['phase']} and must be resumed or reconciled",
                    )
                )
        except WorkspaceError as exc:
            blockers.append(Blocker("corrupt-workspace-journal", str(exc)))
    return journals, blockers


def status_payload(context: RepositoryContext) -> dict[str, object]:
    worktrees = list_worktrees(context, include_status=True)
    leases, lease_blockers = load_active_leases(context)
    journals, journal_blockers = load_journals(context)
    blockers = [*lease_blockers, *journal_blockers]
    try:
        stored = load_topology_state(context)
    except WorkspaceError as exc:
        stored = None
        blockers.append(Blocker("corrupt-workspace-topology", str(exc)))
    derived = derive_topology(leases, stored)
    if stored is not None:
        for field in ("phase", "active_count"):
            if stored.get(field) != derived.get(field):
                blockers.append(
                    Blocker(
                        "topology-registry-drift",
                        f"stored {field}={stored.get(field)!r} differs from derived {field}={derived.get(field)!r}",
                    )
                )
    guarded_leases: list[dict[str, object]] = []
    for lease in leases:
        reasons, actual = guard_lease(context, lease)
        blockers.extend(reasons)
        projected = lease_projection(lease)
        projected["guard_valid"] = not reasons
        dependency_reasons = [item.as_dict() for item in reasons if item.code.startswith("dependency-")]
        projected["dependency_baseline_state"] = "stale" if dependency_reasons else "current"
        projected["dependency_blocking_reasons"] = dependency_reasons
        projected["dependency_refresh_required"] = bool(dependency_reasons)
        projected["actual"] = actual
        guarded_leases.append(projected)
    next_gate = "reconcile" if blockers else {
        "IDLE": "activate-local",
        "SINGLE_LOCAL": "create-worktree-or-continue-local",
        "PARALLEL": "continue-isolated-workspaces",
        "DRAINING": "continue-survivor-in-place",
    }[str(derived["phase"])]
    return {
        "schema_version": PUBLIC_SCHEMA,
        "command": "workspace-status",
        "action_level": "silent",
        "pushed": False,
        "project_root": str(context.project_root),
        "git_common_dir": str(context.common_dir),
        "phase": "blocked" if blockers else "ready",
        "topology": derived,
        "writer_leases": guarded_leases,
        "worktrees": worktrees,
        "journals": [journal_projection(item) for item in journals],
        "warnings": [],
        "blocking_reasons": [item.as_dict() for item in blockers],
        "next_gate": next_gate,
        "exclusions": list(EXCLUSIONS),
    }


def print_payload(value: Mapping[str, object], *, as_json: bool) -> None:
    if as_json:
        print(canonical_json(dict(value)).decode("utf-8"))
        return
    print(f"{str(value.get('command', 'workspace')).upper()} {value.get('phase', 'unknown')}")
    print(f"PROJECT_ROOT {value.get('project_root', '(unknown)')}")
    if value.get("operation_id"):
        print(f"OPERATION {value['operation_id']}")
    if value.get("plan_digest"):
        print(f"PLAN_DIGEST {value['plan_digest']}")
    for reason in value.get("blocking_reasons", []):
        if isinstance(reason, Mapping):
            print(f"BLOCKED {reason.get('code')}: {reason.get('message')}")


def error_payload(command: str, message: str) -> dict[str, object]:
    return {
        "schema_version": PUBLIC_SCHEMA,
        "command": command,
        "action_level": "silent",
        "pushed": False,
        "phase": "error",
        "blocking_reasons": [{"code": "workspace-error", "message": message}],
        "next_gate": "blocked",
        "exclusions": list(EXCLUSIONS),
    }


def add_activation_arguments(parser: argparse.ArgumentParser, *, apply: bool) -> None:
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--iteration", required=True)
    parser.add_argument("--execution-topology", required=True, choices=("local", "worktree"))
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--branch-ref", required=True)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--lease-generation", required=True, type=int)
    parser.add_argument(
        "--dependency-bindings-json",
        default="[]",
        help="Exact ordered stable-candidate bindings emitted by the coordinator",
    )
    parser.add_argument("--operation-id", required=apply)
    if apply:
        parser.add_argument("--accept-plan-digest", required=True)
    parser.add_argument("--json", action="store_true")


def dependency_bindings_argument(raw: str) -> tuple[dict[str, str], ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkspaceError("dependency bindings JSON is invalid") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise WorkspaceError("dependency bindings JSON must be an array of objects")
    return normalize_dependency_bindings(value)


def add_release_arguments(parser: argparse.ArgumentParser, *, apply: bool) -> None:
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--iteration", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--lease-generation", required=True, type=int)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--branch-ref", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--operation-id", required=apply)
    if apply:
        parser.add_argument("--accept-plan-digest", required=True)
    parser.add_argument("--json", action="store_true")


def add_bind_local_branch_arguments(parser: argparse.ArgumentParser, *, apply: bool) -> None:
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--iteration", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--lease-generation", required=True, type=int)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--new-branch-ref", required=True)
    parser.add_argument("--operation-id", required=apply)
    if apply:
        parser.add_argument("--accept-plan-digest", required=True)
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harness Lite Local/worktree orchestration safety slice")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="derive Local/Parallel/Draining workspace state")
    status_parser.add_argument("--project-root", required=True)
    status_parser.add_argument("--json", action="store_true")

    guard_parser = subparsers.add_parser("guard", help="validate writer/root/path/branch/base before mutation")
    guard_parser.add_argument("--project-root", required=True)
    guard_parser.add_argument("--iteration", required=True)
    guard_parser.add_argument("--owner", required=True)
    guard_parser.add_argument("--lease-generation", required=True, type=int)
    guard_parser.add_argument("--worktree-path", required=True)
    guard_parser.add_argument("--branch-ref", required=True)
    guard_parser.add_argument("--base-commit", required=True)
    guard_parser.add_argument("--json", action="store_true")

    plan_parser = subparsers.add_parser("plan", help="compute a zero-write workspace mutation manifest")
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command", required=True)
    plan_activate = plan_subparsers.add_parser("activate", help="plan Local activation or linked worktree add")
    add_activation_arguments(plan_activate, apply=False)
    plan_release = plan_subparsers.add_parser("release", help="plan writer release without workspace migration")
    add_release_arguments(plan_release, apply=False)
    plan_bind = plan_subparsers.add_parser(
        "bind-local-branch",
        help="plan an in-place dirty Local branch bind that releases main",
    )
    add_bind_local_branch_arguments(plan_bind, apply=False)

    activate_parser = subparsers.add_parser("activate", help="apply an accepted Local/worktree activation plan")
    add_activation_arguments(activate_parser, apply=True)
    release_parser = subparsers.add_parser("release", help="apply an accepted writer release plan")
    add_release_arguments(release_parser, apply=True)
    bind_parser = subparsers.add_parser(
        "bind-local-branch",
        help="atomically bind dirty Local HEAD to a PRD branch without checkout/stash/commit",
    )
    add_bind_local_branch_arguments(bind_parser, apply=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(arguments)
    as_json = bool(getattr(args, "json", False))
    command_label = str(args.command)
    try:
        if args.command == "status":
            payload = status_payload(resolve_repository(args.project_root))
        elif args.command == "guard":
            payload = guard_payload(
                resolve_repository(args.project_root),
                iteration=args.iteration,
                owner=args.owner,
                lease_generation=args.lease_generation,
                worktree_path=args.worktree_path,
                branch_ref=args.branch_ref,
                base_commit=args.base_commit,
            )
        elif args.command == "plan" and args.plan_command == "activate":
            command_label = "activate-workspace"
            payload = build_activation_plan(
                args.project_root,
                iteration=args.iteration,
                execution_topology=args.execution_topology,
                base_ref=args.base_ref,
                branch_ref=args.branch_ref,
                worktree_path=args.worktree_path,
                owner=args.owner,
                lease_generation=args.lease_generation,
                dependency_bindings=dependency_bindings_argument(args.dependency_bindings_json),
                operation_id=args.operation_id,
            ).as_dict()
        elif args.command == "activate":
            command_label = "activate-workspace"
            payload = apply_activation(
                args.project_root,
                iteration=args.iteration,
                execution_topology=args.execution_topology,
                base_ref=args.base_ref,
                branch_ref=args.branch_ref,
                worktree_path=args.worktree_path,
                owner=args.owner,
                lease_generation=args.lease_generation,
                operation_id=args.operation_id,
                accepted_plan_digest=args.accept_plan_digest,
                dependency_bindings=dependency_bindings_argument(args.dependency_bindings_json),
            )
        elif args.command == "plan" and args.plan_command == "release":
            command_label = "release-writer"
            payload = build_release_plan(
                args.project_root,
                iteration=args.iteration,
                owner=args.owner,
                lease_generation=args.lease_generation,
                worktree_path=args.worktree_path,
                branch_ref=args.branch_ref,
                base_commit=args.base_commit,
                operation_id=args.operation_id,
            ).as_dict()
        elif args.command == "plan" and args.plan_command == "bind-local-branch":
            command_label = "bind-local-branch"
            payload = build_bind_local_branch_plan(
                args.project_root,
                iteration=args.iteration,
                owner=args.owner,
                lease_generation=args.lease_generation,
                worktree_path=args.worktree_path,
                base_commit=args.base_commit,
                new_branch_ref=args.new_branch_ref,
                operation_id=args.operation_id,
            ).as_dict()
        elif args.command == "release":
            command_label = "release-writer"
            payload = apply_release(
                args.project_root,
                iteration=args.iteration,
                owner=args.owner,
                lease_generation=args.lease_generation,
                worktree_path=args.worktree_path,
                branch_ref=args.branch_ref,
                base_commit=args.base_commit,
                operation_id=args.operation_id,
                accepted_plan_digest=args.accept_plan_digest,
            )
        elif args.command == "bind-local-branch":
            command_label = "bind-local-branch"
            payload = apply_bind_local_branch(
                args.project_root,
                iteration=args.iteration,
                owner=args.owner,
                lease_generation=args.lease_generation,
                worktree_path=args.worktree_path,
                base_commit=args.base_commit,
                new_branch_ref=args.new_branch_ref,
                operation_id=args.operation_id,
                accepted_plan_digest=args.accept_plan_digest,
            )
        else:
            raise WorkspaceError("unsupported workspace command")
    except WorkspaceError as exc:
        payload = error_payload(command_label, str(exc))
    print_payload(payload, as_json=as_json)
    return 1 if payload.get("phase") in {"blocked", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
