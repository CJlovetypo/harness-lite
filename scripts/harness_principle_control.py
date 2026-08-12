#!/usr/bin/env python3
"""Composite principle-impact control with mandatory progress evidence.

``harness_principle_audit`` deliberately persists product-impact facts without
editing governance files, while ``harness_progress`` owns crash-safe immutable
event appends.  This module composes those two public APIs into one operation:
an audit receipt is not considered complete by the public control gate until
its deterministic CHECKPOINT event is also present with exact bytes.

Planning is read-only.  Applying writes a common-dir journal before invoking
either child, resumes both children by their stable operation/event identity,
and never commits, pushes, merges, changes refs, or mutates worktrees.
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Mapping

try:
    from . import harness_principle_audit as principle_audit
    from . import harness_progress as progress
except ImportError:  # pragma: no cover - direct script/test import
    import harness_principle_audit as principle_audit
    import harness_progress as progress


PLAN_SCHEMA = "harness-lite.principle-control-plan/v1"
JOURNAL_SCHEMA = "harness-lite.principle-control-journal/v1"
RESULT_SCHEMA = "harness-lite.principle-control-result/v1"
GATE_SCHEMA = "harness-lite.principle-control-gate/v1"
PROGRESS_BINDING_SCHEMA = "harness-lite.principle-control-progress-binding/v1"

OPERATION_RE = re.compile(r"OP-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
REGISTRY_PARTS = ("project-harness", "principle-control", "v1")
PHASES = (
    "PLANNED",
    "AUDIT_APPLIED",
    "PROGRESS_PLANNED",
    "PROGRESS_APPLIED",
    "COMPLETE",
    "FAILED_NEEDS_RECONCILE",
)
EXCLUSIONS = (
    "no PRD/SPEC approval",
    "no implementation authorization",
    "no governance document rewrite",
    "no commit",
    "no push",
    "no ref update",
    "no worktree mutation",
)


class PrincipleControlError(RuntimeError):
    """The composite operation cannot prove an exact, recoverable state."""


class InjectedControlCrash(BaseException):
    """Fault-injection signal that intentionally leaves a resumable journal."""


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PrincipleControlPlan:
    plan_digest: str
    manifest: dict[str, object]
    audit_plan: principle_audit.PrincipleImpactAuditPlan
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

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLAN_SCHEMA,
            "operation_id": self.operation_id,
            "iteration": self.iteration,
            "project_root": self.project_root,
            "audit_plan_digest": self.audit_plan.plan_digest,
            "disposition": self.audit_plan.disposition,
            "generation": self.audit_plan.generation,
            "event_id": self.manifest["event_template"]["event_id"],  # type: ignore[index]
            "plan_digest": self.plan_digest,
            "blocking_reasons": [item.as_dict() for item in self.blockers],
            "exclusions": list(EXCLUSIONS),
            "pushed": False,
        }


@dataclass(frozen=True)
class PrincipleControlResult:
    operation_id: str
    iteration: str
    plan_digest: str
    phase: str
    audit_receipt_digest: str
    progress_event_id: str
    progress_plan_digest: str
    journal_path: str
    idempotent: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESULT_SCHEMA,
            **asdict(self),
            "exclusions": list(EXCLUSIONS),
            "pushed": False,
        }


@dataclass(frozen=True)
class PrincipleControlGate:
    iteration: str
    allowed: bool
    audit_allowed: bool
    evidence_complete: bool
    drift: bool
    disposition: str | None
    receipt_digest: str | None
    operation_id: str | None
    progress_event_id: str | None
    blockers: tuple[str, ...]
    next_gate: str

    def as_dict(self) -> dict[str, object]:
        return {"schema_version": GATE_SCHEMA, **asdict(self)}


Failpoint = Callable[[str], None]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_operation(value: object) -> str:
    if not isinstance(value, str) or OPERATION_RE.fullmatch(value) is None:
        raise PrincipleControlError("operation_id must use OP- plus 32 lowercase hexadecimal characters")
    return value


def _validate_iteration(value: object) -> str:
    if (
        not isinstance(value, str)
        or ITERATION_RE.fullmatch(value) is None
        or value != f"{int(value):03d}"
        or int(value) < 1
    ):
        raise PrincipleControlError("iteration must be a canonical zero-padded decimal identity")
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise PrincipleControlError(f"{label} must be a SHA-256 digest")
    return value


def _registry_root(common: Path) -> Path:
    return common.joinpath(*REGISTRY_PARTS)


def _journal_path(common: Path, operation_id: str) -> Path:
    return _registry_root(common) / "operations" / f"{_validate_operation(operation_id)}.json"


def _lock_path(common: Path, iteration: str, principle_sha256: str) -> Path:
    return _registry_root(common) / "locks" / f"I{_validate_iteration(iteration)}-{_validate_digest(principle_sha256, 'principle hash')}.lock"


def _assert_operational_path(path: Path, common: Path) -> None:
    base = common.resolve()
    absolute = path.absolute()
    try:
        absolute.relative_to(base)
        absolute.resolve(strict=False).relative_to(base)
    except ValueError as exc:
        raise PrincipleControlError(f"principle-control path escapes Git common directory: {path}") from exc
    current = absolute
    while current != base:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.exists() and (current.is_symlink() or is_junction()):
            raise PrincipleControlError(f"principle-control path traverses a link or junction: {current}")
        if current.parent == current:
            raise PrincipleControlError(f"cannot prove principle-control path containment: {path}")
        current = current.parent


def _atomic_json(path: Path, value: Mapping[str, object], common: Path) -> None:
    _assert_operational_path(path, common)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_operational_path(path.parent, common)
    raw = _canonical_json(dict(value)) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".principle-control.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None  # type: ignore[assignment]
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _read_json(path: Path, common: Path) -> dict[str, object]:
    _assert_operational_path(path, common)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PrincipleControlError(f"cannot read principle-control journal: {path}") from exc
    if len(raw) < 2 or len(raw) > MAX_JOURNAL_BYTES:
        raise PrincipleControlError(f"principle-control journal exceeds its safe size: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrincipleControlError(f"principle-control journal is corrupt: {path}") from exc
    if not isinstance(value, dict):
        raise PrincipleControlError(f"principle-control journal is not an object: {path}")
    return value


@contextlib.contextmanager
def _file_lock(path: Path, common: Path, timeout_seconds: float = 30.0):
    _assert_operational_path(path, common)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_operational_path(path.parent, common)
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
                    raise PrincipleControlError(f"timed out waiting for principle-control lock: {path}") from exc
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


def _progress_spec(plan: principle_audit.PrincipleImpactAuditPlan) -> dict[str, object]:
    raw = plan.manifest.get("progress_checkpoint")
    if not isinstance(raw, dict):
        raise PrincipleControlError("audit plan lacks its public progress checkpoint spec")
    required = {
        "schema_version",
        "event_id",
        "iteration",
        "scope",
        "event_type",
        "event_key",
        "operation_id",
        "source_ref",
        "source_commit",
        "evidence_refs",
        "summary_code",
        "requires_session_id",
        "requires_occurred_at",
        "requires_causal_parent",
        "write_progress",
    }
    if set(raw) != required:
        raise PrincipleControlError("audit progress checkpoint spec fields are invalid")
    if (
        raw.get("iteration") != plan.iteration
        or raw.get("operation_id") != plan.operation_id
        or raw.get("scope") != "principle"
        or raw.get("event_type") != "CHECKPOINT"
        or raw.get("requires_session_id") is not True
        or raw.get("requires_occurred_at") is not True
        or raw.get("requires_causal_parent") is not True
        or raw.get("write_progress") is not False
        or not isinstance(raw.get("evidence_refs"), list)
    ):
        raise PrincipleControlError("audit progress checkpoint spec is inconsistent")
    return dict(raw)


def _event_from_spec(
    spec: Mapping[str, object],
    *,
    session_id: str,
    occurred_at: str,
    causal_parent: str,
    evidence_refs: tuple[str, ...],
) -> progress.ProgressEventV2:
    try:
        event = progress.build_progress_event(
            session_id=session_id,
            iteration=str(spec["iteration"]),
            scope=str(spec["scope"]),
            event_type=str(spec["event_type"]),
            event_key=str(spec["event_key"]),
            occurred_at=occurred_at,
            source_ref=str(spec["source_ref"]),
            source_commit=str(spec["source_commit"]),
            operation_id=str(spec["operation_id"]),
            causal_parent=causal_parent,
            evidence_refs=evidence_refs,
            summary=str(spec["summary_code"]),
        )
    except (progress.ProgressError, TypeError, ValueError) as exc:
        raise PrincipleControlError(f"principle-control progress event is invalid: {exc}") from exc
    if event.event_id != spec.get("event_id"):
        raise PrincipleControlError("audit progress event identity differs from its deterministic spec")
    return event


def _predecessor_blockers(
    audit_plan: principle_audit.PrincipleImpactAuditPlan,
) -> tuple[Blocker, ...]:
    if audit_plan.supersedes is None:
        return ()
    common = Path(audit_plan.git_common_dir).resolve()
    try:
        predecessor = principle_audit.load_principle_impact_audit_receipt(
            common,
            audit_plan.iteration,
            audit_plan.current_principle_sha256,
            audit_plan.supersedes,
        )
    except principle_audit.PrincipleAuditError as exc:
        return (Blocker("principle-control-predecessor-invalid", str(exc)),)
    if predecessor is None:
        return (Blocker("principle-control-predecessor-missing", "superseded audit receipt is absent"),)
    path = _journal_path(common, predecessor.operation_id)
    if not path.is_file():
        return (
            Blocker(
                "principle-control-predecessor-progress-missing",
                "a successor audit cannot start before its predecessor has exact progress evidence",
            ),
        )
    try:
        journal = _validate_journal(_read_json(path, common), path=path, common=common)
    except PrincipleControlError as exc:
        return (Blocker("principle-control-predecessor-journal-invalid", str(exc)),)
    if journal.get("phase") != "COMPLETE" or (
        not isinstance(journal.get("audit_receipt"), Mapping)
        or journal["audit_receipt"].get("receipt_digest") != predecessor.receipt_digest  # type: ignore[index]
    ):
        return (
            Blocker(
                "principle-control-predecessor-incomplete",
                "a successor audit cannot start before its predecessor composite operation completes",
            ),
        )
    return ()


def plan_principle_control(
    project_root: Path | str,
    *,
    decision: principle_audit.PrincipleAuditDecision,
    session_id: str,
    occurred_at: str,
    causal_parent: str,
    operation_id: str | None = None,
) -> PrincipleControlPlan:
    """Build an exact zero-write audit + progress plan."""

    if not isinstance(causal_parent, str) or not causal_parent.strip():
        raise PrincipleControlError("principle audit CHECKPOINT requires an explicit causal parent")
    audit_plan = principle_audit.plan_principle_impact_audit(
        project_root,
        decision=decision,
        operation_id=operation_id,
    )
    spec = _progress_spec(audit_plan)
    base_refs = tuple(str(item) for item in spec["evidence_refs"])  # type: ignore[index]
    template = _event_from_spec(
        spec,
        session_id=session_id,
        occurred_at=occurred_at,
        causal_parent=causal_parent,
        evidence_refs=base_refs,
    )
    audit_blockers = tuple(Blocker(f"audit:{item.code}", item.message) for item in audit_plan.blockers)
    predecessor_blockers = _predecessor_blockers(audit_plan) if not audit_blockers else ()
    blockers = (*audit_blockers, *predecessor_blockers)
    manifest: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "operation_id": audit_plan.operation_id,
        "project_root": audit_plan.project_root,
        "git_common_dir": audit_plan.git_common_dir,
        "iteration": audit_plan.iteration,
        "current_principle_sha256": audit_plan.current_principle_sha256,
        "audit": {
            "schema_version": principle_audit.PLAN_SCHEMA,
            "plan_digest": audit_plan.plan_digest,
            "manifest": audit_plan.manifest,
            "disposition": audit_plan.disposition,
            "generation": audit_plan.generation,
            "supersedes": audit_plan.supersedes,
        },
        "event_template": template.as_dict(),
        "event_evidence_rule": {
            "base_refs": list(base_refs),
            "tail": [f"audit-plan:{audit_plan.plan_digest}", "audit-receipt:<receipt-digest>"],
        },
        "blocking_reasons": [item.code for item in blockers],
        "exclusions": list(EXCLUSIONS),
        "pushed": False,
    }
    return PrincipleControlPlan(_digest(manifest), manifest, audit_plan, tuple(blockers))


def _validate_plan(plan: PrincipleControlPlan) -> None:
    if not isinstance(plan, PrincipleControlPlan):
        raise TypeError("plan must be PrincipleControlPlan")
    manifest = plan.manifest
    required = {
        "schema_version",
        "operation_id",
        "project_root",
        "git_common_dir",
        "iteration",
        "current_principle_sha256",
        "audit",
        "event_template",
        "event_evidence_rule",
        "blocking_reasons",
        "exclusions",
        "pushed",
    }
    if set(manifest) != required or manifest.get("schema_version") != PLAN_SCHEMA:
        raise PrincipleControlError("principle-control plan schema is invalid")
    if plan.plan_digest != _digest(manifest):
        raise PrincipleControlError("principle-control plan differs from its digest")
    if manifest.get("exclusions") != list(EXCLUSIONS) or manifest.get("pushed") is not False:
        raise PrincipleControlError("principle-control plan exclusions/push state changed")
    if manifest.get("operation_id") != plan.audit_plan.operation_id or manifest.get("iteration") != plan.audit_plan.iteration:
        raise PrincipleControlError("principle-control and audit identities differ")
    audit_value = manifest.get("audit")
    if not isinstance(audit_value, Mapping) or audit_value.get("plan_digest") != plan.audit_plan.plan_digest or audit_value.get("manifest") != plan.audit_plan.manifest:
        raise PrincipleControlError("principle-control audit binding changed")
    if hashlib.sha256(_canonical_json(plan.audit_plan.manifest)).hexdigest() != plan.audit_plan.plan_digest:
        raise PrincipleControlError("embedded audit plan digest is invalid")
    template = progress.ProgressEventV2.from_dict(manifest.get("event_template"))
    if template.operation_id != plan.operation_id or template.iteration != plan.iteration:
        raise PrincipleControlError("principle-control event template identity changed")
    expected_codes = [item.code for item in plan.blockers]
    if manifest.get("blocking_reasons") != expected_codes:
        raise PrincipleControlError("principle-control blockers differ from the accepted plan")


JOURNAL_FIELDS = {
    "schema_version",
    "operation_id",
    "plan_digest",
    "accepted_plan_digest",
    "project_root",
    "git_common_dir",
    "iteration",
    "phase",
    "manifest",
    "audit_receipt",
    "event",
    "progress_binding",
    "history",
    "error",
}


def _new_journal(plan: PrincipleControlPlan) -> dict[str, object]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schema_version": JOURNAL_SCHEMA,
        "operation_id": plan.operation_id,
        "plan_digest": plan.plan_digest,
        "accepted_plan_digest": plan.plan_digest,
        "project_root": plan.project_root,
        "git_common_dir": plan.git_common_dir,
        "iteration": plan.iteration,
        "phase": "PLANNED",
        "manifest": plan.manifest,
        "audit_receipt": None,
        "event": None,
        "progress_binding": None,
        "history": [{"phase": "PLANNED", "at": now}],
        "error": None,
    }


def _validate_progress_binding(value: object, event: progress.ProgressEventV2) -> dict[str, object]:
    required = {
        "schema_version",
        "event_id",
        "event_digest",
        "plan_digest",
        "action",
        "before_sha256",
        "after_sha256",
        "event_bytes_sha256",
        "journal_path",
        "result_phase",
        "result_sha256",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != PROGRESS_BINDING_SCHEMA:
        raise PrincipleControlError("principle-control progress binding schema is invalid")
    if value.get("event_id") != event.event_id or value.get("event_digest") != _digest(event.as_dict()):
        raise PrincipleControlError("principle-control progress binding event identity changed")
    for field in ("plan_digest", "before_sha256", "after_sha256", "event_bytes_sha256"):
        _validate_digest(value.get(field), f"progress binding {field}")
    if value.get("action") not in {"APPEND", "IDEMPOTENT"}:
        raise PrincipleControlError("principle-control progress action is invalid")
    if value.get("result_phase") not in {None, "APPLIED"}:
        raise PrincipleControlError("principle-control progress result phase is invalid")
    if value.get("result_phase") == "APPLIED":
        _validate_digest(value.get("result_sha256"), "progress binding result SHA-256")
    elif value.get("result_sha256") is not None:
        raise PrincipleControlError("unapplied principle-control progress has a result digest")
    if not isinstance(value.get("journal_path"), str):
        raise PrincipleControlError("principle-control progress journal path is invalid")
    return dict(value)


def _validate_journal(value: object, *, path: Path, common: Path) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != JOURNAL_FIELDS or value.get("schema_version") != JOURNAL_SCHEMA:
        raise PrincipleControlError(f"principle-control journal schema is invalid: {path}")
    if value.get("phase") not in PHASES:
        raise PrincipleControlError(f"principle-control journal phase is invalid: {path}")
    operation = _validate_operation(value.get("operation_id"))
    _validate_digest(value.get("plan_digest"), "journal plan digest")
    if value.get("accepted_plan_digest") != value.get("plan_digest"):
        raise PrincipleControlError(f"principle-control accepted plan identity is invalid: {path}")
    if value.get("git_common_dir") != str(common) or path != _journal_path(common, operation):
        raise PrincipleControlError(f"principle-control journal path identity is invalid: {path}")
    manifest = value.get("manifest")
    if not isinstance(manifest, dict) or _digest(manifest) != value.get("plan_digest"):
        raise PrincipleControlError(f"principle-control journal manifest digest is invalid: {path}")
    if value.get("project_root") != manifest.get("project_root") or value.get("iteration") != manifest.get("iteration"):
        raise PrincipleControlError(f"principle-control journal project/iteration identity is invalid: {path}")
    history = value.get("history")
    if not isinstance(history, list) or not history or any(
        not isinstance(item, dict) or set(item) - {"phase", "at", "error"} or item.get("phase") not in PHASES
        for item in history
    ):
        raise PrincipleControlError(f"principle-control journal history is invalid: {path}")
    if history[-1].get("phase") != value.get("phase"):
        raise PrincipleControlError(f"principle-control journal phase/history differ: {path}")
    event_raw = value.get("event")
    event = progress.ProgressEventV2.from_dict(event_raw) if isinstance(event_raw, dict) else None
    if value.get("phase") in {"AUDIT_APPLIED", "PROGRESS_PLANNED", "PROGRESS_APPLIED", "COMPLETE"}:
        receipt = value.get("audit_receipt")
        if not isinstance(receipt, dict) or not isinstance(receipt.get("receipt_digest"), str) or event is None:
            raise PrincipleControlError(f"principle-control journal lacks audit/event evidence: {path}")
    if value.get("progress_binding") is not None:
        if event is None:
            raise PrincipleControlError(f"principle-control progress binding lacks its event: {path}")
        _validate_progress_binding(value["progress_binding"], event)
    if value.get("phase") in {"PROGRESS_PLANNED", "PROGRESS_APPLIED", "COMPLETE"} and value.get("progress_binding") is None:
        raise PrincipleControlError(f"principle-control journal lacks progress binding: {path}")
    if value.get("phase") in {"PROGRESS_APPLIED", "COMPLETE"}:
        binding = value["progress_binding"]
        assert isinstance(binding, dict)
        if binding.get("result_phase") != "APPLIED":
            raise PrincipleControlError(f"principle-control journal progress is not APPLIED: {path}")
    error = value.get("error")
    if error is not None and (not isinstance(error, str) or len(error) > 2000):
        raise PrincipleControlError(f"principle-control journal error is invalid: {path}")
    return dict(value)


def _advance(
    path: Path,
    journal: Mapping[str, object],
    common: Path,
    phase: str,
    *,
    error: str | None = None,
    updates: Mapping[str, object] | None = None,
) -> dict[str, object]:
    updated = dict(journal)
    if updates:
        updated.update(updates)
    updated["phase"] = phase
    updated["error"] = error
    history = list(updated["history"])
    if not history or history[-1].get("phase") != phase:
        entry: dict[str, object] = {
            "phase": phase,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if error:
            entry["error"] = error
        history.append(entry)
    updated["history"] = history
    _atomic_json(path, updated, common)
    return updated


def _exact_event_for_receipt(
    plan: PrincipleControlPlan,
    receipt: principle_audit.PrincipleImpactAuditReceipt,
) -> progress.ProgressEventV2:
    if (
        receipt.operation_id != plan.operation_id
        or receipt.plan_digest != plan.audit_plan.plan_digest
        or receipt.iteration != plan.iteration
        or receipt.disposition != plan.audit_plan.disposition
        or receipt.generation != plan.audit_plan.generation
        or receipt.supersedes != plan.audit_plan.supersedes
    ):
        raise PrincipleControlError("audit receipt differs from the accepted composite plan")
    original = _progress_spec(plan.audit_plan)
    final = receipt.progress_checkpoint
    expected_final = dict(original)
    expected_final["evidence_refs"] = [
        *original["evidence_refs"],  # type: ignore[misc]
        f"audit-plan:{plan.audit_plan.plan_digest}",
        f"audit-receipt:{receipt.receipt_digest}",
    ]
    if final != expected_final:
        raise PrincipleControlError("audit receipt progress spec differs from its exact planned derivation")
    template = progress.ProgressEventV2.from_dict(plan.manifest["event_template"])
    event = _event_from_spec(
        final,
        session_id=template.session_id,
        occurred_at=template.occurred_at,
        causal_parent=template.causal_parent or "",
        evidence_refs=tuple(str(item) for item in final["evidence_refs"]),
    )
    expected_template = template.as_dict()
    actual = event.as_dict()
    expected_template["evidence_refs"] = actual["evidence_refs"]
    if actual != expected_template:
        raise PrincipleControlError("receipt-bound progress event changed outside its evidence refs")
    return event


def _progress_binding(plan: progress.ProgressAppendPlan) -> dict[str, object]:
    return {
        "schema_version": PROGRESS_BINDING_SCHEMA,
        "event_id": plan.event.event_id,
        "event_digest": _digest(plan.event.as_dict()),
        "plan_digest": plan.plan_digest,
        "action": plan.action,
        "before_sha256": plan.before_sha256,
        "after_sha256": plan.after_sha256,
        "event_bytes_sha256": hashlib.sha256(plan.event_bytes).hexdigest(),
        "journal_path": str(progress.journal_path(plan.git_common_dir, plan.operation_id, plan.event.event_id)),
        "result_phase": None,
        "result_sha256": None,
    }


def _event_in_current_progress(
    root: Path,
    event: progress.ProgressEventV2,
    exact_bytes: bytes,
) -> tuple[bool, tuple[str, ...]]:
    target = root / progress.PROGRESS_PATH
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return False, (f"principle-control-progress-unreadable:{exc}",)
    parsed = progress.parse_progress_events(raw, source="principle-control-current-progress")
    blockers = [f"{item.code}:{item.message}" for item in parsed.blockers]
    matches = [item for item in parsed.events if item.identity == event.event_id]
    if len(matches) != 1:
        blockers.append("principle-control-progress-event-missing-or-duplicated")
    elif matches[0].exact_bytes != exact_bytes:
        blockers.append("principle-control-progress-event-bytes-mismatch")
    return not blockers, tuple(blockers)


def _load_bound_progress_plan(
    common: Path,
    event: progress.ProgressEventV2,
    binding: Mapping[str, object],
) -> progress.ProgressAppendPlan:
    try:
        child = progress.load_progress_append_plan(common, event.operation_id, event.event_id)
    except progress.ProgressError as exc:
        raise PrincipleControlError(f"progress child journal is invalid: {exc}") from exc
    expected = _progress_binding(child)
    for field in (
        "schema_version",
        "event_id",
        "event_digest",
        "plan_digest",
        "action",
        "before_sha256",
        "after_sha256",
        "event_bytes_sha256",
        "journal_path",
    ):
        if binding.get(field) != expected.get(field):
            raise PrincipleControlError(f"progress child binding changed: {field}")
    if child.event != event:
        raise PrincipleControlError("progress child journal carries another event")
    return child


def _completion_blockers(
    *,
    root: Path,
    common: Path,
    journal: Mapping[str, object],
) -> tuple[str, ...]:
    blockers: list[str] = []
    receipt_raw = journal.get("audit_receipt")
    event_raw = journal.get("event")
    binding_raw = journal.get("progress_binding")
    if not isinstance(receipt_raw, Mapping) or not isinstance(event_raw, dict) or not isinstance(binding_raw, Mapping):
        return ("principle-control-composite-evidence-missing",)
    try:
        receipt = principle_audit.load_principle_impact_audit_receipt(
            common,
            str(journal["iteration"]),
            str(journal["manifest"]["current_principle_sha256"]),  # type: ignore[index]
            str(receipt_raw.get("receipt_digest")),
        )
    except principle_audit.PrincipleAuditError as exc:
        blockers.append(f"principle-control-audit-receipt-invalid:{exc}")
        receipt = None
    if receipt is None or receipt.as_dict() != dict(receipt_raw):
        blockers.append("principle-control-audit-receipt-missing-or-drifted")
    try:
        event = progress.ProgressEventV2.from_dict(event_raw)
        binding = _validate_progress_binding(binding_raw, event)
        child = _load_bound_progress_plan(common, event, binding)
    except (PrincipleControlError, progress.ProgressError) as exc:
        blockers.append(f"principle-control-progress-binding-invalid:{exc}")
    else:
        exact, event_blockers = _event_in_current_progress(root, event, child.event_bytes)
        if not exact:
            blockers.extend(event_blockers)
        if binding.get("result_phase") != "APPLIED" or binding.get("result_sha256") != child.after_sha256:
            blockers.append("principle-control-progress-result-incomplete")
    return tuple(dict.fromkeys(blockers))


def apply_principle_control(
    plan: PrincipleControlPlan,
    *,
    accept_plan_digest: str,
    fault_injector: Failpoint | None = None,
) -> PrincipleControlResult:
    """Persist or resume one exact audit receipt plus CHECKPOINT event."""

    _validate_plan(plan)
    if plan.blockers:
        raise PrincipleControlError(
            "principle-control plan is blocked: " + "; ".join(item.code for item in plan.blockers)
        )
    if accept_plan_digest != plan.plan_digest:
        raise PrincipleControlError("accepted digest differs from the reviewed principle-control plan")
    root = Path(plan.project_root).resolve()
    common = Path(plan.git_common_dir).resolve()
    journal_path = _journal_path(common, plan.operation_id)
    lock = _lock_path(common, plan.iteration, str(plan.manifest["current_principle_sha256"]))
    with _file_lock(lock, common):
        resumed = journal_path.exists()
        if resumed:
            journal = _validate_journal(_read_json(journal_path, common), path=journal_path, common=common)
            if journal.get("plan_digest") != plan.plan_digest or journal.get("manifest") != plan.manifest:
                raise PrincipleControlError("durable principle-control journal differs from the accepted plan")
            if journal.get("phase") == "FAILED_NEEDS_RECONCILE":
                raise PrincipleControlError(f"principle-control operation requires reconcile: {journal.get('error')}")
            if journal.get("phase") == "COMPLETE":
                completion = _completion_blockers(root=root, common=common, journal=journal)
                if completion:
                    raise PrincipleControlError("completed principle-control evidence is stale: " + "; ".join(completion))
                receipt = journal["audit_receipt"]
                event = journal["event"]
                binding = journal["progress_binding"]
                assert isinstance(receipt, Mapping) and isinstance(event, Mapping) and isinstance(binding, Mapping)
                return PrincipleControlResult(
                    plan.operation_id,
                    plan.iteration,
                    plan.plan_digest,
                    "COMPLETE",
                    str(receipt["receipt_digest"]),
                    str(event["event_id"]),
                    str(binding["plan_digest"]),
                    str(journal_path),
                    True,
                )
        else:
            journal = _new_journal(plan)
            _atomic_json(journal_path, journal, common)
        if fault_injector is not None:
            fault_injector("after_journal")
        try:
            predecessor = _predecessor_blockers(plan.audit_plan)
            if predecessor:
                raise PrincipleControlError("principle-control predecessor changed: " + "; ".join(item.code for item in predecessor))
            audit_result = principle_audit.apply_principle_impact_audit(
                plan.audit_plan,
                accept_plan_digest=plan.audit_plan.plan_digest,
            )
            event = _exact_event_for_receipt(plan, audit_result.receipt)
            existing_receipt = journal.get("audit_receipt")
            if existing_receipt is not None and existing_receipt != audit_result.receipt.as_dict():
                raise PrincipleControlError("durable composite journal carries another audit receipt")
            existing_event = journal.get("event")
            if existing_event is not None and existing_event != event.as_dict():
                raise PrincipleControlError("durable composite journal carries another progress event")
            if journal.get("phase") == "PLANNED":
                journal = _advance(
                    journal_path,
                    journal,
                    common,
                    "AUDIT_APPLIED",
                    updates={"audit_receipt": audit_result.receipt.as_dict(), "event": event.as_dict()},
                )
            if fault_injector is not None:
                fault_injector("after_audit_before_progress")

            raw_binding = journal.get("progress_binding")
            if isinstance(raw_binding, Mapping):
                child_plan = _load_bound_progress_plan(common, event, raw_binding)
                binding = dict(raw_binding)
            else:
                child_plan = progress.plan_progress_append(project_root=root, event=event)
                binding = _progress_binding(child_plan)
                journal = _advance(
                    journal_path,
                    journal,
                    common,
                    "PROGRESS_PLANNED",
                    updates={"progress_binding": binding},
                )
            exact_before_apply, _ = _event_in_current_progress(root, event, child_plan.event_bytes)
            if binding.get("result_phase") == "APPLIED" and exact_before_apply:
                result_sha = str(binding["result_sha256"])
            else:
                child_result = progress.apply_progress_append(
                    child_plan,
                    accept_plan_digest=child_plan.plan_digest,
                )
                if child_result.phase != "APPLIED":
                    raise PrincipleControlError("progress child did not reach APPLIED")
                result_sha = child_result.result_sha256
                binding.update({"result_phase": "APPLIED", "result_sha256": result_sha})
            journal = _advance(
                journal_path,
                journal,
                common,
                "PROGRESS_APPLIED",
                updates={"progress_binding": binding},
            )
            if fault_injector is not None:
                fault_injector("after_progress_before_complete")
            completion = _completion_blockers(root=root, common=common, journal=journal)
            if completion:
                raise PrincipleControlError("principle-control completion evidence is invalid: " + "; ".join(completion))
            journal = _advance(journal_path, journal, common, "COMPLETE")
            return PrincipleControlResult(
                plan.operation_id,
                plan.iteration,
                plan.plan_digest,
                "COMPLETE",
                audit_result.receipt.receipt_digest,
                event.event_id,
                child_plan.plan_digest,
                str(journal_path),
                resumed,
            )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            with contextlib.suppress(Exception):
                _advance(
                    journal_path,
                    journal,
                    common,
                    "FAILED_NEEDS_RECONCILE",
                    error=message[:2000],
                )
            if isinstance(exc, PrincipleControlError):
                raise
            if isinstance(exc, (principle_audit.PrincipleAuditError, progress.ProgressError)):
                raise PrincipleControlError(message) from exc
            raise PrincipleControlError(message) from exc


def load_principle_control_journal(
    project_root: Path | str,
    operation_id: str,
) -> dict[str, object] | None:
    """Load one strict composite journal for status/recovery tooling."""

    try:
        common = progress.resolve_git_common_dir(project_root)
    except progress.ProgressError as exc:
        raise PrincipleControlError(str(exc)) from exc
    path = _journal_path(common, operation_id)
    if not path.is_file():
        return None
    return _validate_journal(_read_json(path, common), path=path, common=common)


def current_principle_control_gate(
    project_root: Path | str,
    *,
    iteration: str,
    authority_ref: str | None = None,
) -> PrincipleControlGate:
    """Require current audit policy *and* its exact immutable progress event."""

    number = _validate_iteration(iteration)
    try:
        audit_gate = principle_audit.current_principle_gate(
            project_root,
            iteration=number,
            authority_ref=authority_ref,
        )
    except principle_audit.PrincipleAuditError as exc:
        return PrincipleControlGate(
            number,
            False,
            False,
            False,
            True,
            None,
            None,
            None,
            None,
            (f"principle-control-audit-gate-invalid:{exc}",),
            "reconcile-principle-control",
        )
    if not audit_gate.drift:
        return PrincipleControlGate(
            number,
            audit_gate.allowed,
            audit_gate.allowed,
            True,
            False,
            audit_gate.disposition,
            None,
            None,
            None,
            audit_gate.blockers,
            audit_gate.next_gate,
        )
    blockers = list(audit_gate.blockers)
    evidence_complete = False
    operation_id: str | None = None
    event_id: str | None = None
    if audit_gate.receipt_digest is None:
        blockers.append("principle-control-audit-receipt-missing")
    else:
        try:
            common = progress.resolve_git_common_dir(project_root)
            receipt = principle_audit.load_principle_impact_audit_receipt(
                common,
                number,
                audit_gate.current_principle_sha256,
                audit_gate.receipt_digest,
            )
            if receipt is None:
                raise PrincipleControlError("current audit receipt is absent")
            operation_id = receipt.operation_id
            path = _journal_path(common, operation_id)
            if not path.is_file():
                raise PrincipleControlError("current audit has no composite journal")
            journal = _validate_journal(_read_json(path, common), path=path, common=common)
            if journal.get("phase") != "COMPLETE":
                raise PrincipleControlError(f"composite journal is {journal.get('phase')}")
            recorded_receipt = journal.get("audit_receipt")
            if not isinstance(recorded_receipt, Mapping) or recorded_receipt.get("receipt_digest") != receipt.receipt_digest:
                raise PrincipleControlError("composite journal names another audit receipt")
            event_raw = journal.get("event")
            if isinstance(event_raw, Mapping):
                event_id = str(event_raw.get("event_id"))
            completion = _completion_blockers(
                root=Path(project_root).resolve(),
                common=common,
                journal=journal,
            )
            if completion:
                blockers.extend(completion)
            else:
                evidence_complete = True
        except (PrincipleControlError, principle_audit.PrincipleAuditError, progress.ProgressError) as exc:
            blockers.append(f"principle-control-incomplete:{exc}")
    allowed = audit_gate.allowed and evidence_complete
    if audit_gate.allowed and not evidence_complete:
        blockers.append("principle-control-progress-evidence-required")
    return PrincipleControlGate(
        number,
        allowed,
        audit_gate.allowed,
        evidence_complete,
        audit_gate.drift,
        audit_gate.disposition,
        audit_gate.receipt_digest,
        operation_id,
        event_id,
        tuple(dict.fromkeys(blockers)),
        audit_gate.next_gate if not audit_gate.allowed else "candidate-or-integration-principle-gate" if allowed else "reconcile-principle-control",
    )


__all__ = [
    "Blocker",
    "EXCLUSIONS",
    "GATE_SCHEMA",
    "InjectedControlCrash",
    "JOURNAL_SCHEMA",
    "PLAN_SCHEMA",
    "PrincipleControlError",
    "PrincipleControlGate",
    "PrincipleControlPlan",
    "PrincipleControlResult",
    "RESULT_SCHEMA",
    "apply_principle_control",
    "current_principle_control_gate",
    "load_principle_control_journal",
    "plan_principle_control",
]
