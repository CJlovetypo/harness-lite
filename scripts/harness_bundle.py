#!/usr/bin/env python3
"""Create the v2 governance bundle for an already reserved iteration.

This adapter closes the reservation-to-bundle lifecycle gap without changing
the immutable v2 identity.  Plan is zero-write.  Apply accepts the exact plan
digest, uses a per-operation OS lock and durable journal, checks every expected
file hash before the first write, writes with atomic replace, and can be safely
retried.  It never creates branches/worktrees, commits, merges, or pushes.
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
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

try:
    from . import project_harness as core
except ImportError:  # pragma: no cover - direct execution
    import project_harness as core


PLAN_SCHEMA = "harness-lite.bundle-plan/v1"
JOURNAL_SCHEMA = "harness-lite.bundle-journal/v1"
PUBLIC_SCHEMA = "harness-lite.bundle-operation/v1"
OP_RE = re.compile(r"OP-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")


class BundleError(RuntimeError):
    """Raised when bundle creation cannot prove all writes safe."""


@dataclass(frozen=True)
class FileMutation:
    path: str
    before_sha256: str | None
    after_sha256: str
    content_utf8: str


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


def _git(root: Path, arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("git")
    if not executable:
        raise BundleError("Git is required")
    environment = os.environ.copy()
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


def _read_allocation(root: Path, number: str) -> tuple[str, str, str, str, str]:
    allocation_ref = f"refs/project-harness/v2/allocations/{number}"
    base_ref = f"refs/project-harness/v2/iterations/{number}/base"
    allocation = _git(root, ["show-ref", "--verify", "--hash", allocation_ref], check=False)
    base = _git(root, ["show-ref", "--verify", "--hash", base_ref], check=False)
    if allocation.returncode or base.returncode:
        raise BundleError(f"PRD-{number} does not have a complete v2 reservation")
    allocation_oid = _text(allocation)
    base_commit = _text(base)
    if _text(_git(root, ["cat-file", "-t", allocation_oid])) != "blob":
        raise BundleError("allocation ref must point to metadata blob")
    raw = _git(root, ["cat-file", "-p", allocation_oid]).stdout
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError("allocation metadata is invalid") from exc
    if not isinstance(metadata, dict):
        raise BundleError("allocation metadata must be an object")
    required = {"iteration", "base_commit", "base_branch", "title", "operation_id", "plan_digest"}
    if not required.issubset(metadata):
        raise BundleError("allocation metadata lacks required identity fields")
    if metadata["iteration"] != number or metadata["base_commit"] != base_commit:
        raise BundleError("allocation metadata and immutable base disagree")
    return allocation_ref, allocation_oid, base_ref, base_commit, str(metadata["base_branch"])


def _op_bytes(operations: Sequence[core.Operation], root: Path) -> tuple[FileMutation, ...]:
    result: list[FileMutation] = []
    for operation in operations:
        relative = operation.path.resolve(strict=False).relative_to(root).as_posix()
        result.append(
            FileMutation(
                path=relative,
                before_sha256=_sha(operation.old_raw) if operation.old_raw is not None else None,
                after_sha256=_sha(operation.new_raw),
                content_utf8=operation.new_raw.decode("utf-8-sig"),
            )
        )
    return tuple(result)


def plan_bundle(
    project_root: str | Path,
    *,
    iteration: str,
    operation_id: str | None = None,
    planned_at: datetime | None = None,
) -> BundlePlan:
    root = _root(project_root)
    number = iteration.strip()
    if not ITERATION_RE.fullmatch(number) or number != f"{int(number):03d}":
        raise BundleError("iteration must be a canonical NNN identity")
    operation = operation_id or f"OP-{uuid.uuid4().hex}"
    if not OP_RE.fullmatch(operation):
        raise BundleError("operation ID is invalid")
    allocation_ref, allocation_oid, base_ref, base_commit, base_branch = _read_allocation(root, number)
    records = core.git_ref_records(shutil.which("git") or "git", root)
    metadata = core.read_allocation_metadata(shutil.which("git") or "git", root, allocation_oid)
    title = str(metadata["title"])
    expected_numbers = core.find_existing_numbers(root / "harness" / "iterations")
    render_time = planned_at or datetime.now().astimezone()
    if render_time.tzinfo is None or render_time.utcoffset() is None:
        raise BundleError("planned_at must include an explicit timezone")
    planned_at_text = render_time.isoformat(timespec="seconds")
    if number in {f"{value:03d}" for value in expected_numbers}:
        blockers = ("iteration-bundle-already-present",)
        files: tuple[FileMutation, ...] = ()
    else:
        expected_next = (max(expected_numbers) + 1) if expected_numbers else 1
        blockers_list: list[str] = []
        if number != f"{expected_next:03d}":
            blockers_list.append(f"reserved-id-not-next-bundle:{number}/{expected_next:03d}")
        # Require the reserved identity pair still exist exactly before using
        # the existing renderer. It derives the number from bundles, not refs.
        if records.get(allocation_ref, (None,))[0] != allocation_oid or records.get(base_ref, (None,))[0] != base_commit:
            blockers_list.append("reservation-ref-drift")
        rendered_number, operations = core.build_new_iteration_operations(
            root,
            title,
            render_time,
            base_commit,
            base_branch,
        )
        if rendered_number != number:
            blockers_list.append(f"renderer-id-mismatch:{rendered_number}/{number}")
        files = _op_bytes(operations, root)
        blockers = tuple(blockers_list)
    digest_payload = {
        "schema_version": PLAN_SCHEMA,
        "operation_id": operation,
        "project_root": str(root),
        "iteration": number,
        "allocation_ref": allocation_ref,
        "allocation_object": allocation_oid,
        "base_ref": base_ref,
        "base_commit": base_commit,
        "title": title,
        "planned_at": planned_at_text,
        "files": [asdict(item) for item in files],
        "blockers": list(blockers),
    }
    digest = _sha(_canonical(digest_payload))
    return BundlePlan(
        schema_version=PLAN_SCHEMA,
        command="create-v2-bundle",
        action_level="silent",
        pushed=False,
        operation_id=operation,
        project_root=str(root),
        iteration=number,
        allocation_ref=allocation_ref,
        allocation_object=allocation_oid,
        base_ref=base_ref,
        base_commit=base_commit,
        title=title,
        planned_at=planned_at_text,
        files=files,
        plan_digest=digest,
        phase="blocked" if blockers else "planned",
        blocking_reasons=blockers,
        next_gate="reconcile" if blockers else "accept-plan-digest",
    )


def _registry(common: Path) -> Path:
    return common / "project-harness" / "bundle" / "v1"


def _journal(common: Path, operation: str) -> Path:
    return _registry(common) / "journal" / f"{operation}.json"


@contextlib.contextmanager
def _lock(common: Path, operation: str):
    path = _registry(common) / "locks" / f"{operation}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
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
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise BundleError("timed out waiting for bundle operation lock")
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


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical(value) + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _verify_plan_files(root: Path, files: Sequence[FileMutation], *, expect_after: bool = False) -> None:
    for item in files:
        path = (root / item.path).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BundleError(f"planned path escapes project root: {item.path}") from exc
        current = path.read_bytes() if path.is_file() else None
        expected = item.after_sha256 if expect_after else item.before_sha256
        actual = _sha(current) if current is not None else None
        if actual != expected:
            raise BundleError(f"bundle file drifted: {item.path}; expected {expected}, found {actual}")


def apply_bundle(
    project_root: str | Path,
    *,
    iteration: str,
    operation_id: str,
    accepted_plan_digest: str,
    planned_at: datetime,
) -> dict[str, object]:
    root = _root(project_root)
    if not DIGEST_RE.fullmatch(accepted_plan_digest):
        raise BundleError("accepted plan digest is invalid")
    common = _common(root)
    path = _journal(common, operation_id)
    with _lock(common, operation_id):
        if path.is_file():
            try:
                journal = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BundleError(f"bundle journal is corrupt: {path}") from exc
            if journal.get("schema_version") != JOURNAL_SCHEMA or journal.get("plan_digest") != accepted_plan_digest:
                raise BundleError("bundle journal does not match accepted plan")
            files = tuple(FileMutation(**item) for item in journal["files"])
            if journal.get("phase") == "READY":
                _verify_plan_files(root, files, expect_after=True)
                return _result(journal, created=False)
            if journal.get("phase") == "FAILED_NEEDS_RECONCILE":
                raise BundleError("bundle operation requires reconcile")
        else:
            plan = plan_bundle(root, iteration=iteration, operation_id=operation_id, planned_at=planned_at)
            if plan.phase != "planned" or plan.plan_digest != accepted_plan_digest:
                raise BundleError("bundle plan changed or is blocked; create a new plan")
            journal = {
                "schema_version": JOURNAL_SCHEMA,
                "operation_id": operation_id,
                "plan_digest": plan.plan_digest,
                "iteration": plan.iteration,
                "allocation_ref": plan.allocation_ref,
                "allocation_object": plan.allocation_object,
                "base_ref": plan.base_ref,
                "base_commit": plan.base_commit,
                "planned_at": plan.planned_at,
                "files": [asdict(item) for item in plan.files],
                "phase": "PLANNED",
                "error": None,
            }
            _atomic_json(path, journal)
            files = plan.files
        try:
            _verify_plan_files(root, files)
            operations: list[core.Operation] = []
            for item in files:
                target = root / item.path
                old = target.read_bytes() if target.is_file() else None
                operations.append(core.Operation(target, item.content_utf8.encode("utf-8"), old))
            core.apply_operations(root, operations)
            _verify_plan_files(root, files, expect_after=True)
            report = core.collect_validation(root)
            if report.errors:
                raise BundleError("created v2 bundle failed Harness validation")
            journal["phase"] = "READY"
            _atomic_json(path, journal)
            return _result(journal, created=True)
        except Exception as exc:
            journal["phase"] = "FAILED_NEEDS_RECONCILE"
            journal["error"] = str(exc)[:1000]
            _atomic_json(path, journal)
            raise


def _result(journal: Mapping[str, object], *, created: bool) -> dict[str, object]:
    return {
        "schema_version": PUBLIC_SCHEMA,
        "command": "create-v2-bundle",
        "action_level": "silent",
        "pushed": False,
        "phase": "succeeded",
        "operation_id": journal["operation_id"],
        "iteration": journal["iteration"],
        "plan_digest": journal["plan_digest"],
        "journal_phase": journal["phase"],
        "created_now": created,
        "idempotent_replay": not created,
        "paths": [item["path"] for item in journal["files"]],
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
            timestamp = datetime.fromisoformat(args.planned_at)
            payload = apply_bundle(
                args.project_root,
                iteration=args.iteration,
                operation_id=args.operation_id,
                accepted_plan_digest=args.accept_plan_digest,
                planned_at=timestamp,
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
