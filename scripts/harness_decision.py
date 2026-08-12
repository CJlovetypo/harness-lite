#!/usr/bin/env python3
"""Pure product-governance, execution-topology, and action-level decisions.

This module deliberately accepts structured facts rather than trying to parse a
user prompt.  The coordinating agent is responsible for inspecting the project
and supplying those facts.  Keeping the classifier pure makes every automatic
decision explainable, deterministic, and independently testable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Literal


DECISION_SCHEMA_V1 = "harness-lite.decision/v1"

GovernancePath = Literal["read-only", "grill", "co-draft", "prd-first"]
ExecutionTopology = Literal[
    "read-only",
    "local",
    "independent-worktree",
    "stacked-worktree",
    "serialize",
]
ActionLevel = Literal["silent", "notify", "confirm"]


HARD_RISK_FIELDS = (
    "public_contract",
    "schema_or_data",
    "migration",
    "permissions",
    "security_privacy_compliance",
    "irreversible",
    "compatibility",
    "external_system",
)


@dataclass(frozen=True)
class RiskVector:
    user_visible: bool = False
    public_contract: bool = False
    schema_or_data: bool = False
    migration: bool = False
    permissions: bool = False
    security_privacy_compliance: bool = False
    irreversible: bool = False
    compatibility: bool = False
    external_system: bool = False
    cross_system_coordination: bool = False
    localized_impact: bool = False
    straightforward_rollback: bool = False
    unknowns: tuple[str, ...] = ()

    def hard_risks(self) -> tuple[str, ...]:
        return tuple(field for field in HARD_RISK_FIELDS if getattr(self, field))


@dataclass(frozen=True)
class AuthorizationState:
    prd_approved: bool = False
    spec_approved: bool = False
    implementation_authorized: bool = False
    integration_authorized: bool = False
    finally_accepted: bool = False


@dataclass(frozen=True)
class DecisionInput:
    read_only: bool
    ambiguities: tuple[str, ...] = ()
    risk: RiskVector = RiskVector()
    active_writers: int = 0
    depends_on: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    principle_change: bool = False
    exclusive_resource: bool = False
    incompatible_schema: bool = False
    authorization: AuthorizationState = AuthorizationState()


@dataclass(frozen=True)
class DecisionResult:
    schema_version: str
    governance_path: GovernancePath
    execution_topology: ExecutionTopology
    authorization_gate: str
    reason_codes: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    inferred_authorization: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


ACTION_LEVELS: dict[str, ActionLevel] = {
    "read-status": "silent",
    "scan-repository": "silent",
    "classify-request": "silent",
    "preview-governance": "silent",
    "preview-worktree": "silent",
    "retry-authorized-operation": "silent",
    "create-worktree": "notify",
    "remove-clean-worktree": "notify",
    "create-branch": "notify",
    "bind-local-branch": "notify",
    "candidate-invalidated": "notify",
    "merge-queue-change": "notify",
    "prd-approval": "confirm",
    "spec-approval": "confirm",
    "implementation-authorization": "confirm",
    "principle-change": "confirm",
    "commit": "confirm",
    "push": "confirm",
    "main-advance": "confirm",
    "merge": "confirm",
    "rebase": "confirm",
    "cherry-pick": "confirm",
    "delete-branch": "confirm",
    "destructive-cleanup": "confirm",
    "lease-takeover": "confirm",
    "external-resource": "confirm",
    "shared-data-migration": "confirm",
    "final-acceptance": "confirm",
}


def action_level(action: str) -> ActionLevel:
    """Return the mandatory interaction level for a known action.

    Unknown mutations fail closed at ``confirm``.  Unknown read-only actions
    must be registered explicitly instead of being silently guessed.
    """

    normalized = action.strip().lower()
    if not normalized:
        raise ValueError("action must not be empty")
    return ACTION_LEVELS.get(normalized, "confirm")


def _authorization_gate(state: AuthorizationState) -> str:
    if not state.prd_approved:
        return "approve-prd"
    if not state.spec_approved:
        return "approve-spec"
    if not state.implementation_authorized:
        return "authorize-implementation"
    if not state.integration_authorized:
        return "candidate-verification"
    if not state.finally_accepted:
        return "final-acceptance"
    return "closed"


def _governance_path(facts: DecisionInput) -> tuple[GovernancePath, list[str], list[str]]:
    if facts.read_only:
        return "read-only", ["read-only-request"], []

    ambiguities = tuple(item.strip() for item in facts.ambiguities if item.strip())
    unknowns = tuple(item.strip() for item in facts.risk.unknowns if item.strip())
    if ambiguities or unknowns:
        reasons = ["decision-bearing-ambiguity"]
        blockers = [f"ambiguity:{item}" for item in ambiguities]
        blockers.extend(f"risk-unknown:{item}" for item in unknowns)
        return "grill", reasons, blockers

    hard_risks = facts.risk.hard_risks()
    small_and_clear = (
        facts.risk.localized_impact
        and facts.risk.straightforward_rollback
        and not hard_risks
        and not facts.risk.cross_system_coordination
    )
    if small_and_clear:
        return "co-draft", ["small-clear-localized", "rollback-straightforward"], []

    reasons = ["clear-non-small"]
    reasons.extend(f"risk:{risk}" for risk in hard_risks)
    if facts.risk.cross_system_coordination:
        reasons.append("cross-system-coordination")
    if not facts.risk.localized_impact:
        reasons.append("impact-not-localized")
    if not facts.risk.straightforward_rollback:
        reasons.append("rollback-not-straightforward")
    return "prd-first", reasons, []


def _execution_topology(facts: DecisionInput) -> tuple[ExecutionTopology, list[str], list[str]]:
    if facts.read_only:
        return "read-only", ["no-writer-required"], []
    if facts.active_writers < 0:
        raise ValueError("active_writers must be zero or greater")

    serialize_reasons: list[str] = []
    if facts.principle_change:
        serialize_reasons.append("global-principle-barrier")
    if facts.exclusive_resource:
        serialize_reasons.append("exclusive-resource")
    if facts.incompatible_schema:
        serialize_reasons.append("incompatible-schema")
    if facts.conflicts_with:
        serialize_reasons.append("declared-conflict")
    if serialize_reasons:
        return "serialize", serialize_reasons, serialize_reasons.copy()

    if facts.depends_on:
        return "stacked-worktree", ["declared-dependency", "stable-candidate-required"], []
    if facts.active_writers == 0:
        return "local", ["first-active-writer", "local-fast-path"], []
    return "independent-worktree", ["additional-active-writer", "isolation-required"], []


def classify(facts: DecisionInput) -> DecisionResult:
    governance, governance_reasons, governance_blockers = _governance_path(facts)
    topology, topology_reasons, topology_blockers = _execution_topology(facts)
    return DecisionResult(
        schema_version=DECISION_SCHEMA_V1,
        governance_path=governance,
        execution_topology=topology,
        authorization_gate=_authorization_gate(facts.authorization),
        reason_codes=tuple(dict.fromkeys((*governance_reasons, *topology_reasons))),
        blocking_reasons=tuple(dict.fromkeys((*governance_blockers, *topology_blockers))),
        inferred_authorization=False,
    )


def normalize_string_tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
