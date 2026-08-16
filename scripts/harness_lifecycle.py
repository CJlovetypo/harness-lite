#!/usr/bin/env python3
"""Unified, fail-closed Harness Lite lifecycle orchestration.

The lifecycle facade deliberately applies at most one already-planned child
operation per accepted plan.  It composes reservation, workspace, candidate,
merge-train, public evidence, final acceptance, and conservative cleanup
without inventing a second approval path.  Every Git mutation remains inside
its public low-level adapter and requires the exact child digest, confirmation
token, or notification boundary defined there.  The facade never infers an
approval/token and never implements push, force, stash, reset, clean, rebase,
history rewrite, or branch deletion.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import types
import collections.abc
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, get_args, get_origin, get_type_hints

try:
    from . import harness_bundle as bundle
    from . import harness_coordinator as coordinator
    from . import harness_principle_audit as principle_audit
    from . import harness_progress as progress
    from . import harness_workspace as workspace
    from . import project_harness as core
    from .harness_decision import AuthorizationState, DecisionInput, classify
except ImportError:  # pragma: no cover - direct script execution
    import harness_bundle as bundle
    import harness_coordinator as coordinator
    import harness_principle_audit as principle_audit
    import harness_progress as progress
    import harness_workspace as workspace
    import project_harness as core
    from harness_decision import AuthorizationState, DecisionInput, classify


REQUEST_SCHEMA = "harness-lite.lifecycle-request/v1"
STATUS_SCHEMA = "harness-lite.lifecycle-status/v1"
ROUTE_SCHEMA = "harness-lite.lifecycle-route/v1"
PLAN_SCHEMA = "harness-lite.lifecycle-plan/v1"
RESULT_SCHEMA = "harness-lite.lifecycle-result/v1"
JOURNAL_SCHEMA = "harness-lite.lifecycle-journal/v2"
NOTIFICATION_SCHEMA = "harness-lite.lifecycle-notification/v2"
NOTIFICATION_RECEIPT_SCHEMA = "harness-lite.lifecycle-notification-receipt/v1"
TRAIN_SCHEMA = "harness-lite.train/v1"
ACTIVATION_PROGRESS_SCHEMA = "harness-lite.lifecycle-activation-progress/v1"
PROGRESS_STATUS_SCHEMA = "harness-lite.lifecycle-progress-status/v1"
STAGE_REQUEST_SCHEMA = "harness-lite.lifecycle-stage-request/v1"
STAGE_PLAN_SCHEMA = "harness-lite.lifecycle-stage-plan/v1"
ORDERED_PREPARATION_SCHEMA = "harness-lite.ordered-integration-preparation/v1"
STAGE_RESULT_SCHEMA = "harness-lite.lifecycle-stage-result/v1"
STAGE_JOURNAL_SCHEMA = "harness-lite.lifecycle-stage-journal/v1"
STAGE_STATUS_SCHEMA = "harness-lite.lifecycle-stage-status/v1"
STAGE_NOTIFICATION_RECEIPT_SCHEMA = "harness-lite.lifecycle-stage-notification-receipt/v1"

OPERATION_RE = re.compile(r"OP-[0-9a-f]{32}")
NOTIFICATION_ID_RE = re.compile(r"NT-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_PROGRESS_BYTES = 16 * 1024 * 1024
MAX_NOTIFICATION_BYTES = 64 * 1024
MAX_NOTIFICATION_RECEIPTS = 64
REGISTRY_PARTS = ("project-harness", "lifecycle", "v1")
EXCLUSIONS = (
    "no inferred approval",
    "no commit",
    "no push",
    "no merge",
    "no rebase",
    "no cherry-pick",
    "no stash",
    "no reset",
    "no clean",
    "no worktree removal",
    "no branch deletion",
)

NEXT_GATE_AFTER_ACTION = {
    "reserve-iteration": "plan-governance-bundle",
    "create-v2-bundle": "approve-prd",
    "activate-workspace": "implementation-ready",
}

STAGE_NEXT_GATE = {
    "local-main-release": "main-released-progress-recorded",
    "candidate-preverify": "confirm-candidate-seal",
    "candidate-register": "plan-integration",
    "integration-prepare": "confirm-integration-commit",
    "integration-commit": "register-public-integrated-evidence",
    "integrated-evidence-register": "plan-final-acceptance",
    # The exact final-acceptance apply owns the atomic main/ref CAS.  There is
    # no second main-advance mutation after this stage.
    "final-acceptance-register": "plan-cleanup",
    "integration-cleanup": "iteration-close-or-next-candidate",
}

STAGE_ORDER = tuple(STAGE_NEXT_GATE)
# Integration preparation has a composite public authority (candidate order +
# Git preparation plan).  It remains a durable lifecycle stage, but generic
# stage dispatch must not accept the unwrapped child artifact.
GENERIC_STAGE_ORDER = tuple(stage for stage in STAGE_ORDER if stage != "integration-prepare")
STAGE_JOURNAL_FIELDS = {
    "schema_version",
    "operation_id",
    "project_root",
    "phase",
    "active_plan",
    "completed_stages",
    "notification_receipts",
    "last_error",
}


class LifecycleError(RuntimeError):
    """Raised when an orchestration fact cannot be proven safely."""


Notify = Callable[[dict[str, object]], None]
Failpoint = Callable[[str], None]


@dataclass(frozen=True)
class LifecycleStagePlan:
    schema_version: str
    operation_id: str
    project_root: str
    stage: str
    child_operation_id: str
    child_schema_version: str
    child_plan_digest: str
    child_snapshot_digest: str
    subject_digest: str
    iterations: tuple[str, ...]
    action_level: str
    requires_confirmation: bool
    confirmation_action: str | None
    evidence_refs: tuple[str, ...]
    next_gate: str
    blockers: tuple[str, ...]
    plan_digest: str
    pushed: bool = False

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LifecycleStageResult:
    schema_version: str
    operation_id: str
    project_root: str
    stage: str
    accepted_plan_digest: str
    child_result: object
    child_result_digest: str
    evidence_refs: tuple[str, ...]
    notification_receipts: tuple[Mapping[str, object], ...]
    next_gate: str
    idempotent_replay: bool
    journal_path: str
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "child_result": _public_object_snapshot(self.child_result),
            "notification_receipts": [dict(item) for item in self.notification_receipts],
        }


@dataclass(frozen=True)
class OrderedIntegrationPreparationPlan:
    schema_version: str
    order_plan: object
    order_plan_digest: str
    ordered_iterations: tuple[str, ...]
    integration_plan: object
    lifecycle_plan: LifecycleStagePlan
    plan_digest: str
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "order_plan": _public_object_snapshot(self.order_plan),
            "order_plan_digest": self.order_plan_digest,
            "ordered_iterations": list(self.ordered_iterations),
            "integration_plan": _public_object_snapshot(self.integration_plan),
            "lifecycle_plan": self.lifecycle_plan.as_dict(),
            "plan_digest": self.plan_digest,
            "pushed": self.pushed,
        }


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _operation(value: str) -> str:
    operation = value.strip()
    if not OPERATION_RE.fullmatch(operation):
        raise LifecycleError("operation_id must use OP- plus 32 lowercase hexadecimal characters")
    return operation


def _iteration(value: object) -> str:
    number = str(value).strip()
    if not ITERATION_RE.fullmatch(number) or number != f"{int(number):03d}" or int(number) < 1:
        raise LifecycleError("iteration must be a canonical zero-padded decimal identity")
    return number


def _single_line(value: object, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise LifecycleError(f"{label} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise LifecycleError(f"{label} must be a non-empty single-line value of at most {maximum} characters")
    return result


def _child_operation(operation_id: str, stage: str) -> str:
    identity = hashlib.sha256(f"{operation_id}\0{stage}".encode("utf-8")).hexdigest()[:32]
    return f"OP-{identity}"


def _train_modules():
    try:
        from . import harness_integrated_evidence as integrated_registry
        from . import harness_train as train
        from . import harness_train_governance as train_governance
    except ImportError:  # pragma: no cover - direct script execution
        import harness_integrated_evidence as integrated_registry
        import harness_train as train
        import harness_train_governance as train_governance
    return train, train_governance, integrated_registry


def _final_acceptance_module():
    try:
        from . import harness_final_acceptance as final_acceptance
    except ImportError:
        try:
            import harness_final_acceptance as final_acceptance
        except ImportError:
            return None
    return final_acceptance


def _public_object_snapshot(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        snapshot = dict(value)
    else:
        serializer = getattr(value, "as_dict", None)
        if not callable(serializer):
            raise LifecycleError("lifecycle stage artifact lacks a public as_dict snapshot")
        snapshot = serializer()
    if not isinstance(snapshot, dict):
        raise LifecycleError("lifecycle stage artifact snapshot is not an object")
    try:
        canonical_json(snapshot)
    except (TypeError, ValueError) as exc:
        raise LifecycleError("lifecycle stage artifact snapshot is not canonical JSON") from exc
    return snapshot


def _decode_typed_json(value: object, annotation: object) -> object:
    """Strictly reconstruct a whitelisted public dataclass from JSON."""

    if annotation in {Any, object}:
        return value
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {types.UnionType, getattr(__import__("typing"), "Union")}:
        if value is None and type(None) in arguments:
            return None
        errors: list[Exception] = []
        for choice in arguments:
            if choice is type(None):
                continue
            try:
                return _decode_typed_json(value, choice)
            except (TypeError, ValueError, LifecycleError) as exc:
                errors.append(exc)
        raise LifecycleError("JSON value does not match its public union schema") from (errors[-1] if errors else None)
    if origin is tuple:
        if not isinstance(value, list):
            raise LifecycleError("JSON tuple field must be an array")
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_decode_typed_json(item, arguments[0]) for item in value)
        if len(arguments) != len(value):
            raise LifecycleError("JSON fixed tuple field length is invalid")
        return tuple(_decode_typed_json(item, item_type) for item, item_type in zip(value, arguments))
    if origin is list:
        if not isinstance(value, list):
            raise LifecycleError("JSON list field must be an array")
        item_type = arguments[0] if arguments else object
        return [_decode_typed_json(item, item_type) for item in value]
    if origin in {dict, Mapping, collections.abc.Mapping}:
        if not isinstance(value, dict):
            raise LifecycleError("JSON mapping field must be an object")
        key_type, item_type = arguments if len(arguments) == 2 else (str, object)
        return {
            _decode_typed_json(key, key_type): _decode_typed_json(item, item_type)
            for key, item in value.items()
        }
    if origin is not None and str(origin).endswith("Literal"):
        if value not in arguments:
            raise LifecycleError("JSON literal field is invalid")
        return value
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        if not isinstance(value, dict):
            raise LifecycleError(f"{annotation.__name__} JSON must be an object")
        hints = get_type_hints(annotation)
        expected = {field.name for field in dataclasses.fields(annotation)}
        if set(value) != expected:
            raise LifecycleError(f"{annotation.__name__} JSON fields are invalid")
        return annotation(
            **{
                field.name: _decode_typed_json(value[field.name], hints.get(field.name, object))
                for field in dataclasses.fields(annotation)
            }
        )
    if annotation is bool:
        if not isinstance(value, bool):
            raise LifecycleError("JSON boolean field is invalid")
        return value
    if annotation is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise LifecycleError("JSON integer field is invalid")
        return value
    if annotation is str:
        if not isinstance(value, str):
            raise LifecycleError("JSON string field is invalid")
        return value
    return value


def _decode_public_artifact(path: str | Path, expected_type: type) -> object:
    value = _read_json(Path(path).expanduser().resolve(), label=f"{expected_type.__name__} artifact")
    return _decode_typed_json(value, expected_type)


def _stage_artifact_type(stage: str) -> type:
    train, _governance, registry = _train_modules()
    types_by_stage: dict[str, type] = {
        "candidate-preverify": train.CandidateRegistrationPlan,
        "candidate-register": train.CandidateSealPlan,
        "integration-commit": train.IntegrationCommitPlan,
        "integrated-evidence-register": train.IntegrationCommitResult,
        "final-acceptance-register": registry.RegisteredIntegratedEvidence,
        "integration-cleanup": train.MainAdvanceResult,
    }
    if stage not in types_by_stage:
        raise LifecycleError(f"unsupported lifecycle stage: {stage}")
    return types_by_stage[stage]


def _load_confirmation_token(path: str | Path | None) -> object:
    if path is None:
        raise LifecycleError("this lifecycle stage requires an exact confirmation token JSON file")
    train, _governance, _registry = _train_modules()
    return _decode_public_artifact(path, train.ConfirmationToken)


def _object_digest(value: object) -> str:
    return digest(_public_object_snapshot(value))


_TRANSIENT_STAGE_RESULT_FIELDS = {
    "appended",
    "created_now",
    "idempotent",
    "idempotent_replay",
    "resumed",
}


def _stage_result_identity(value: object) -> object:
    """Remove runtime replay flags while preserving every authority-bearing byte."""

    if isinstance(value, Mapping):
        return {
            str(key): _stage_result_identity(item)
            for key, item in value.items()
            if key not in _TRANSIENT_STAGE_RESULT_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_stage_result_identity(item) for item in value]
    return value


def _stage_plan_payload(value: LifecycleStagePlan) -> dict[str, object]:
    payload = value.as_dict()
    payload.pop("plan_digest", None)
    return payload


def _stage_child_digest(child: object, attribute: str) -> str:
    value = child.get(attribute) if isinstance(child, Mapping) else getattr(child, attribute, None)
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise LifecycleError(f"lifecycle child lacks exact {attribute}")
    return value


def lifecycle_stage_status(project_root: str | Path) -> dict[str, object]:
    """Project durable post-implementation facade stages and evidence refs."""

    context = _context(project_root)
    root = _registry(context) / "stage-journal"
    operations: list[dict[str, object]] = []
    blockers: list[str] = []
    evidence_refs: list[str] = []
    if root.exists():
        if not root.is_dir() or _is_link_or_junction(root):
            blockers.append(f"lifecycle-stage-registry-unsafe:{root}")
        else:
            for path in sorted(root.glob("OP-*.json"), key=lambda item: item.name):
                operation_id = path.stem
                try:
                    journal = _load_stage_journal(context, operation_id)
                    assert journal is not None
                    completed = journal["completed_stages"]
                    assert isinstance(completed, list)
                    active = journal.get("active_plan")
                    for item in completed:
                        if isinstance(item, Mapping):
                            raw_refs = item.get("evidence_refs", [])
                            if isinstance(raw_refs, list):
                                evidence_refs.extend(
                                    ref for ref in raw_refs if isinstance(ref, str) and ref not in evidence_refs
                                )
                    next_gate = (
                        str(active.get("next_gate"))
                        if isinstance(active, Mapping)
                        else str(completed[-1]["next_gate"])
                        if completed
                        else "plan-candidate-preverification"
                    )
                    operations.append(
                        {
                            "operation_id": operation_id,
                            "phase": journal["phase"],
                            "active_stage": active.get("stage") if isinstance(active, Mapping) else None,
                            "completed_stages": [item["stage"] for item in completed],
                            "evidence_refs": [
                                ref
                                for item in completed
                                if isinstance(item, Mapping) and isinstance(item.get("evidence_refs"), list)
                                for ref in item["evidence_refs"]
                            ],
                            "next_gate": next_gate,
                            "last_error": journal.get("last_error"),
                        }
                    )
                    if journal.get("last_error"):
                        blockers.append(f"lifecycle-stage-error:{operation_id}:{journal['last_error']}")
                except LifecycleError as exc:
                    blockers.append(f"lifecycle-stage-corrupt:{operation_id}:{exc}")
    next_gate = (
        "reconcile-lifecycle-stage"
        if blockers
        else str(operations[-1]["next_gate"])
        if operations
        else "plan-candidate-preverification"
    )
    return {
        "schema_version": STAGE_STATUS_SCHEMA,
        "registry_root": str(root),
        "phase": "blocked" if blockers else "applying" if any(item["phase"] == "APPLYING" for item in operations) else "ready",
        "has_history": bool(operations),
        "operations": operations,
        "evidence_refs": evidence_refs,
        "blocking_reasons": blockers,
        "next_gate": next_gate,
        "pushed": False,
    }


def _stage_child_blockers(child: object) -> tuple[str, ...]:
    raw = child.get("blockers", ()) if isinstance(child, Mapping) else getattr(child, "blockers", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise LifecycleError("lifecycle child blockers are invalid")
    blockers: list[str] = []
    for item in raw:
        code = getattr(item, "code", None)
        message = getattr(item, "message", None)
        if isinstance(code, str) and isinstance(message, str):
            blockers.append(f"{code}:{message}")
        else:
            blockers.append(str(item))
    return tuple(blockers)


def _stage_iterations(subject: object) -> tuple[str, ...]:
    direct = getattr(subject, "iteration", None)
    if isinstance(direct, str):
        return (_iteration(direct),)
    raw = getattr(subject, "candidates", None)
    if raw is None:
        metadata = getattr(subject, "metadata", None)
        raw = getattr(metadata, "candidate_bindings", None)
    values: list[str] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            candidate = getattr(item, "iteration", None)
            if isinstance(candidate, str):
                number = _iteration(candidate)
                if number not in values:
                    values.append(number)
    return tuple(values)


def _stage_evidence_refs(value: object) -> tuple[str, ...]:
    refs: list[str] = []
    for name in (
        "candidate_ref",
        "candidate_evidence_ref",
        "commit_ref",
        "evidence_ref",
        "operation_commit_ref",
        "operation_evidence_ref",
    ):
        item = getattr(value, name, None)
        if isinstance(item, str) and item.startswith("refs/") and item not in refs:
            refs.append(item)
    for name in ("iteration_evidence_refs", "updated_refs", "ref_updates"):
        raw = getattr(value, name, ())
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            continue
        for item in raw:
            ref: object = item
            if hasattr(item, "ref_name"):
                ref = getattr(item, "ref_name")
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and item:
                ref = item[0]
            if isinstance(ref, str) and ref.startswith("refs/") and ref not in refs:
                refs.append(ref)
    return tuple(refs)


def _build_stage_plan(
    *,
    project_root: str | Path,
    lifecycle_operation_id: str,
    stage: str,
    subject: object,
    child: object,
    child_digest_attribute: str,
    action_level: str,
    confirmation_action: str | None,
    evidence_refs: Sequence[str] = (),
    child_operation_id: str | None = None,
) -> LifecycleStagePlan:
    if stage not in STAGE_NEXT_GATE:
        raise LifecycleError(f"unsupported lifecycle stage: {stage}")
    context = _context(project_root)
    operation = _operation(lifecycle_operation_id)
    child_operation = (
        child_operation_id
        if child_operation_id is not None
        else child.get("operation_id")
        if isinstance(child, Mapping)
        else getattr(child, "operation_id", None)
    )
    if not isinstance(child_operation, str) or OPERATION_RE.fullmatch(child_operation) is None:
        raise LifecycleError("lifecycle child operation identity is invalid")
    snapshot = _public_object_snapshot(child)
    child_digest = _stage_child_digest(child, child_digest_attribute)
    if action_level not in {"silent", "notify", "confirm"}:
        raise LifecycleError("lifecycle stage action level is invalid")
    provisional = LifecycleStagePlan(
        schema_version=STAGE_PLAN_SCHEMA,
        operation_id=operation,
        project_root=str(context.project_root),
        stage=stage,
        child_operation_id=child_operation,
        child_schema_version=str(snapshot.get("schema_version", "")),
        child_plan_digest=child_digest,
        child_snapshot_digest=digest(snapshot),
        subject_digest=_object_digest(subject),
        iterations=_stage_iterations(subject),
        action_level=action_level,
        requires_confirmation=confirmation_action is not None,
        confirmation_action=confirmation_action,
        evidence_refs=tuple(dict.fromkeys((*evidence_refs, *_stage_evidence_refs(subject), *_stage_evidence_refs(child)))),
        next_gate=STAGE_NEXT_GATE[stage],
        blockers=_stage_child_blockers(child),
        plan_digest="0" * 64,
    )
    return replace(provisional, plan_digest=digest(_stage_plan_payload(provisional)))


def _validate_stage_plan(value: LifecycleStagePlan) -> None:
    if not isinstance(value, LifecycleStagePlan) or value.schema_version != STAGE_PLAN_SCHEMA:
        raise LifecycleError("lifecycle stage plan schema is invalid")
    _operation(value.operation_id)
    if value.stage not in STAGE_NEXT_GATE or value.next_gate != STAGE_NEXT_GATE[value.stage]:
        raise LifecycleError("lifecycle stage plan transition is invalid")
    if value.plan_digest != digest(_stage_plan_payload(value)):
        raise LifecycleError("lifecycle stage plan digest changed")
    if value.pushed:
        raise LifecycleError("lifecycle stage plan cannot claim a push")


def plan_candidate_preverification_stage(
    registration_plan: object,
    *,
    lifecycle_operation_id: str,
) -> LifecycleStagePlan:
    train, _governance, _registry = _train_modules()
    if not isinstance(registration_plan, train.CandidateRegistrationPlan):
        raise LifecycleError("candidate preverification requires CandidateRegistrationPlan")
    return _build_stage_plan(
        project_root=registration_plan.project_root,
        lifecycle_operation_id=lifecycle_operation_id,
        stage="candidate-preverify",
        subject=registration_plan,
        child=registration_plan,
        child_digest_attribute="plan_digest",
        action_level="silent",
        confirmation_action=None,
    )


def _local_main_release_components(
    workspace_plan: object,
    *,
    session_id: str,
    occurred_at: str,
    causal_parent: str | None,
) -> tuple[progress.ProgressEventV2, progress.ProgressAppendPlan, dict[str, object]]:
    """Build the exact read-only workspace/progress composite child."""

    if not isinstance(workspace_plan, workspace.WorkspacePlan):
        raise LifecycleError("local main release requires WorkspacePlan")
    if workspace_plan.action != "bind-local-branch":
        raise LifecycleError("local main release requires bind-local-branch plan")
    manifest = workspace_plan.manifest
    branch = manifest.get("branch")
    base = manifest.get("base")
    preconditions = manifest.get("preconditions")
    if not all(isinstance(item, Mapping) for item in (branch, base, preconditions)):
        raise LifecycleError("local main release workspace manifest is malformed")
    assert isinstance(branch, Mapping)
    assert isinstance(base, Mapping)
    assert isinstance(preconditions, Mapping)
    source = preconditions.get("source_snapshot")
    lease_after = preconditions.get("writer_lease_after")
    if not isinstance(source, Mapping) or not isinstance(lease_after, Mapping):
        raise LifecycleError("local main release lacks source or lease evidence")
    event = progress.workspace_event(
        workspace_state="main-released",
        session_id=session_id,
        iteration=workspace_plan.iteration,
        occurred_at=occurred_at,
        source_ref=str(branch["from_ref"]),
        source_commit=str(source["head_oid"]),
        operation_id=workspace_plan.operation_id,
        causal_parent=causal_parent,
        evidence_refs=(
            f"workspace-plan:{workspace_plan.digest}",
            f"branch-from:{branch['from_ref']}",
            f"branch-to:{branch['to_ref']}",
            f"allocation-base:{base['commit']}",
            f"lease-generation:{manifest['lease_generation']}->{lease_after['generation']}",
            f"source-snapshot:{digest(source)}",
        ),
        summary=(
            f"PRD-{workspace_plan.iteration} released main by binding the Local checkout "
            "in place; path, cwd, worktree bytes, index, commit and stash state are preserved."
        ),
    )
    # Planning the progress append now binds the pre-bind progress bytes and
    # source commit.  It remains read-only; apply happens only after the exact
    # workspace child proves preservation.
    common = Path(str(workspace_plan.manifest["git_common_dir"])).resolve()
    durable_progress_path = progress.journal_path(
        common,
        event.operation_id,
        event.event_id,
    )
    if durable_progress_path.exists():
        progress_plan = progress.load_progress_append_plan(
            common,
            event.operation_id,
            event.event_id,
        )
        if (
            progress_plan.event != event
            or progress_plan.project_root
            != str(Path(str(workspace_plan.manifest["project_root"])).resolve())
        ):
            raise LifecycleError(
                "durable Local main release progress plan differs from the accepted composite"
            )
    else:
        progress_plan = progress.plan_progress_append(
            project_root=workspace_plan.manifest["project_root"],
            event=event,
        )
    child = {
        "schema_version": "harness-lite.local-main-release-child/v1",
        "operation_id": workspace_plan.operation_id,
        "workspace_plan_digest": workspace_plan.digest,
        "progress_plan_digest": progress_plan.plan_digest,
        "event_id": event.event_id,
        "blockers": [item.as_dict() for item in workspace_plan.blockers],
        "pushed": False,
    }
    return event, progress_plan, child


def plan_local_main_release_stage(
    workspace_plan: object,
    *,
    lifecycle_operation_id: str,
    session_id: str,
    occurred_at: str,
    causal_parent: str | None,
) -> LifecycleStagePlan:
    """Bind Local A in place and pre-bind its exactly-once progress event."""

    event, _progress_plan, child = _local_main_release_components(
        workspace_plan,
        session_id=session_id,
        occurred_at=occurred_at,
        causal_parent=causal_parent,
    )
    assert isinstance(workspace_plan, workspace.WorkspacePlan)
    return _build_stage_plan(
        project_root=str(workspace_plan.manifest["project_root"]),
        lifecycle_operation_id=lifecycle_operation_id,
        stage="local-main-release",
        subject=workspace_plan,
        child=child,
        child_digest_attribute="progress_plan_digest",
        action_level="notify",
        confirmation_action=None,
        evidence_refs=(event.event_id, f"workspace-plan:{workspace_plan.digest}"),
    )


def plan_candidate_registration_stage(
    seal_plan: object,
    *,
    lifecycle_operation_id: str,
) -> LifecycleStagePlan:
    train, _governance, _registry = _train_modules()
    if not isinstance(seal_plan, train.CandidateSealPlan):
        raise LifecycleError("candidate registration requires CandidateSealPlan")
    return _build_stage_plan(
        project_root=seal_plan.registration_plan.project_root,
        lifecycle_operation_id=lifecycle_operation_id,
        stage="candidate-register",
        subject=seal_plan,
        child=seal_plan,
        child_digest_attribute="seal_plan_digest",
        action_level="confirm",
        confirmation_action="create-candidate-seal",
        evidence_refs=(
            seal_plan.registration_plan.candidate_ref,
            seal_plan.registration_plan.candidate_evidence_ref,
        ),
        # CandidateSealPlan deliberately nests the canonical operation under
        # its exact registration plan; it has no duplicate top-level field.
        child_operation_id=seal_plan.registration_plan.operation_id,
    )


def _plan_integration_preparation_stage(
    integration_plan: object,
    *,
    lifecycle_operation_id: str,
    order_plan: object,
) -> LifecycleStagePlan:
    train, _governance, _registry = _train_modules()
    if not isinstance(integration_plan, train.IntegrationPreparePlan):
        raise LifecycleError("integration preparation requires IntegrationPreparePlan")
    try:
        from . import harness_merge_train as ordering
    except ImportError:  # pragma: no cover - direct execution
        import harness_merge_train as ordering
    if not isinstance(order_plan, ordering.MergeTrainOrderPlan):
        raise LifecycleError("integration preparation requires a public merge-train order plan")
    order_blockers = ordering.merge_train_order_gate(order_plan)
    if order_blockers:
        raise LifecycleError(
            "merge train ordering is blocked: "
            + "; ".join(item.code for item in order_blockers)
        )
    if (
        tuple(integration_plan.candidates) != tuple(order_plan.ordered_candidates)
        or tuple(integration_plan.dependency_order) != tuple(order_plan.ordered_iterations)
    ):
        raise LifecycleError("integration preparation candidates differ from accepted merge-train order")
    return _build_stage_plan(
        project_root=integration_plan.project_root,
        lifecycle_operation_id=lifecycle_operation_id,
        stage="integration-prepare",
        subject=integration_plan,
        child=integration_plan,
        child_digest_attribute="plan_digest",
        action_level="confirm",
        confirmation_action="prepare-integration",
        evidence_refs=(f"merge-train-order:{order_plan.plan_digest}",),
    )


def plan_ordered_integration_preparation_stage(
    project_root: str | Path,
    *,
    lifecycle_operation_id: str,
    generation: str,
    candidates: Sequence[object],
    verify_commands: Sequence[object],
    queue_metadata: Mapping[str, Mapping[str, object]] | None = None,
    **integration_options: object,
) -> OrderedIntegrationPreparationPlan:
    """Derive a public, stable merge-train order before Git preparation.

    Candidate readiness order is never trusted.  The order plan authenticates
    each public candidate, applies the dependency DAG, and uses explicit queue
    metadata plus deterministic tie breakers for independent candidates.
    """

    train, _governance, _registry = _train_modules()
    try:
        from . import harness_merge_train as ordering
    except ImportError:  # pragma: no cover - direct execution
        import harness_merge_train as ordering

    repo = train.open_repository(project_root)
    main_ref = str(integration_options.get("main_ref", train.DEFAULT_MAIN_REF))
    current_main = train._resolve_ref(repo, main_ref)
    if current_main is None:
        raise LifecycleError("merge train main authority is missing")
    principle_path = str(
        integration_options.get("principle_path", train.DEFAULT_PRINCIPLE_PATH)
    )
    _principle_blob, principle_raw = train._blob_at(repo, current_main, principle_path)
    principle_sha256 = hashlib.sha256(principle_raw).hexdigest()
    order_plan = ordering.plan_merge_train_order(
        repo.root,
        candidates=candidates,
        current_principle_sha256=principle_sha256,
        queue_metadata=queue_metadata,
    )
    order_blockers = ordering.merge_train_order_gate(order_plan)
    if order_blockers:
        raise LifecycleError(
            "merge train ordering is blocked: "
            + "; ".join(item.code for item in order_blockers)
        )
    integration_plan = train.plan_prepare_integration(
        repo.root,
        generation=generation,
        candidates=order_plan.ordered_candidates,
        verify_commands=verify_commands,
        **integration_options,
    )
    stage = _plan_integration_preparation_stage(
        integration_plan,
        lifecycle_operation_id=lifecycle_operation_id,
        order_plan=order_plan,
    )
    provisional = OrderedIntegrationPreparationPlan(
        schema_version=ORDERED_PREPARATION_SCHEMA,
        order_plan=order_plan,
        order_plan_digest=order_plan.plan_digest,
        ordered_iterations=order_plan.ordered_iterations,
        integration_plan=integration_plan,
        lifecycle_plan=stage,
        plan_digest="0" * 64,
    )
    return replace(provisional, plan_digest=digest({
        "schema_version": provisional.schema_version,
        "order_plan": _public_object_snapshot(provisional.order_plan),
        "order_plan_digest": provisional.order_plan_digest,
        "ordered_iterations": list(provisional.ordered_iterations),
        "integration_plan": _public_object_snapshot(provisional.integration_plan),
        "lifecycle_plan": provisional.lifecycle_plan.as_dict(),
        "pushed": provisional.pushed,
    }))


def plan_integration_commit_stage(
    commit_plan: object,
    *,
    lifecycle_operation_id: str,
) -> LifecycleStagePlan:
    train, _governance, _registry = _train_modules()
    if not isinstance(commit_plan, train.IntegrationCommitPlan):
        raise LifecycleError("integration commit requires IntegrationCommitPlan")
    return _build_stage_plan(
        project_root=commit_plan.project_root,
        lifecycle_operation_id=lifecycle_operation_id,
        stage="integration-commit",
        subject=commit_plan,
        child=commit_plan,
        child_digest_attribute="commit_plan_digest",
        action_level="confirm",
        confirmation_action="create-integration-commit",
    )


def plan_integrated_evidence_stage(
    integration_result: object,
    *,
    lifecycle_operation_id: str,
    commit_confirmation_token: object,
    progress_bindings: Sequence[object] = (),
) -> LifecycleStagePlan:
    train, _governance, registry = _train_modules()
    if not isinstance(integration_result, train.IntegrationCommitResult):
        raise LifecycleError("integrated evidence registration requires IntegrationCommitResult")
    if not isinstance(commit_confirmation_token, train.ConfirmationToken):
        raise LifecycleError("integrated evidence planning requires the exact commit confirmation token")
    child = registry.plan_register_integrated_evidence(
        integration_result,
        commit_confirmation_token=commit_confirmation_token,
        progress_bindings=progress_bindings,
    )
    return _build_stage_plan(
        project_root=integration_result.project_root,
        lifecycle_operation_id=lifecycle_operation_id,
        stage="integrated-evidence-register",
        subject=integration_result,
        child=child,
        child_digest_attribute="plan_digest",
        action_level="silent",
        confirmation_action=None,
    )


def plan_final_acceptance_stage(
    registered_integrated_evidence: object,
    *,
    lifecycle_operation_id: str,
    main_confirmation_token: object,
    authorization_id: str | None = None,
) -> LifecycleStagePlan:
    """Plan the public final-acceptance registry, never an integrated shortcut."""

    train, _governance, registry = _train_modules()
    final_acceptance = _final_acceptance_module()
    if not isinstance(registered_integrated_evidence, registry.RegisteredIntegratedEvidence):
        raise LifecycleError("final acceptance requires RegisteredIntegratedEvidence")
    if not isinstance(main_confirmation_token, train.ConfirmationToken):
        raise LifecycleError("final acceptance requires an exact advance-main confirmation token")
    if final_acceptance is None or not callable(getattr(final_acceptance, "plan_final_acceptance", None)):
        raise LifecycleError("public final-acceptance registry is unavailable; integrated evidence is not final acceptance")
    main_plan = train.plan_main_advance(registered_integrated_evidence)
    child = final_acceptance.plan_final_acceptance(
        registered_integrated_evidence.project_root,
        main_plan=main_plan,
        integrated=registered_integrated_evidence,
        confirmation=main_confirmation_token,
    )
    return _build_stage_plan(
        project_root=registered_integrated_evidence.project_root,
        lifecycle_operation_id=lifecycle_operation_id,
        stage="final-acceptance-register",
        subject=registered_integrated_evidence,
        child=child,
        child_digest_attribute="plan_digest",
        action_level="confirm",
        confirmation_action="advance-main",
    )


def _final_acceptance_recovery_child(
    registered_integrated_evidence: object,
    *,
    main_confirmation_token: object,
) -> tuple[object, object]:
    """Return the exact final child and its embedded main plan.

    Once the final registry has written its pre-CAS journal, recovery must use
    that immutable snapshot.  Replanning against already-advanced main would
    make a successful atomic CAS look stale and strand cleanup evidence.
    """

    train, _governance, registry = _train_modules()
    final_acceptance = _final_acceptance_module()
    if final_acceptance is None:
        raise LifecycleError("public final-acceptance registry is unavailable")
    if not isinstance(registered_integrated_evidence, registry.RegisteredIntegratedEvidence):
        raise LifecycleError("final acceptance requires RegisteredIntegratedEvidence")
    if not isinstance(main_confirmation_token, train.ConfirmationToken):
        raise LifecycleError("final acceptance requires an exact advance-main confirmation token")
    load_plan = getattr(final_acceptance, "load_final_acceptance_plan", None)
    durable = (
        load_plan(
            registered_integrated_evidence.project_root,
            operation_id=registered_integrated_evidence.operation_id,
        )
        if callable(load_plan)
        else None
    )
    if durable is None:
        main_plan = train.plan_main_advance(registered_integrated_evidence)
        durable = final_acceptance.plan_final_acceptance(
            registered_integrated_evidence.project_root,
            main_plan=main_plan,
            integrated=registered_integrated_evidence,
            confirmation=main_confirmation_token,
        )
    else:
        main_plan = _decode_typed_json(
            dict(durable.metadata.main_plan_snapshot),
            train.MainAdvancePlan,
        )
    if not isinstance(main_plan, train.MainAdvancePlan):
        raise LifecycleError("final acceptance recovery lacks its exact MainAdvancePlan")
    if (
        main_plan.operation_id != registered_integrated_evidence.operation_id
        or durable.operation_id != registered_integrated_evidence.operation_id
        or main_plan.plan_digest != train.main_advance_plan_digest(main_plan)
        or durable.metadata.main_plan_digest != main_plan.plan_digest
    ):
        raise LifecycleError("final acceptance recovery plan identity differs from integrated evidence")
    token_blockers = train.confirmation_token_gate(
        main_confirmation_token,
        action="advance-main",
        subject_digest=main_plan.plan_digest,
    )
    if token_blockers or (
        durable.metadata.confirmation_authorization_id
        != main_confirmation_token.authorization_id
        or durable.metadata.confirmation_token_digest
        != main_confirmation_token.token_digest
    ):
        raise LifecycleError("final acceptance recovery confirmation identity differs")
    return durable, main_plan


def plan_main_advance_stage(
    registered_final_acceptance: object,
    *,
    lifecycle_operation_id: str,
) -> LifecycleStagePlan:
    raise LifecycleError(
        "main advance is part of final-acceptance-register; a second main mutation is forbidden"
    )


def plan_integration_cleanup_stage(
    main_advance_result: object,
    *,
    lifecycle_operation_id: str,
) -> LifecycleStagePlan:
    train, _governance, _registry = _train_modules()
    if not isinstance(main_advance_result, train.MainAdvanceResult):
        raise LifecycleError("integration cleanup requires MainAdvanceResult")
    child = train.plan_cleanup_integration(main_advance_result)
    return _build_stage_plan(
        project_root=main_advance_result.project_root,
        lifecycle_operation_id=lifecycle_operation_id,
        stage="integration-cleanup",
        subject=main_advance_result,
        child=child,
        child_digest_attribute="plan_digest",
        action_level="notify",
        confirmation_action=None,
        evidence_refs=main_advance_result.updated_refs,
    )


def _local_main_release_result(
    workspace_plan: workspace.WorkspacePlan,
    workspace_result: Mapping[str, object],
    progress_plan: progress.ProgressAppendPlan,
    progress_result: progress.ProgressAppendResult,
) -> dict[str, object]:
    """Project only stable, authority-bearing evidence from both children."""

    if workspace_result.get("phase") != "succeeded" or workspace_result.get("journal_phase") != "READY":
        reasons = workspace_result.get("blocking_reasons")
        raise LifecycleError(f"Local main release workspace child did not succeed: {reasons}")
    notification = workspace_result.get("notification")
    if not isinstance(notification, Mapping):
        raise LifecycleError("Local main release workspace child lacks its after evidence")
    preservation = notification.get("preservation")
    required_preservation = {
        "workspace_path_unchanged",
        "head_commit_unchanged",
        "status_fingerprint_unchanged",
        "index_bytes_unchanged",
        "worktree_bytes_unchanged",
    }
    if (
        not isinstance(preservation, Mapping)
        or set(preservation) != required_preservation
        or not all(value is True for value in preservation.values())
    ):
        raise LifecycleError("Local main release lacks complete source-preservation evidence")
    if progress_result.phase != "APPLIED" or progress_result.result_sha256 != progress_plan.after_sha256:
        raise LifecycleError("Local main release progress child did not reach its exact accepted bytes")
    branch = workspace_plan.manifest.get("branch")
    worktree = workspace_plan.manifest.get("worktree")
    preconditions = workspace_plan.manifest.get("preconditions")
    if not all(isinstance(value, Mapping) for value in (branch, worktree, preconditions)):
        raise LifecycleError("Local main release manifest lost its bound workspace identity")
    assert isinstance(branch, Mapping)
    assert isinstance(worktree, Mapping)
    assert isinstance(preconditions, Mapping)
    lease_after = preconditions.get("writer_lease_after")
    source = preconditions.get("source_snapshot")
    if not isinstance(lease_after, Mapping) or not isinstance(source, Mapping):
        raise LifecycleError("Local main release manifest lost its lease/source identity")
    return {
        "schema_version": "harness-lite.local-main-release-result/v1",
        "operation_id": workspace_plan.operation_id,
        "project_root": str(workspace_plan.manifest["project_root"]),
        "iteration": workspace_plan.iteration,
        "phase": "succeeded",
        "workspace": {
            "plan_digest": workspace_plan.digest,
            "journal_phase": "READY",
            "path": worktree.get("path"),
            "branch_from_ref": branch.get("from_ref"),
            "branch_to_ref": branch.get("to_ref"),
            "head_commit": source.get("head_oid"),
            "lease_generation_before": workspace_plan.manifest.get("lease_generation"),
            "lease_generation_after": lease_after.get("generation"),
            "preservation": dict(preservation),
        },
        "progress": {
            "event_id": progress_plan.event.event_id,
            "plan_digest": progress_plan.plan_digest,
            "phase": "APPLIED",
            "progress_path": progress_plan.progress_path,
            "result_sha256": progress_result.result_sha256,
            "journal_path": progress_result.journal_path,
        },
        "remote": {"involved": False, "pushed": False, "force": False},
        "next_gate": STAGE_NEXT_GATE["local-main-release"],
        "pushed": False,
    }


def apply_local_main_release_stage(
    plan: LifecycleStagePlan,
    workspace_plan: workspace.WorkspacePlan,
    *,
    accepted_plan_digest: str,
    session_id: str,
    occurred_at: str,
    causal_parent: str | None,
    notify: Callable[[object], None] | None = None,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    """Release main in place, then append its exact progress checkpoint once."""

    event, progress_plan, _child = _local_main_release_components(
        workspace_plan,
        session_id=session_id,
        occurred_at=occurred_at,
        causal_parent=causal_parent,
    )
    replanned = plan_local_main_release_stage(
        workspace_plan,
        lifecycle_operation_id=plan.operation_id,
        session_id=session_id,
        occurred_at=occurred_at,
        causal_parent=causal_parent,
    )
    manifest = workspace_plan.manifest
    branch = manifest.get("branch")
    base = manifest.get("base")
    worktree = manifest.get("worktree")
    if not all(isinstance(value, Mapping) for value in (branch, base, worktree)):
        raise LifecycleError("Local main release workspace manifest is malformed")
    assert isinstance(branch, Mapping)
    assert isinstance(base, Mapping)
    assert isinstance(worktree, Mapping)

    def execute(proxy: Callable[[object], None]) -> dict[str, object]:
        proxy(
            {
                "schema_version": "harness-lite.local-main-release-notification/v1",
                "phase": "before",
                "action": "bind-local-branch-and-record-progress",
                "operation_id": workspace_plan.operation_id,
                "iteration": workspace_plan.iteration,
                "workspace_plan": workspace_plan.as_dict(),
                "progress_plan": progress_plan.as_dict(),
                "remote": {"involved": False, "pushed": False, "force": False},
            }
        )
        workspace_result = workspace.apply_bind_local_branch(
            manifest["project_root"],
            iteration=workspace_plan.iteration,
            owner=str(manifest["owner"]),
            lease_generation=int(manifest["lease_generation"]),
            worktree_path=str(worktree["path"]),
            base_commit=str(base["commit"]),
            new_branch_ref=str(branch["to_ref"]),
            operation_id=workspace_plan.operation_id,
            accepted_plan_digest=workspace_plan.digest,
        )
        if workspace_result.get("phase") != "succeeded":
            raise LifecycleError(
                "Local main release workspace child was blocked: "
                + canonical_json(workspace_result.get("blocking_reasons", [])).decode("utf-8")
            )
        if failpoint is not None:
            failpoint("local-main-release-after-workspace")
        progress_result = progress.apply_progress_append(
            progress_plan,
            accept_plan_digest=progress_plan.plan_digest,
            fault_injector=(
                (lambda stage, _path: failpoint(f"local-main-release-progress:{stage}"))
                if failpoint is not None
                else None
            ),
        )
        if failpoint is not None:
            failpoint("local-main-release-after-progress")
        result = _local_main_release_result(
            workspace_plan,
            workspace_result,
            progress_plan,
            progress_result,
        )
        proxy(
            {
                "schema_version": "harness-lite.local-main-release-notification/v1",
                "phase": "after",
                "action": "bind-local-branch-and-record-progress",
                "operation_id": workspace_plan.operation_id,
                "iteration": workspace_plan.iteration,
                "facts": result,
                "event_id": event.event_id,
                "remote": {"involved": False, "pushed": False, "force": False},
            }
        )
        return result

    return _apply_stage_transaction(
        plan,
        accepted_plan_digest=accepted_plan_digest,
        subject=workspace_plan,
        replanned=replanned,
        execute=execute,
        notify=notify,
        failpoint=failpoint,
    )


def apply_candidate_preverification_stage(
    plan: LifecycleStagePlan,
    registration_plan: object,
    *,
    accepted_plan_digest: str,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    train, _governance, _registry = _train_modules()
    replanned = plan_candidate_preverification_stage(
        registration_plan,
        lifecycle_operation_id=plan.operation_id,
    )
    return _apply_stage_transaction(
        plan,
        accepted_plan_digest=accepted_plan_digest,
        subject=registration_plan,
        replanned=replanned,
        execute=lambda _notify: train.prepare_candidate_registration(
            registration_plan,
            accepted_plan_digest=plan.child_plan_digest,
        ),
        failpoint=failpoint,
    )


def apply_candidate_registration_stage(
    plan: LifecycleStagePlan,
    seal_plan: object,
    *,
    accepted_plan_digest: str,
    confirmation_token: object,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    train, _governance, _registry = _train_modules()
    if not isinstance(confirmation_token, train.ConfirmationToken):
        raise LifecycleError("candidate registration confirmation token is required")
    replanned = plan_candidate_registration_stage(seal_plan, lifecycle_operation_id=plan.operation_id)
    return _apply_stage_transaction(
        plan,
        accepted_plan_digest=accepted_plan_digest,
        subject=seal_plan,
        replanned=replanned,
        execute=lambda _notify: train.apply_register_candidate(
            seal_plan,
            accepted_seal_plan_digest=plan.child_plan_digest,
            confirmation_token=confirmation_token,
            failpoint=failpoint,
        ),
        failpoint=failpoint,
    )


def _apply_integration_preparation_stage(
    plan: LifecycleStagePlan,
    integration_plan: object,
    *,
    accepted_plan_digest: str,
    confirmation_token: object,
    order_plan: object | None = None,
    readme_authority: object | None = None,
    principle_approvals: Mapping[str, object] | None = None,
    principle_leases: Mapping[str, object] | None = None,
    notify: Callable[[object], None] | None = None,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    train, governance, _registry = _train_modules()
    if not isinstance(confirmation_token, train.ConfirmationToken):
        raise LifecycleError("integration preparation confirmation token is required")
    if order_plan is None:
        raise LifecycleError("integration preparation requires the accepted public merge-train order plan")
    replanned = _plan_integration_preparation_stage(
        integration_plan,
        lifecycle_operation_id=plan.operation_id,
        order_plan=order_plan,
    )

    def execute(proxy: Callable[[object], None]) -> object:
        callback = governance.build_governance_callback(
            integration_plan,
            readme_authority=readme_authority,
            principle_approvals=principle_approvals,
            principle_leases=principle_leases,
        )
        return train.apply_prepare_integration(
            integration_plan,
            accepted_plan_digest=plan.child_plan_digest,
            confirmation_token=confirmation_token,
            notify=proxy,
            governance_callback=callback,
            governance_conflict_normalizer=governance.build_conflict_normalizer(integration_plan),
            failpoint=failpoint,
        )

    return _apply_stage_transaction(
        plan,
        accepted_plan_digest=accepted_plan_digest,
        subject=integration_plan,
        replanned=replanned,
        execute=execute,
        notify=notify,
        failpoint=failpoint,
    )


def _ordered_preparation_payload(value: OrderedIntegrationPreparationPlan) -> dict[str, object]:
    payload = value.as_dict()
    payload.pop("plan_digest", None)
    return payload


def apply_ordered_integration_preparation_stage(
    ordered_plan: OrderedIntegrationPreparationPlan,
    *,
    accepted_plan_digest: str,
    confirmation_token: object,
    readme_authority: object | None = None,
    principle_approvals: Mapping[str, object] | None = None,
    principle_leases: Mapping[str, object] | None = None,
    notify: Callable[[object], None] | None = None,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    """Apply only an exact, publicly gated dependency/queue order."""

    try:
        from . import harness_merge_train as ordering
    except ImportError:  # pragma: no cover - direct execution
        import harness_merge_train as ordering
    if not isinstance(ordered_plan, OrderedIntegrationPreparationPlan):
        raise LifecycleError("ordered integration preparation plan type is invalid")
    if (
        ordered_plan.schema_version != ORDERED_PREPARATION_SCHEMA
        or ordered_plan.plan_digest != digest(_ordered_preparation_payload(ordered_plan))
        or accepted_plan_digest != ordered_plan.plan_digest
        or ordered_plan.pushed
    ):
        raise LifecycleError("ordered integration preparation plan was not accepted exactly")
    if not isinstance(ordered_plan.order_plan, ordering.MergeTrainOrderPlan):
        raise LifecycleError("ordered integration preparation lacks its public order authority")
    blockers = ordering.merge_train_order_gate(ordered_plan.order_plan)
    if blockers:
        raise LifecycleError(
            "merge train ordering changed before apply: "
            + "; ".join(item.code for item in blockers)
        )
    if (
        ordered_plan.order_plan_digest != ordered_plan.order_plan.plan_digest
        or ordered_plan.ordered_iterations != ordered_plan.order_plan.ordered_iterations
        or tuple(getattr(ordered_plan.integration_plan, "candidates", ()))
        != ordered_plan.order_plan.ordered_candidates
    ):
        raise LifecycleError("ordered integration preparation authority bindings differ")
    expected_stage = _plan_integration_preparation_stage(
        ordered_plan.integration_plan,
        lifecycle_operation_id=ordered_plan.lifecycle_plan.operation_id,
        order_plan=ordered_plan.order_plan,
    )
    if expected_stage != ordered_plan.lifecycle_plan:
        raise LifecycleError("ordered integration lifecycle child differs from the public order plan")
    return _apply_integration_preparation_stage(
        ordered_plan.lifecycle_plan,
        ordered_plan.integration_plan,
        accepted_plan_digest=ordered_plan.lifecycle_plan.plan_digest,
        confirmation_token=confirmation_token,
        order_plan=ordered_plan.order_plan,
        readme_authority=readme_authority,
        principle_approvals=principle_approvals,
        principle_leases=principle_leases,
        notify=notify,
        failpoint=failpoint,
    )


def apply_integration_commit_stage(
    plan: LifecycleStagePlan,
    commit_plan: object,
    *,
    accepted_plan_digest: str,
    confirmation_token: object,
    identity_rebindings: Sequence[object] = (),
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    train, _governance, _registry = _train_modules()
    if not isinstance(confirmation_token, train.ConfirmationToken):
        raise LifecycleError("integration commit confirmation token is required")
    replanned = plan_integration_commit_stage(commit_plan, lifecycle_operation_id=plan.operation_id)
    return _apply_stage_transaction(
        plan,
        accepted_plan_digest=accepted_plan_digest,
        subject=commit_plan,
        replanned=replanned,
        execute=lambda _notify: train.apply_integration_commit(
            commit_plan,
            accepted_commit_plan_digest=plan.child_plan_digest,
            confirmation_token=confirmation_token,
            identity_rebindings=identity_rebindings,
            failpoint=failpoint,
        ),
        failpoint=failpoint,
    )


def apply_integrated_evidence_stage(
    plan: LifecycleStagePlan,
    integration_result: object,
    *,
    accepted_plan_digest: str,
    commit_confirmation_token: object,
    progress_bindings: Sequence[object] = (),
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    train, _governance, registry = _train_modules()
    child = registry.plan_register_integrated_evidence(
        integration_result,
        commit_confirmation_token=commit_confirmation_token,
        progress_bindings=progress_bindings,
    )
    replanned = _build_stage_plan(
        project_root=integration_result.project_root,
        lifecycle_operation_id=plan.operation_id,
        stage="integrated-evidence-register",
        subject=integration_result,
        child=child,
        child_digest_attribute="plan_digest",
        action_level="silent",
        confirmation_action=None,
    )
    return _apply_stage_transaction(
        plan,
        accepted_plan_digest=accepted_plan_digest,
        subject=integration_result,
        replanned=replanned,
        execute=lambda _notify: registry.apply_register_integrated_evidence(
            child,
            accepted_plan_digest=plan.child_plan_digest,
            commit_confirmation_token=commit_confirmation_token,
            failpoint=failpoint,
        ),
        failpoint=failpoint,
    )


def apply_final_acceptance_stage(
    plan: LifecycleStagePlan,
    registered_integrated_evidence: object,
    *,
    accepted_plan_digest: str,
    main_confirmation_token: object,
    authorization_id: str | None = None,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    train, _governance, _registry = _train_modules()
    child, main_plan = _final_acceptance_recovery_child(
        registered_integrated_evidence,
        main_confirmation_token=main_confirmation_token,
    )
    replanned = _build_stage_plan(
        project_root=registered_integrated_evidence.project_root,
        lifecycle_operation_id=plan.operation_id,
        stage="final-acceptance-register",
        subject=registered_integrated_evidence,
        child=child,
        child_digest_attribute="plan_digest",
        action_level="confirm",
        confirmation_action="advance-main",
    )
    return _apply_stage_transaction(
        plan,
        accepted_plan_digest=accepted_plan_digest,
        subject=registered_integrated_evidence,
        replanned=replanned,
        # The train compatibility entrypoint delegates to the public final
        # registry and returns the cleanup-capable MainAdvanceResult.  Its one
        # final-registry apply performs the main/ref CAS exactly once.
        execute=lambda _notify: train.apply_main_advance(
            main_plan,
            accepted_plan_digest=main_plan.plan_digest,
            accepted_integrated_evidence_digest=registered_integrated_evidence.registration_digest,
            confirmation_token=main_confirmation_token,
            failpoint=failpoint,
        ),
        failpoint=failpoint,
    )


def apply_main_advance_stage(
    plan: LifecycleStagePlan,
    registered_final_acceptance: object,
    *,
    accepted_plan_digest: str,
    confirmation_token: object,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    raise LifecycleError(
        "main advance is part of final-acceptance-register; a second main mutation is forbidden"
    )


def apply_integration_cleanup_stage(
    plan: LifecycleStagePlan,
    main_advance_result: object,
    *,
    accepted_plan_digest: str,
    notify: Callable[[object], None] | None,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    train, _governance, _registry = _train_modules()
    replanned = plan_integration_cleanup_stage(main_advance_result, lifecycle_operation_id=plan.operation_id)
    cleanup_plan = train.plan_cleanup_integration(main_advance_result)
    return _apply_stage_transaction(
        plan,
        accepted_plan_digest=accepted_plan_digest,
        subject=main_advance_result,
        replanned=replanned,
        execute=lambda proxy: train.apply_cleanup_integration(
            cleanup_plan,
            accepted_plan_digest=plan.child_plan_digest,
            notify=proxy,
            failpoint=failpoint,
        ),
        notify=notify,
        failpoint=failpoint,
    )


def _risk_vector(value: Mapping[str, object]) -> coordinator.RiskVector:
    allowed = set(coordinator.RiskVector.__dataclass_fields__)
    unknown = set(value) - allowed
    if unknown:
        raise LifecycleError("unknown risk fields: " + ", ".join(sorted(unknown)))
    normalized = dict(value)
    raw_unknowns = normalized.get("unknowns", [])
    if not isinstance(raw_unknowns, list) or not all(isinstance(item, str) for item in raw_unknowns):
        raise LifecycleError("risk.unknowns must be an array of strings")
    normalized["unknowns"] = tuple(raw_unknowns)
    try:
        return coordinator.RiskVector(**normalized)
    except TypeError as exc:  # defensive against a changed coordinator schema
        raise LifecycleError(f"risk vector is invalid: {exc}") from exc


def _read_json(path: Path, *, label: str, maximum: int = MAX_JSON_BYTES) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LifecycleError(f"cannot read {label}: {path}: {exc}") from exc
    if len(raw) < 2 or len(raw) > maximum:
        raise LifecycleError(f"{label} exceeds its safe size limit: {path}")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"{label} must be a JSON object: {path}")
    return value


REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "summary",
    "iteration",
    "read_only",
    "ambiguities",
    "risk",
    "principle_change",
    "exclusive_resource",
    "incompatible_schema",
    "owner",
    "base_ref",
    "governance_ref",
    "branch_ref",
    "worktree_path",
}


def validate_request(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LifecycleError("request must be a JSON object")
    unknown = set(value) - REQUEST_FIELDS
    if unknown:
        raise LifecycleError("unknown request fields: " + ", ".join(sorted(unknown)))
    if value.get("schema_version") != REQUEST_SCHEMA:
        raise LifecycleError(f"request schema_version must be {REQUEST_SCHEMA}")
    result = dict(value)
    if "request_id" in result:
        result["request_id"] = _single_line(result["request_id"], "request_id")
    if "summary" in result:
        result["summary"] = _single_line(result["summary"], "summary", maximum=1000)
    if result.get("iteration") is not None:
        result["iteration"] = _iteration(result["iteration"])
    for field in ("read_only", "principle_change", "exclusive_resource", "incompatible_schema"):
        if field in result and not isinstance(result[field], bool):
            raise LifecycleError(f"{field} must be a JSON boolean")
        result.setdefault(field, False)
    ambiguities = result.get("ambiguities", [])
    if not isinstance(ambiguities, list) or not all(isinstance(item, str) for item in ambiguities):
        raise LifecycleError("ambiguities must be an array of strings")
    result["ambiguities"] = list(
        dict.fromkeys(
            _single_line(item, f"ambiguities[{index}]", maximum=1000)
            for index, item in enumerate(ambiguities)
            if item.strip()
        )
    )
    risk = result.get("risk", {})
    if not isinstance(risk, dict):
        raise LifecycleError("risk must be a JSON object")
    for field in set(coordinator.RiskVector.__dataclass_fields__) - {"unknowns"}:
        if field in risk and not isinstance(risk[field], bool):
            raise LifecycleError(f"risk.{field} must be a JSON boolean")
    unknown_risks = risk.get("unknowns", [])
    if isinstance(unknown_risks, list):
        if not all(isinstance(item, str) for item in unknown_risks):
            raise LifecycleError("risk.unknowns must be an array of strings")
        risk = dict(risk)
        risk["unknowns"] = [
            _single_line(item, f"risk.unknowns[{index}]", maximum=1000)
            for index, item in enumerate(unknown_risks)
            if item.strip()
        ]
    _risk_vector(risk)
    result["risk"] = dict(risk)
    for field, default in (
        ("owner", "harness-lifecycle"),
        ("base_ref", "refs/heads/main"),
        ("governance_ref", "refs/heads/main"),
    ):
        result[field] = _single_line(result.get(field, default), field, maximum=255)
    for field in ("branch_ref", "worktree_path"):
        if result.get(field) is not None:
            result[field] = _single_line(result[field], field, maximum=4096)
    return result


def load_request(path: str | Path) -> dict[str, object]:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise LifecycleError(f"request JSON file does not exist: {candidate}")
    return validate_request(_read_json(candidate, label="lifecycle request"))


def _context(project_root: str | Path) -> workspace.RepositoryContext:
    try:
        return workspace.resolve_repository(project_root)
    except workspace.WorkspaceError as exc:
        raise LifecycleError(str(exc)) from exc


def _active_leases(context: workspace.RepositoryContext) -> list[dict[str, object]]:
    leases, blockers = workspace.load_active_leases(context)
    if blockers:
        raise LifecycleError("workspace lease registry is invalid: " + "; ".join(item.message for item in blockers))
    return leases


def _decision_for_authority(
    request: Mapping[str, object],
    authority: coordinator.IterationAuthority | None,
    active_writers: int,
) -> dict[str, object]:
    decision = classify(
        DecisionInput(
            read_only=bool(request.get("read_only")),
            ambiguities=tuple(str(item) for item in request.get("ambiguities", [])),
            risk=_risk_vector(request.get("risk", {})),
            active_writers=active_writers,
            depends_on=authority.depends_on if authority else (),
            conflicts_with=authority.conflicts_with if authority else (),
            principle_change=bool(request.get("principle_change")),
            exclusive_resource=bool(request.get("exclusive_resource")),
            incompatible_schema=bool(request.get("incompatible_schema")),
            authorization=AuthorizationState(
                prd_approved=authority.prd_approved if authority else False,
                spec_approved=authority.spec_approved if authority else False,
                implementation_authorized=authority.implementation_authorized if authority else False,
                integration_authorized=bool(authority.verified_candidate_refs) if authority else False,
                finally_accepted=authority.integrated if authority else False,
            ),
        )
    )
    return decision.as_dict()


def _principle_gate_projection(
    context: workspace.RepositoryContext,
    iteration: str,
) -> dict[str, object] | None:
    """Recompute one v2 principle gate from durable common-dir evidence.

    The lifecycle facade intentionally accepts no caller-supplied receipt.  The
    audit module reloads the allocation, latest committed main authority, unique
    causal-tip receipt, and its exact APPLIED journal on every projection.
    """

    number = _iteration(iteration)
    try:
        gate = principle_audit.current_principle_gate(
            context.project_root,
            iteration=number,
        )
    except principle_audit.PrincipleAuditError as exc:
        return {
            "iteration": number,
            "allowed": False,
            "drift": None,
            "allocation_principle_sha256": None,
            "current_principle_sha256": None,
            "disposition": None,
            "receipt_digest": None,
            "generation": None,
            "causal_tip": None,
            "supersedes": None,
            "blockers": [f"principle-audit-gate-invalid:{exc}"],
            "next_gate": "reconcile-principle-audit-chain",
        }

    value = gate.as_dict()
    value.update({"generation": None, "causal_tip": None, "supersedes": None})
    if gate.receipt_digest is not None:
        try:
            receipt = principle_audit.load_principle_impact_audit(
                context.common_dir,
                number,
                gate.current_principle_sha256,
            )
        except principle_audit.PrincipleAuditError as exc:
            blockers = list(value["blockers"])
            blockers.append(f"principle-audit-tip-invalid:{exc}")
            value.update(
                {
                    "allowed": False,
                    "blockers": list(dict.fromkeys(blockers)),
                    "next_gate": "reconcile-principle-audit-chain",
                }
            )
        else:
            if receipt is None or receipt.receipt_digest != gate.receipt_digest:
                blockers = list(value["blockers"])
                blockers.append("principle-audit-tip-mismatch")
                value.update(
                    {
                        "allowed": False,
                        "blockers": list(dict.fromkeys(blockers)),
                        "next_gate": "reconcile-principle-audit-chain",
                    }
                )
            else:
                value.update(
                    {
                        "generation": receipt.generation,
                        "causal_tip": receipt.receipt_digest,
                        "supersedes": receipt.supersedes,
                    }
                )
    return value


def _principle_gate_blockers(gate: Mapping[str, object] | None) -> list[str]:
    if gate is None or bool(gate.get("allowed")):
        return []
    raw = gate.get("blockers")
    if not isinstance(raw, (list, tuple)) or not raw:
        return ["principle:principle-audit-gate-denied"]
    return [f"principle:{item}" for item in raw]


def route_request(
    project_root: str | Path,
    request: Mapping[str, object],
    *,
    operation_id: str | None = None,
) -> dict[str, object]:
    """Classify all three axes without writing or inferring authorization."""

    context = _context(project_root)
    normalized = validate_request(dict(request))
    operation = _operation(operation_id) if operation_id else None
    leases = _active_leases(context)
    number = normalized.get("iteration")
    authority: coordinator.IterationAuthority | None = None
    coordinator_payload: dict[str, object] | None = None
    principle_gate: dict[str, object] | None = None
    route_blockers: list[str] = []
    if isinstance(number, str):
        child = operation or _child_operation("OP-" + "0" * 32, f"route-{number}")
        try:
            coordinated = coordinator.plan_route(
                context.project_root,
                iteration=number,
                read_only=bool(normalized["read_only"]),
                ambiguities=tuple(str(item) for item in normalized["ambiguities"]),
                risk=normalized["risk"],
                operation_id=child,
            )
        except (coordinator.CoordinatorError, core.HarnessError, workspace.WorkspaceError) as exc:
            raise LifecycleError(f"coordinator authority is unavailable: {exc}") from exc
        authority = coordinated.authority
        coordinator_payload = coordinated.as_dict()
        route_blockers.extend(coordinated.blocking_reasons)
        if (
            authority is not None
            and isinstance(authority.base_ref, str)
            and authority.base_ref.startswith("refs/project-harness/v2/iterations/")
        ):
            principle_gate = _principle_gate_projection(context, authority.iteration)
            route_blockers.extend(_principle_gate_blockers(principle_gate))
        other_writers = len({str(item["iteration"]) for item in leases} - {number})
    else:
        other_writers = len(leases)
    decision = _decision_for_authority(normalized, authority, other_writers)
    route_blockers.extend(str(item) for item in decision["blocking_reasons"])
    # Authorization is reported as a gate, never fabricated as a blocker for
    # read-only classification.  Existing iteration mutation keeps every
    # coordinator blocker; a new request can still reserve/draft before approval.
    blocked = bool(route_blockers) and (isinstance(number, str) or decision["governance_path"] == "grill")
    payload = {
        "schema_version": ROUTE_SCHEMA,
        "command": "route",
        "action_level": "silent",
        "pushed": False,
        "project_root": str(context.project_root),
        "operation_id": operation,
        "request_digest": digest(normalized),
        "iteration": number,
        "authority": asdict(authority) if authority else None,
        "axes": {
            "governance_path": decision["governance_path"],
            "execution_topology": decision["execution_topology"],
            "authorization_gate": decision["authorization_gate"],
        },
        "reason_codes": decision["reason_codes"],
        "inferred_authorization": False,
        "active_writers": [str(item["iteration"]) for item in leases],
        "coordinator": coordinator_payload,
        "principle_gate": principle_gate,
        "phase": "blocked" if blocked else "routed",
        "blocking_reasons": sorted(set(route_blockers)),
        "next_gate": (
            str(principle_gate["next_gate"])
            if principle_gate is not None and not bool(principle_gate.get("allowed"))
            else "resolve-routing-blockers"
            if blocked
            else str(decision["authorization_gate"])
            if isinstance(number, str)
            else "plan-reservation-and-draft"
        ),
        "exclusions": list(EXCLUSIONS),
    }
    return payload


def _safe_json_projection(path: Path, *, root: Path) -> dict[str, object]:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
        value = _read_json(resolved, label="train journal")
    except (OSError, ValueError, LifecycleError) as exc:
        return {"path": str(path), "corrupt": True, "error": str(exc)}
    return {
        "path": str(path),
        "corrupt": False,
        "schema_version": value.get("schema_version"),
        "kind": value.get("kind"),
        "operation_id": value.get("operation_id"),
        "status": value.get("status"),
        "iteration": value.get("iteration"),
        "generation": value.get("generation"),
        "candidate_ref": value.get("candidate_ref"),
        "candidate_commit": value.get("candidate_commit"),
        "integrated_commit": value.get("integrated_commit"),
        "failure": value.get("failure"),
    }


def _train_status(context: workspace.RepositoryContext, core_status: Mapping[str, object]) -> dict[str, object]:
    root = context.common_dir / "project-harness" / "train" / "v1"
    journals: list[dict[str, object]] = []
    journal_dir = root / "journal"
    if journal_dir.is_dir() and not journal_dir.is_symlink():
        journals = [_safe_json_projection(path, root=root) for path in sorted(journal_dir.glob("*.json"))]
    refs: list[dict[str, object]] = []
    for item in core_status.get("iterations", []):
        if not isinstance(item, Mapping):
            continue
        refs.append(
            {
                "iteration": item.get("number"),
                "candidates": item.get("candidates", []),
                "integrated_ref": item.get("integrated_ref"),
                "integrated_object": item.get("integrated_object"),
                "final_ref": item.get("final_ref"),
                "final_object": item.get("final_object"),
            }
        )
    corrupt = [item for item in journals if item.get("corrupt")]
    return {
        "schema_version": TRAIN_SCHEMA,
        "registry_root": str(root),
        "refs": refs,
        "journals": journals,
        "blocking_reasons": [f"corrupt-train-journal:{item['path']}" for item in corrupt],
        "next_gate": "reconcile-train" if corrupt else "candidate-or-integration-gate",
    }


def _governance_status(context: workspace.RepositoryContext, core_status: Mapping[str, object]) -> dict[str, object]:
    git = context.git
    try:
        governance_ref, commit, snapshot = core.committed_governance_snapshot(git, context.project_root, "refs/heads/main")
        canonical: dict[str, object] = {
            "ref": governance_ref,
            "commit": commit,
            "tree": snapshot["tree"],
            "principle_sha256": snapshot["principle_sha256"],
        }
        canonical_error = None
    except core.HarnessError as exc:
        canonical = {}
        canonical_error = str(exc)
    validation = core.collect_validation(context.project_root)
    iteration_gates: list[dict[str, object]] = []
    principle_gate_reasons: list[dict[str, object]] = []
    try:
        open_iterations = principle_audit.discover_open_v2_iterations(context.project_root)
        principle_audits = [
            _principle_gate_projection(context, item.iteration)
            for item in open_iterations
        ]
        audit_by_number = {
            str(item["iteration"]): item
            for item in principle_audits
            if isinstance(item, Mapping)
        }
        audit_inventory_error = None
    except principle_audit.PrincipleAuditError as exc:
        principle_audits = []
        audit_by_number = {}
        audit_inventory_error = str(exc)
    for number, principle_gate in audit_by_number.items():
        for reason in _principle_gate_blockers(principle_gate):
            principle_gate_reasons.append(
                {
                    "code": reason.removeprefix("principle:"),
                    "iteration": number,
                    "message": reason,
                }
            )
    for item in core_status.get("iterations", []):
        if not isinstance(item, Mapping) or not item.get("bundle_present"):
            continue
        number = str(item.get("number"))
        try:
            authority = coordinator.derive_iteration_authority(context.project_root, number)
            allocation = item.get("allocation_metadata")
            allocation_principle = allocation.get("principle_sha256") if isinstance(allocation, Mapping) else None
            principle_gate = (
                audit_by_number.get(number)
            )
            principle_drift = bool(principle_gate and principle_gate.get("drift"))
            principle_allowed = bool(principle_gate is None or principle_gate.get("allowed"))
            if not principle_allowed:
                gate = str(principle_gate["next_gate"])
            elif not authority.prd_approved:
                gate = "approve-prd"
            elif not authority.spec_approved:
                gate = "approve-spec"
            elif not authority.implementation_authorized:
                gate = "authorize-implementation"
            elif authority.blockers:
                gate = "reconcile-authority"
            elif not authority.active_writer:
                gate = "activate-writer"
            elif not authority.candidate_refs:
                gate = "implement-and-register-candidate"
            elif not authority.integrated:
                gate = "verify-and-integrate"
            else:
                gate = "final-acceptance"
            iteration_gates.append(
                {
                    "iteration": number,
                    "prd_status": authority.prd_status,
                    "spec_status": authority.spec_status,
                    "allocation_principle_sha256": allocation_principle,
                    "canonical_main_principle_sha256": authority.principle_sha256,
                    "principle_drift": principle_drift,
                    "principle_gate": principle_gate,
                    "audit_generation": principle_gate.get("generation") if principle_gate else None,
                    "audit_causal_tip": principle_gate.get("causal_tip") if principle_gate else None,
                    "candidate_integration_allowed": (
                        principle_allowed
                        and authority.prd_approved
                        and authority.spec_approved
                        and authority.implementation_authorized
                        and not authority.blockers
                    ),
                    "next_gate": gate,
                    "blockers": list(authority.blockers),
                }
            )
        except (coordinator.CoordinatorError, core.HarnessError, workspace.WorkspaceError) as exc:
            iteration_gates.append({"iteration": number, "next_gate": "reconcile-authority", "blockers": [str(exc)]})
    blockers = [asdict(issue) for issue in validation.errors]
    blockers.extend(principle_gate_reasons)
    if audit_inventory_error:
        blockers.append(
            {
                "code": "principle-audit-inventory-invalid",
                "message": audit_inventory_error,
            }
        )
    if canonical_error:
        blockers.insert(0, {"code": "canonical-governance-invalid", "message": canonical_error})
    return {
        "canonical_main": canonical,
        "principle": {
            "path": str(context.project_root / "harness" / "principle.md"),
            "sha256": hashlib.sha256((context.project_root / "harness" / "principle.md").read_bytes()).hexdigest()
            if (context.project_root / "harness" / "principle.md").is_file()
            else None,
        },
        "progress": {
            "path": str(context.project_root / "harness" / "progress.md"),
            "sha256": hashlib.sha256((context.project_root / "harness" / "progress.md").read_bytes()).hexdigest()
            if (context.project_root / "harness" / "progress.md").is_file()
            else None,
            "role": "append-only-historical-evidence",
        },
        "live_validation": validation.as_dict(),
        "principle_audits": principle_audits,
        "iteration_gates": iteration_gates,
        "blocking_reasons": blockers,
        "next_gate": (
            "principle-impact-audit"
            if principle_gate_reasons or audit_inventory_error
            else "repair-governance"
            if blockers
            else "follow-iteration-gates"
        ),
    }


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _progress_child_status(context: workspace.RepositoryContext) -> dict[str, object]:
    """Project durable progress children, including a pre-bound not-yet-started child."""

    operations_root = (
        context.common_dir / "project-harness" / "progress" / "v2" / "operations"
    )
    children: list[dict[str, object]] = []
    blockers: list[str] = []
    by_identity: dict[tuple[str, str], dict[str, object]] = {}
    if operations_root.exists():
        if not operations_root.is_dir() or _is_link_or_junction(operations_root):
            blockers.append(f"progress-registry-unsafe:{operations_root}")
        else:
            for operation_dir in sorted(operations_root.iterdir(), key=lambda item: item.name):
                if (
                    not operation_dir.is_dir()
                    or _is_link_or_junction(operation_dir)
                    or OPERATION_RE.fullmatch(operation_dir.name) is None
                ):
                    blockers.append(f"progress-operation-path-invalid:{operation_dir}")
                    continue
                for path in sorted(operation_dir.iterdir(), key=lambda item: item.name):
                    journal: Mapping[str, object] | None = None
                    if (
                        not path.is_file()
                        or _is_link_or_junction(path)
                        or path.suffix != ".json"
                        or re.fullmatch(r"event-[0-9a-f]{64}", path.stem) is None
                    ):
                        blockers.append(f"progress-journal-path-invalid:{path}")
                        continue
                    try:
                        journal = _read_json(path, label="progress append journal")
                        event_id = journal.get("event_id")
                        if not isinstance(event_id, str):
                            raise LifecycleError("progress journal has no canonical event identity")
                        expected_path = progress.journal_path(
                            context.common_dir,
                            operation_dir.name,
                            event_id,
                        )
                        if expected_path != path:
                            raise LifecycleError("progress journal locator does not match its event identity")
                        append_plan = progress.load_progress_append_plan(
                            context.common_dir,
                            operation_dir.name,
                            event_id,
                        )
                        phase = str(journal.get("phase"))
                        error = journal.get("error")
                        item: dict[str, object] = {
                            "operation_id": operation_dir.name,
                            "event_id": event_id,
                            "iteration": append_plan.event.iteration,
                            "session_id": append_plan.event.session_id,
                            "scope": append_plan.event.scope,
                            "phase": phase,
                            "project_root": append_plan.project_root,
                            "progress_path": append_plan.progress_path,
                            "plan_digest": append_plan.plan_digest,
                            "before_sha256": append_plan.before_sha256,
                            "after_sha256": append_plan.after_sha256,
                            "error": error,
                            "blocking": phase == "FAILED_NEEDS_RECONCILE",
                        }
                        if phase == "FAILED_NEEDS_RECONCILE":
                            blockers.append(
                                f"progress-child-failed:{event_id}:{error or 'reconcile required'}"
                            )
                    except (LifecycleError, progress.ProgressError, OSError, ValueError) as exc:
                        event_id = (
                            str(journal.get("event_id"))
                            if isinstance(journal, Mapping)
                            else path.stem
                        )
                        item = {
                            "operation_id": operation_dir.name,
                            "event_id": event_id,
                            "phase": "CORRUPT",
                            "path": str(path),
                            "error": str(exc),
                            "blocking": True,
                        }
                        blockers.append(f"progress-child-corrupt:{event_id}:{exc}")
                    children.append(item)
                    by_identity[(operation_dir.name, event_id)] = item

    lifecycle_journals = _registry(context) / "journal"
    if lifecycle_journals.exists():
        if not lifecycle_journals.is_dir() or _is_link_or_junction(lifecycle_journals):
            blockers.append(f"lifecycle-journal-registry-unsafe:{lifecycle_journals}")
        else:
            for path in sorted(lifecycle_journals.iterdir(), key=lambda item: item.name):
                if (
                    not path.is_file()
                    or _is_link_or_junction(path)
                    or path.suffix != ".json"
                    or OPERATION_RE.fullmatch(path.stem) is None
                ):
                    blockers.append(f"lifecycle-journal-path-invalid:{path}")
                    continue
                try:
                    lifecycle_journal = _load_lifecycle_journal(context, path.stem)
                except LifecycleError as exc:
                    blockers.append(f"lifecycle-progress-binding-corrupt:{path}:{exc}")
                    continue
                if not isinstance(lifecycle_journal, Mapping):
                    continue
                active = lifecycle_journal.get("active_plan")
                if not isinstance(active, Mapping):
                    continue
                child = active.get("accepted_child")
                if not isinstance(child, Mapping) or child.get("action") != "activate-workspace":
                    continue
                parameters = child.get("parameters")
                binding = parameters.get("activation_progress") if isinstance(parameters, Mapping) else None
                event = binding.get("event") if isinstance(binding, Mapping) else None
                child_operation = child.get("operation_id")
                event_id = event.get("event_id") if isinstance(event, Mapping) else None
                if not isinstance(child_operation, str) or not isinstance(event_id, str):
                    blockers.append(f"lifecycle-progress-binding-invalid:{path}")
                    continue
                key = (child_operation, event_id)
                durable = by_identity.get(key)
                if durable is not None:
                    durable["lifecycle_operation_id"] = path.stem
                    durable["lifecycle_plan_digest"] = active.get("plan_digest")
                    continue
                workspace_phase = "NOT_STARTED"
                workspace_error: str | None = None
                try:
                    workspace_journal = workspace.load_journal(context, child_operation)
                except workspace.WorkspaceError as exc:
                    workspace_phase = "CORRUPT"
                    workspace_error = str(exc)
                else:
                    if isinstance(workspace_journal, Mapping):
                        workspace_phase = str(workspace_journal.get("phase"))
                        if workspace_journal.get("error") is not None:
                            workspace_error = str(workspace_journal.get("error"))
                last_error = lifecycle_journal.get("last_error")
                if isinstance(last_error, str) and last_error:
                    phase = "BLOCKED"
                    error = last_error
                    is_blocking = True
                elif workspace_phase == "FAILED_NEEDS_RECONCILE" or workspace_phase == "CORRUPT":
                    phase = "BLOCKED_BY_WORKSPACE"
                    error = workspace_error or "workspace child requires reconcile"
                    is_blocking = True
                elif workspace_phase == "READY":
                    phase = "PENDING_AFTER_WORKSPACE"
                    error = None
                    is_blocking = False
                else:
                    phase = "PREBOUND"
                    error = None
                    is_blocking = False
                item = {
                    "operation_id": child_operation,
                    "event_id": event_id,
                    "iteration": event.get("iteration") if isinstance(event, Mapping) else None,
                    "session_id": event.get("session_id") if isinstance(event, Mapping) else None,
                    "scope": event.get("scope") if isinstance(event, Mapping) else None,
                    "phase": phase,
                    "project_root": binding.get("target_project_root") if isinstance(binding, Mapping) else None,
                    "progress_path": binding.get("progress_path") if isinstance(binding, Mapping) else None,
                    "lifecycle_operation_id": path.stem,
                    "lifecycle_plan_digest": active.get("plan_digest"),
                    "workspace_phase": workspace_phase,
                    "error": error,
                    "blocking": is_blocking,
                }
                if is_blocking:
                    blockers.append(f"progress-child-blocked:{event_id}:{error}")
                children.append(item)
                by_identity[key] = item

    pending_phases = {"PLANNED", "APPLYING", "PREBOUND", "PENDING_AFTER_WORKSPACE"}
    pending = [item for item in children if item.get("phase") in pending_phases]
    return {
        "schema_version": PROGRESS_STATUS_SCHEMA,
        "registry_root": str(operations_root.parent),
        "phase": "blocked" if blockers else "pending" if pending else "ready",
        "children": children,
        "pending_count": len(pending),
        "blocking_reasons": sorted(set(blockers)),
        "next_gate": (
            "reconcile-progress"
            if blockers
            else "resume-progress-append"
            if pending
            else "progress-ready"
        ),
        "pushed": False,
    }


def lifecycle_status(project_root: str | Path) -> dict[str, object]:
    """Aggregate core, route, workspace, train, principle, and progress facts."""

    context = _context(project_root)
    core_snapshot = core.build_status_snapshot(context.project_root, context.git, all_worktrees=True).as_dict()
    workspace_snapshot = workspace.status_payload(context)
    governance_snapshot = _governance_status(context, core_snapshot)
    progress_snapshot = _progress_child_status(context)
    governance_progress = governance_snapshot.get("progress")
    if isinstance(governance_progress, dict):
        governance_progress["child_phase"] = progress_snapshot["phase"]
        governance_progress["child_next_gate"] = progress_snapshot["next_gate"]
        governance_progress["child_blocking_reasons"] = progress_snapshot["blocking_reasons"]
    routes: list[dict[str, object]] = []
    for item in core_snapshot["iterations"]:
        if not isinstance(item, Mapping) or not item.get("bundle_present"):
            continue
        request = validate_request(
            {
                "schema_version": REQUEST_SCHEMA,
                "iteration": item["number"],
                "read_only": True,
            }
        )
        try:
            routes.append(route_request(context.project_root, request))
        except LifecycleError as exc:
            routes.append({"iteration": item["number"], "phase": "blocked", "blocking_reasons": [str(exc)]})
    train_snapshot = _train_status(context, core_snapshot)
    stage_snapshot = lifecycle_stage_status(context.project_root)
    blockers: list[object] = []
    blockers.extend(core_snapshot.get("blocking_reasons", []))
    blockers.extend(workspace_snapshot.get("blocking_reasons", []))
    blockers.extend(governance_snapshot.get("blocking_reasons", []))
    blockers.extend(train_snapshot.get("blocking_reasons", []))
    blockers.extend(progress_snapshot.get("blocking_reasons", []))
    blockers.extend(stage_snapshot.get("blocking_reasons", []))
    principle_audits = governance_snapshot.get("principle_audits", [])
    principle_drift = any(
        isinstance(item, Mapping) and bool(item.get("drift"))
        for item in principle_audits
    ) if isinstance(principle_audits, list) else False
    principle_gate_blocked = governance_snapshot.get("next_gate") == "principle-impact-audit"
    return {
        "schema_version": STATUS_SCHEMA,
        "command": "status",
        "action_level": "silent",
        "pushed": False,
        "project_root": str(context.project_root),
        "git_common_dir": str(context.common_dir),
        "phase": "blocked" if blockers else "ready",
        "core": core_snapshot,
        "routes": routes,
        "workspace": workspace_snapshot,
        "progress": progress_snapshot,
        "train": train_snapshot,
        "lifecycle_stages": stage_snapshot,
        "governance": governance_snapshot,
        "principle_drift": principle_drift,
        "blocking_reasons": blockers,
        "next_gate": (
            "principle-impact-audit"
            if principle_gate_blocked
            else "reconcile-progress"
            if progress_snapshot.get("blocking_reasons")
            else "reconcile-lifecycle-stage"
            if stage_snapshot.get("blocking_reasons")
            else "reconcile"
            if blockers
            else "resume-progress-append"
            if progress_snapshot.get("pending_count")
            else stage_snapshot["next_gate"]
            if stage_snapshot.get("has_history")
            else workspace_snapshot["next_gate"]
        ),
        "exclusions": list(EXCLUSIONS),
    }


def _registry(context: workspace.RepositoryContext) -> Path:
    return context.common_dir.joinpath(*REGISTRY_PARTS)


def _journal_path(context: workspace.RepositoryContext, operation_id: str) -> Path:
    return _registry(context) / "journal" / f"{_operation(operation_id)}.json"


def _lock_path(context: workspace.RepositoryContext, operation_id: str) -> Path:
    return _registry(context) / "locks" / f"{_operation(operation_id)}.lock"


def _stage_journal_path(context: workspace.RepositoryContext, operation_id: str) -> Path:
    return _registry(context) / "stage-journal" / f"{_operation(operation_id)}.json"


def _new_stage_journal(plan: LifecycleStagePlan) -> dict[str, object]:
    return {
        "schema_version": STAGE_JOURNAL_SCHEMA,
        "operation_id": plan.operation_id,
        "project_root": plan.project_root,
        "phase": "APPLYING",
        "active_plan": plan.as_dict(),
        "completed_stages": [],
        "notification_receipts": [],
        "last_error": None,
    }


def _load_stage_journal(
    context: workspace.RepositoryContext,
    operation_id: str,
) -> dict[str, object] | None:
    path = _stage_journal_path(context, operation_id)
    if not path.exists():
        return None
    value = _read_json(path, label="lifecycle stage journal")
    if set(value) != STAGE_JOURNAL_FIELDS or value.get("schema_version") != STAGE_JOURNAL_SCHEMA:
        raise LifecycleError(f"lifecycle stage journal schema is invalid: {path}")
    if value.get("operation_id") != operation_id or value.get("project_root") != str(context.project_root):
        raise LifecycleError(f"lifecycle stage journal identity is invalid: {path}")
    if value.get("phase") not in {"APPLYING", "STEPPED"}:
        raise LifecycleError(f"lifecycle stage journal phase is invalid: {path}")
    completed = value.get("completed_stages")
    receipts = value.get("notification_receipts")
    if not isinstance(completed, list) or not isinstance(receipts, list):
        raise LifecycleError(f"lifecycle stage journal history is invalid: {path}")
    previous_rank = -1
    seen_plans: set[str] = set()
    for item in completed:
        fields = {
            "stage", "plan_digest", "child_plan_digest", "subject_digest",
            "result_digest", "result", "evidence_refs", "next_gate",
        }
        if not isinstance(item, Mapping) or set(item) != fields:
            raise LifecycleError(f"lifecycle stage journal result schema is invalid: {path}")
        stage = item.get("stage")
        plan_digest = item.get("plan_digest")
        if stage not in STAGE_NEXT_GATE or not isinstance(plan_digest, str) or DIGEST_RE.fullmatch(plan_digest) is None:
            raise LifecycleError(f"lifecycle stage journal result identity is invalid: {path}")
        rank = STAGE_ORDER.index(str(stage))
        if rank <= previous_rank or plan_digest in seen_plans:
            raise LifecycleError(f"lifecycle stage journal order is invalid: {path}")
        previous_rank = rank
        seen_plans.add(plan_digest)
        result = item.get("result")
        if not isinstance(result, Mapping) or item.get("result_digest") != digest(dict(result)):
            raise LifecycleError(f"lifecycle stage journal result digest is invalid: {path}")
    active = value.get("active_plan")
    if active is not None:
        if not isinstance(active, Mapping):
            raise LifecycleError(f"lifecycle stage journal active plan is invalid: {path}")
        try:
            active_plan = LifecycleStagePlan(**dict(active))
            _validate_stage_plan(active_plan)
        except (TypeError, LifecycleError) as exc:
            raise LifecycleError(f"lifecycle stage journal active plan is invalid: {path}") from exc
    if (value["phase"] == "APPLYING") != (active is not None):
        raise LifecycleError(f"lifecycle stage journal phase/active plan is inconsistent: {path}")
    for receipt in receipts:
        fields = {
            "schema_version", "notification_id", "stage_plan_digest", "payload",
            "payload_digest", "callback_state", "callback_error",
        }
        if not isinstance(receipt, Mapping) or set(receipt) != fields:
            raise LifecycleError(f"lifecycle stage notification receipt is invalid: {path}")
        payload = receipt.get("payload")
        if (
            receipt.get("schema_version") != STAGE_NOTIFICATION_RECEIPT_SCHEMA
            or not isinstance(receipt.get("notification_id"), str)
            or not str(receipt["notification_id"]).startswith("NT-")
            or not isinstance(payload, Mapping)
            or receipt.get("payload_digest") != digest(dict(payload))
            or receipt.get("callback_state") not in {"pending", "returned", "raised"}
        ):
            raise LifecycleError(f"lifecycle stage notification receipt binding is invalid: {path}")
    return value


def _ensure_registry_path(context: workspace.RepositoryContext, path: Path) -> None:
    root = _registry(context).resolve(strict=False)
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LifecycleError(f"lifecycle operational path escapes its registry: {path}") from exc
    current = candidate
    while True:
        if current.exists() and (current.is_symlink() or bool(getattr(current, "is_junction", lambda: False)())):
            raise LifecycleError(f"lifecycle operational path crosses a link or junction: {current}")
        if os.path.normcase(str(current)) == os.path.normcase(str(context.common_dir.resolve())):
            return
        if current.parent == current:
            raise LifecycleError(f"lifecycle registry is not under the Git common directory: {path}")
        current = current.parent


def _atomic_json(context: workspace.RepositoryContext, path: Path, value: Mapping[str, object]) -> None:
    _ensure_registry_path(context, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_registry_path(context, path.parent)
    raw = canonical_json(dict(value)) + b"\n"
    if len(raw) > MAX_JSON_BYTES:
        raise LifecycleError(f"lifecycle JSON exceeds its safe size limit: {path}")
    handle = tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _stage_notification_proxy(
    context: workspace.RepositoryContext,
    journal: dict[str, object],
    *,
    stage_plan_digest: str,
    notify: Callable[[object], None] | None,
) -> Callable[[object], None]:
    def proxy(payload_value: object) -> None:
        payload = _public_object_snapshot(payload_value)
        payload_digest = digest(payload)
        notification_id = "NT-" + hashlib.sha256(
            canonical_json({"stage_plan_digest": stage_plan_digest, "payload_digest": payload_digest})
        ).hexdigest()[:32]
        receipts = journal["notification_receipts"]
        assert isinstance(receipts, list)
        existing = next(
            (item for item in receipts if isinstance(item, Mapping) and item.get("notification_id") == notification_id),
            None,
        )
        if existing is not None:
            if existing.get("payload") != payload or existing.get("stage_plan_digest") != stage_plan_digest:
                raise LifecycleError("lifecycle stage notification identity collided with different bytes")
            return
        receipt: dict[str, object] = {
            "schema_version": STAGE_NOTIFICATION_RECEIPT_SCHEMA,
            "notification_id": notification_id,
            "stage_plan_digest": stage_plan_digest,
            "payload": payload,
            "payload_digest": payload_digest,
            "callback_state": "pending",
            "callback_error": None,
        }
        receipts.append(receipt)
        path = _stage_journal_path(context, str(journal["operation_id"]))
        _atomic_json(context, path, journal)
        if notify is None:
            return
        try:
            notify(payload_value)
        except BaseException as exc:
            receipt["callback_state"] = "raised"
            receipt["callback_error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            _atomic_json(context, path, journal)
            raise
        receipt["callback_state"] = "returned"
        _atomic_json(context, path, journal)

    return proxy


@contextlib.contextmanager
def _operation_lock(context: workspace.RepositoryContext, operation_id: str, timeout: float = 30.0):
    path = _lock_path(context, operation_id)
    _ensure_registry_path(context, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_registry_path(context, path.parent)
    deadline = time.monotonic() + timeout
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
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
                    raise LifecycleError(f"lifecycle operation is already active: {operation_id}") from exc
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


def _apply_stage_transaction(
    plan: LifecycleStagePlan,
    *,
    accepted_plan_digest: str,
    subject: object,
    replanned: LifecycleStagePlan,
    execute: Callable[[Callable[[object], None]], object],
    notify: Callable[[object], None] | None = None,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    _validate_stage_plan(plan)
    _validate_stage_plan(replanned)
    accepted = accepted_plan_digest.strip()
    if accepted != plan.plan_digest:
        raise LifecycleError("accepted lifecycle stage digest differs from the exact plan")
    if _object_digest(subject) != plan.subject_digest:
        raise LifecycleError("lifecycle stage subject changed after planning")
    if plan.blockers:
        raise LifecycleError("blocked lifecycle stage cannot be applied: " + "; ".join(plan.blockers))
    context = _context(plan.project_root)
    path = _stage_journal_path(context, plan.operation_id)
    with _operation_lock(context, plan.operation_id):
        journal = _load_stage_journal(context, plan.operation_id)
        if journal is None:
            if replanned != plan:
                raise LifecycleError("lifecycle stage child plan changed; create and accept a new stage plan")
            journal = _new_stage_journal(plan)
            replay = False
        else:
            if replanned != plan:
                raise LifecycleError(
                    "lifecycle stage child plan changed; recover or accept a new exact stage plan"
                )
            completed = journal["completed_stages"]
            assert isinstance(completed, list)
            existing = next(
                (item for item in completed if isinstance(item, Mapping) and item.get("plan_digest") == plan.plan_digest),
                None,
            )
            replay = existing is not None
            active = journal.get("active_plan")
            if replay:
                if active is not None:
                    raise LifecycleError(
                        "a completed lifecycle stage cannot replay while another stage is active"
                    )
            else:
                if completed:
                    prior_stage = str(completed[-1]["stage"])
                    if STAGE_ORDER.index(plan.stage) <= STAGE_ORDER.index(prior_stage):
                        raise LifecycleError("lifecycle stage would move backward or replace completed evidence")
                if active is not None and digest(dict(active)) != digest(plan.as_dict()):
                    raise LifecycleError("another lifecycle stage plan is already active")
                journal["phase"] = "APPLYING"
                journal["active_plan"] = plan.as_dict()
                journal["last_error"] = None
        if not replay:
            _atomic_json(context, path, journal)
        proxy = _stage_notification_proxy(context, journal, stage_plan_digest=plan.plan_digest, notify=notify)
        try:
            child_result = execute(proxy)
            result_snapshot = _public_object_snapshot(child_result)
            result_digest = digest(result_snapshot)
            if failpoint is not None:
                failpoint("after-child-before-stage-journal")
        except BaseException as exc:
            if not replay:
                journal["last_error"] = str(exc)[:1000]
                _atomic_json(context, path, journal)
            raise
        completed = journal["completed_stages"]
        assert isinstance(completed, list)
        existing = next(
            (item for item in completed if isinstance(item, Mapping) and item.get("plan_digest") == plan.plan_digest),
            None,
        )
        evidence_refs = tuple(dict.fromkeys((*plan.evidence_refs, *_stage_evidence_refs(child_result))))
        if existing is not None:
            durable_result = existing.get("result")
            if (
                not isinstance(durable_result, Mapping)
                or digest(_stage_result_identity(durable_result))
                != digest(_stage_result_identity(result_snapshot))
                or tuple(existing.get("evidence_refs", ())) != evidence_refs
            ):
                raise LifecycleError("idempotent lifecycle child result differs from durable evidence")
            # A completed replay returns the exact durable public result.  The
            # just-executed child is used only to revalidate live authority;
            # its transient replay flags must not rewrite accepted evidence.
            child_result = (
                _decode_typed_json(dict(durable_result), type(child_result))
                if dataclasses.is_dataclass(child_result)
                else dict(durable_result)
            )
            result_snapshot = dict(durable_result)
            result_digest = str(existing["result_digest"])
        else:
            completed.append(
                {
                    "stage": plan.stage,
                    "plan_digest": plan.plan_digest,
                    "child_plan_digest": plan.child_plan_digest,
                    "subject_digest": plan.subject_digest,
                    "result_digest": result_digest,
                    "result": result_snapshot,
                    "evidence_refs": list(evidence_refs),
                    "next_gate": plan.next_gate,
                }
            )
        if not replay:
            journal["phase"] = "STEPPED"
            journal["active_plan"] = None
            journal["last_error"] = None
            _atomic_json(context, path, journal)
        receipts = tuple(
            dict(item)
            for item in journal["notification_receipts"]
            if isinstance(item, Mapping) and item.get("stage_plan_digest") == plan.plan_digest
        )
        return LifecycleStageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            operation_id=plan.operation_id,
            project_root=plan.project_root,
            stage=plan.stage,
            accepted_plan_digest=accepted,
            child_result=child_result,
            child_result_digest=result_digest,
            evidence_refs=evidence_refs,
            notification_receipts=receipts,
            next_gate=plan.next_gate,
            idempotent_replay=replay,
            journal_path=str(path),
        )


JOURNAL_FIELDS = {
    "schema_version",
    "operation_id",
    "project_root",
    "request_digest",
    "title",
    "phase",
    "iteration",
    "active_plan",
    "completed_plans",
    "child_results",
    "notification_receipts",
    "last_error",
}

NOTIFICATION_FIELDS = {
    "schema_version",
    "notification_id",
    "child_operation_id",
    "child_plan_digest",
    "action",
    "action_level",
    "phase",
    "summary",
    "facts",
    "facts_digest",
    "requires_user_response",
}

NOTIFICATION_FACT_FIELDS = {
    "iteration",
    "operation_id",
    "project_root",
    "base",
    "implementation_start",
    "branch",
    "worktree",
    "reason_code",
    "effect_on_existing_prds",
    "git_effects",
    "remote",
}

NOTIFICATION_RECEIPT_FIELDS = {
    "schema_version",
    "notification_id",
    "lifecycle_operation_id",
    "accepted_plan_digest",
    "child_action",
    "child_operation_id",
    "child_plan_digest",
    "phase",
    "payload",
    "payload_digest",
    "binding_digest",
    "callback_state",
    "callback_error",
}


def _exact_mapping(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise LifecycleError(f"{label} schema is invalid")
    return value


def _validate_notification_payload(value: object) -> dict[str, object]:
    payload = _exact_mapping(value, NOTIFICATION_FIELDS, "lifecycle notification")
    try:
        raw = canonical_json(dict(payload))
    except (TypeError, ValueError) as exc:
        raise LifecycleError("lifecycle notification is not canonical JSON") from exc
    if len(raw) > MAX_NOTIFICATION_BYTES:
        raise LifecycleError("lifecycle notification exceeds its safe size limit")
    if payload.get("schema_version") != NOTIFICATION_SCHEMA:
        raise LifecycleError("lifecycle notification schema version is invalid")
    notification_id = payload.get("notification_id")
    child_operation_id = payload.get("child_operation_id")
    child_plan_digest = payload.get("child_plan_digest")
    phase = payload.get("phase")
    if not isinstance(notification_id, str) or not NOTIFICATION_ID_RE.fullmatch(notification_id):
        raise LifecycleError("lifecycle notification identity is invalid")
    if not isinstance(child_operation_id, str) or not OPERATION_RE.fullmatch(child_operation_id):
        raise LifecycleError("lifecycle notification child operation identity is invalid")
    if not isinstance(child_plan_digest, str) or not DIGEST_RE.fullmatch(child_plan_digest):
        raise LifecycleError("lifecycle notification child plan digest is invalid")
    if phase not in {"before", "after"}:
        raise LifecycleError("lifecycle notification phase is invalid")
    if payload.get("action") not in {"create-worktree", "activate-local"}:
        raise LifecycleError("lifecycle notification action is invalid")
    expected_level = "notify" if payload.get("action") == "create-worktree" else "silent"
    if payload.get("action_level") != expected_level or payload.get("requires_user_response") is not False:
        raise LifecycleError("lifecycle notification interaction policy is invalid")
    _single_line(payload.get("summary"), "lifecycle notification summary", maximum=1000)
    facts = _exact_mapping(payload.get("facts"), NOTIFICATION_FACT_FIELDS, "lifecycle notification facts")
    base = _exact_mapping(facts.get("base"), {"ref", "commit"}, "lifecycle notification base")
    implementation = _exact_mapping(
        facts.get("implementation_start"),
        {"ref", "commit"},
        "lifecycle notification implementation start",
    )
    branch = _exact_mapping(facts.get("branch"), {"ref", "will_create"}, "lifecycle notification branch")
    worktree_value = _exact_mapping(
        facts.get("worktree"),
        {"path", "will_create"},
        "lifecycle notification worktree",
    )
    _exact_mapping(
        facts.get("effect_on_existing_prds"),
        {"strategy", "moved", "committed", "stashed", "files_copied"},
        "lifecycle notification existing-PRD effects",
    )
    _exact_mapping(
        facts.get("git_effects"),
        {"worktree_created", "branch_created", "commit_created", "main_advanced", "push_performed"},
        "lifecycle notification Git effects",
    )
    _exact_mapping(facts.get("remote"), {"involved", "pushed", "force"}, "lifecycle notification remote effects")
    topology = "worktree" if payload.get("action") == "create-worktree" else "local"
    reconstructed_child = {
        "action": "activate-workspace",
        "operation_id": child_operation_id,
        "plan_digest": child_plan_digest,
        "parameters": {
            "iteration": facts.get("iteration"),
            "execution_topology": topology,
            "base_ref": base.get("ref"),
            "base_commit": base.get("commit"),
            "implementation_ref": implementation.get("ref"),
            "implementation_commit": implementation.get("commit"),
            "branch_ref": branch.get("ref"),
            "worktree_path": worktree_value.get("path"),
            "project_root": facts.get("project_root"),
        },
    }
    expected = _workspace_interaction(reconstructed_child, str(phase))
    if dict(payload) != expected:
        raise LifecycleError("lifecycle notification payload identity or facts are inconsistent")
    return expected


def _receipt_binding(receipt: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": NOTIFICATION_RECEIPT_SCHEMA,
        "notification_id": receipt["notification_id"],
        "lifecycle_operation_id": receipt["lifecycle_operation_id"],
        "accepted_plan_digest": receipt["accepted_plan_digest"],
        "child_action": receipt["child_action"],
        "child_operation_id": receipt["child_operation_id"],
        "child_plan_digest": receipt["child_plan_digest"],
        "phase": receipt["phase"],
        "payload_digest": receipt["payload_digest"],
    }


def _validate_notification_receipt(
    value: object,
    *,
    lifecycle_operation_id: str,
    project_root: str,
    valid_plan_digests: set[str],
) -> dict[str, object]:
    receipt = _exact_mapping(value, NOTIFICATION_RECEIPT_FIELDS, "lifecycle notification receipt")
    if receipt.get("schema_version") != NOTIFICATION_RECEIPT_SCHEMA:
        raise LifecycleError("lifecycle notification receipt schema version is invalid")
    notification_id = receipt.get("notification_id")
    accepted_plan_digest = receipt.get("accepted_plan_digest")
    child_operation_id = receipt.get("child_operation_id")
    child_plan_digest = receipt.get("child_plan_digest")
    if not isinstance(notification_id, str) or not NOTIFICATION_ID_RE.fullmatch(notification_id):
        raise LifecycleError("lifecycle notification receipt identity is invalid")
    if receipt.get("lifecycle_operation_id") != lifecycle_operation_id:
        raise LifecycleError("lifecycle notification receipt belongs to another lifecycle operation")
    if not isinstance(accepted_plan_digest, str) or accepted_plan_digest not in valid_plan_digests:
        raise LifecycleError("lifecycle notification receipt plan identity is invalid")
    if receipt.get("child_action") != "activate-workspace":
        raise LifecycleError("lifecycle notification receipt child action is invalid")
    if not isinstance(child_operation_id, str) or not OPERATION_RE.fullmatch(child_operation_id):
        raise LifecycleError("lifecycle notification receipt child operation is invalid")
    if not isinstance(child_plan_digest, str) or not DIGEST_RE.fullmatch(child_plan_digest):
        raise LifecycleError("lifecycle notification receipt child plan digest is invalid")
    if receipt.get("phase") not in {"before", "after"}:
        raise LifecycleError("lifecycle notification receipt phase is invalid")
    payload = _validate_notification_payload(receipt.get("payload"))
    if (
        payload.get("notification_id") != notification_id
        or payload.get("child_operation_id") != child_operation_id
        or payload.get("child_plan_digest") != child_plan_digest
        or payload.get("phase") != receipt.get("phase")
        or payload.get("action") != "create-worktree"
        or not isinstance(payload.get("facts"), Mapping)
        or payload["facts"].get("project_root") != project_root  # type: ignore[index]
    ):
        raise LifecycleError("lifecycle notification receipt payload binding is invalid")
    payload_digest = receipt.get("payload_digest")
    if not isinstance(payload_digest, str) or payload_digest != digest(payload):
        raise LifecycleError("lifecycle notification receipt payload digest is invalid")
    binding_digest = receipt.get("binding_digest")
    if not isinstance(binding_digest, str) or binding_digest != digest(_receipt_binding(receipt)):
        raise LifecycleError("lifecycle notification receipt binding digest is invalid")
    callback_state = receipt.get("callback_state")
    callback_error = receipt.get("callback_error")
    if callback_state not in {"not-requested", "pending", "returned", "raised"}:
        raise LifecycleError("lifecycle notification receipt callback state is invalid")
    if callback_state == "raised":
        error = _exact_mapping(callback_error, {"type", "message"}, "lifecycle notification callback error")
        _single_line(error.get("type"), "lifecycle notification callback error type", maximum=200)
        message = error.get("message")
        if not isinstance(message, str) or len(message) > 1000 or any(ord(char) < 32 for char in message):
            raise LifecycleError("lifecycle notification callback error message is invalid")
    elif callback_error is not None:
        raise LifecycleError("lifecycle notification receipt has an unexpected callback error")
    return dict(receipt)


def _load_lifecycle_journal(context: workspace.RepositoryContext, operation_id: str) -> dict[str, object] | None:
    path = _journal_path(context, operation_id)
    if not path.exists():
        return None
    _ensure_registry_path(context, path)
    value = _read_json(path, label="lifecycle journal")
    if set(value) != JOURNAL_FIELDS or value.get("schema_version") != JOURNAL_SCHEMA:
        raise LifecycleError(f"lifecycle journal schema is invalid: {path}")
    if value.get("operation_id") != operation_id or value.get("project_root") != str(context.project_root):
        raise LifecycleError(f"lifecycle journal identity is invalid: {path}")
    request_digest = value.get("request_digest")
    if not isinstance(request_digest, str) or not DIGEST_RE.fullmatch(request_digest):
        raise LifecycleError(f"lifecycle journal request digest is invalid: {path}")
    _single_line(value.get("title"), "lifecycle journal title")
    if value.get("phase") not in {"APPLYING", "STEPPED"}:
        raise LifecycleError(f"lifecycle journal phase is invalid: {path}")
    number = value.get("iteration")
    if number is not None:
        _iteration(number)
    completed = value.get("completed_plans")
    results = value.get("child_results")
    if (
        not isinstance(completed, list)
        or not all(isinstance(item, str) and DIGEST_RE.fullmatch(item) for item in completed)
        or len(set(completed)) != len(completed)
        or not isinstance(results, list)
        or not all(
            isinstance(item, dict)
            and set(item) == {"plan_digest", "child_action", "result"}
            and item.get("child_action") in NEXT_GATE_AFTER_ACTION
            and isinstance(item.get("result"), dict)
            for item in results
        )
    ):
        raise LifecycleError(f"lifecycle journal history is invalid: {path}")
    result_digests = [item.get("plan_digest") for item in results if isinstance(item, dict)]
    if result_digests != completed:
        raise LifecycleError(f"lifecycle journal result history is invalid: {path}")
    last_error = value.get("last_error")
    if last_error is not None and (not isinstance(last_error, str) or len(last_error) > 1000):
        raise LifecycleError(f"lifecycle journal error field is invalid: {path}")
    active = value.get("active_plan")
    if active is not None:
        if not isinstance(active, dict):
            raise LifecycleError(f"lifecycle journal active plan is invalid: {path}")
        expected = active.get("plan_digest")
        body = dict(active)
        body.pop("plan_digest", None)
        if (
            active.get("schema_version") != PLAN_SCHEMA
            or active.get("operation_id") != operation_id
            or active.get("project_root") != str(context.project_root)
            or active.get("request_digest") != request_digest
            or active.get("title") != value.get("title")
            or not isinstance(expected, str)
            or expected != digest(body)
        ):
            raise LifecycleError(f"lifecycle journal active plan digest is invalid: {path}")
    if value.get("phase") == "APPLYING" and active is None:
        raise LifecycleError(f"applying lifecycle journal has no active plan: {path}")
    if value.get("phase") == "STEPPED" and active is not None:
        raise LifecycleError(f"stepped lifecycle journal unexpectedly has an active plan: {path}")
    raw_receipts = value.get("notification_receipts")
    if not isinstance(raw_receipts, list) or len(raw_receipts) > MAX_NOTIFICATION_RECEIPTS:
        raise LifecycleError(f"lifecycle journal notification receipt history is invalid: {path}")
    valid_plan_digests = set(completed)
    if isinstance(active, Mapping):
        active_digest = active.get("plan_digest")
        if isinstance(active_digest, str):
            valid_plan_digests.add(active_digest)
    completed_actions = {
        str(item["plan_digest"]): str(item["child_action"])
        for item in results
        if isinstance(item, Mapping)
    }
    seen_ids: set[str] = set()
    seen_plan_phases: set[tuple[str, str]] = set()
    phases_by_plan: dict[str, list[str]] = {}
    for raw_receipt in raw_receipts:
        try:
            receipt = _validate_notification_receipt(
                raw_receipt,
                lifecycle_operation_id=operation_id,
                project_root=str(context.project_root),
                valid_plan_digests=valid_plan_digests,
            )
        except LifecycleError as exc:
            raise LifecycleError(f"lifecycle journal notification receipt is invalid: {path}: {exc}") from exc
        notification_id = str(receipt["notification_id"])
        plan_digest = str(receipt["accepted_plan_digest"])
        receipt_phase = str(receipt["phase"])
        plan_phase = (plan_digest, receipt_phase)
        if notification_id in seen_ids or plan_phase in seen_plan_phases:
            raise LifecycleError(f"lifecycle journal notification receipt identity is duplicated: {path}")
        seen_ids.add(notification_id)
        seen_plan_phases.add(plan_phase)
        phases_by_plan.setdefault(plan_digest, []).append(receipt_phase)
        payload = receipt["payload"]
        assert isinstance(payload, Mapping)
        facts = payload["facts"]
        assert isinstance(facts, Mapping)
        if number is not None and facts.get("iteration") != number:
            raise LifecycleError(f"lifecycle journal notification receipt iteration is invalid: {path}")
        if plan_digest in completed_actions and completed_actions[plan_digest] != "activate-workspace":
            raise LifecycleError(f"lifecycle journal notification receipt child history is invalid: {path}")
        if isinstance(active, Mapping) and active.get("plan_digest") == plan_digest:
            active_child = active.get("accepted_child")
            if not isinstance(active_child, Mapping) or active_child.get("action") != "activate-workspace":
                raise LifecycleError(f"lifecycle journal notification receipt active child is invalid: {path}")
            if payload != _workspace_interaction(active_child, receipt_phase):
                raise LifecycleError(f"lifecycle journal notification receipt differs from its active plan: {path}")
    for plan_digest, receipt_phases in phases_by_plan.items():
        if receipt_phases not in (["before"], ["before", "after"]):
            raise LifecycleError(f"lifecycle journal notification receipt order is invalid: {path}")
        if plan_digest in completed_actions and receipt_phases != ["before", "after"]:
            raise LifecycleError(f"completed lifecycle notification history is incomplete: {path}")
    return value


def _base_time(context: workspace.RepositoryContext, commit: str) -> datetime:
    result = workspace.run_git(context, context.project_root, ["show", "-s", "--format=%cI", commit])
    try:
        value = datetime.fromisoformat(workspace.decode_stdout(result))
    except ValueError as exc:
        raise LifecycleError("Git commit has no parseable timezone-aware timestamp") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise LifecycleError("Git commit timestamp lacks a timezone")
    return value


def _progress_newline(content: bytes) -> bytes:
    crlf = content.count(b"\r\n")
    lf = content.count(b"\n") - crlf
    return b"\r\n" if crlf > lf else b"\n"


def _append_progress_event(content: bytes, event_bytes: bytes, newline: bytes) -> bytes:
    if newline not in {b"\n", b"\r\n"}:
        raise LifecycleError("progress newline identity is invalid")
    if not content:
        return event_bytes
    if content.endswith(newline + newline):
        separator = b""
    elif content.endswith(newline):
        separator = newline
    else:
        separator = newline + newline
    return content + separator + event_bytes


def _committed_progress_snapshot(
    context: workspace.RepositoryContext,
    commit: str,
) -> bytes:
    result = workspace.run_git(
        context,
        context.project_root,
        ["show", f"{commit}:{progress.PROGRESS_PATH}"],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise LifecycleError(
            "implementation start has no readable committed Harness progress history"
            + (f": {detail}" if detail else "")
        )
    content = result.stdout
    if len(content) > MAX_PROGRESS_BYTES:
        raise LifecycleError("implementation-start progress history exceeds the safe size")
    payload = content[3:] if content.startswith(b"\xef\xbb\xbf") else content
    if not payload.startswith(
        (
            b"<!-- managed-by: harness-lite v1 -->",
            b"<!-- managed-by: init-project-harness v1 -->",
        )
    ):
        raise LifecycleError("implementation-start progress history is not Harness-managed")
    parsed = progress.parse_progress_events(
        content,
        source=f"{commit}:{progress.PROGRESS_PATH}",
    )
    if parsed.blockers:
        raise LifecycleError(
            "implementation-start progress history is invalid: "
            + "; ".join(f"{item.code}: {item.message}" for item in parsed.blockers)
        )
    return content


def _activation_session_id(operation_id: str, occurred_at: datetime) -> str:
    """Derive a display/session label independently from the immutable EV ID."""

    slot = int(hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:8], 16) % 99 + 1
    return f"S-{occurred_at.strftime('%Y%m%d')}-{slot:02d}"


def _activation_progress_binding(
    context: workspace.RepositoryContext,
    *,
    iteration: str,
    topology: str,
    target: Path,
    operation_id: str,
    allocation_ref: str,
    allocation_commit: str,
    implementation_ref: str,
    implementation_commit: str,
    source_ref: str,
) -> dict[str, object]:
    """Pre-bind one exact activation event before a linked worktree exists."""

    if topology not in {"local", "worktree"}:
        raise LifecycleError("activation progress topology must be local or worktree")
    source_progress = _committed_progress_snapshot(context, implementation_commit)
    parsed = progress.parse_progress_events(
        source_progress,
        source=f"{implementation_commit}:{progress.PROGRESS_PATH}",
    )
    causal_parent = parsed.events[-1].identity if parsed.events else None
    occurred_at = _base_time(context, implementation_commit)
    evidence_refs = (
        f"topology:{topology}",
        f"allocation-base:{allocation_ref}@{allocation_commit}",
        f"implementation-start:{implementation_ref}@{implementation_commit}",
        f"source:{source_ref}@{implementation_commit}",
    )
    event = progress.workspace_event(
        workspace_state=f"activated-{topology}",
        session_id=_activation_session_id(operation_id, occurred_at),
        iteration=iteration,
        occurred_at=occurred_at.isoformat(timespec="seconds"),
        source_ref=source_ref,
        source_commit=implementation_commit,
        operation_id=operation_id,
        causal_parent=causal_parent,
        evidence_refs=evidence_refs,
        summary=(
            f"Activated PRD-{iteration} as {topology}; immutable allocation base "
            f"{allocation_ref}@{allocation_commit}; exact implementation start "
            f"{implementation_ref}@{implementation_commit}; source {source_ref}@{implementation_commit}."
        ),
    )
    try:
        source_blob_oid = progress.source_progress_blob_oid(
            context.project_root,
            implementation_commit,
            progress.PROGRESS_PATH,
        )
        checkout_policy = progress.resolve_progress_checkout_policy(
            context.project_root,
            implementation_commit,
            progress.PROGRESS_PATH,
        )
        checkout_variants = progress.checkout_progress_variants(
            context.project_root,
            progress.PROGRESS_PATH,
            source_blob_oid,
            source_progress,
            checkout_policy,
        )
    except progress.ProgressError as exc:
        raise LifecycleError(f"activation progress checkout policy is unsafe: {exc}") from exc
    allowed_variants: dict[str, dict[str, object]] = {}
    for style, before in sorted(checkout_variants.items()):
        newline = b"\r\n" if style == "crlf" else b"\n"
        event_bytes = event.render(newline)
        after = _append_progress_event(before, event_bytes, newline)
        if len(after) > MAX_PROGRESS_BYTES:
            raise LifecycleError("activation progress append would exceed the safe size")
        allowed_variants[style] = {
            "newline": "CRLF" if newline == b"\r\n" else "LF",
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "event_sha256": hashlib.sha256(event_bytes).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
        }
    source_style = "crlf" if _progress_newline(source_progress) == b"\r\n" else "lf"
    primary = allowed_variants.get(source_style)
    if primary is None:
        raise LifecycleError("committed progress bytes are absent from Git-proven checkout variants")
    return {
        "schema_version": ACTIVATION_PROGRESS_SCHEMA,
        "progress_path": progress.PROGRESS_PATH,
        "target_project_root": str(target.resolve(strict=False)),
        "topology": topology,
        "allocation_base": {"ref": allocation_ref, "commit": allocation_commit},
        "implementation_start": {
            "ref": implementation_ref,
            "commit": implementation_commit,
        },
        "source": {"ref": source_ref, "commit": implementation_commit},
        "event": event.as_dict(),
        "causal_parent": causal_parent,
        "source_progress_sha256": hashlib.sha256(source_progress).hexdigest(),
        "source_progress_blob_oid": source_blob_oid,
        "checkout_policy": checkout_policy.as_dict(),
        "allowed_variants": allowed_variants,
        "expected_before_sha256": primary["before_sha256"],
        "event_sha256": primary["event_sha256"],
        "expected_after_sha256": primary["after_sha256"],
        "newline": primary["newline"],
        "exclusions": ["no commit", "no push", "no ref update"],
        "pushed": False,
    }


def _plan_payload(
    *,
    context: workspace.RepositoryContext,
    operation_id: str,
    request: Mapping[str, object],
    title: str,
    iteration: str | None,
    route: Mapping[str, object],
    child: Mapping[str, object] | None,
    actions: Sequence[Mapping[str, object]],
    expected_topology: str,
    blockers: Sequence[str],
    next_gate: str,
) -> dict[str, object]:
    action_values = [dict(item) for item in actions]
    body = {
        "schema_version": PLAN_SCHEMA,
        "command": "plan-start",
        "action_level": "notify" if any(item.get("action_level") == "notify" for item in action_values) else "silent",
        "pushed": False,
        "project_root": str(context.project_root),
        "operation_id": operation_id,
        "request_digest": digest(request),
        "title": title,
        "iteration": iteration,
        "route": dict(route),
        "authority_gates": {
            "inferred_authorization": False,
            "current": route.get("axes", {}).get("authorization_gate") if isinstance(route.get("axes"), Mapping) else None,
        },
        "expected_topology": expected_topology,
        "actions": action_values,
        "accepted_child": dict(child) if child else None,
        "phase": "blocked" if blockers else "planned" if child else "ready",
        "blocking_reasons": sorted(set(blockers)),
        "next_gate": next_gate,
        "exclusions": list(EXCLUSIONS),
    }
    body["plan_digest"] = digest(body)
    return body


def _workspace_target(context: workspace.RepositoryContext, number: str, request: Mapping[str, object]) -> Path:
    supplied = request.get("worktree_path")
    if isinstance(supplied, str):
        path = Path(supplied).expanduser()
        if not path.is_absolute():
            raise LifecycleError("request worktree_path must be absolute")
        return path.resolve(strict=False)
    return (context.project_root.parent / f"{context.project_root.name}.prd-{number}").resolve(strict=False)


def _committed_authority_blockers(
    context: workspace.RepositoryContext,
    number: str,
    authority: Mapping[str, object],
    *,
    base_ref: str,
) -> list[str]:
    """Bind implementation authority to exact bytes committed on canonical main.

    The coordinator intentionally parses live PRD/SPEC files so drafting remains
    responsive.  Starting an implementation is a stronger boundary: status text
    or caller booleans are insufficient, and an uncommitted approval must never
    acquire a writer lease.  This facade therefore proves that the exact PRD,
    SPEC, and global principle bytes it inspected exist at the canonical main
    commit and that the immutable allocation still uses that principle baseline.
    """

    blockers: list[str] = []
    if not authority.get("prd_approved"):
        blockers.append("prd-not-approved")
    if not authority.get("spec_approved"):
        blockers.append("spec-not-approved")
    if not authority.get("implementation_authorized"):
        blockers.append("implementation-not-authorized")
    raw_authority_blockers = authority.get("blockers")
    if not isinstance(raw_authority_blockers, (list, tuple)):
        blockers.append("authority-blockers-invalid")
    else:
        blockers.extend(f"authority:{item}" for item in raw_authority_blockers)

    commit = authority.get("governance_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        blockers.append("governance-commit-invalid")
        return blockers
    main = workspace.run_git(
        context,
        context.project_root,
        ["rev-parse", "--verify", "refs/heads/main^{commit}"],
        check=False,
    )
    if main.returncode != 0 or workspace.decode_stdout(main) != commit:
        blockers.append("canonical-main-changed")
        return blockers

    paths = (
        f"harness/iterations/{number}/prd-{number}.md",
        f"harness/iterations/{number}/spec-{number}.md",
        "harness/principle.md",
    )
    for relative in paths:
        live_path = context.project_root / relative
        if not live_path.is_file():
            blockers.append(f"authority-live-file-unreadable:{relative}")
            continue
        committed_blob = workspace.run_git(
            context,
            context.project_root,
            ["rev-parse", "--verify", f"{commit}:{relative}"],
            check=False,
        )
        live_blob = workspace.run_git(
            context,
            context.project_root,
            ["hash-object", f"--path={relative}", "--", relative],
            check=False,
        )
        if committed_blob.returncode != 0:
            blockers.append(f"authority-not-committed:{relative}")
        elif live_blob.returncode != 0:
            blockers.append(f"authority-live-file-unreadable:{relative}")
        elif workspace.decode_stdout(committed_blob) != workspace.decode_stdout(live_blob):
            blockers.append(f"authority-live-commit-mismatch:{relative}")

    principle_gate = _principle_gate_projection(context, number)
    if principle_gate is None:
        blockers.append("principle-audit-gate-unavailable")
    else:
        blockers.extend(_principle_gate_blockers(principle_gate))

    validation = core.collect_validation(context.project_root)
    blockers.extend(f"governance:{issue.code}:{issue.path}" for issue in validation.errors)
    return blockers


def _route_dependency_bindings(route: Mapping[str, object]) -> tuple[dict[str, str], ...]:
    """Extract the coordinator-authenticated ordered dependency identity.

    The coordinator deliberately publishes this evidence inside its exact
    workspace-plan step.  The lifecycle must not independently choose a
    candidate generation or trust dependency data supplied by the request.
    """

    coordinator_plan = route.get("coordinator")
    if not isinstance(coordinator_plan, Mapping):
        raise LifecycleError("stacked workspace route lacks its coordinator plan")
    steps = coordinator_plan.get("planned_steps")
    if not isinstance(steps, (list, tuple)):
        raise LifecycleError("stacked workspace route lacks coordinator steps")
    workspace_steps = [
        item
        for item in steps
        if isinstance(item, Mapping) and item.get("step") == "workspace-plan"
    ]
    if len(workspace_steps) != 1:
        raise LifecycleError("stacked workspace route lacks one exact workspace plan")
    step = workspace_steps[0]
    raw = step.get("dependency_bindings")
    expected_digest = step.get("dependency_bindings_digest")
    if not isinstance(raw, list) or not raw:
        raise LifecycleError("stacked workspace route lacks stable dependency bindings")
    try:
        normalized = workspace.normalize_dependency_bindings(raw)
    except workspace.WorkspaceError as exc:
        raise LifecycleError(f"stacked dependency bindings are invalid: {exc}") from exc
    actual_digest = workspace.dependency_bindings_digest(normalized)
    if expected_digest != actual_digest:
        raise LifecycleError("stacked dependency binding digest differs from coordinator evidence")
    implementation_ref = step.get("implementation_ref")
    implementation_commit = step.get("implementation_commit")
    if (
        implementation_ref != normalized[-1]["candidate_ref"]
        or implementation_commit != normalized[-1]["candidate_commit"]
    ):
        raise LifecycleError("stacked implementation start differs from its last dependency candidate")
    return tuple(dict(item) for item in normalized)


def plan_start(
    project_root: str | Path,
    request: Mapping[str, object],
    *,
    title: str,
    operation_id: str,
) -> dict[str, object]:
    """Return one exact, zero-write next-step plan."""

    context = _context(project_root)
    normalized = validate_request(dict(request))
    operation = _operation(operation_id)
    title_value = _single_line(title, "title")
    existing = _load_lifecycle_journal(context, operation)
    request_digest = digest(normalized)
    if existing is not None:
        if existing["request_digest"] != request_digest or existing["title"] != title_value:
            raise LifecycleError("lifecycle operation belongs to a different request or title")
        active = existing.get("active_plan")
        if existing.get("phase") == "APPLYING" and isinstance(active, Mapping):
            return dict(active)
    journal_number = str(existing["iteration"]) if existing and existing.get("iteration") else None
    requested_number = normalized.get("iteration")
    if journal_number is not None and requested_number not in {None, journal_number}:
        raise LifecycleError("lifecycle operation iteration differs from its reserved identity")
    number = journal_number or requested_number
    provisional_route = route_request(context.project_root, normalized, operation_id=_child_operation(operation, "route"))
    if bool(normalized["read_only"]):
        return _plan_payload(
            context=context,
            operation_id=operation,
            request=normalized,
            title=title_value,
            iteration=str(number) if number else None,
            route=provisional_route,
            child=None,
            actions=(),
            expected_topology="read-only",
            blockers=("read-only-request-cannot-start",),
            next_gate="use-status-or-route",
        )

    if not number:
        if provisional_route["axes"]["governance_path"] == "grill":  # type: ignore[index]
            return _plan_payload(
                context=context,
                operation_id=operation,
                request=normalized,
                title=title_value,
                iteration=None,
                route=provisional_route,
                child=None,
                actions=(),
                expected_topology=str(provisional_route["axes"]["execution_topology"]),  # type: ignore[index]
                blockers=tuple(str(item) for item in provisional_route["blocking_reasons"]),
                next_gate="resolve-request-ambiguities",
            )
        reserve_operation = _child_operation(operation, "reserve")
        try:
            child_plan = core.build_reserve_iteration_plan(
                context.project_root,
                context.git,
                title=title_value,
                operation_id=reserve_operation,
                base_ref=str(normalized["base_ref"]),
                governance_ref=str(normalized["governance_ref"]),
            )
        except core.HarnessError as exc:
            raise LifecycleError(str(exc)) from exc
        child = {
            "action": "reserve-iteration",
            "operation_id": child_plan.operation_id,
            "plan_digest": child_plan.plan_digest,
            "parameters": {
                "title": title_value,
                "base_ref": child_plan.base_branch,
                "governance_ref": child_plan.governance_ref,
                "observed_iteration": child_plan.observed_next_iteration,
            },
        }
        return _plan_payload(
            context=context,
            operation_id=operation,
            request=normalized,
            title=title_value,
            iteration=child_plan.observed_next_iteration,
            route=provisional_route,
            child=child,
            actions=(
                {"sequence": 1, "action": "reserve-iteration", "writes": True, "plan_digest": child_plan.plan_digest},
                {"sequence": 2, "action": "stop-and-replan", "writes": False, "reason": "bundle digest is not available before reservation"},
            ),
            expected_topology=str(provisional_route["axes"]["execution_topology"]),  # type: ignore[index]
            blockers=tuple(f"reserve:{reason.code}:{reason.message}" for reason in child_plan.blocking_reasons),
            next_gate="accept-reservation-plan" if not child_plan.blocking_reasons else "reconcile-reservation",
        )

    number = _iteration(number)
    core_snapshot = core.build_status_snapshot(context.project_root, context.git, all_worktrees=True)
    state = next((item for item in core_snapshot.iterations if item.number == number), None)
    if state is None or state.allocation_ref is None or state.base_ref is None or state.base_commit is None:
        return _plan_payload(
            context=context,
            operation_id=operation,
            request=normalized,
            title=title_value,
            iteration=number,
            route=provisional_route,
            child=None,
            actions=(),
            expected_topology=str(provisional_route["axes"]["execution_topology"]),  # type: ignore[index]
            blockers=(f"v2-reservation-missing:PRD-{number}",),
            next_gate="reserve-iteration",
        )
    if not state.bundle_present:
        bundle_operation = _child_operation(operation, f"bundle-{number}")
        planned_at = _base_time(context, state.base_commit)
        try:
            child_plan = bundle.plan_bundle(
                context.project_root,
                iteration=number,
                operation_id=bundle_operation,
                planned_at=planned_at,
            )
        except (bundle.BundleError, core.HarnessError) as exc:
            raise LifecycleError(str(exc)) from exc
        child = {
            "action": "create-v2-bundle",
            "operation_id": child_plan.operation_id,
            "plan_digest": child_plan.plan_digest,
            "parameters": {"iteration": number, "planned_at": child_plan.planned_at},
        }
        return _plan_payload(
            context=context,
            operation_id=operation,
            request=normalized,
            title=title_value,
            iteration=number,
            route=provisional_route,
            child=child,
            actions=(
                {"sequence": 1, "action": "create-v2-bundle", "writes": True, "plan_digest": child_plan.plan_digest},
                {"sequence": 2, "action": "stop-at-approval-gates", "writes": False},
            ),
            expected_topology=str(provisional_route["axes"]["execution_topology"]),  # type: ignore[index]
            blockers=tuple(str(item) for item in child_plan.blocking_reasons),
            next_gate="accept-bundle-plan" if not child_plan.blocking_reasons else "reconcile-bundle",
        )

    # Re-route now that the complete bundle is present; this is the only
    # authority source for implementation activation.
    route = route_request(context.project_root, {**normalized, "iteration": number}, operation_id=_child_operation(operation, "route-authority"))
    authority = route.get("authority")
    if not isinstance(authority, Mapping):
        blockers = ["iteration-authority-unavailable"]
    else:
        blockers = _committed_authority_blockers(
            context,
            number,
            authority,
            base_ref=state.base_ref,
        )
        blockers.extend(str(item) for item in route.get("blocking_reasons", []))
    if blockers:
        if any(item.startswith("principle:") for item in blockers):
            principle_gate = route.get("principle_gate")
            blocked_gate = (
                str(principle_gate.get("next_gate"))
                if isinstance(principle_gate, Mapping)
                else "reconcile-principle-audit-chain"
            )
        elif any(
            item.startswith(
                (
                    "authority-live-",
                    "authority-not-committed:",
                    "canonical-main-",
                    "governance:",
                    "governance-commit-",
                )
            )
            for item in blockers
        ):
            blocked_gate = "commit-or-reconcile-authority"
        else:
            blocked_gate = (
                str(route.get("next_gate"))
                if route.get("phase") == "blocked"
                else str(route["axes"]["authorization_gate"])  # type: ignore[index]
            )
        return _plan_payload(
            context=context,
            operation_id=operation,
            request=normalized,
            title=title_value,
            iteration=number,
            route=route,
            child=None,
            actions=({"sequence": 1, "action": "stop-before-implementation", "writes": False},),
            expected_topology=str(route["axes"]["execution_topology"]),  # type: ignore[index]
            blockers=tuple(blockers),
            next_gate=blocked_gate,
        )

    active = next((item for item in _active_leases(context) if item["iteration"] == number), None)
    if active is not None:
        return _plan_payload(
            context=context,
            operation_id=operation,
            request=normalized,
            title=title_value,
            iteration=number,
            route=route,
            child=None,
            actions=({"sequence": 1, "action": "writer-already-active", "writes": False},),
            expected_topology=str(active["execution_topology"]),
            blockers=(),
            next_gate="implementation-ready",
        )

    topology = str(route["axes"]["execution_topology"])
    dependency_bindings: tuple[dict[str, str], ...] = ()
    if topology == "local":
        workspace_topology = "local"
        branch_ref = str(normalized.get("branch_ref") or state.base_branch or "refs/heads/main")
        target = context.project_root
    elif topology == "independent-worktree":
        workspace_topology = "worktree"
        branch_ref = str(normalized.get("branch_ref") or f"refs/heads/harness/prd-{number}")
        target = _workspace_target(context, number, normalized)
    elif topology == "stacked-worktree":
        workspace_topology = "worktree"
        dependency_bindings = _route_dependency_bindings(route)
        branch_ref = str(normalized.get("branch_ref") or f"refs/heads/harness/prd-{number}")
        target = _workspace_target(context, number, normalized)
    else:
        return _plan_payload(
            context=context,
            operation_id=operation,
            request=normalized,
            title=title_value,
            iteration=number,
            route=route,
            child=None,
            actions=(),
            expected_topology=topology,
            blockers=(f"topology-requires-specialized-plan:{topology}",),
            next_gate="resolve-dependency-or-serialization",
        )
    workspace_operation = _child_operation(operation, f"workspace-{number}")
    try:
        child_plan = workspace.build_activation_plan(
            context.project_root,
            iteration=number,
            execution_topology=workspace_topology,
            base_ref=state.base_ref,
            branch_ref=branch_ref,
            worktree_path=target,
            owner=str(normalized["owner"]),
            lease_generation=1,
            dependency_bindings=dependency_bindings,
            operation_id=workspace_operation,
        )
    except workspace.WorkspaceError as exc:
        raise LifecycleError(str(exc)) from exc
    activation_base = child_plan.manifest.get("base")
    expected_implementation_ref = (
        dependency_bindings[-1]["candidate_ref"]
        if dependency_bindings
        else "refs/heads/main"
    )
    expected_implementation_commit = (
        dependency_bindings[-1]["candidate_commit"]
        if dependency_bindings
        else authority.get("governance_commit")
    )
    if (
        not isinstance(activation_base, Mapping)
        or activation_base.get("implementation_ref") != expected_implementation_ref
        or not isinstance(activation_base.get("implementation_commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", str(activation_base["implementation_commit"]))
        or activation_base.get("implementation_commit") != expected_implementation_commit
        or activation_base.get("dependency_bindings") != [dict(item) for item in dependency_bindings]
        or activation_base.get("dependency_bindings_digest")
        != workspace.dependency_bindings_digest(dependency_bindings)
    ):
        raise LifecycleError("workspace plan did not bind its exact approved implementation start")
    progress_binding = _activation_progress_binding(
        context,
        iteration=number,
        topology=workspace_topology,
        target=target,
        operation_id=workspace_operation,
        allocation_ref=state.base_ref,
        allocation_commit=state.base_commit,
        implementation_ref=str(activation_base["implementation_ref"]),
        implementation_commit=str(activation_base["implementation_commit"]),
        source_ref=branch_ref,
    )
    child = {
        "action": "activate-workspace",
        "operation_id": child_plan.operation_id,
        "plan_digest": child_plan.digest,
        "parameters": {
            "iteration": number,
            "execution_topology": workspace_topology,
            "base_ref": state.base_ref,
            "base_commit": state.base_commit,
            "implementation_ref": activation_base["implementation_ref"],
            "implementation_commit": activation_base["implementation_commit"],
            "dependency_bindings": [dict(item) for item in dependency_bindings],
            "dependency_bindings_digest": workspace.dependency_bindings_digest(dependency_bindings),
            "branch_ref": branch_ref,
            "worktree_path": str(target),
            "owner": normalized["owner"],
            "lease_generation": 1,
            "project_root": str(context.project_root),
            "authority": {
                "governance_commit": authority["governance_commit"],
                "governance_tree": authority["governance_tree"],
                "principle_sha256": authority["principle_sha256"],
            },
            "activation_progress": progress_binding,
        },
    }
    action: dict[str, object] = {
        "sequence": 1,
        "action": "activate-local" if workspace_topology == "local" else "create-worktree",
        "writes": True,
        "action_level": "silent" if workspace_topology == "local" else "notify",
        "plan_digest": child_plan.digest,
    }
    if workspace_topology == "worktree":
        action["notification"] = _workspace_interaction(child, "before")
    return _plan_payload(
        context=context,
        operation_id=operation,
        request=normalized,
        title=title_value,
        iteration=number,
        route=route,
        child=child,
        actions=(
            action,
            {
                "sequence": 2,
                "action": "append-workspace-activation-progress",
                "writes": True,
                "action_level": "silent",
                "event_id": progress_binding["event"]["event_id"],  # type: ignore[index]
                "expected_before_sha256": progress_binding["expected_before_sha256"],
                "expected_after_sha256": progress_binding["expected_after_sha256"],
                "commit_created": False,
                "pushed": False,
            },
        ),
        expected_topology=workspace_topology,
        blockers=tuple(f"workspace:{item.code}:{item.message}" for item in child_plan.blockers),
        next_gate="accept-workspace-plan" if not child_plan.blockers else "reconcile-workspace",
    )


def _initial_journal(plan: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": JOURNAL_SCHEMA,
        "operation_id": plan["operation_id"],
        "project_root": plan["project_root"],
        "request_digest": plan["request_digest"],
        "title": plan["title"],
        "phase": "APPLYING",
        "iteration": plan.get("iteration"),
        "active_plan": dict(plan),
        "completed_plans": [],
        "child_results": [],
        "notification_receipts": [],
        "last_error": None,
    }


def _resume_reservation(
    context: workspace.RepositoryContext,
    child: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    operation = str(child["operation_id"])
    expected_digest = str(child["plan_digest"])
    parameters = child["parameters"]
    if not isinstance(parameters, Mapping):
        raise LifecycleError("reservation child parameters are invalid")
    common = context.common_dir
    path = core.operation_journal_path(common, operation)
    if path.exists():
        record, _ = core.load_operation_journal(common, operation)
        plan = core.plan_from_operation_journal(context.project_root, common, record)
    else:
        plan = core.build_reserve_iteration_plan(
            context.project_root,
            context.git,
            title=str(parameters["title"]),
            operation_id=operation,
            base_ref=str(parameters["base_ref"]),
            governance_ref=str(parameters["governance_ref"]),
        )
    if plan.plan_digest != expected_digest:
        raise LifecycleError("reservation child plan changed after top-level acceptance")
    journal, created = core.reserve_iteration(plan, context.git, context.project_root)
    result = core.reservation_result(plan, journal, created_now=created)
    if journal.iteration is None:
        raise LifecycleError("reservation completed without an iteration identity")
    return result, journal.iteration


def _workspace_interaction(child: Mapping[str, object], phase: str) -> dict[str, object]:
    if child.get("action") != "activate-workspace":
        raise LifecycleError("workspace interaction child action is invalid")
    child_operation_id = child.get("operation_id")
    child_plan_digest = child.get("plan_digest")
    if not isinstance(child_operation_id, str):
        raise LifecycleError("workspace interaction child operation identity is invalid")
    _operation(child_operation_id)
    if not isinstance(child_plan_digest, str) or not DIGEST_RE.fullmatch(child_plan_digest):
        raise LifecycleError("workspace interaction child plan digest is invalid")
    parameters = child.get("parameters")
    if not isinstance(parameters, Mapping):
        raise LifecycleError("workspace child parameters are invalid")
    if phase not in {"before", "after"}:
        raise LifecycleError("workspace interaction phase must be before or after")
    topology = parameters.get("execution_topology")
    if topology not in {"local", "worktree"}:
        raise LifecycleError("workspace interaction topology is invalid")
    number = _iteration(parameters.get("iteration"))
    project_root = _single_line(parameters.get("project_root"), "workspace interaction project root", maximum=4096)
    worktree_path = _single_line(parameters.get("worktree_path"), "workspace interaction worktree path", maximum=4096)
    if not Path(project_root).is_absolute() or not Path(worktree_path).is_absolute():
        raise LifecycleError("workspace interaction paths must be absolute")
    base_ref = _single_line(parameters.get("base_ref"), "workspace interaction base ref", maximum=1024)
    implementation_ref = _single_line(
        parameters.get("implementation_ref"),
        "workspace interaction implementation ref",
        maximum=1024,
    )
    branch_ref = _single_line(parameters.get("branch_ref"), "workspace interaction branch ref", maximum=1024)
    if not all(value.startswith("refs/") for value in (base_ref, implementation_ref, branch_ref)):
        raise LifecycleError("workspace interaction refs must be full refs")
    base_commit = _single_line(parameters.get("base_commit"), "workspace interaction base commit", maximum=64)
    implementation_commit = _single_line(
        parameters.get("implementation_commit"),
        "workspace interaction implementation commit",
        maximum=64,
    )
    if not re.fullmatch(r"[0-9a-f]{40,64}", base_commit) or not re.fullmatch(
        r"[0-9a-f]{40,64}", implementation_commit
    ):
        raise LifecycleError("workspace interaction commits are invalid")
    action = "create-worktree" if topology == "worktree" else "activate-local"
    stacked = topology == "worktree" and implementation_ref != "refs/heads/main"
    facts = {
        "iteration": number,
        "operation_id": child_operation_id,
        "project_root": project_root,
        "base": {"ref": base_ref, "commit": base_commit},
        "implementation_start": {
            "ref": implementation_ref,
            "commit": implementation_commit,
        },
        "branch": {"ref": branch_ref, "will_create": topology == "worktree"},
        "worktree": {"path": worktree_path, "will_create": topology == "worktree"},
        "reason_code": (
            "stable-dependency-stacked-worktree"
            if stacked
            else "parallel-prd-lazy-worktree"
            if topology == "worktree"
            else "single-active-prd-local"
        ),
        "effect_on_existing_prds": {
            "strategy": "add-only" if topology == "worktree" else "stay-local",
            "moved": False,
            "committed": False,
            "stashed": False,
            "files_copied": False,
        },
        "git_effects": {
            "worktree_created": topology == "worktree" and phase == "after",
            "branch_created": topology == "worktree" and phase == "after",
            "commit_created": False,
            "main_advanced": False,
            "push_performed": False,
        },
        "remote": {"involved": False, "pushed": False, "force": False},
    }
    facts_digest = digest(facts)
    notification_id = "NT-" + hashlib.sha256(
        canonical_json(
            {
                "schema_version": NOTIFICATION_SCHEMA,
                "child_operation_id": child_operation_id,
                "child_plan_digest": child_plan_digest,
                "phase": phase,
                "facts_digest": facts_digest,
            }
        )
    ).hexdigest()[:32]
    payload = {
        "schema_version": NOTIFICATION_SCHEMA,
        "notification_id": notification_id,
        "child_operation_id": child_operation_id,
        "child_plan_digest": child_plan_digest,
        "action": action,
        "action_level": "notify" if topology == "worktree" else "silent",
        "phase": phase,
        "summary": (
            f"Harness {'will create' if phase == 'before' else 'created'} a "
            f"{'dependency-stacked' if stacked else 'parallel'} worktree for PRD-{facts['iteration']}"
            if topology == "worktree"
            else f"Harness {'will activate' if phase == 'before' else 'activated'} PRD-{facts['iteration']} locally"
        ),
        "facts": facts,
        "facts_digest": facts_digest,
        "requires_user_response": False,
    }
    if len(canonical_json(payload)) > MAX_NOTIFICATION_BYTES:
        raise LifecycleError("workspace interaction notification exceeds its safe size limit")
    return payload


def _json_object_copy(value: Mapping[str, object]) -> dict[str, object]:
    copied = json.loads(canonical_json(dict(value)).decode("utf-8"))
    if not isinstance(copied, dict):  # pragma: no cover - canonical input is a mapping
        raise LifecycleError("canonical JSON copy unexpectedly changed object type")
    return copied


def _callback_error_value(exc: BaseException) -> dict[str, str]:
    error_type = type(exc).__name__.strip() or "BaseException"
    error_type = "".join(char if ord(char) >= 32 else " " for char in error_type)[:200].strip()
    try:
        message = str(exc)
    except BaseException:  # pragma: no cover - hostile exception __str__
        message = "callback exception message was not printable"
    message = "".join(char if ord(char) >= 32 else " " for char in message)[:1000]
    return {"type": error_type or "BaseException", "message": message}


def _plan_notification_receipts(
    journal: Mapping[str, object],
    accepted_plan_digest: str,
) -> list[dict[str, object]]:
    raw_receipts = journal.get("notification_receipts")
    if not isinstance(raw_receipts, list):
        raise LifecycleError("lifecycle journal notification receipt history is invalid")
    return [
        _json_object_copy(item)
        for item in raw_receipts
        if isinstance(item, Mapping) and item.get("accepted_plan_digest") == accepted_plan_digest
    ]


def _notification_recovery_summary(
    receipts: Sequence[Mapping[str, object]],
    suppressed_recorded_ids: Sequence[str],
) -> dict[str, object]:
    return {
        "suppressed_recorded_ids": list(suppressed_recorded_ids),
        "callback_outcome_unknown_ids": [
            str(item["notification_id"])
            for item in receipts
            if item.get("callback_state") == "pending"
        ],
        "callback_failed_ids": [
            str(item["notification_id"])
            for item in receipts
            if item.get("callback_state") == "raised"
        ],
        "callback_grants_authority": False,
    }


def _record_workspace_notification(
    context: workspace.RepositoryContext,
    journal: dict[str, object],
    *,
    lifecycle_operation_id: str,
    accepted_plan_digest: str,
    child: Mapping[str, object],
    phase: str,
    notify: Notify | None,
    interaction_events: list[dict[str, object]],
) -> tuple[dict[str, object], bool]:
    """Record an immutable notification binding before observing its callback.

    A matching receipt is an at-most-once delivery key.  A retry never invokes
    the callback again, including when the durable state is ``pending`` because
    the process disappeared between invocation and outcome recording.  The
    receipt itself remains available to the caller/UI for deterministic replay;
    callback success is neither approval nor mutation authority.
    """

    payload = _validate_notification_payload(_workspace_interaction(child, phase))
    notification_id = str(payload["notification_id"])
    raw_receipts = journal.get("notification_receipts")
    if not isinstance(raw_receipts, list) or len(raw_receipts) > MAX_NOTIFICATION_RECEIPTS:
        raise LifecycleError("lifecycle journal notification receipt history is invalid")
    matches = [
        item
        for item in raw_receipts
        if isinstance(item, Mapping) and item.get("notification_id") == notification_id
    ]
    if len(matches) > 1:
        raise LifecycleError("lifecycle notification receipt identity is duplicated")
    if matches:
        existing = _validate_notification_receipt(
            matches[0],
            lifecycle_operation_id=lifecycle_operation_id,
            project_root=str(context.project_root),
            valid_plan_digests={accepted_plan_digest},
        )
        if (
            existing.get("accepted_plan_digest") != accepted_plan_digest
            or existing.get("payload") != payload
            or existing.get("phase") != phase
        ):
            raise LifecycleError("recorded lifecycle notification differs from the accepted plan")
        return existing, False
    if len(raw_receipts) >= MAX_NOTIFICATION_RECEIPTS:
        raise LifecycleError("lifecycle notification receipt limit has been reached")
    if any(
        isinstance(item, Mapping)
        and item.get("accepted_plan_digest") == accepted_plan_digest
        and item.get("phase") == phase
        for item in raw_receipts
    ):
        raise LifecycleError("accepted lifecycle plan already has a different notification identity for this phase")
    receipt: dict[str, object] = {
        "schema_version": NOTIFICATION_RECEIPT_SCHEMA,
        "notification_id": notification_id,
        "lifecycle_operation_id": lifecycle_operation_id,
        "accepted_plan_digest": accepted_plan_digest,
        "child_action": "activate-workspace",
        "child_operation_id": payload["child_operation_id"],
        "child_plan_digest": payload["child_plan_digest"],
        "phase": phase,
        "payload": _json_object_copy(payload),
        "payload_digest": digest(payload),
        "binding_digest": "",
        "callback_state": "pending" if notify is not None else "not-requested",
        "callback_error": None,
    }
    receipt["binding_digest"] = digest(_receipt_binding(receipt))
    _validate_notification_receipt(
        receipt,
        lifecycle_operation_id=lifecycle_operation_id,
        project_root=str(context.project_root),
        valid_plan_digests={accepted_plan_digest},
    )
    raw_receipts.append(receipt)
    _atomic_json(context, _journal_path(context, lifecycle_operation_id), journal)
    interaction_events.append(_json_object_copy(payload))
    if notify is None:
        return _json_object_copy(receipt), True
    try:
        # The callback receives a detached copy and cannot mutate the journal
        # payload or influence the accepted plan identity.
        notify(_json_object_copy(payload))
    except BaseException as exc:
        receipt["callback_state"] = "raised"
        receipt["callback_error"] = _callback_error_value(exc)
        _atomic_json(context, _journal_path(context, lifecycle_operation_id), journal)
        raise
    receipt["callback_state"] = "returned"
    receipt["callback_error"] = None
    _atomic_json(context, _journal_path(context, lifecycle_operation_id), journal)
    return _json_object_copy(receipt), True


def _revalidate_workspace_authority(
    context: workspace.RepositoryContext,
    parameters: Mapping[str, object],
) -> None:
    number = _iteration(parameters.get("iteration"))
    planned = parameters.get("authority")
    if not isinstance(planned, Mapping) or set(planned) != {
        "governance_commit",
        "governance_tree",
        "principle_sha256",
    }:
        raise LifecycleError("workspace child lacks its exact derived authority snapshot")
    try:
        current = coordinator.derive_iteration_authority(context.project_root, number)
    except (coordinator.CoordinatorError, core.HarnessError, workspace.WorkspaceError) as exc:
        raise LifecycleError(f"workspace authority cannot be revalidated: {exc}") from exc
    current_projection = {
        "governance_commit": current.governance_commit,
        "governance_tree": current.governance_tree,
        "principle_sha256": current.principle_sha256,
    }
    if dict(planned) != current_projection:
        raise LifecycleError("workspace authority changed after planning; create and accept a new plan")
    raw_bindings = parameters.get("dependency_bindings")
    binding_digest = parameters.get("dependency_bindings_digest")
    if not isinstance(raw_bindings, list):
        raise LifecycleError("workspace child lacks exact dependency bindings")
    try:
        dependency_bindings = workspace.normalize_dependency_bindings(raw_bindings)
    except workspace.WorkspaceError as exc:
        raise LifecycleError(f"workspace dependency bindings are invalid: {exc}") from exc
    if binding_digest != workspace.dependency_bindings_digest(dependency_bindings):
        raise LifecycleError("workspace dependency binding digest changed after planning")
    if tuple(item["iteration"] for item in dependency_bindings) != current.depends_on:
        raise LifecycleError("workspace dependency bindings differ from approved PRD authority")
    for binding in dependency_bindings:
        try:
            dependency = coordinator.derive_iteration_authority(
                context.project_root,
                binding["iteration"],
            )
        except (coordinator.CoordinatorError, core.HarnessError, workspace.WorkspaceError) as exc:
            raise LifecycleError(f"workspace dependency authority cannot be revalidated: {exc}") from exc
        if not dependency.stable_candidate_bindings or dict(
            dependency.stable_candidate_bindings[-1]
        ) != dict(binding):
            raise LifecycleError(
                f"workspace dependency PRD-{binding['iteration']} stable candidate changed after planning"
            )
    live_dependency_blockers = workspace.dependency_order_blockers(context, dependency_bindings)
    if live_dependency_blockers:
        raise LifecycleError(
            "workspace dependency baseline is no longer live: "
            + "; ".join(item.code for item in live_dependency_blockers)
        )
    expected_ref = dependency_bindings[-1]["candidate_ref"] if dependency_bindings else "refs/heads/main"
    expected_commit = (
        dependency_bindings[-1]["candidate_commit"]
        if dependency_bindings
        else current.governance_commit
    )
    if (
        parameters.get("implementation_ref") != expected_ref
        or parameters.get("implementation_commit") != expected_commit
    ):
        raise LifecycleError("workspace implementation start no longer matches approved authority")
    blockers = _committed_authority_blockers(
        context,
        number,
        current_projection | {
            "prd_approved": current.prd_approved,
            "spec_approved": current.spec_approved,
            "implementation_authorized": current.implementation_authorized,
            "blockers": current.blockers,
            "principle_sha256": current.principle_sha256,
        },
        base_ref=str(parameters.get("base_ref")),
    )
    if blockers:
        raise LifecycleError("workspace authority is no longer valid: " + "; ".join(sorted(set(blockers))))


ACTIVATION_PROGRESS_FIELDS = {
    "schema_version",
    "progress_path",
    "target_project_root",
    "topology",
    "allocation_base",
    "implementation_start",
    "source",
    "event",
    "causal_parent",
    "source_progress_sha256",
    "source_progress_blob_oid",
    "checkout_policy",
    "allowed_variants",
    "expected_before_sha256",
    "event_sha256",
    "expected_after_sha256",
    "newline",
    "exclusions",
    "pushed",
}


def _validated_activation_progress_binding(
    context: workspace.RepositoryContext,
    child: Mapping[str, object],
    parameters: Mapping[str, object],
) -> tuple[dict[str, object], progress.ProgressEventV2, Path]:
    raw = parameters.get("activation_progress")
    if not isinstance(raw, Mapping) or set(raw) != ACTIVATION_PROGRESS_FIELDS:
        raise LifecycleError("workspace child lacks its exact activation progress binding")
    target = Path(str(parameters.get("worktree_path"))).expanduser()
    if not target.is_absolute():
        raise LifecycleError("activation progress target worktree must be absolute")
    target = target.resolve(strict=False)
    expected = _activation_progress_binding(
        context,
        iteration=_iteration(parameters.get("iteration")),
        topology=str(parameters.get("execution_topology")),
        target=target,
        operation_id=str(child.get("operation_id")),
        allocation_ref=str(parameters.get("base_ref")),
        allocation_commit=str(parameters.get("base_commit")),
        implementation_ref=str(parameters.get("implementation_ref")),
        implementation_commit=str(parameters.get("implementation_commit")),
        source_ref=str(parameters.get("branch_ref")),
    )
    binding = dict(raw)
    if binding != expected:
        raise LifecycleError(
            "activation progress binding changed after planning; create and accept a new lifecycle plan"
        )
    try:
        event = progress.ProgressEventV2.from_dict(binding["event"])
    except progress.ProgressError as exc:
        raise LifecycleError(f"activation progress event is invalid: {exc}") from exc
    if event.event_id == event.session_id:
        raise LifecycleError("activation progress event ID must be independent from its session ID")
    return binding, event, target


def _apply_activation_progress(
    context: workspace.RepositoryContext,
    child: Mapping[str, object],
    parameters: Mapping[str, object],
    *,
    failpoint: Failpoint | None,
) -> dict[str, object]:
    binding, event, target = _validated_activation_progress_binding(context, child, parameters)
    progress_path = target / progress.PROGRESS_PATH
    if not progress_path.is_file():
        raise LifecycleError(
            f"activated workspace is missing its pinned progress file: {progress_path}"
        )
    try:
        current = progress_path.read_bytes()
    except OSError as exc:
        raise LifecycleError(f"cannot read activated progress file: {progress_path}: {exc}") from exc
    if len(current) > MAX_PROGRESS_BYTES:
        raise LifecycleError("activated progress history exceeds the safe size")
    current_sha = hashlib.sha256(current).hexdigest()
    variants = binding.get("allowed_variants")
    if not isinstance(variants, Mapping) or not variants:
        raise LifecycleError("activation progress has no Git-proven exact byte variants")
    selected: Mapping[str, object] | None = None
    for raw_variant in variants.values():
        if not isinstance(raw_variant, Mapping):
            raise LifecycleError("activation progress exact byte variant is invalid")
        if current_sha in {
            raw_variant.get("before_sha256"),
            raw_variant.get("after_sha256"),
        }:
            if selected is not None and dict(selected) != dict(raw_variant):
                raise LifecycleError("activation progress bytes match multiple ambiguous variants")
            selected = raw_variant
    if selected is None:
        raise LifecycleError(
            "activated progress bytes differ from the pre-bound implementation-start history; "
            "immutable history requires reconcile"
        )
    try:
        append_plan = progress.plan_progress_append(project_root=target, event=event)
    except progress.ProgressError as exc:
        raise LifecycleError(f"activation progress planning failed closed: {exc}") from exc
    manifest = append_plan.manifest
    mismatches: list[str] = []
    expected_manifest_values = {
        "project_root": str(target),
        "progress_path": progress.PROGRESS_PATH,
        "event_sha256": selected["event_sha256"],
        "source_progress_sha256": binding["source_progress_sha256"],
        "semantic_source_progress_sha256": selected["before_sha256"],
        "source_progress_blob_oid": binding["source_progress_blob_oid"],
        "checkout_policy": binding["checkout_policy"],
        "allowed_source_variants": {
            str(style): raw["before_sha256"]
            for style, raw in variants.items()
            if isinstance(raw, Mapping)
        },
        "source_ref_observed_commit": parameters.get("implementation_commit"),
        "before_sha256": selected["before_sha256"],
        "after_sha256": selected["after_sha256"],
        "newline": selected["newline"],
    }
    for field, expected in expected_manifest_values.items():
        if manifest.get(field) != expected:
            mismatches.append(f"{field}:expected={expected}:actual={manifest.get(field)}")
    if append_plan.event.as_dict() != binding["event"]:
        mismatches.append("event:pre-bound-event-differs")
    if mismatches:
        raise LifecycleError(
            "derived activation progress plan differs from the accepted pre-binding: "
            + "; ".join(mismatches)
        )

    progress_fault = None
    if failpoint is not None:
        progress_fault = lambda stage, _path: failpoint(f"progress:{stage}")
    try:
        result = progress.apply_progress_append(
            append_plan,
            accept_plan_digest=append_plan.plan_digest,
            fault_injector=progress_fault,
        )
    except progress.ProgressError as exc:
        raise LifecycleError(f"activation progress append failed closed: {exc}") from exc
    payload = result.as_dict()
    payload.update(
        {
            "prebound_schema_version": ACTIVATION_PROGRESS_SCHEMA,
            "session_id": event.session_id,
            "topology": parameters.get("execution_topology"),
            "allocation_base": binding["allocation_base"],
            "implementation_start": binding["implementation_start"],
            "source": binding["source"],
            "causal_parent": event.causal_parent,
            "evidence_refs": list(event.evidence_refs),
            "commit_created": False,
            "pushed": False,
        }
    )
    return payload


def start_lifecycle(
    project_root: str | Path,
    request: Mapping[str, object],
    *,
    title: str,
    operation_id: str,
    accepted_plan_digest: str,
    notify: Notify | None = None,
    failpoint: Failpoint | None = None,
) -> dict[str, object]:
    """Apply exactly one accepted child and durably advance the lifecycle."""

    context = _context(project_root)
    normalized = validate_request(dict(request))
    operation = _operation(operation_id)
    title_value = _single_line(title, "title")
    accepted = accepted_plan_digest.strip()
    if not DIGEST_RE.fullmatch(accepted):
        raise LifecycleError("accepted plan digest must contain 64 lowercase hexadecimal characters")
    with _operation_lock(context, operation):
        journal = _load_lifecycle_journal(context, operation)
        if journal is not None:
            if journal["request_digest"] != digest(normalized) or journal["title"] != title_value:
                raise LifecycleError("lifecycle operation belongs to a different request or title")
            completed = journal["completed_plans"]
            if accepted in completed:
                record = next(
                    (item for item in journal["child_results"] if item.get("plan_digest") == accepted),
                    None,
                )
                recorded_notifications = _plan_notification_receipts(journal, accepted)
                recorded_ids = [str(item["notification_id"]) for item in recorded_notifications]
                return {
                    "schema_version": RESULT_SCHEMA,
                    "command": "start",
                    "action_level": "silent",
                    "pushed": False,
                    "project_root": str(context.project_root),
                    "operation_id": operation,
                    "iteration": journal.get("iteration"),
                    "phase": "progressed",
                    "accepted_plan_digest": accepted,
                    "idempotent_replay": True,
                    "child_result": record.get("result") if isinstance(record, Mapping) else None,
                    "interaction_events": [],
                    "notification_receipts": recorded_notifications,
                    "notification_recovery": _notification_recovery_summary(
                        recorded_notifications,
                        recorded_ids,
                    ),
                    "next_gate": NEXT_GATE_AFTER_ACTION.get(
                        str(record.get("child_action")) if isinstance(record, Mapping) else "",
                        "plan-next-lifecycle-step",
                    ),
                    "exclusions": list(EXCLUSIONS),
                }
            active = journal.get("active_plan")
            if journal.get("phase") == "APPLYING" and isinstance(active, Mapping):
                plan = dict(active)
            else:
                plan = plan_start(context.project_root, normalized, title=title_value, operation_id=operation)
        else:
            plan = plan_start(context.project_root, normalized, title=title_value, operation_id=operation)
            journal = _initial_journal(plan)
        if plan.get("plan_digest") != accepted:
            raise LifecycleError("accepted plan digest differs from the durable lifecycle plan")
        child = plan.get("accepted_child")
        # A blocked/no-op start is zero-write.  It cannot become an
        # authorization receipt merely because its exact digest was supplied.
        if plan.get("phase") != "planned" or not isinstance(child, Mapping):
            return {
                "schema_version": RESULT_SCHEMA,
                "command": "start",
                "action_level": "silent",
                "pushed": False,
                "project_root": str(context.project_root),
                "operation_id": operation,
                "iteration": plan.get("iteration"),
                "phase": plan.get("phase"),
                "accepted_plan_digest": accepted,
                "idempotent_replay": False,
                "child_result": None,
                "interaction_events": [],
                "notification_receipts": [],
                "notification_recovery": _notification_recovery_summary([], []),
                "blocking_reasons": plan.get("blocking_reasons", []),
                "next_gate": plan.get("next_gate"),
                "exclusions": list(EXCLUSIONS),
            }
        action = child.get("action")
        events: list[dict[str, object]] = []
        suppressed_notification_ids: list[str] = []
        if action == "activate-workspace":
            parameters = child.get("parameters")
            if not isinstance(parameters, Mapping):
                raise LifecycleError("workspace child parameters are invalid")
            _revalidate_workspace_authority(context, parameters)
            # The accepted top-level digest must already bind the exact EV
            # payload and committed progress bytes.  For a linked worktree the
            # target does not exist yet, so this validation deliberately reads
            # only the pinned implementation commit.
            _validated_activation_progress_binding(context, child, parameters)
        # Persist only after the exact accepted digest has been checked.  An
        # arbitrary or stale digest must remain a zero-write failed request.
        if journal.get("phase") != "APPLYING" or journal.get("active_plan") != plan:
            journal["phase"] = "APPLYING"
            journal["active_plan"] = dict(plan)
            journal["last_error"] = None
        _atomic_json(context, _journal_path(context, operation), journal)
        try:
            if action == "activate-workspace":
                parameters = child.get("parameters")
                assert isinstance(parameters, Mapping)
                if parameters.get("execution_topology") == "worktree":
                    _, created = _record_workspace_notification(
                        context,
                        journal,
                        lifecycle_operation_id=operation,
                        accepted_plan_digest=accepted,
                        child=child,
                        phase="before",
                        notify=notify,
                        interaction_events=events,
                    )
                    if not created:
                        suppressed_notification_ids.append(
                            str(_workspace_interaction(child, "before")["notification_id"])
                        )
            if action == "reserve-iteration":
                child_result, number = _resume_reservation(context, child)
                journal["iteration"] = number
            elif action == "create-v2-bundle":
                parameters = child.get("parameters")
                if not isinstance(parameters, Mapping):
                    raise LifecycleError("bundle child parameters are invalid")
                child_result = bundle.apply_bundle(
                    context.project_root,
                    iteration=str(parameters["iteration"]),
                    operation_id=str(child["operation_id"]),
                    accepted_plan_digest=str(child["plan_digest"]),
                    planned_at=datetime.fromisoformat(str(parameters["planned_at"])),
                )
                journal["iteration"] = str(parameters["iteration"])
            elif action == "activate-workspace":
                parameters = child.get("parameters")
                if not isinstance(parameters, Mapping):
                    raise LifecycleError("workspace child parameters are invalid")
                enriched = dict(child)
                enriched["parameters"] = {**parameters, "project_root": str(context.project_root)}
                child_result = workspace.apply_activation(
                    context.project_root,
                    iteration=str(parameters["iteration"]),
                    execution_topology=str(parameters["execution_topology"]),
                    base_ref=str(parameters["base_ref"]),
                    branch_ref=str(parameters["branch_ref"]),
                    worktree_path=str(parameters["worktree_path"]),
                    owner=str(parameters["owner"]),
                    lease_generation=int(parameters["lease_generation"]),
                    operation_id=str(child["operation_id"]),
                    accepted_plan_digest=str(child["plan_digest"]),
                    dependency_bindings=parameters.get("dependency_bindings", ()),
                )
                if child_result.get("phase") != "succeeded":
                    raise LifecycleError("workspace child did not succeed: " + json.dumps(child_result.get("blocking_reasons", []), ensure_ascii=False))
                if parameters.get("execution_topology") == "worktree":
                    after_receipt, created = _record_workspace_notification(
                        context,
                        journal,
                        lifecycle_operation_id=operation,
                        accepted_plan_digest=accepted,
                        child=enriched,
                        phase="after",
                        notify=notify,
                        interaction_events=events,
                    )
                    if not created:
                        suppressed_notification_ids.append(str(after_receipt["notification_id"]))
                if failpoint is not None:
                    failpoint("after-workspace-before-progress")
                progress_result = _apply_activation_progress(
                    context,
                    child,
                    parameters,
                    failpoint=failpoint,
                )
                child_result = {**child_result, "progress_child": progress_result}
                if failpoint is not None:
                    failpoint("after-progress-before-lifecycle-journal")
                journal["iteration"] = str(parameters["iteration"])
            else:
                raise LifecycleError(f"unsupported lifecycle child action: {action}")
            if failpoint is not None:
                failpoint("after-child-before-journal")
        except Exception as exc:
            journal["last_error"] = str(exc)[:1000]
            _atomic_json(context, _journal_path(context, operation), journal)
            raise
        completed = journal["completed_plans"]
        assert isinstance(completed, list)
        completed.append(accepted)
        results = journal["child_results"]
        assert isinstance(results, list)
        results.append({"plan_digest": accepted, "child_action": action, "result": child_result})
        journal["phase"] = "STEPPED"
        journal["active_plan"] = None
        journal["last_error"] = None
        _atomic_json(context, _journal_path(context, operation), journal)
        recorded_notifications = _plan_notification_receipts(journal, accepted)
        return {
            "schema_version": RESULT_SCHEMA,
            "command": "start",
            "action_level": "notify" if events else "silent",
            "pushed": False,
            "project_root": str(context.project_root),
            "operation_id": operation,
            "iteration": journal.get("iteration"),
            "phase": "progressed",
            "accepted_plan_digest": accepted,
            "idempotent_replay": False,
            "child_result": child_result,
            "interaction_events": events,
            "notification_receipts": recorded_notifications,
            "notification_recovery": _notification_recovery_summary(
                recorded_notifications,
                suppressed_notification_ids,
            ),
            "next_gate": NEXT_GATE_AFTER_ACTION.get(str(action), "plan-next-lifecycle-step"),
            "exclusions": list(EXCLUSIONS),
        }


def plan_lifecycle_stage(
    project_root: str | Path,
    *,
    stage: str,
    artifact: object,
    lifecycle_operation_id: str,
    confirmation_token: object | None = None,
    authorization_id: str | None = None,
) -> LifecycleStagePlan:
    """Dispatch one zero-write post-implementation stage plan."""

    expected_type = _stage_artifact_type(stage)
    if not isinstance(artifact, expected_type):
        raise LifecycleError(f"{stage} artifact has an unsupported public type")
    context = _context(project_root)
    artifact_root = getattr(artifact, "project_root", None)
    if artifact_root is None and hasattr(artifact, "registration_plan"):
        artifact_root = getattr(artifact.registration_plan, "project_root", None)
    if isinstance(artifact_root, str) and os.path.normcase(str(Path(artifact_root).resolve())) != os.path.normcase(str(context.project_root)):
        raise LifecycleError("lifecycle stage artifact belongs to another project root")
    dispatch = {
        "candidate-preverify": lambda: plan_candidate_preverification_stage(artifact, lifecycle_operation_id=lifecycle_operation_id),
        "candidate-register": lambda: plan_candidate_registration_stage(artifact, lifecycle_operation_id=lifecycle_operation_id),
        "integration-commit": lambda: plan_integration_commit_stage(artifact, lifecycle_operation_id=lifecycle_operation_id),
        "integrated-evidence-register": lambda: plan_integrated_evidence_stage(
            artifact,
            lifecycle_operation_id=lifecycle_operation_id,
            commit_confirmation_token=confirmation_token,
        ),
        "final-acceptance-register": lambda: plan_final_acceptance_stage(
            artifact,
            lifecycle_operation_id=lifecycle_operation_id,
            main_confirmation_token=confirmation_token,
            authorization_id=authorization_id or "",
        ),
        "integration-cleanup": lambda: plan_integration_cleanup_stage(artifact, lifecycle_operation_id=lifecycle_operation_id),
    }
    return dispatch[stage]()


def apply_lifecycle_stage(
    plan: LifecycleStagePlan,
    artifact: object,
    *,
    accepted_plan_digest: str,
    confirmation_token: object | None = None,
    authorization_id: str | None = None,
    notify: Callable[[object], None] | None = None,
    failpoint: Failpoint | None = None,
) -> LifecycleStageResult:
    """Dispatch one exact stage apply without inferring any confirmation."""

    _validate_stage_plan(plan)
    expected_type = _stage_artifact_type(plan.stage)
    if not isinstance(artifact, expected_type):
        raise LifecycleError(f"{plan.stage} artifact has an unsupported public type")
    dispatch = {
        "candidate-preverify": lambda: apply_candidate_preverification_stage(
            plan, artifact, accepted_plan_digest=accepted_plan_digest, failpoint=failpoint
        ),
        "candidate-register": lambda: apply_candidate_registration_stage(
            plan,
            artifact,
            accepted_plan_digest=accepted_plan_digest,
            confirmation_token=confirmation_token,
            failpoint=failpoint,
        ),
        "integration-commit": lambda: apply_integration_commit_stage(
            plan,
            artifact,
            accepted_plan_digest=accepted_plan_digest,
            confirmation_token=confirmation_token,
            failpoint=failpoint,
        ),
        "integrated-evidence-register": lambda: apply_integrated_evidence_stage(
            plan,
            artifact,
            accepted_plan_digest=accepted_plan_digest,
            commit_confirmation_token=confirmation_token,
            failpoint=failpoint,
        ),
        "final-acceptance-register": lambda: apply_final_acceptance_stage(
            plan,
            artifact,
            accepted_plan_digest=accepted_plan_digest,
            main_confirmation_token=confirmation_token,
            authorization_id=authorization_id or "",
            failpoint=failpoint,
        ),
        "integration-cleanup": lambda: apply_integration_cleanup_stage(
            plan,
            artifact,
            accepted_plan_digest=accepted_plan_digest,
            notify=notify,
            failpoint=failpoint,
        ),
    }
    return dispatch[plan.stage]()


def _print(value: Mapping[str, object], *, as_json: bool) -> None:
    if as_json:
        print(canonical_json(dict(value)).decode("utf-8"))
    else:
        print(f"{str(value.get('command', 'lifecycle')).upper()} {value.get('phase', 'unknown')}")
        print(f"PROJECT_ROOT {value.get('project_root', '(unknown)')}")
        if value.get("operation_id"):
            print(f"OPERATION {value['operation_id']}")
        if value.get("plan_digest"):
            print(f"PLAN_DIGEST {value['plan_digest']}")
        print(f"NEXT_GATE {value.get('next_gate', '(unknown)')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--project-root", required=True)
    status.add_argument("--json", action="store_true")
    route = sub.add_parser("route")
    route.add_argument("--project-root", required=True)
    route.add_argument("--request", required=True)
    route.add_argument("--operation-id")
    route.add_argument("--json", action="store_true")
    plan = sub.add_parser("plan-start")
    plan.add_argument("--project-root", required=True)
    plan.add_argument("--request", required=True)
    plan.add_argument("--title", required=True)
    plan.add_argument("--operation-id", required=True)
    plan.add_argument("--json", action="store_true")
    start = sub.add_parser("start")
    start.add_argument("--project-root", required=True)
    start.add_argument("--request", required=True)
    start.add_argument("--title", required=True)
    start.add_argument("--operation-id", required=True)
    start.add_argument("--accept-plan-digest", required=True)
    start.add_argument("--json", action="store_true")
    stage_plan = sub.add_parser("plan-stage")
    stage_plan.add_argument("--project-root", required=True)
    stage_plan.add_argument("--stage", required=True, choices=GENERIC_STAGE_ORDER)
    stage_plan.add_argument("--operation-id", required=True)
    stage_plan.add_argument("--artifact", required=True, help="Exact public artifact JSON; never a private journal")
    stage_plan.add_argument("--token", help="Exact confirmation-token JSON when this planning boundary requires it")
    stage_plan.add_argument("--authorization-id")
    stage_plan.add_argument("--json", action="store_true")
    stage_apply = sub.add_parser("apply-stage")
    stage_apply.add_argument("--plan", required=True, help="Exact LifecycleStagePlan JSON")
    stage_apply.add_argument("--artifact", required=True, help="Same exact public artifact JSON used for planning")
    stage_apply.add_argument("--accept-plan-digest", required=True)
    stage_apply.add_argument("--token", help="Exact confirmation-token JSON; never inferred")
    stage_apply.add_argument("--authorization-id")
    stage_apply.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            payload = lifecycle_status(args.project_root)
        elif args.command == "plan-stage":
            artifact_type = _stage_artifact_type(args.stage)
            artifact = _decode_public_artifact(args.artifact, artifact_type)
            token = _load_confirmation_token(args.token) if args.token else None
            plan = plan_lifecycle_stage(
                args.project_root,
                stage=args.stage,
                artifact=artifact,
                lifecycle_operation_id=args.operation_id,
                confirmation_token=token,
                authorization_id=args.authorization_id,
            )
            payload = {"command": "plan-stage", "phase": "planned" if plan.ready else "blocked", **plan.as_dict()}
        elif args.command == "apply-stage":
            plan = _decode_public_artifact(args.plan, LifecycleStagePlan)
            assert isinstance(plan, LifecycleStagePlan)
            artifact = _decode_public_artifact(args.artifact, _stage_artifact_type(plan.stage))
            token_stages = {
                "candidate-register",
                "integration-commit",
                "integrated-evidence-register",
                "final-acceptance-register",
            }
            token = _load_confirmation_token(args.token) if plan.stage in token_stages else None
            interactions: list[dict[str, object]] = []
            result = apply_lifecycle_stage(
                plan,
                artifact,
                accepted_plan_digest=args.accept_plan_digest,
                confirmation_token=token,
                authorization_id=args.authorization_id,
                notify=lambda item: interactions.append(_public_object_snapshot(item)),
            )
            payload = {"command": "apply-stage", "phase": "progressed", **result.as_dict(), "interaction_events": interactions}
        else:
            request = load_request(args.request)
            if args.command == "route":
                payload = route_request(args.project_root, request, operation_id=args.operation_id)
            elif args.command == "plan-start":
                payload = plan_start(
                    args.project_root,
                    request,
                    title=args.title,
                    operation_id=args.operation_id,
                )
            else:
                payload = start_lifecycle(
                    args.project_root,
                    request,
                    title=args.title,
                    operation_id=args.operation_id,
                    accepted_plan_digest=args.accept_plan_digest,
                )
    except Exception as exc:
        payload = {
            "schema_version": RESULT_SCHEMA,
            "command": args.command,
            "action_level": "silent",
            "pushed": False,
            "phase": "error",
            "blocking_reasons": [str(exc)],
            "next_gate": "fix-input-or-reconcile",
            "exclusions": list(EXCLUSIONS),
        }
    _print(payload, as_json=bool(args.json))
    return 0 if payload.get("phase") in {"ready", "routed", "planned", "progressed"} else 2


__all__ = [
    "LifecycleError",
    "REQUEST_SCHEMA",
    "STATUS_SCHEMA",
    "ROUTE_SCHEMA",
    "PLAN_SCHEMA",
    "RESULT_SCHEMA",
    "STAGE_PLAN_SCHEMA",
    "STAGE_RESULT_SCHEMA",
    "STAGE_STATUS_SCHEMA",
    "LifecycleStagePlan",
    "LifecycleStageResult",
    "OrderedIntegrationPreparationPlan",
    "ORDERED_PREPARATION_SCHEMA",
    "validate_request",
    "load_request",
    "route_request",
    "lifecycle_status",
    "lifecycle_stage_status",
    "plan_start",
    "start_lifecycle",
    "plan_lifecycle_stage",
    "apply_lifecycle_stage",
    "plan_candidate_preverification_stage",
    "apply_candidate_preverification_stage",
    "plan_local_main_release_stage",
    "apply_local_main_release_stage",
    "plan_candidate_registration_stage",
    "apply_candidate_registration_stage",
    "plan_ordered_integration_preparation_stage",
    "apply_ordered_integration_preparation_stage",
    "plan_integration_commit_stage",
    "apply_integration_commit_stage",
    "plan_integrated_evidence_stage",
    "apply_integrated_evidence_stage",
    "plan_final_acceptance_stage",
    "apply_final_acceptance_stage",
    "plan_main_advance_stage",
    "apply_main_advance_stage",
    "plan_integration_cleanup_stage",
    "apply_integration_cleanup_stage",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
