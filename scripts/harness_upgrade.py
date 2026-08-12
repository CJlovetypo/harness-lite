#!/usr/bin/env python3
"""Plan and apply a conservative legacy-to-v2 Harness Lite adoption.

Planning is zero-write and binds the committed ``main`` governance snapshot,
legacy identities, dirty state, and every proposed v2 ref.  Apply requires the
exact accepted digest, records a durable common-dir journal before ref writes,
creates every allocation/base pair in one Git ref transaction, and recovers
idempotently when a crash occurs after that transaction.  Legacy files and
refs are never rewritten or removed; this module never creates a branch,
worktree, commit, merge, or remote write.
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from . import project_harness as core
except ImportError:  # pragma: no cover - direct script execution
    import project_harness as core


SCHEMA_V1 = "harness-lite.upgrade-plan/v1"
JOURNAL_SCHEMA_V1 = "harness-lite.upgrade-journal/v1"
OID_RE = re.compile(r"[0-9a-f]{40,64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
OPERATION_RE = re.compile(r"OP-[0-9a-f]{32}")
MAX_JOURNAL_BYTES = 4 * 1024 * 1024


class UpgradeError(RuntimeError):
    """Raised when a read-only upgrade plan cannot be established safely."""


class InjectedUpgradeCrash(BaseException):
    """Test-only crash injection that deliberately bypasses normal error handling."""


@dataclass(frozen=True)
class IterationUpgrade:
    iteration: str
    lifecycle: str
    bundle_present: bool
    legacy_base_refs: tuple[str, ...]
    legacy_final_ref: str | None
    v2_allocation_ref: str | None
    v2_base_ref: str | None
    disposition: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class UpgradePlan:
    schema_version: str
    command: str
    action_level: str
    pushed: bool
    project_root: str
    operation_id: str
    head: str
    branch_ref: str | None
    dirty: bool
    governance_ref: str
    governance_commit: str | None
    governance_tree: str | None
    principle_sha256: str | None
    iterations: tuple[IterationUpgrade, ...]
    planned_actions: tuple[dict[str, object], ...]
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    plan_digest: str
    phase: str
    next_gate: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class UpgradeApplyResult:
    schema_version: str
    command: str
    action_level: str
    pushed: bool
    project_root: str
    operation_id: str
    plan_digest: str
    phase: str
    adopted_iterations: tuple[str, ...]
    preserved_iterations: tuple[str, ...]
    journal_path: str
    idempotent: bool
    next_gate: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _operation_id(plan_digest: str, iteration: str | None = None) -> str:
    suffix = f":{iteration}" if iteration is not None else ""
    return "OP-" + hashlib.sha256(f"upgrade:{plan_digest}{suffix}".encode("ascii")).hexdigest()[:32]


def _common_dir(root: Path) -> Path:
    result = _run_git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"], check=False)
    if result.returncode != 0:
        result = _run_git(root, ["rev-parse", "--git-common-dir"])
    value = Path(_stdout(result))
    if not value.is_absolute():
        value = root / value
    return value.resolve()


def _state_root(common_dir: Path) -> Path:
    return common_dir / "project-harness" / "upgrade" / "v1"


def _journal_path(common_dir: Path, operation_id: str) -> Path:
    if not OPERATION_RE.fullmatch(operation_id):
        raise UpgradeError("upgrade operation ID is not canonical")
    return _state_root(common_dir) / "journals" / f"{operation_id}.json"


def _lock_path(common_dir: Path) -> Path:
    return _state_root(common_dir) / "upgrade.lock"


def _ensure_operational_path(path: Path, common_dir: Path) -> None:
    resolved_common = common_dir.resolve()
    try:
        path.absolute().relative_to(resolved_common)
        path.resolve(strict=False).relative_to(resolved_common)
    except ValueError as exc:
        raise UpgradeError(f"operational path escapes Git common directory: {path}") from exc
    current = path
    while current != resolved_common:
        if current.exists():
            is_junction = getattr(current, "is_junction", lambda: False)
            if current.is_symlink() or is_junction():
                raise UpgradeError(f"operational path crosses a link or junction: {current}")
        if current.parent == current:
            raise UpgradeError(f"cannot prove operational path containment: {path}")
        current = current.parent


@contextlib.contextmanager
def _upgrade_lock(common_dir: Path, *, timeout_seconds: float = 30.0):
    path = _lock_path(common_dir)
    _ensure_operational_path(path, common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_operational_path(path.parent, common_dir)
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
                else:  # pragma: no cover - Windows is the primary integration target
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise UpgradeError("timed out waiting for the upgrade coordinator lease") from exc
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


def _read_journal(path: Path) -> dict[str, object]:
    try:
        if path.stat().st_size > MAX_JOURNAL_BYTES:
            raise UpgradeError(f"upgrade journal exceeds its size limit and was preserved: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"upgrade journal is corrupt and was preserved: {path}") from exc
    required = {
        "schema_version",
        "operation_id",
        "plan_digest",
        "project_root",
        "phase",
        "governance_commit",
        "expected_refs",
        "allocations",
        "plan_snapshot",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != JOURNAL_SCHEMA_V1:
        raise UpgradeError(f"upgrade journal schema is invalid and was preserved: {path}")
    if value.get("phase") not in {"PLANNED", "REFS_COMMITTED", "READY", "FAILED_NEEDS_RECONCILE"}:
        raise UpgradeError(f"upgrade journal phase is invalid and was preserved: {path}")
    return value


def _upgrade_plan_from_dict(value: Mapping[str, object]) -> UpgradePlan:
    expected = {
        "schema_version",
        "command",
        "action_level",
        "pushed",
        "project_root",
        "operation_id",
        "head",
        "branch_ref",
        "dirty",
        "governance_ref",
        "governance_commit",
        "governance_tree",
        "principle_sha256",
        "iterations",
        "planned_actions",
        "blocking_reasons",
        "warnings",
        "plan_digest",
        "phase",
        "next_gate",
    }
    if set(value) != expected:
        raise UpgradeError("upgrade journal plan snapshot fields are invalid")
    if value.get("schema_version") != SCHEMA_V1 or value.get("command") != "upgrade-dry-run":
        raise UpgradeError("upgrade journal plan snapshot schema is unsupported")
    raw_iterations = value.get("iterations")
    raw_actions = value.get("planned_actions")
    if not isinstance(raw_iterations, (list, tuple)) or not isinstance(raw_actions, (list, tuple)):
        raise UpgradeError("upgrade journal plan snapshot collections are invalid")
    try:
        iterations = tuple(
            IterationUpgrade(
                iteration=str(item["iteration"]),
                lifecycle=str(item["lifecycle"]),
                bundle_present=bool(item["bundle_present"]),
                legacy_base_refs=tuple(str(entry) for entry in item["legacy_base_refs"]),
                legacy_final_ref=str(item["legacy_final_ref"]) if item["legacy_final_ref"] is not None else None,
                v2_allocation_ref=str(item["v2_allocation_ref"]) if item["v2_allocation_ref"] is not None else None,
                v2_base_ref=str(item["v2_base_ref"]) if item["v2_base_ref"] is not None else None,
                disposition=str(item["disposition"]),
                blockers=tuple(str(entry) for entry in item["blockers"]),
            )
            for item in raw_iterations
            if isinstance(item, dict)
        )
    except (KeyError, TypeError) as exc:
        raise UpgradeError("upgrade journal iteration snapshot is invalid") from exc
    if len(iterations) != len(raw_iterations):
        raise UpgradeError("upgrade journal iteration snapshot contains a non-object")
    plan = UpgradePlan(
        schema_version=SCHEMA_V1,
        command="upgrade-dry-run",
        action_level=str(value["action_level"]),
        pushed=bool(value["pushed"]),
        project_root=str(value["project_root"]),
        operation_id=str(value["operation_id"]),
        head=str(value["head"]),
        branch_ref=str(value["branch_ref"]) if value["branch_ref"] is not None else None,
        dirty=bool(value["dirty"]),
        governance_ref=str(value["governance_ref"]),
        governance_commit=str(value["governance_commit"]) if value["governance_commit"] is not None else None,
        governance_tree=str(value["governance_tree"]) if value["governance_tree"] is not None else None,
        principle_sha256=str(value["principle_sha256"]) if value["principle_sha256"] is not None else None,
        iterations=iterations,
        planned_actions=tuple(dict(item) for item in raw_actions if isinstance(item, dict)),
        blocking_reasons=tuple(str(item) for item in value["blocking_reasons"]),
        warnings=tuple(str(item) for item in value["warnings"]),
        plan_digest=str(value["plan_digest"]),
        phase=str(value["phase"]),
        next_gate=str(value["next_gate"]),
    )
    digest_payload = {
        "schema_version": plan.schema_version,
        "project_root": plan.project_root,
        "governance_ref": plan.governance_ref,
        "governance_commit": plan.governance_commit,
        "governance_tree": plan.governance_tree,
        "principle_sha256": plan.principle_sha256,
        "head": plan.head,
        "branch_ref": plan.branch_ref,
        "dirty": plan.dirty,
        "iterations": [asdict(item) for item in plan.iterations],
        "planned_actions": list(plan.planned_actions),
        "blocking_reasons": list(plan.blocking_reasons),
        "warnings": list(plan.warnings),
    }
    expected_digest = hashlib.sha256(_canonical_json(digest_payload)).hexdigest()
    if (
        plan.plan_digest != expected_digest
        or plan.operation_id != _operation_id(expected_digest)
        or plan.action_level != "silent"
        or plan.pushed
        or plan.phase != "planned"
        or plan.blocking_reasons
    ):
        raise UpgradeError("upgrade journal plan snapshot digest or policy is invalid")
    return plan


def _write_journal(path: Path, value: Mapping[str, object], common_dir: Path, *, create: bool) -> None:
    _ensure_operational_path(path, common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_operational_path(path.parent, common_dir)
    if create and path.exists():
        raise UpgradeError(f"upgrade journal appeared concurrently: {path}")
    raw = _canonical_json(value) + b"\n"
    handle = tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(handle.name)
    try:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        if create and path.exists():
            raise UpgradeError(f"upgrade journal appeared concurrently: {path}")
        os.replace(temporary, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if not handle.closed:
            handle.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _allocation_metadata(
    reservation: core.OperationPlan,
    iteration: str,
) -> dict[str, object]:
    return core.build_allocation_metadata(reservation, iteration)


def _reservation_plan(
    *,
    root: Path,
    common_dir: Path,
    iteration: str,
    legacy_ref: str,
    base_commit: str,
    governance_ref: str,
    governance_commit: str,
    governance_snapshot: Mapping[str, object],
) -> core.OperationPlan:
    marker = f"/{iteration}/base/"
    if marker not in legacy_ref:
        raise UpgradeError(f"PRD-{iteration} legacy base ref is malformed")
    base_branch = legacy_ref.split(marker, 1)[1]
    stable_identity = _canonical_json(
        {
            "project_root": str(root),
            "iteration": iteration,
            "legacy_ref": legacy_ref,
            "base_commit": base_commit,
            "governance_commit": governance_commit,
        }
    )
    operation_id = "OP-" + hashlib.sha256(b"upgrade-adopt:" + stable_identity).hexdigest()[:32]
    title = f"adopt legacy PRD-{iteration}"
    manifest = core.operation_intent(
        operation_id=operation_id,
        project_root=root,
        title=title,
        base_commit=base_commit,
        base_ref=base_branch,
        governance_ref=governance_ref,
        governance_commit=governance_commit,
        governance_snapshot=governance_snapshot,
        observed_next_iteration=iteration,
    )
    plan_digest = core.schema_digest(manifest)
    return core.OperationPlan(
        operation_id=operation_id,
        project_root=str(root),
        git_common_dir=str(common_dir),
        title=title,
        base_commit=base_commit,
        base_branch=base_branch,
        governance_ref=governance_ref,
        governance_commit=governance_commit,
        governance_snapshot=dict(governance_snapshot),
        observed_next_iteration=iteration,
        plan_digest=plan_digest,
        manifest=manifest,
        reservation={
            "iteration": iteration,
            "allocation_ref": core.v2_allocation_ref(iteration),
            "base_ref": core.v2_iteration_base_ref(iteration),
        },
    )


_PARENT_ALLOCATION_FIELDS = {
    "iteration",
    "operation_id",
    "plan_digest",
    "legacy_ref",
    "base_commit",
    "allocation_ref",
    "allocation_object",
    "base_ref",
}


def _validated_parent_allocations(
    journal: Mapping[str, object],
    plan: UpgradePlan,
) -> list[dict[str, str]]:
    """Validate the durable ref manifest without consulting mutable refs."""

    raw_allocations = journal.get("allocations")
    if not isinstance(raw_allocations, list):
        raise UpgradeError("upgrade journal allocation manifest is invalid")
    actions = {
        str(action.get("iteration")): action
        for action in plan.planned_actions
        if action.get("action") == "adopt-legacy-iteration"
    }
    expected_iterations = [
        item.iteration for item in plan.iterations if item.lifecycle == "legacy-active-clean"
    ]
    allocations: list[dict[str, str]] = []
    for raw in raw_allocations:
        if not isinstance(raw, dict) or set(raw) != _PARENT_ALLOCATION_FIELDS:
            raise UpgradeError("upgrade journal allocation entry schema is invalid")
        if not all(isinstance(raw.get(key), str) for key in _PARENT_ALLOCATION_FIELDS):
            raise UpgradeError("upgrade journal allocation entry contains a non-string field")
        allocation = {key: str(raw[key]) for key in _PARENT_ALLOCATION_FIELDS}
        iteration = allocation["iteration"]
        action = actions.get(iteration)
        if (
            not ITERATION_RE.fullmatch(iteration)
            or not OPERATION_RE.fullmatch(allocation["operation_id"])
            or not DIGEST_RE.fullmatch(allocation["plan_digest"])
            or not OID_RE.fullmatch(allocation["base_commit"])
            or not OID_RE.fullmatch(allocation["allocation_object"])
            or allocation["allocation_ref"] != core.v2_allocation_ref(iteration)
            or allocation["base_ref"] != core.v2_iteration_base_ref(iteration)
            or action is None
            or allocation["legacy_ref"] != action.get("legacy_base_ref")
            or allocation["base_commit"] != action.get("legacy_base_commit")
        ):
            raise UpgradeError("upgrade journal allocation entry differs from its accepted plan")
        allocations.append(allocation)
    if [item["iteration"] for item in allocations] != expected_iterations:
        raise UpgradeError("upgrade journal allocation order or membership differs from its accepted plan")
    expected_refs = [
        reference
        for allocation in allocations
        for reference in (allocation["allocation_ref"], allocation["base_ref"])
    ]
    if journal.get("expected_refs") != expected_refs or len(set(expected_refs)) != len(expected_refs):
        raise UpgradeError("upgrade journal expected refs differ from its allocation manifest")
    return allocations


def _load_owned_reservation_journal(
    *,
    git: str,
    root: Path,
    common_dir: Path,
    plan: UpgradePlan,
    allocation: Mapping[str, str],
) -> core.OperationJournal:
    """Bind a parent allocation to its canonical owner journal and metadata."""

    try:
        child, _ = core.load_operation_journal(common_dir, allocation["operation_id"])
    except core.HarnessError as exc:
        raise UpgradeError(
            f"PRD-{allocation['iteration']} canonical adoption owner is unavailable: {exc}"
        ) from exc
    snapshot = child.manifest.get("governance_snapshot")
    if not isinstance(snapshot, dict):
        raise UpgradeError("canonical adoption owner lacks its governance snapshot")
    expected = _reservation_plan(
        root=root,
        common_dir=common_dir,
        iteration=allocation["iteration"],
        legacy_ref=allocation["legacy_ref"],
        base_commit=allocation["base_commit"],
        governance_ref=plan.governance_ref,
        governance_commit=plan.governance_commit or "",
        governance_snapshot=snapshot,
    )
    expected_refs = (allocation["allocation_ref"], allocation["base_ref"])
    if (
        expected.operation_id != allocation["operation_id"]
        or expected.plan_digest != allocation["plan_digest"]
        or child.operation_id != expected.operation_id
        or child.plan_digest != expected.plan_digest
        or child.manifest != expected.manifest
        or child.action != "reserve-iteration"
        or child.project_root != plan.project_root
        or child.iteration != allocation["iteration"]
        or child.base_commit != allocation["base_commit"]
        or child.base_branch != expected.base_branch
        or child.governance_ref != plan.governance_ref
        or child.governance_commit != plan.governance_commit
        or child.principle_sha256 != plan.principle_sha256
        or child.expected_refs != expected_refs
        or (
            child.phase in {"RESERVED", "READY"}
            and (
                child.allocation_object != allocation["allocation_object"]
                or child.created_refs != expected_refs
            )
        )
    ):
        raise UpgradeError(
            f"PRD-{allocation['iteration']} allocation differs from its canonical owner journal"
        )
    try:
        metadata = core.read_allocation_metadata(git, root, allocation["allocation_object"])
    except core.HarnessError as exc:
        raise UpgradeError(
            f"PRD-{allocation['iteration']} durable allocation metadata is invalid: {exc}"
        ) from exc
    if metadata != core.build_allocation_metadata(expected, allocation["iteration"]):
        raise UpgradeError(
            f"PRD-{allocation['iteration']} allocation metadata differs from its canonical owner journal"
        )
    if child.phase == "FAILED_NEEDS_RECONCILE":
        raise UpgradeError("canonical adoption journal requires manual reconcile")
    return child


def _validate_preserved_v2_semantics(
    *,
    git: str,
    root: Path,
    iteration: str,
    allocation_object: str,
    metadata: Mapping[str, object],
    child: core.OperationJournal,
) -> None:
    """Prove a structurally valid v2 identity still names real Git objects.

    ``load_operation_journal`` proves the journal is internally consistent and
    ``read_allocation_metadata`` proves the allocation blob has the expected
    schema.  Those checks alone are insufficient: an actor could rewrite both
    records coherently while substituting a plausible-looking governance hash
    or reservation-policy iteration.  Preservation therefore re-derives the
    canonical intent and binds the recorded snapshot to the historical commit
    objects.  It deliberately does *not* require ``main`` to remain at that
    commit, so a normal later main advance remains compatible.
    """

    snapshot = child.manifest.get("governance_snapshot")
    if not isinstance(snapshot, dict):
        raise core.HarnessError("READY owner journal lacks a governance snapshot")
    if child.iteration != iteration:
        raise core.HarnessError("READY owner journal names a different iteration")
    expected_manifest = core.operation_intent(
        operation_id=child.operation_id,
        project_root=root,
        title=child.title,
        base_commit=child.base_commit,
        base_ref=child.base_branch,
        governance_ref=child.governance_ref,
        governance_commit=child.governance_commit,
        governance_snapshot=snapshot,
        observed_next_iteration=iteration,
    )
    if child.manifest != expected_manifest or child.plan_digest != core.schema_digest(expected_manifest):
        raise core.HarnessError("READY owner journal does not encode its canonical reservation intent")

    expected_metadata = {
        "schema_version": metadata.get("schema_version"),
        "operation_id": child.operation_id,
        "plan_digest": child.plan_digest,
        "iteration": iteration,
        "base_commit": child.base_commit,
        "base_branch": child.base_branch,
        "governance_ref": child.governance_ref,
        "governance_commit": child.governance_commit,
        "governance_tree": snapshot.get("tree"),
        "principle_sha256": snapshot.get("principle_sha256"),
        "title": child.title,
    }
    if metadata != expected_metadata or child.allocation_object != allocation_object:
        raise core.HarnessError("allocation metadata does not exactly match its READY owner journal")

    object_type = core.decode_output(
        core.run_git(git, root, ["cat-file", "-t", child.governance_commit]).stdout
    )
    if object_type != "commit":
        raise core.HarnessError("recorded governance identity is not a commit")
    observed_tree = core.decode_output(
        core.run_git(
            git,
            root,
            ["rev-parse", "--verify", f"{child.governance_commit}^{{tree}}"],
        ).stdout
    )
    required_paths = (
        "AGENTS.md",
        "harness/README.md",
        "harness/principle.md",
        "harness/progress.md",
    )
    entries = core.read_committed_governance_entries(git, root, child.governance_commit)
    observed_blobs: dict[str, str] = {}
    for relative in required_paths:
        entry = entries.get(relative)
        if entry is None:
            raise core.HarnessError(f"recorded governance commit lacks {relative}")
        observed_blobs[relative] = entry[1]
    principle_raw = entries["harness/principle.md"][2]
    if (
        snapshot.get("commit") != child.governance_commit
        or snapshot.get("tree") != observed_tree
        or snapshot.get("blobs") != observed_blobs
        or snapshot.get("principle_sha256") != hashlib.sha256(principle_raw).hexdigest()
    ):
        raise core.HarnessError("recorded governance snapshot differs from its committed Git objects")


def _revalidate_pre_ref_preconditions(root: Path, git: str, plan: UpgradePlan) -> None:
    """Revalidate mutable safety gates before a replay can create any refs."""

    if plan.dirty:
        raise UpgradeError("accepted upgrade plan was not based on a clean worktree")
    status = _run_git(
        root,
        ["-c", "core.fsmonitor=false", "status", "--porcelain=v2", "-z", "--untracked-files=all"],
    ).stdout
    if status:
        raise UpgradeError("worktree became dirty before the v2 ref transaction; reconcile and re-plan")
    validation = core.collect_validation(root)
    if validation.errors:
        raise UpgradeError("live governance became invalid before the v2 ref transaction")
    observed_head = _stdout(_run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"]))
    branch_result = _run_git(root, ["symbolic-ref", "-q", "HEAD"], check=False)
    observed_branch = _stdout(branch_result) if branch_result.returncode == 0 else None
    if observed_head != plan.head or observed_branch != plan.branch_ref:
        raise UpgradeError("HEAD or attached branch changed before the v2 ref transaction")
    try:
        _, governance_commit, governance_snapshot = core.committed_governance_snapshot(
            git, root, plan.governance_ref
        )
    except core.HarnessError as exc:
        raise UpgradeError(f"committed main governance is no longer valid: {exc}") from exc
    if (
        governance_commit != plan.governance_commit
        or governance_snapshot.get("tree") != plan.governance_tree
        or governance_snapshot.get("principle_sha256") != plan.principle_sha256
    ):
        raise UpgradeError("committed main governance changed from the accepted upgrade plan")
    refs = _refs(root)
    for action in plan.planned_actions:
        if action.get("action") != "adopt-legacy-iteration":
            continue
        if refs.get(str(action["legacy_base_ref"])) != action.get("legacy_base_commit"):
            raise UpgradeError("legacy base identity changed before the v2 ref transaction")


def _run_git(root: Path, arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    git = shutil.which("git")
    if not git:
        raise UpgradeError("Git is required to inspect an upgrade")
    environment = os.environ.copy()
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
        [git, "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise UpgradeError(message or f"Git exited with {result.returncode}")
    return result


def _stdout(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="strict").strip()


def _resolve_root(value: str | Path) -> Path:
    supplied = Path(value).expanduser().resolve()
    if not supplied.is_dir():
        raise UpgradeError(f"project root is not an existing directory: {supplied}")
    result = _run_git(supplied, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise UpgradeError("upgrade planning requires an existing Git worktree")
    root = Path(_stdout(result)).resolve()
    if os.path.normcase(str(root)) != os.path.normcase(str(supplied)):
        raise UpgradeError(f"project root must be the exact worktree root: {root}")
    return root


def _refs(root: Path) -> dict[str, str]:
    result = _run_git(root, ["for-each-ref", "--format=%(refname)%00%(objectname)"])
    output: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        if not raw_line:
            continue
        try:
            raw_ref, raw_oid = raw_line.split(b"\0", 1)
            ref = raw_ref.decode("utf-8", errors="strict")
            oid = raw_oid.decode("ascii", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpgradeError("Git returned malformed ref inventory") from exc
        if not OID_RE.fullmatch(oid):
            raise UpgradeError(f"Git returned malformed object ID for {ref}")
        output[ref] = oid
    return output


def _ref_records(root: Path) -> dict[str, tuple[str, str]]:
    result = _run_git(root, ["for-each-ref", "--format=%(refname)%00%(objectname)%00%(objecttype)"])
    output: dict[str, tuple[str, str]] = {}
    for raw_line in result.stdout.splitlines():
        if not raw_line:
            continue
        try:
            raw_ref, raw_oid, raw_type = raw_line.split(b"\0", 2)
            ref = raw_ref.decode("utf-8", errors="strict")
            oid = raw_oid.decode("ascii", errors="strict")
            object_type = raw_type.decode("ascii", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpgradeError("Git returned malformed typed ref inventory") from exc
        if not OID_RE.fullmatch(oid):
            raise UpgradeError(f"Git returned malformed object ID for {ref}")
        output[ref] = (oid, object_type)
    return output


def _bundle_ids(root: Path) -> set[str]:
    iterations = root / "harness" / "iterations"
    if not iterations.is_dir():
        return set()
    return {
        child.name
        for child in iterations.iterdir()
        if child.is_dir() and ITERATION_RE.fullmatch(child.name)
    }


def _ref_ids(refs: Iterable[str]) -> set[str]:
    patterns = (
        re.compile(r"refs/project-harness/iterations/([0-9]{3,})/(?:base/|final$)"),
        re.compile(r"refs/project-harness/v2/(?:allocations|iterations)/([0-9]{3,})(?:$|/)"),
    )
    result: set[str] = set()
    for ref in refs:
        for pattern in patterns:
            match = pattern.match(ref)
            if match:
                result.add(match.group(1))
                break
    return result


def build_upgrade_plan(project_root: str | Path) -> UpgradePlan:
    """Return a deterministic, zero-write compatibility/adoption plan."""

    root = _resolve_root(project_root)
    refs = _refs(root)
    typed_refs = _ref_records(root)
    bundle_ids = _bundle_ids(root)
    iteration_ids = sorted(bundle_ids | _ref_ids(refs), key=int)
    head = _stdout(_run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"]))
    branch_result = _run_git(root, ["symbolic-ref", "-q", "HEAD"], check=False)
    branch_ref = _stdout(branch_result) if branch_result.returncode == 0 else None
    status = _run_git(
        root,
        ["-c", "core.fsmonitor=false", "status", "--porcelain=v2", "-z", "--untracked-files=all"],
    ).stdout
    dirty = bool(status)

    governance_ref = "refs/heads/main"
    governance_commit: str | None = None
    governance_tree: str | None = None
    principle_sha256: str | None = None
    governance_blocker: str | None = None
    try:
        _, governance_commit, snapshot = core.committed_governance_snapshot(
            shutil.which("git") or "git", root, governance_ref
        )
    except core.HarnessError as exc:
        governance_blocker = f"committed-main-governance-invalid:{exc}"
    else:
        governance_tree = str(snapshot["tree"])
        principle_sha256 = str(snapshot["principle_sha256"])
    validation = core.collect_validation(root)
    validation_errors = [
        f"{issue.code}:{issue.path}:{issue.message}" for issue in validation.errors
    ]
    canonical_status_blockers: list[str] = []
    try:
        canonical_status = core.build_status_snapshot(
            root, shutil.which("git") or "git", all_worktrees=True
        )
    except core.HarnessError as exc:
        canonical_status_blockers.append(f"canonical-status-invalid:{exc}")
    else:
        canonical_status_blockers.extend(
            f"canonical-v2-status:{reason.code}:{reason.message}"
            for reason in canonical_status.blocking_reasons
        )

    iterations: list[IterationUpgrade] = []
    actions: list[dict[str, object]] = []
    global_blockers: list[str] = []
    warnings: list[str] = []
    if governance_blocker is not None:
        global_blockers.append(governance_blocker)
    global_blockers.extend(f"live-governance-invalid:{item}" for item in validation_errors)
    global_blockers.extend(canonical_status_blockers)
    for number in iteration_ids:
        legacy_prefix = f"refs/project-harness/iterations/{number}/base/"
        legacy_bases = tuple(sorted(ref for ref in refs if ref.startswith(legacy_prefix)))
        legacy_final = f"refs/project-harness/iterations/{number}/final"
        v2_allocation = f"refs/project-harness/v2/allocations/{number}"
        v2_base = f"refs/project-harness/v2/iterations/{number}/base"
        has_legacy_final = legacy_final in refs
        has_v2_allocation = v2_allocation in refs
        has_v2_base = v2_base in refs
        blockers: list[str] = []
        if has_v2_allocation != has_v2_base:
            blockers.append("partial-v2-identity")
        if len(legacy_bases) > 1:
            blockers.append("multiple-legacy-base-anchors")

        if has_v2_allocation and has_v2_base:
            try:
                allocation_oid = refs[v2_allocation]
                allocation_type = typed_refs.get(v2_allocation, ("", ""))[1]
                base_type = typed_refs.get(v2_base, ("", ""))[1]
                if allocation_type != "blob" or base_type != "commit":
                    raise core.HarnessError("v2 allocation/base ref object types are invalid")
                metadata = core.read_allocation_metadata(shutil.which("git") or "git", root, allocation_oid)
                observed_base = refs[v2_base]
                if metadata.get("iteration") != number or metadata.get("base_commit") != observed_base:
                    raise core.HarnessError("allocation metadata does not match its v2 base identity")
                owner = str(metadata.get("operation_id"))
                child, _ = core.load_operation_journal(_common_dir(root), owner)
                _validate_preserved_v2_semantics(
                    git=shutil.which("git") or "git",
                    root=root,
                    iteration=number,
                    allocation_object=allocation_oid,
                    metadata=metadata,
                    child=child,
                )
                if (
                    child.phase != "READY"
                    or child.action != "reserve-iteration"
                    or child.project_root != str(root)
                    or child.iteration != number
                    or child.plan_digest != metadata.get("plan_digest")
                    or child.allocation_object != allocation_oid
                    or child.base_commit != observed_base
                    or child.base_branch != metadata.get("base_branch")
                    or child.governance_ref != metadata.get("governance_ref")
                    or child.governance_commit != metadata.get("governance_commit")
                    or child.manifest.get("governance_snapshot", {}).get("tree")
                    != metadata.get("governance_tree")
                    or child.principle_sha256 != metadata.get("principle_sha256")
                    or child.title != metadata.get("title")
                    or child.expected_refs != (v2_allocation, v2_base)
                    or child.created_refs != (v2_allocation, v2_base)
                ):
                    raise core.HarnessError("allocation metadata does not match its READY owner journal")
            except core.HarnessError as exc:
                blockers.append(f"invalid-v2-identity:{exc}")
            lifecycle = "v2"
            disposition = "preserve-v2"
        elif has_legacy_final:
            lifecycle = "legacy-complete"
            disposition = "preserve-legacy-history"
        elif legacy_bases:
            lifecycle = "legacy-active-dirty" if dirty else "legacy-active-clean"
            if dirty:
                disposition = "continue-legacy-or-snapshot-with-explicit-authorization"
                blockers.append("dirty-active-iteration-requires-explicit-adoption")
            else:
                disposition = "eligible-for-explicit-v2-adoption"
                actions.append(
                    {
                        "action": "adopt-legacy-iteration",
                        "iteration": number,
                        "legacy_base_ref": legacy_bases[0],
                        "legacy_base_commit": refs[legacy_bases[0]],
                        "writes": False,
                        "authorization": "confirm",
                    }
                )
        else:
            lifecycle = "bundle-only"
            disposition = "reconcile-missing-identity"
            blockers.append("iteration-bundle-has-no-base-identity")

        if number not in bundle_ids:
            blockers.append("iteration-identity-has-no-bundle")
        if blockers:
            global_blockers.extend(f"{number}:{blocker}" for blocker in blockers)
        iterations.append(
            IterationUpgrade(
                iteration=number,
                lifecycle=lifecycle,
                bundle_present=number in bundle_ids,
                legacy_base_refs=legacy_bases,
                legacy_final_ref=legacy_final if has_legacy_final else None,
                v2_allocation_ref=v2_allocation if has_v2_allocation else None,
                v2_base_ref=v2_base if has_v2_base else None,
                disposition=disposition,
                blockers=tuple(blockers),
            )
        )

    if not iteration_ids:
        warnings.append("no-iterations-found")
    digest_payload = {
        "schema_version": SCHEMA_V1,
        "project_root": str(root),
        "governance_ref": governance_ref,
        "governance_commit": governance_commit,
        "governance_tree": governance_tree,
        "principle_sha256": principle_sha256,
        "head": head,
        "branch_ref": branch_ref,
        "dirty": dirty,
        "iterations": [asdict(item) for item in iterations],
        "planned_actions": actions,
        "blocking_reasons": global_blockers,
        "warnings": warnings,
    }
    plan_digest = hashlib.sha256(_canonical_json(digest_payload)).hexdigest()
    operation_id = _operation_id(plan_digest)
    return UpgradePlan(
        schema_version=SCHEMA_V1,
        command="upgrade-dry-run",
        action_level="silent",
        pushed=False,
        project_root=str(root),
        operation_id=operation_id,
        head=head,
        branch_ref=branch_ref,
        dirty=dirty,
        governance_ref=governance_ref,
        governance_commit=governance_commit,
        governance_tree=governance_tree,
        principle_sha256=principle_sha256,
        iterations=tuple(iterations),
        planned_actions=tuple(actions),
        blocking_reasons=tuple(global_blockers),
        warnings=tuple(warnings),
        plan_digest=plan_digest,
        phase="blocked" if global_blockers else "planned",
        next_gate="fix-input-or-reconcile" if global_blockers else "review-explicit-adoption-plan",
    )


def apply_upgrade_plan(
    project_root: str | Path,
    *,
    accepted_plan_digest: str,
    _failpoint: str | None = None,
) -> UpgradeApplyResult:
    """Apply only an exact, currently clean legacy adoption plan.

    Completed legacy iterations are deliberately no-op preserved. Active clean
    legacy iterations gain v2 allocation metadata + base refs in one Git ref
    transaction. Existing legacy refs/content are never rewritten or removed.
    """

    if not re.fullmatch(r"[0-9a-f]{64}", accepted_plan_digest.strip().lower()):
        raise UpgradeError("accepted plan digest must be a SHA-256 digest")
    accepted = accepted_plan_digest.strip().lower()
    initial_root = _resolve_root(project_root)
    common_dir = _common_dir(initial_root)
    provisional_operation = _operation_id(accepted)
    journal_path = _journal_path(common_dir, provisional_operation)
    with _upgrade_lock(common_dir):
        existing_upgrade = _read_journal(journal_path) if journal_path.exists() else None
        if existing_upgrade is not None:
            if existing_upgrade.get("plan_digest") != accepted:
                raise UpgradeError("existing upgrade journal belongs to a different accepted plan")
            if existing_upgrade.get("project_root") != str(initial_root):
                raise UpgradeError("existing upgrade journal belongs to another project root")
            # Recovery never rebuilds a post-mutation dry-run plan: the exact
            # pre-state intent is already durable.  A READY replay is accepted
            # only after every ref and canonical child reservation journal is
            # revalidated below.
            snapshot = existing_upgrade.get("plan_snapshot")
            if not isinstance(snapshot, dict):
                raise UpgradeError("upgrade journal lacks its accepted pre-state snapshot")
            plan = _upgrade_plan_from_dict(snapshot)
        else:
            plan = build_upgrade_plan(initial_root)
            if plan.phase != "planned" or plan.blocking_reasons:
                raise UpgradeError("upgrade plan is blocked and cannot be applied")
            if plan.plan_digest != accepted:
                raise UpgradeError("upgrade state changed or accepted plan digest does not match")
        root = Path(plan.project_root)
        if plan.operation_id != provisional_operation:
            raise UpgradeError("accepted upgrade digest does not derive the journal operation identity")
        if not all((plan.governance_commit, plan.governance_tree, plan.principle_sha256)):
            raise UpgradeError("accepted upgrade plan lacks a valid committed governance snapshot")
        if root != initial_root:
            raise UpgradeError("accepted upgrade plan resolved a different project root")
        adopted = tuple(
            item.iteration for item in plan.iterations if item.lifecycle == "legacy-active-clean"
        )
        preserved = tuple(
            item.iteration for item in plan.iterations if item.lifecycle in {"legacy-complete", "v2"}
        )
        git = shutil.which("git")
        if not git:
            raise UpgradeError("Git is required")
        environment = os.environ.copy()
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

        fresh = plan
        allocations: list[dict[str, str]] = []
        reservation_plans: list[core.OperationPlan] = []
        if existing_upgrade is None:
            rebuilt = build_upgrade_plan(root)
            if rebuilt.plan_digest != plan.plan_digest or rebuilt.operation_id != plan.operation_id:
                raise UpgradeError("upgrade state changed after acquiring the coordinator lease")
            fresh = rebuilt
            _revalidate_pre_ref_preconditions(root, git, fresh)
            _, _, governance_snapshot = core.committed_governance_snapshot(
                git, root, fresh.governance_ref
            )
            for item in fresh.iterations:
                if item.lifecycle != "legacy-active-clean":
                    continue
                action = next(
                    (entry for entry in fresh.planned_actions if entry.get("iteration") == item.iteration),
                    None,
                )
                if action is None:
                    raise UpgradeError(f"PRD-{item.iteration} lacks its exact adoption action")
                reservation = _reservation_plan(
                    root=root,
                    common_dir=common_dir,
                    iteration=item.iteration,
                    legacy_ref=str(action["legacy_base_ref"]),
                    base_commit=str(action["legacy_base_commit"]),
                    governance_ref=fresh.governance_ref,
                    governance_commit=fresh.governance_commit or "",
                    governance_snapshot=governance_snapshot,
                )
                reservation_plans.append(reservation)
                metadata = _allocation_metadata(reservation, item.iteration)
                raw = _canonical_json(metadata)
                written = subprocess.run(
                    [git, "-C", str(root), "hash-object", "-w", "--stdin"],
                    input=raw,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env=environment,
                )
                if written.returncode != 0:
                    raise UpgradeError(
                        written.stderr.decode("utf-8", errors="replace").strip()
                        or "metadata write failed"
                    )
                blob = written.stdout.decode("ascii").strip()
                if not OID_RE.fullmatch(blob):
                    raise UpgradeError("Git returned an invalid allocation metadata object")
                try:
                    parsed = core.read_allocation_metadata(git, root, blob)
                except core.HarnessError as exc:
                    raise UpgradeError(f"generated allocation metadata is incompatible: {exc}") from exc
                if parsed != metadata:
                    raise UpgradeError(
                        "generated allocation metadata changed during compatibility validation"
                    )
                allocations.append(
                    {
                        "iteration": item.iteration,
                        "operation_id": reservation.operation_id,
                        "plan_digest": reservation.plan_digest,
                        "legacy_ref": str(action["legacy_base_ref"]),
                        "base_commit": str(action["legacy_base_commit"]),
                        "allocation_ref": core.v2_allocation_ref(item.iteration),
                        "allocation_object": blob,
                        "base_ref": core.v2_iteration_base_ref(item.iteration),
                    }
                )
            expected_refs = tuple(
                reference
                for allocation in allocations
                for reference in (allocation["allocation_ref"], allocation["base_ref"])
            )
            journal: dict[str, object] = {
                "schema_version": JOURNAL_SCHEMA_V1,
                "operation_id": fresh.operation_id,
                "plan_digest": fresh.plan_digest,
                "project_root": fresh.project_root,
                "phase": "PLANNED",
                "governance_commit": fresh.governance_commit,
                "expected_refs": list(expected_refs),
                "allocations": allocations,
                "plan_snapshot": fresh.as_dict(),
            }
            _write_journal(journal_path, journal, common_dir, create=True)
            for reservation in reservation_plans:
                try:
                    core.create_operation_journal(common_dir, reservation)
                except core.HarnessError as exc:
                    raise UpgradeError(
                        f"could not establish PRD-{reservation.observed_next_iteration} adoption journal: {exc}"
                    ) from exc
        else:
            journal = existing_upgrade
            if journal["phase"] == "FAILED_NEEDS_RECONCILE":
                raise UpgradeError("existing upgrade operation requires manual reconcile")
            for key, expected in (
                ("operation_id", fresh.operation_id),
                ("plan_digest", fresh.plan_digest),
                ("project_root", fresh.project_root),
                ("governance_commit", fresh.governance_commit),
                ("plan_snapshot", fresh.as_dict()),
            ):
                if _canonical_json(journal.get(key)) != _canonical_json(expected):
                    raise UpgradeError("existing upgrade journal belongs to a different accepted plan")
            allocations = _validated_parent_allocations(journal, fresh)
            expected_refs = tuple(str(item) for item in journal["expected_refs"])

        # Bind each durable parent entry to the canonical production owner
        # journal and exact metadata before inspecting or mutating refs.
        reservation_journals = [
            _load_owned_reservation_journal(
                git=git,
                root=root,
                common_dir=common_dir,
                plan=fresh,
                allocation=allocation,
            )
            for allocation in allocations
        ]
        if journal["phase"] == "READY" and any(
            child.phase != "READY" for child in reservation_journals
        ):
            raise UpgradeError("READY upgrade journal differs from its canonical owner journals")
        idempotent = False

        refs = _refs(root)
        all_present = all(
            refs.get(allocation["allocation_ref"]) == allocation["allocation_object"]
            and refs.get(allocation["base_ref"]) == allocation["base_commit"]
            for allocation in allocations
        )
        any_present = any(ref in refs for ref in expected_refs)
        if any_present and not all_present:
            failed = dict(journal)
            failed["phase"] = "FAILED_NEEDS_RECONCILE"
            _write_journal(journal_path, failed, common_dir, create=False)
            raise UpgradeError("v2 adoption refs are partial or differ from the accepted journal")
        if all_present:
            idempotent = True
        elif allocations:
            if existing_upgrade is not None and (
                journal["phase"] in {"REFS_COMMITTED", "READY"}
                or any(child.phase in {"RESERVED", "READY"} for child in reservation_journals)
            ):
                raise UpgradeError(
                    "durably committed v2 refs are missing; automatic recreation is forbidden and reconcile is required"
                )
            _revalidate_pre_ref_preconditions(root, git, fresh)
            if _failpoint == "before-ref-transaction":
                raise InjectedUpgradeCrash("injected crash before upgrade ref transaction")
            commands = ["start", f"verify {fresh.governance_ref} {fresh.governance_commit}"]
            for allocation in allocations:
                commands.extend(
                    (
                        f"verify {allocation['legacy_ref']} {allocation['base_commit']}",
                        f"create {allocation['allocation_ref']} {allocation['allocation_object']}",
                        f"create {allocation['base_ref']} {allocation['base_commit']}",
                    )
                )
            commands.extend(("prepare", "commit"))
            transaction = subprocess.run(
                [git, "-C", str(root), "update-ref", "--stdin"],
                input=("\n".join(commands) + "\n").encode("ascii"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=environment,
            )
            if transaction.returncode != 0:
                failed = dict(journal)
                failed["phase"] = "FAILED_NEEDS_RECONCILE"
                _write_journal(journal_path, failed, common_dir, create=False)
                raise UpgradeError(
                    transaction.stderr.decode("utf-8", errors="replace").strip()
                    or "multi-iteration adoption ref transaction failed"
                )
            if _failpoint == "after-ref-transaction":
                raise InjectedUpgradeCrash("injected crash after upgrade ref transaction")
        if journal["phase"] == "READY":
            committed = dict(journal)
        else:
            committed = dict(journal)
            committed["phase"] = "REFS_COMMITTED"
            _write_journal(journal_path, committed, common_dir, create=False)
        observed = _refs(root)
        for allocation in allocations:
            if (
                observed.get(allocation["allocation_ref"]) != allocation["allocation_object"]
                or observed.get(allocation["base_ref"]) != allocation["base_commit"]
            ):
                failed = dict(committed)
                failed["phase"] = "FAILED_NEEDS_RECONCILE"
                _write_journal(journal_path, failed, common_dir, create=False)
                raise UpgradeError("post-transaction adoption identity verification failed")
        # Only after exact refs are observed may each canonical child become
        # READY. A crash here is recoverable because the parent journal and
        # refs prove the outcome without rebuilding the old dry-run state.
        allocation_by_iteration = {item["iteration"]: item for item in allocations}
        for child in reservation_journals:
            allocation = allocation_by_iteration[child.iteration or ""]
            if child.phase == "PLANNED":
                child = core.advance_operation_journal(
                    common_dir,
                    child,
                    "RESERVED",
                    allocation_object=allocation["allocation_object"],
                    created_refs=(allocation["allocation_ref"], allocation["base_ref"]),
                )
            if child.phase == "RESERVED":
                core.advance_operation_journal(common_dir, child, "READY")
        ready = dict(committed)
        ready["phase"] = "READY"
        _write_journal(journal_path, ready, common_dir, create=False)

    return UpgradeApplyResult(
        schema_version=SCHEMA_V1,
        command="upgrade-apply",
        action_level="notify",
        pushed=False,
        project_root=plan.project_root,
        operation_id=plan.operation_id,
        plan_digest=plan.plan_digest,
        phase="succeeded",
        adopted_iterations=adopted,
        preserved_iterations=preserved,
        journal_path=str(journal_path),
        idempotent=idempotent,
        next_gate="validate-v2-adoption",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--accept-plan-digest")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = (
            apply_upgrade_plan(args.project_root, accepted_plan_digest=args.accept_plan_digest)
            if args.accept_plan_digest
            else build_upgrade_plan(args.project_root)
        )
    except UpgradeError as exc:
        payload = {
            "schema_version": SCHEMA_V1,
            "command": "upgrade-dry-run",
            "action_level": "silent",
            "pushed": False,
            "phase": "blocked",
            "blocking_reasons": [str(exc)],
            "next_gate": "fix-input-or-reconcile",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        else:
            print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    payload = result.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(f"Upgrade: {result.phase}; digest={result.plan_digest}")
        if isinstance(result, UpgradePlan):
            for item in result.iterations:
                print(f"- PRD-{item.iteration}: {item.lifecycle} -> {item.disposition}")
    return 0 if result.phase in {"planned", "succeeded"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
