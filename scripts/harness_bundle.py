#!/usr/bin/env python3
"""Create the v2 governance bundle for an already reserved iteration.

Planning is zero-write. Apply accepts the exact plan digest, binds the bundle
to the canonical reservation owner, serializes all writers for that allocation,
and journals exact before/after bytes. A retry can finish a crash-partial apply;
an ordinary failure rolls back only bytes still owned by this operation. This
adapter never creates branches/worktrees, commits, merges, or pushes.
"""

from __future__ import annotations

import argparse
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
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

try:
    from . import project_harness as core
except ImportError:  # pragma: no cover - direct execution
    import project_harness as core


PLAN_SCHEMA = "harness-lite.bundle-plan/v2"
JOURNAL_SCHEMA = "harness-lite.bundle-journal/v2"
PUBLIC_SCHEMA = "harness-lite.bundle-operation/v1"
OP_RE = re.compile(r"OP-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
OID_RE = re.compile(r"[0-9a-f]{40,64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
GIT_ENVIRONMENT_OVERRIDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


class BundleError(RuntimeError):
    """Raised when bundle creation cannot prove every write safe."""


@dataclass(frozen=True)
class FileMutation:
    path: str
    before_sha256: str | None
    after_sha256: str
    before_base64: str | None
    after_base64: str


@dataclass(frozen=True)
class ReservationIdentity:
    allocation_ref: str
    allocation_object: str
    base_ref: str
    base_commit: str
    base_branch: str
    title: str
    owner_operation_id: str
    owner_plan_digest: str
    governance_ref: str
    governance_commit: str
    governance_tree: str
    principle_sha256: str


@dataclass(frozen=True)
class BundlePlan:
    schema_version: str
    command: str
    action_level: str
    pushed: bool
    operation_id: str
    project_root: str
    iteration: str
    allocation_ref: str
    allocation_object: str
    base_ref: str
    base_commit: str
    base_branch: str
    reservation_operation_id: str
    reservation_plan_digest: str
    governance_ref: str
    governance_commit: str
    governance_tree: str
    principle_sha256: str
    title: str
    planned_at: str
    files: tuple[FileMutation, ...]
    plan_digest: str
    phase: str
    blocking_reasons: tuple[str, ...]
    next_gate: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str, *, label: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise BundleError(f"{label} is not canonical base64") from exc


def _git(root: Path, arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("git")
    if not executable:
        raise BundleError("Git is required")
    environment = os.environ.copy()
    for name in GIT_ENVIRONMENT_OVERRIDES:
        environment.pop(name, None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        [executable, "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise BundleError(message or f"Git exited with {result.returncode}")
    return result


def _text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="strict").strip()


def _root(value: str | Path) -> Path:
    supplied = Path(value).expanduser().resolve()
    if not supplied.is_dir():
        raise BundleError(f"project root is not an existing directory: {supplied}")
    result = _git(supplied, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise BundleError("project root is not a Git worktree")
    actual = Path(_text(result)).resolve()
    if os.path.normcase(str(actual)) != os.path.normcase(str(supplied)):
        raise BundleError(f"project root must name the exact worktree root: {actual}")
    return actual


def _common(root: Path) -> Path:
    raw = Path(_text(_git(root, ["rev-parse", "--git-common-dir"])))
    return (raw if raw.is_absolute() else root / raw).resolve()


def _iteration(value: str) -> str:
    number = value.strip()
    if not ITERATION_RE.fullmatch(number) or number != f"{int(number):03d}":
        raise BundleError("iteration must be a canonical NNN identity")
    return number


def _operation(value: str) -> str:
    operation = value.strip()
    if not OP_RE.fullmatch(operation):
        raise BundleError("operation ID is invalid")
    return operation


def _planned_at(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BundleError("planned_at must include an explicit timezone")
    return value.isoformat(timespec="seconds")


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def _read_reservation(root: Path, number: str) -> ReservationIdentity:
    git = shutil.which("git") or "git"
    records = core.git_ref_records(git, root)
    allocation_ref = f"refs/project-harness/v2/allocations/{number}"
    base_ref = f"refs/project-harness/v2/iterations/{number}/base"
    allocation = records.get(allocation_ref)
    base = records.get(base_ref)
    if allocation is None or base is None:
        raise BundleError(f"PRD-{number} does not have a complete v2 reservation")
    allocation_oid, allocation_type = allocation
    base_commit, base_type = base
    if allocation_type != "blob" or base_type != "commit":
        raise BundleError("reservation refs have invalid Git object types")
    try:
        metadata = core.read_allocation_metadata(git, root, allocation_oid)
    except core.HarnessError as exc:
        raise BundleError(f"allocation metadata is invalid: {exc}") from exc
    if metadata["iteration"] != number or metadata["base_commit"] != base_commit:
        raise BundleError("allocation metadata and immutable base disagree")
    owner = str(metadata["operation_id"])
    try:
        journal, _ = core.load_operation_journal(_common(root), owner)
    except core.HarnessError as exc:
        raise BundleError(f"reservation owner journal is unavailable or invalid: {exc}") from exc
    mismatches: list[str] = []
    if journal.phase != "READY":
        mismatches.append("phase")
    if journal.plan_digest != metadata["plan_digest"]:
        mismatches.append("plan digest")
    if journal.iteration != number:
        mismatches.append("iteration")
    if journal.allocation_object != allocation_oid:
        mismatches.append("allocation object")
    if journal.base_commit != base_commit:
        mismatches.append("base commit")
    if journal.base_branch != metadata["base_branch"]:
        mismatches.append("base ref")
    if journal.governance_ref != metadata["governance_ref"]:
        mismatches.append("governance ref")
    if journal.governance_commit != metadata["governance_commit"]:
        mismatches.append("governance commit")
    if journal.principle_sha256 != metadata["principle_sha256"]:
        mismatches.append("principle hash")
    if not _same_path(journal.project_root, root):
        mismatches.append("project root")
    expected_refs = (allocation_ref, base_ref)
    if journal.created_refs != expected_refs or journal.expected_refs != expected_refs:
        mismatches.append("created refs")
    if mismatches:
        raise BundleError("reservation owner evidence disagrees: " + ", ".join(mismatches))
    return ReservationIdentity(
        allocation_ref=allocation_ref,
        allocation_object=allocation_oid,
        base_ref=base_ref,
        base_commit=base_commit,
        base_branch=str(metadata["base_branch"]),
        title=str(metadata["title"]),
        owner_operation_id=owner,
        owner_plan_digest=str(metadata["plan_digest"]),
        governance_ref=str(metadata["governance_ref"]),
        governance_commit=str(metadata["governance_commit"]),
        governance_tree=str(metadata["governance_tree"]),
        principle_sha256=str(metadata["principle_sha256"]),
    )


def _verify_live_governance(root: Path, reservation: ReservationIdentity) -> None:
    git = shutil.which("git") or "git"
    try:
        governance_ref, commit, snapshot = core.committed_governance_snapshot(
            git, root, reservation.governance_ref
        )
    except core.HarnessError as exc:
        raise BundleError(f"committed governance snapshot is invalid: {exc}") from exc
    if (
        governance_ref != reservation.governance_ref
        or commit != reservation.governance_commit
        or snapshot.get("tree") != reservation.governance_tree
        or snapshot.get("principle_sha256") != reservation.principle_sha256
    ):
        raise BundleError("committed governance changed after reservation")
    head = _text(_git(root, ["rev-parse", "--verify", "HEAD^{commit}"]))
    branch = _git(root, ["symbolic-ref", "-q", "HEAD"], check=False)
    if head != reservation.governance_commit or branch.returncode or _text(branch) != reservation.governance_ref:
        raise BundleError("bundle creation requires the exact reserved governance checkout")
    blobs = snapshot.get("blobs")
    if not isinstance(blobs, Mapping):
        raise BundleError("committed governance blob map is invalid")
    for relative, expected_blob in blobs.items():
        path = root / str(relative)
        if not path.is_file():
            raise BundleError(f"live governance file is missing: {relative}")
        actual_blob = core.hash_git_blob(git, root, path.read_bytes(), write=False)
        if actual_blob != expected_blob:
            raise BundleError(f"live governance differs from the reserved commit: {relative}")


def _op_bytes(operations: Sequence[core.Operation], root: Path) -> tuple[FileMutation, ...]:
    result: list[FileMutation] = []
    for operation in operations:
        relative = operation.path.resolve(strict=False).relative_to(root).as_posix()
        result.append(
            FileMutation(
                path=relative,
                before_sha256=_sha(operation.old_raw) if operation.old_raw is not None else None,
                after_sha256=_sha(operation.new_raw),
                before_base64=_b64(operation.old_raw) if operation.old_raw is not None else None,
                after_base64=_b64(operation.new_raw),
            )
        )
    return tuple(result)


def _plan_payload(
    *,
    root: Path,
    operation: str,
    number: str,
    reservation: ReservationIdentity,
    planned_at_text: str,
    files: Sequence[FileMutation],
    blockers: Sequence[str],
) -> dict[str, object]:
    return {
        "schema_version": PLAN_SCHEMA,
        "operation_id": operation,
        "project_root": str(root),
        "iteration": number,
        "allocation_ref": reservation.allocation_ref,
        "allocation_object": reservation.allocation_object,
        "base_ref": reservation.base_ref,
        "base_commit": reservation.base_commit,
        "base_branch": reservation.base_branch,
        "reservation_operation_id": reservation.owner_operation_id,
        "reservation_plan_digest": reservation.owner_plan_digest,
        "governance_ref": reservation.governance_ref,
        "governance_commit": reservation.governance_commit,
        "governance_tree": reservation.governance_tree,
        "principle_sha256": reservation.principle_sha256,
        "title": reservation.title,
        "planned_at": planned_at_text,
        "files": [asdict(item) for item in files],
        "blockers": list(blockers),
    }


def plan_bundle(
    project_root: str | Path,
    *,
    iteration: str,
    operation_id: str | None = None,
    planned_at: datetime | None = None,
) -> BundlePlan:
    root = _root(project_root)
    number = _iteration(iteration)
    operation = _operation(operation_id or f"OP-{uuid.uuid4().hex}")
    reservation = _read_reservation(root, number)
    render_time = planned_at or datetime.now().astimezone()
    planned_at_text = _planned_at(render_time)
    expected_numbers = core.find_existing_numbers(root / "harness" / "iterations")
    if number in {f"{value:03d}" for value in expected_numbers}:
        blockers = ("iteration-bundle-already-present",)
        files: tuple[FileMutation, ...] = ()
    else:
        _verify_live_governance(root, reservation)
        expected_next = (max(expected_numbers) + 1) if expected_numbers else 1
        blockers_list: list[str] = []
        if number != f"{expected_next:03d}":
            blockers_list.append(f"reserved-id-not-next-bundle:{number}/{expected_next:03d}")
        rendered_number, operations = core.build_new_iteration_operations(
            root,
            reservation.title,
            render_time,
            reservation.base_commit,
            reservation.base_branch,
            v2_progress_operation_id=operation,
            v2_progress_source_ref=reservation.governance_ref,
            v2_progress_source_commit=reservation.governance_commit,
            v2_progress_evidence_refs=(
                f"allocation-ref:{reservation.allocation_ref}",
                f"allocation-object:{reservation.allocation_object}",
                f"base-ref:{reservation.base_ref}",
                f"base-commit:{reservation.base_commit}",
                f"governance-ref:{reservation.governance_ref}",
                f"governance-commit:{reservation.governance_commit}",
            ),
        )
        if rendered_number != number:
            blockers_list.append(f"renderer-id-mismatch:{rendered_number}/{number}")
        files = _op_bytes(operations, root)
        blockers = tuple(blockers_list)
    payload = _plan_payload(
        root=root,
        operation=operation,
        number=number,
        reservation=reservation,
        planned_at_text=planned_at_text,
        files=files,
        blockers=blockers,
    )
    digest = _sha(_canonical(payload))
    return BundlePlan(
        schema_version=PLAN_SCHEMA,
        command="create-v2-bundle",
        action_level="silent",
        pushed=False,
        operation_id=operation,
        project_root=str(root),
        iteration=number,
        allocation_ref=reservation.allocation_ref,
        allocation_object=reservation.allocation_object,
        base_ref=reservation.base_ref,
        base_commit=reservation.base_commit,
        base_branch=reservation.base_branch,
        reservation_operation_id=reservation.owner_operation_id,
        reservation_plan_digest=reservation.owner_plan_digest,
        governance_ref=reservation.governance_ref,
        governance_commit=reservation.governance_commit,
        governance_tree=reservation.governance_tree,
        principle_sha256=reservation.principle_sha256,
        title=reservation.title,
        planned_at=planned_at_text,
        files=files,
        plan_digest=digest,
        phase="blocked" if blockers else "planned",
        blocking_reasons=blockers,
        next_gate="reconcile" if blockers else "accept-plan-digest",
    )


def _registry(common: Path) -> Path:
    return common / "project-harness" / "bundle" / "v2"


def _journal(common: Path, operation: str) -> Path:
    return _registry(common) / "journal" / f"{_operation(operation)}.json"


def _ensure_operational_path(path: Path, common: Path) -> None:
    resolved_common = common.resolve()
    try:
        path.absolute().relative_to(resolved_common)
        path.resolve(strict=False).relative_to(resolved_common)
    except ValueError as exc:
        raise BundleError(f"operational path escapes Git common directory: {path}") from exc
    current = path
    while current != resolved_common:
        if current.exists():
            is_junction = getattr(current, "is_junction", lambda: False)
            if current.is_symlink() or is_junction():
                raise BundleError(f"operational path crosses a link or junction: {current}")
        if current.parent == current:
            raise BundleError(f"cannot prove operational path containment: {path}")
        current = current.parent


@contextlib.contextmanager
def _allocation_lock(common: Path, number: str):
    path = _registry(common) / "locks" / f"allocation-{_iteration(number)}.lock"
    _ensure_operational_path(path, common)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_operational_path(path.parent, common)
    handle = path.open("a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"0")
        handle.flush()
        os.fsync(handle.fileno())
    acquired = False
    try:
        deadline = time.monotonic() + 30
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - Windows is the primary integration target
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise BundleError("timed out waiting for allocation-scoped bundle lock") from exc
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, object], common: Path) -> None:
    _ensure_operational_path(path, common)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_operational_path(path.parent, common)
    raw = _canonical(value) + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _expected_paths(number: str) -> set[str]:
    prefix = f"harness/iterations/{number}"
    return {
        "harness/README.md",
        "harness/progress.md",
        f"{prefix}/README.md",
        f"{prefix}/prd-{number}.md",
        f"{prefix}/spec-{number}.md",
        f"{prefix}/deviation-{number}.md",
    }


def _mutation_from_dict(value: object, *, number: str) -> FileMutation:
    fields = {"path", "before_sha256", "after_sha256", "before_base64", "after_base64"}
    if not isinstance(value, dict) or set(value) != fields:
        raise BundleError("bundle journal file mutation schema is invalid")
    path = value.get("path")
    before_sha = value.get("before_sha256")
    after_sha = value.get("after_sha256")
    before_b64 = value.get("before_base64")
    after_b64 = value.get("after_base64")
    if not isinstance(path, str) or path not in _expected_paths(number) or "\\" in path:
        raise BundleError(f"bundle journal path is not managed for PRD-{number}: {path!r}")
    if before_sha is not None and (not isinstance(before_sha, str) or not DIGEST_RE.fullmatch(before_sha)):
        raise BundleError(f"bundle before hash is invalid: {path}")
    if not isinstance(after_sha, str) or not DIGEST_RE.fullmatch(after_sha):
        raise BundleError(f"bundle after hash is invalid: {path}")
    if before_sha is None:
        if before_b64 is not None:
            raise BundleError(f"new bundle path unexpectedly stores before bytes: {path}")
    else:
        if not isinstance(before_b64, str) or _sha(_unb64(before_b64, label=f"before bytes for {path}")) != before_sha:
            raise BundleError(f"bundle before bytes do not match their hash: {path}")
    if not isinstance(after_b64, str) or _sha(_unb64(after_b64, label=f"after bytes for {path}")) != after_sha:
        raise BundleError(f"bundle after bytes do not match their hash: {path}")
    return FileMutation(path, before_sha, after_sha, before_b64, after_b64)


def _load_journal(
    path: Path,
    *,
    common: Path,
    root: Path,
    operation: str,
    number: str,
    digest: str,
    planned_at_text: str,
    reservation: ReservationIdentity,
) -> tuple[dict[str, object], tuple[FileMutation, ...]]:
    _ensure_operational_path(path, common)
    try:
        if path.stat().st_size > MAX_JOURNAL_BYTES:
            raise BundleError(f"bundle journal exceeds its safe size limit: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"bundle journal is corrupt: {path}") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "phase", "error", "plan_digest", "plan"}:
        raise BundleError("bundle journal schema is invalid")
    if value.get("schema_version") != JOURNAL_SCHEMA or value.get("phase") not in {
        "PLANNED",
        "READY",
        "FAILED_NEEDS_RECONCILE",
    }:
        raise BundleError("bundle journal identity or phase is invalid")
    if value.get("plan_digest") != digest:
        raise BundleError("bundle journal does not match accepted plan")
    plan = value.get("plan")
    expected_plan_fields = {
        "schema_version",
        "operation_id",
        "project_root",
        "iteration",
        "allocation_ref",
        "allocation_object",
        "base_ref",
        "base_commit",
        "base_branch",
        "reservation_operation_id",
        "reservation_plan_digest",
        "governance_ref",
        "governance_commit",
        "governance_tree",
        "principle_sha256",
        "title",
        "planned_at",
        "files",
        "blockers",
    }
    if not isinstance(plan, dict) or set(plan) != expected_plan_fields or plan.get("schema_version") != PLAN_SCHEMA:
        raise BundleError("bundle journal plan schema is invalid")
    if _sha(_canonical(plan)) != digest:
        raise BundleError("bundle journal plan payload does not match its digest")
    identity = {
        "operation_id": operation,
        "iteration": number,
        "allocation_ref": reservation.allocation_ref,
        "allocation_object": reservation.allocation_object,
        "base_ref": reservation.base_ref,
        "base_commit": reservation.base_commit,
        "base_branch": reservation.base_branch,
        "reservation_operation_id": reservation.owner_operation_id,
        "reservation_plan_digest": reservation.owner_plan_digest,
        "governance_ref": reservation.governance_ref,
        "governance_commit": reservation.governance_commit,
        "governance_tree": reservation.governance_tree,
        "principle_sha256": reservation.principle_sha256,
        "title": reservation.title,
        "planned_at": planned_at_text,
    }
    for key, expected in identity.items():
        if plan.get(key) != expected:
            raise BundleError(f"bundle journal {key} differs from the accepted request/reservation")
    if not isinstance(plan.get("project_root"), str) or not _same_path(str(plan["project_root"]), root):
        raise BundleError("bundle journal belongs to another worktree root")
    if plan.get("blockers") != []:
        raise BundleError("bundle journal contains a blocked plan")
    raw_files = plan.get("files")
    if not isinstance(raw_files, list):
        raise BundleError("bundle journal files must be an array")
    files = tuple(_mutation_from_dict(item, number=number) for item in raw_files)
    if len(files) != len(_expected_paths(number)) or {item.path for item in files} != _expected_paths(number):
        raise BundleError("bundle journal does not contain the exact managed path set")
    error = value.get("error")
    if value["phase"] == "FAILED_NEEDS_RECONCILE":
        if not isinstance(error, str) or not error:
            raise BundleError("failed bundle journal lacks an error summary")
    elif error is not None:
        raise BundleError("non-failed bundle journal unexpectedly contains an error")
    return value, files


def _target(root: Path, item: FileMutation) -> Path:
    path = root / Path(item.path)
    core.ensure_inside_root(path, root)
    return path


def _raw_before(item: FileMutation) -> bytes | None:
    return None if item.before_base64 is None else _unb64(item.before_base64, label=f"before bytes for {item.path}")


def _raw_after(item: FileMutation) -> bytes:
    return _unb64(item.after_base64, label=f"after bytes for {item.path}")


def _states(root: Path, files: Sequence[FileMutation]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in files:
        path = _target(root, item)
        if path.exists() and not path.is_file():
            result[item.path] = "unknown"
            continue
        current = path.read_bytes() if path.is_file() else None
        actual = _sha(current) if current is not None else None
        if actual == item.before_sha256:
            result[item.path] = "before"
        elif actual == item.after_sha256:
            result[item.path] = "after"
        else:
            result[item.path] = "unknown"
    return result


def _verify_plan_files(root: Path, files: Sequence[FileMutation], *, expect_after: bool = False) -> None:
    expected = "after" if expect_after else "before"
    states = _states(root, files)
    for item in files:
        if states[item.path] != expected:
            wanted = item.after_sha256 if expect_after else item.before_sha256
            raise BundleError(f"bundle file drifted: {item.path}; expected {wanted}, state={states[item.path]}")


def _atomic_bytes(path: Path, raw: bytes, root: Path) -> None:
    core.ensure_inside_root(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    core.ensure_inside_root(path.parent, root)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _rollback_exact(root: Path, files: Sequence[FileMutation]) -> list[str]:
    errors: list[str] = []
    for item in reversed(tuple(files)):
        try:
            path = _target(root, item)
            current = path.read_bytes() if path.is_file() else None
            actual = _sha(current) if current is not None else None
            if actual == item.before_sha256:
                continue
            if actual != item.after_sha256:
                errors.append(f"changed outside operation: {item.path}")
                continue
            before = _raw_before(item)
            if before is None:
                path.unlink()
                _fsync_directory(path.parent)
            else:
                _atomic_bytes(path, before, root)
        except (OSError, core.HarnessError, BundleError) as exc:
            errors.append(f"{item.path}: {exc}")
    # The renderer creates exactly one new iteration directory. Parent
    # directories may have existed as meaningful empty structure and are not
    # operation-owned, so never remove above this exact target.
    iteration_directories = {
        (root / item.path).parent
        for item in files
        if item.before_sha256 is None and item.path.startswith("harness/iterations/")
    }
    for directory in sorted(iteration_directories, key=lambda value: len(value.parts), reverse=True):
        with contextlib.suppress(OSError):
            directory.rmdir()
    return errors


def _failed_journal(
    journal: dict[str, object], path: Path, common: Path, message: str
) -> None:
    failed = dict(journal)
    failed["phase"] = "FAILED_NEEDS_RECONCILE"
    failed["error"] = message[:1000] or "unknown bundle failure"
    _atomic_json(path, failed, common)


def apply_bundle(
    project_root: str | Path,
    *,
    iteration: str,
    operation_id: str,
    accepted_plan_digest: str,
    planned_at: datetime,
) -> dict[str, object]:
    root = _root(project_root)
    number = _iteration(iteration)
    operation = _operation(operation_id)
    digest = accepted_plan_digest.strip()
    if not DIGEST_RE.fullmatch(digest):
        raise BundleError("accepted plan digest is invalid")
    planned_at_text = _planned_at(planned_at)
    common = _common(root)
    path = _journal(common, operation)
    with _allocation_lock(common, number):
        reservation = _read_reservation(root, number)
        if path.is_file():
            journal, files = _load_journal(
                path,
                common=common,
                root=root,
                operation=operation,
                number=number,
                digest=digest,
                planned_at_text=planned_at_text,
                reservation=reservation,
            )
            if journal["phase"] == "READY":
                _verify_plan_files(root, files, expect_after=True)
                report = core.collect_validation(root)
                if report.errors:
                    raise BundleError("ready v2 bundle no longer passes Harness validation")
                return _result(journal, created=False)
            if journal["phase"] == "FAILED_NEEDS_RECONCILE":
                raise BundleError("bundle operation requires reconcile")
        else:
            plan = plan_bundle(root, iteration=number, operation_id=operation, planned_at=planned_at)
            if plan.phase != "planned" or plan.plan_digest != digest:
                raise BundleError("bundle plan changed or is blocked; create a new plan")
            payload = _plan_payload(
                root=root,
                operation=operation,
                number=number,
                reservation=reservation,
                planned_at_text=plan.planned_at,
                files=plan.files,
                blockers=(),
            )
            journal = {
                "schema_version": JOURNAL_SCHEMA,
                "phase": "PLANNED",
                "error": None,
                "plan_digest": plan.plan_digest,
                "plan": payload,
            }
            _atomic_json(path, journal, common)
            files = plan.files

        states = _states(root, files)
        unknown = [name for name, state in states.items() if state == "unknown"]
        if unknown:
            message = "bundle paths differ from both accepted before/after bytes: " + ", ".join(unknown)
            _failed_journal(journal, path, common, message)
            raise BundleError(message)
        try:
            pending = [item for item in files if states[item.path] == "before"]
            operations = [
                core.Operation(_target(root, item), _raw_after(item), _raw_before(item)) for item in pending
            ]
            if operations:
                core.apply_operations(root, operations)
            _verify_plan_files(root, files, expect_after=True)
            report = core.collect_validation(root)
            if report.errors:
                raise BundleError("created v2 bundle failed Harness validation")
            ready = dict(journal)
            ready["phase"] = "READY"
            ready["error"] = None
            _atomic_json(path, ready, common)
            return _result(ready, created=True)
        except Exception as exc:
            current = _states(root, files)
            if all(state in {"before", "after"} for state in current.values()):
                rollback_errors = _rollback_exact(root, files)
            else:
                rollback_errors = ["one or more paths no longer match operation-owned bytes"]
            if rollback_errors:
                _failed_journal(journal, path, common, f"{exc}; rollback: {'; '.join(rollback_errors)}")
            else:
                # PLANNED plus exact-before state is safely retriable. Do not
                # turn a transient replace/validation error into a fake success.
                _atomic_json(path, journal, common)
            raise


def _result(journal: Mapping[str, object], *, created: bool) -> dict[str, object]:
    plan = journal["plan"]
    if not isinstance(plan, Mapping):  # pragma: no cover - strict loader guarantees this
        raise BundleError("bundle journal plan is invalid")
    files = plan["files"]
    if not isinstance(files, list):  # pragma: no cover
        raise BundleError("bundle journal file manifest is invalid")
    return {
        "schema_version": PUBLIC_SCHEMA,
        "command": "create-v2-bundle",
        "action_level": "silent",
        "pushed": False,
        "phase": "succeeded",
        "operation_id": plan["operation_id"],
        "iteration": plan["iteration"],
        "plan_digest": journal["plan_digest"],
        "journal_phase": journal["phase"],
        "created_now": created,
        "idempotent_replay": not created,
        "paths": [item["path"] for item in files],
        "next_gate": "complete-and-approve-prd",
        "exclusions": ["no branch", "no worktree", "no commit", "no merge", "no push"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--project-root", required=True)
    plan.add_argument("--iteration", required=True)
    plan.add_argument("--operation-id")
    plan.add_argument("--planned-at", help="ISO timestamp with timezone; returned value must be reused by apply")
    plan.add_argument("--json", action="store_true")
    apply = sub.add_parser("apply")
    apply.add_argument("--project-root", required=True)
    apply.add_argument("--iteration", required=True)
    apply.add_argument("--operation-id", required=True)
    apply.add_argument("--accept-plan-digest", required=True)
    apply.add_argument("--planned-at", required=True, help="exact planned_at returned by plan")
    apply.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            timestamp = datetime.fromisoformat(args.planned_at) if args.planned_at else None
            payload = plan_bundle(
                args.project_root,
                iteration=args.iteration,
                operation_id=args.operation_id,
                planned_at=timestamp,
            ).as_dict()
        else:
            payload = apply_bundle(
                args.project_root,
                iteration=args.iteration,
                operation_id=args.operation_id,
                accepted_plan_digest=args.accept_plan_digest,
                planned_at=datetime.fromisoformat(args.planned_at),
            )
    except (BundleError, core.HarnessError, ValueError) as exc:
        payload = {
            "schema_version": PUBLIC_SCHEMA,
            "command": "create-v2-bundle",
            "action_level": "silent",
            "pushed": False,
            "phase": "blocked",
            "blocking_reasons": [str(exc)],
            "next_gate": "reconcile-or-replan",
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if payload.get("phase") in {"planned", "succeeded"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
