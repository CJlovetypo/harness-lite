#!/usr/bin/env python3
"""Unified, fail-closed Harness Lite lifecycle orchestration.

The lifecycle facade deliberately applies at most one already-planned child
operation per accepted plan.  That constraint lets it compose the existing
reservation, bundle, coordinator, and workspace safety slices without
inventing a second approval path.  Re-run ``plan-start`` / ``start`` until the
reported next gate is reached; a durable journal makes every accepted step
idempotently resumable.

This module never commits, pushes, merges, rebases, stashes, resets, cleans,
removes worktrees, or deletes branches.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

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


class LifecycleError(RuntimeError):
    """Raised when an orchestration fact cannot be proven safely."""


Notify = Callable[[dict[str, object]], None]
Failpoint = Callable[[str], None]


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
    blockers: list[object] = []
    blockers.extend(core_snapshot.get("blocking_reasons", []))
    blockers.extend(workspace_snapshot.get("blocking_reasons", []))
    blockers.extend(governance_snapshot.get("blocking_reasons", []))
    blockers.extend(train_snapshot.get("blocking_reasons", []))
    blockers.extend(progress_snapshot.get("blocking_reasons", []))
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
        "governance": governance_snapshot,
        "principle_drift": principle_drift,
        "blocking_reasons": blockers,
        "next_gate": (
            "principle-impact-audit"
            if principle_gate_blocked
            else "reconcile-progress"
            if progress_snapshot.get("blocking_reasons")
            else "reconcile"
            if blockers
            else "resume-progress-append"
            if progress_snapshot.get("pending_count")
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
    if topology == "local":
        workspace_topology = "local"
        branch_ref = str(normalized.get("branch_ref") or state.base_branch or "refs/heads/main")
        target = context.project_root
    elif topology == "independent-worktree":
        workspace_topology = "worktree"
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
            operation_id=workspace_operation,
        )
    except workspace.WorkspaceError as exc:
        raise LifecycleError(str(exc)) from exc
    activation_base = child_plan.manifest.get("base")
    if (
        not isinstance(activation_base, Mapping)
        or activation_base.get("implementation_ref") != "refs/heads/main"
        or not isinstance(activation_base.get("implementation_commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", str(activation_base["implementation_commit"]))
        or activation_base.get("implementation_commit") != authority.get("governance_commit")
    ):
        raise LifecycleError("workspace plan did not bind an exact latest-main implementation start")
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
        "reason_code": "parallel-prd-lazy-worktree" if topology == "worktree" else "single-active-prd-local",
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
            f"Harness {'will create' if phase == 'before' else 'created'} an isolated worktree for PRD-{facts['iteration']}"
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
    if (
        parameters.get("implementation_ref") != "refs/heads/main"
        or parameters.get("implementation_commit") != current.governance_commit
    ):
        raise LifecycleError("workspace implementation start no longer matches canonical approved main")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            payload = lifecycle_status(args.project_root)
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
    except (LifecycleError, core.HarnessError, bundle.BundleError, coordinator.CoordinatorError, workspace.WorkspaceError, ValueError) as exc:
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
    "validate_request",
    "load_request",
    "route_request",
    "lifecycle_status",
    "plan_start",
    "start_lifecycle",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
