#!/usr/bin/env python3
"""Pure feature-candidate, identity-rebind, and main-advance gates.

The module consumes exact identities already observed by a Git/governance
adapter.  It hashes evidence and returns immutable gate decisions.  It never
runs Git, writes refs, performs a merge, advances main, commits, or pushes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Iterable, Literal, Mapping


CANDIDATE_SCHEMA_V1 = "harness-lite.candidate/v1"
IDENTITY_REBIND_SCHEMA_V1 = "harness-lite.identity-rebind/v1"
INTEGRATION_SCHEMA_V1 = "harness-lite.integration/v1"

HEX_OBJECT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SHA256 = re.compile(r"[0-9a-f]{64}")
ITERATION = re.compile(r"[0-9]{3,}")
GENERATION = re.compile(r"[a-z0-9][a-z0-9._-]*")
ACCEPTANCE_ID = re.compile(r"AC-[0-9]{3,}-[0-9]{2,}")

DEFAULT_MERGE_STRATEGY = "merge-no-ff"
MergeStrategy = Literal["merge-no-ff", "squash", "cherry-pick", "rebase"]
SUPPORTED_MERGE_STRATEGIES = frozenset({"merge-no-ff", "squash", "cherry-pick", "rebase"})


def _require_object_id(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not HEX_OBJECT.fullmatch(normalized):
        raise ValueError(f"{label} must be a full Git object ID")
    return normalized


def _require_digest(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not SHA256.fullmatch(normalized):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return normalized


def _optional_digest(value: str | None, label: str) -> str | None:
    return None if value is None else _require_digest(value, label)


def _stable_strings(values: Iterable[str], label: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise TypeError(f"{label} entries must be strings")
        value = raw.strip()
        if not value:
            continue
        if "\n" in value or "\r" in value:
            raise ValueError(f"{label} entries must be one line")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _digest(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AcceptanceEvidence:
    acceptance_id: str
    evidence_ids: tuple[str, ...]
    verification_ids: tuple[str, ...]


@dataclass(frozen=True)
class CandidateInput:
    iteration: str
    generation: str
    base_commit: str
    candidate_commit: str
    candidate_tree: str
    principle_sha256: str
    included_paths: tuple[str, ...]
    acceptance_ids: tuple[str, ...]
    acceptance_evidence: tuple[AcceptanceEvidence, ...]
    verification_ids: tuple[str, ...]
    prd_approved: bool
    spec_approved: bool
    implementation_authorized: bool
    deviations_resolved: bool
    dirty_scope_owned: bool


@dataclass(frozen=True)
class CandidateEvidence:
    schema_version: str
    iteration: str
    generation: str
    base_commit: str
    candidate_commit: str
    candidate_tree: str
    principle_sha256: str
    included_paths: tuple[str, ...]
    acceptance_ids: tuple[str, ...]
    acceptance_evidence: tuple[AcceptanceEvidence, ...]
    verification_ids: tuple[str, ...]
    evidence_digest: str
    verified: bool
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _acceptance_payload(value: AcceptanceEvidence) -> dict[str, object]:
    return {
        "acceptance_id": value.acceptance_id,
        "evidence_ids": list(value.evidence_ids),
        "verification_ids": list(value.verification_ids),
    }


def _candidate_payload(value: CandidateEvidence) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "iteration": value.iteration,
        "generation": value.generation,
        "base_commit": value.base_commit,
        "candidate_commit": value.candidate_commit,
        "candidate_tree": value.candidate_tree,
        "principle_sha256": value.principle_sha256,
        "included_paths": list(value.included_paths),
        "acceptance_ids": list(value.acceptance_ids),
        "acceptance_evidence": [_acceptance_payload(item) for item in value.acceptance_evidence],
        "verification_ids": list(value.verification_ids),
        "verified": value.verified,
        "blockers": list(value.blockers),
    }


def candidate_evidence_digest(value: CandidateEvidence) -> str:
    return _digest(_candidate_payload(value))


def candidate_evidence_gate(value: CandidateEvidence) -> GateDecision:
    blockers: list[str] = []
    if value.schema_version != CANDIDATE_SCHEMA_V1:
        blockers.append("candidate-schema-unsupported")
    try:
        supplied_digest = _require_digest(value.evidence_digest, "candidate evidence_digest")
    except (TypeError, ValueError):
        blockers.append("candidate-evidence-digest-invalid")
    else:
        if supplied_digest != candidate_evidence_digest(value):
            blockers.append("candidate-evidence-digest-mismatch")
    if not value.verified or value.blockers:
        blockers.append("candidate-unverified")
    return GateDecision(not blockers, tuple(dict.fromkeys(blockers)))


def build_candidate(value: CandidateInput) -> CandidateEvidence:
    if not isinstance(value, CandidateInput):
        raise TypeError("value must be CandidateInput")
    iteration = value.iteration.strip()
    generation = value.generation.strip().lower()
    if not ITERATION.fullmatch(iteration):
        raise ValueError("iteration must be a canonical NNN identity")
    if not GENERATION.fullmatch(generation):
        raise ValueError("generation is invalid")
    base_commit = _require_object_id(value.base_commit, "base_commit")
    candidate_commit = _require_object_id(value.candidate_commit, "candidate_commit")
    candidate_tree = _require_object_id(value.candidate_tree, "candidate_tree")
    principle_sha256 = _require_digest(value.principle_sha256, "principle_sha256")
    included_paths = _stable_strings(value.included_paths, "included_paths")
    acceptance_ids = _stable_strings(value.acceptance_ids, "acceptance_ids")
    verification_ids = _stable_strings(value.verification_ids, "verification_ids")

    blockers: list[str] = []
    if not value.prd_approved:
        blockers.append("prd-not-approved")
    if not value.spec_approved:
        blockers.append("spec-not-approved")
    if not value.implementation_authorized:
        blockers.append("implementation-not-authorized")
    if not value.deviations_resolved:
        blockers.append("deviations-unresolved")
    if not value.dirty_scope_owned:
        blockers.append("dirty-scope-unowned")
    if not included_paths:
        blockers.append("included-paths-missing")
    if not acceptance_ids:
        blockers.append("acceptance-ids-missing")
    for acceptance_id in acceptance_ids:
        if not ACCEPTANCE_ID.fullmatch(acceptance_id) or not acceptance_id.startswith(f"AC-{iteration}-"):
            blockers.append(f"acceptance-id-invalid:{acceptance_id}")
    if not verification_ids:
        blockers.append("candidate-verification-missing")

    normalized_evidence: list[AcceptanceEvidence] = []
    evidence_by_ac: dict[str, AcceptanceEvidence] = {}
    for item in value.acceptance_evidence:
        if not isinstance(item, AcceptanceEvidence):
            raise TypeError("acceptance_evidence entries must be AcceptanceEvidence")
        acceptance_id = item.acceptance_id.strip()
        if not ACCEPTANCE_ID.fullmatch(acceptance_id):
            raise ValueError(f"invalid acceptance evidence ID: {acceptance_id!r}")
        if not acceptance_id.startswith(f"AC-{iteration}-"):
            blockers.append(f"acceptance-evidence-owner-mismatch:{acceptance_id}")
        normalized = AcceptanceEvidence(
            acceptance_id=acceptance_id,
            evidence_ids=_stable_strings(item.evidence_ids, f"evidence_ids for {acceptance_id}"),
            verification_ids=_stable_strings(
                item.verification_ids,
                f"verification_ids for {acceptance_id}",
            ),
        )
        if acceptance_id in evidence_by_ac:
            blockers.append(f"acceptance-evidence-duplicate:{acceptance_id}")
        else:
            evidence_by_ac[acceptance_id] = normalized
            normalized_evidence.append(normalized)

    acceptance_set = set(acceptance_ids)
    for acceptance_id in acceptance_ids:
        evidence = evidence_by_ac.get(acceptance_id)
        if evidence is None or not evidence.evidence_ids:
            blockers.append(f"acceptance-evidence-missing:{acceptance_id}")
        if evidence is None or not evidence.verification_ids:
            blockers.append(f"acceptance-verification-missing:{acceptance_id}")
    for acceptance_id in evidence_by_ac:
        if acceptance_id not in acceptance_set:
            blockers.append(f"acceptance-evidence-unknown:{acceptance_id}")

    verified = not blockers
    provisional = CandidateEvidence(
        schema_version=CANDIDATE_SCHEMA_V1,
        iteration=iteration,
        generation=generation,
        base_commit=base_commit,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        principle_sha256=principle_sha256,
        included_paths=included_paths,
        acceptance_ids=acceptance_ids,
        acceptance_evidence=tuple(normalized_evidence),
        verification_ids=verification_ids,
        evidence_digest="0" * 64,
        verified=verified,
        blockers=tuple(blockers),
    )
    return CandidateEvidence(
        **{
            **provisional.__dict__,
            "evidence_digest": candidate_evidence_digest(provisional),
        }
    )


def candidate_freshness_gate(
    evidence: CandidateEvidence,
    *,
    current_base_commit: str,
    current_candidate_commit: str,
    current_candidate_tree: str,
    current_principle_sha256: str,
) -> GateDecision:
    blockers = list(candidate_evidence_gate(evidence).blockers)
    if evidence.base_commit != _require_object_id(current_base_commit, "current_base_commit"):
        blockers.append("candidate-base-stale")
    if evidence.candidate_commit != _require_object_id(current_candidate_commit, "current_candidate_commit"):
        blockers.append("candidate-commit-stale")
    if evidence.candidate_tree != _require_object_id(current_candidate_tree, "current_candidate_tree"):
        blockers.append("candidate-tree-stale")
    if evidence.principle_sha256 != _require_digest(current_principle_sha256, "current_principle_sha256"):
        blockers.append("candidate-principle-stale")
    return GateDecision(not blockers, tuple(dict.fromkeys(blockers)))


def candidate_is_current(
    evidence: CandidateEvidence,
    *,
    candidate_commit: str,
    candidate_tree: str,
    principle_sha256: str,
) -> bool:
    """Compatibility predicate; prefer candidate_freshness_gate for reasons."""

    return (
        candidate_evidence_gate(evidence).allowed
        and evidence.candidate_commit == _require_object_id(candidate_commit, "candidate_commit")
        and evidence.candidate_tree == _require_object_id(candidate_tree, "candidate_tree")
        and evidence.principle_sha256 == _require_digest(principle_sha256, "principle_sha256")
    )


@dataclass(frozen=True)
class IdentityRebindInput:
    source_candidate_evidence_digest: str
    source_candidate_commit: str
    source_candidate_tree: str
    integration_generation: str
    target_main: str
    integrated_commit: str
    integrated_tree: str
    principle_sha256: str
    evidence_ids: tuple[str, ...]
    verification_ids: tuple[str, ...]
    explicitly_revalidated: bool


@dataclass(frozen=True)
class IdentityRebindEvidence:
    schema_version: str
    source_candidate_evidence_digest: str
    source_candidate_commit: str
    source_candidate_tree: str
    integration_generation: str
    target_main: str
    integrated_commit: str
    integrated_tree: str
    principle_sha256: str
    evidence_ids: tuple[str, ...]
    verification_ids: tuple[str, ...]
    evidence_digest: str
    verified: bool
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _identity_rebind_payload(value: IdentityRebindEvidence) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "source_candidate_evidence_digest": value.source_candidate_evidence_digest,
        "source_candidate_commit": value.source_candidate_commit,
        "source_candidate_tree": value.source_candidate_tree,
        "integration_generation": value.integration_generation,
        "target_main": value.target_main,
        "integrated_commit": value.integrated_commit,
        "integrated_tree": value.integrated_tree,
        "principle_sha256": value.principle_sha256,
        "evidence_ids": list(value.evidence_ids),
        "verification_ids": list(value.verification_ids),
        "verified": value.verified,
        "blockers": list(value.blockers),
    }


def identity_rebind_evidence_digest(value: IdentityRebindEvidence) -> str:
    return _digest(_identity_rebind_payload(value))


def identity_rebind_evidence_gate(value: IdentityRebindEvidence) -> GateDecision:
    blockers: list[str] = []
    if value.schema_version != IDENTITY_REBIND_SCHEMA_V1:
        blockers.append("identity-rebind-schema-unsupported")
    try:
        supplied_digest = _require_digest(value.evidence_digest, "identity rebind evidence_digest")
    except (TypeError, ValueError):
        blockers.append("identity-rebind-evidence-digest-invalid")
    else:
        if supplied_digest != identity_rebind_evidence_digest(value):
            blockers.append("identity-rebind-evidence-digest-mismatch")
    if not value.verified or value.blockers:
        blockers.append("identity-rebind-unverified")
    return GateDecision(not blockers, tuple(dict.fromkeys(blockers)))


def build_identity_rebinding(value: IdentityRebindInput) -> IdentityRebindEvidence:
    if not isinstance(value, IdentityRebindInput):
        raise TypeError("value must be IdentityRebindInput")
    source_digest = _require_digest(
        value.source_candidate_evidence_digest,
        "source_candidate_evidence_digest",
    )
    source_commit = _require_object_id(value.source_candidate_commit, "source_candidate_commit")
    source_tree = _require_object_id(value.source_candidate_tree, "source_candidate_tree")
    generation = value.integration_generation.strip().lower()
    if not GENERATION.fullmatch(generation):
        raise ValueError("integration_generation is invalid")
    target_main = _require_object_id(value.target_main, "target_main")
    integrated_commit = _require_object_id(value.integrated_commit, "integrated_commit")
    integrated_tree = _require_object_id(value.integrated_tree, "integrated_tree")
    principle = _require_digest(value.principle_sha256, "principle_sha256")
    evidence_ids = _stable_strings(value.evidence_ids, "identity rebind evidence_ids")
    verification_ids = _stable_strings(value.verification_ids, "identity rebind verification_ids")

    blockers: list[str] = []
    if source_commit == integrated_commit:
        blockers.append("identity-did-not-change")
    if not value.explicitly_revalidated:
        blockers.append("identity-revalidation-not-explicit")
    if not evidence_ids:
        blockers.append("identity-rebound-evidence-missing")
    if not verification_ids:
        blockers.append("identity-revalidation-missing")
    verified = not blockers
    provisional = IdentityRebindEvidence(
        schema_version=IDENTITY_REBIND_SCHEMA_V1,
        source_candidate_evidence_digest=source_digest,
        source_candidate_commit=source_commit,
        source_candidate_tree=source_tree,
        integration_generation=generation,
        target_main=target_main,
        integrated_commit=integrated_commit,
        integrated_tree=integrated_tree,
        principle_sha256=principle,
        evidence_ids=evidence_ids,
        verification_ids=verification_ids,
        evidence_digest="0" * 64,
        verified=verified,
        blockers=tuple(blockers),
    )
    return IdentityRebindEvidence(
        **{
            **provisional.__dict__,
            "evidence_digest": identity_rebind_evidence_digest(provisional),
        }
    )


@dataclass(frozen=True)
class IntegrationInput:
    generation: str
    target_main: str
    integrated_commit: str
    integrated_tree: str
    principle_sha256: str
    candidates: tuple[CandidateEvidence, ...]
    merge_strategy: MergeStrategy = DEFAULT_MERGE_STRATEGY
    strategy_declaration_digest: str | None = None
    dependency_order: tuple[str, ...] = ()
    preserved_candidate_commits: tuple[str, ...] = ()
    identity_rebindings: tuple[IdentityRebindEvidence, ...] = ()
    governance_reconciled: bool = False
    governance_evidence_digest: str | None = None
    cross_prd_verification_ids: tuple[str, ...] = ()
    integration_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegratedCandidate:
    schema_version: str
    generation: str
    target_main: str
    integrated_commit: str
    integrated_tree: str
    principle_sha256: str
    merge_strategy: str
    strategy_declaration_digest: str | None
    candidate_digests: tuple[str, ...]
    dependency_order: tuple[str, ...]
    preserved_candidate_commits: tuple[str, ...]
    identity_rebind_digests: tuple[str, ...]
    governance_evidence_digest: str | None
    cross_prd_verification_ids: tuple[str, ...]
    integration_evidence_ids: tuple[str, ...]
    evidence_digest: str
    verified: bool
    blockers: tuple[str, ...]
    requires_user_acceptance: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _integrated_payload(value: IntegratedCandidate) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "generation": value.generation,
        "target_main": value.target_main,
        "integrated_commit": value.integrated_commit,
        "integrated_tree": value.integrated_tree,
        "principle_sha256": value.principle_sha256,
        "merge_strategy": value.merge_strategy,
        "strategy_declaration_digest": value.strategy_declaration_digest,
        "candidate_digests": list(value.candidate_digests),
        "dependency_order": list(value.dependency_order),
        "preserved_candidate_commits": list(value.preserved_candidate_commits),
        "identity_rebind_digests": list(value.identity_rebind_digests),
        "governance_evidence_digest": value.governance_evidence_digest,
        "cross_prd_verification_ids": list(value.cross_prd_verification_ids),
        "integration_evidence_ids": list(value.integration_evidence_ids),
        "verified": value.verified,
        "blockers": list(value.blockers),
        "requires_user_acceptance": value.requires_user_acceptance,
    }


def integrated_evidence_digest(value: IntegratedCandidate) -> str:
    return _digest(_integrated_payload(value))


def integrated_evidence_gate(value: IntegratedCandidate) -> GateDecision:
    blockers: list[str] = []
    if value.schema_version != INTEGRATION_SCHEMA_V1:
        blockers.append("integrated-schema-unsupported")
    try:
        supplied_digest = _require_digest(value.evidence_digest, "integrated evidence_digest")
    except (TypeError, ValueError):
        blockers.append("integrated-evidence-digest-invalid")
    else:
        if supplied_digest != integrated_evidence_digest(value):
            blockers.append("integrated-evidence-digest-mismatch")
    if not value.verified or value.blockers:
        blockers.append("integrated-candidate-not-verified")
    return GateDecision(not blockers, tuple(dict.fromkeys(blockers)))


def build_integrated_candidate(value: IntegrationInput) -> IntegratedCandidate:
    if not isinstance(value, IntegrationInput):
        raise TypeError("value must be IntegrationInput")
    generation = value.generation.strip().lower()
    if not GENERATION.fullmatch(generation):
        raise ValueError("integration generation is invalid")
    target_main = _require_object_id(value.target_main, "target_main")
    integrated_commit = _require_object_id(value.integrated_commit, "integrated_commit")
    integrated_tree = _require_object_id(value.integrated_tree, "integrated_tree")
    principle = _require_digest(value.principle_sha256, "principle_sha256")
    strategy = str(value.merge_strategy).strip().lower()
    if strategy not in SUPPORTED_MERGE_STRATEGIES:
        raise ValueError("merge_strategy is unsupported")
    strategy_declaration = _optional_digest(
        value.strategy_declaration_digest,
        "strategy_declaration_digest",
    )
    dependency_order = _stable_strings(value.dependency_order, "dependency_order")
    preserved_commits = tuple(
        _require_object_id(item, "preserved_candidate_commits entry")
        for item in _stable_strings(value.preserved_candidate_commits, "preserved_candidate_commits")
    )
    verification_ids = _stable_strings(value.cross_prd_verification_ids, "cross_prd_verification_ids")
    integration_evidence_ids = _stable_strings(value.integration_evidence_ids, "integration_evidence_ids")
    governance_digest = _optional_digest(
        value.governance_evidence_digest,
        "governance_evidence_digest",
    )

    blockers: list[str] = []
    if strategy != DEFAULT_MERGE_STRATEGY and strategy_declaration is None:
        blockers.append("merge-strategy-not-explicitly-declared")
    if not value.candidates:
        blockers.append("candidate-missing")
    candidate_digests: list[str] = []
    iterations: list[str] = []
    candidate_by_digest: dict[str, CandidateEvidence] = {}
    for candidate in value.candidates:
        if not isinstance(candidate, CandidateEvidence):
            raise TypeError("candidates entries must be CandidateEvidence")
        candidate_digests.append(candidate.evidence_digest)
        iterations.append(candidate.iteration)
        candidate_by_digest[candidate.evidence_digest] = candidate
        gate = candidate_evidence_gate(candidate)
        if not gate.allowed:
            blockers.append(f"candidate-evidence-invalid:{candidate.iteration}/{candidate.generation}")
        if candidate.principle_sha256 != principle:
            blockers.append(f"principle-drift:{candidate.iteration}/{candidate.generation}")
    if len(set(candidate_digests)) != len(candidate_digests):
        blockers.append("candidate-evidence-duplicate")
    if len(set(iterations)) != len(iterations):
        blockers.append("candidate-iteration-duplicate")
    if not dependency_order:
        blockers.append("dependency-order-missing")
    elif tuple(iterations) != dependency_order:
        blockers.append("dependency-order-mismatch")
    if not value.governance_reconciled or governance_digest is None:
        blockers.append("governance-not-reconciled")
    if not verification_ids:
        blockers.append("integration-verification-missing")
    if not integration_evidence_ids:
        blockers.append("integration-evidence-missing")

    rebind_by_source: dict[str, IdentityRebindEvidence] = {}
    rebind_digests: list[str] = []
    for rebind in value.identity_rebindings:
        if not isinstance(rebind, IdentityRebindEvidence):
            raise TypeError("identity_rebindings entries must be IdentityRebindEvidence")
        rebind_digests.append(rebind.evidence_digest)
        source_digest = rebind.source_candidate_evidence_digest
        if source_digest in rebind_by_source:
            blockers.append(f"identity-rebind-duplicate:{source_digest}")
        else:
            rebind_by_source[source_digest] = rebind

    preserved_set = set(preserved_commits)
    known_commits = {candidate.candidate_commit for candidate in value.candidates}
    for unexpected in sorted(preserved_set - known_commits):
        blockers.append(f"preserved-candidate-unknown:{unexpected}")

    for candidate in value.candidates:
        if candidate.candidate_commit in preserved_set:
            if candidate.evidence_digest in rebind_by_source:
                blockers.append(f"identity-rebind-unexpected:{candidate.iteration}/{candidate.generation}")
            continue
        rebind = rebind_by_source.get(candidate.evidence_digest)
        if rebind is None:
            blockers.append(f"candidate-identity-changed-rebind-required:{candidate.iteration}/{candidate.generation}")
            continue
        gate = identity_rebind_evidence_gate(rebind)
        if not gate.allowed:
            blockers.append(f"identity-rebind-invalid:{candidate.iteration}/{candidate.generation}")
        if rebind.source_candidate_commit != candidate.candidate_commit:
            blockers.append(f"identity-rebind-source-commit-mismatch:{candidate.iteration}/{candidate.generation}")
        if rebind.source_candidate_tree != candidate.candidate_tree:
            blockers.append(f"identity-rebind-source-tree-mismatch:{candidate.iteration}/{candidate.generation}")
        if rebind.integration_generation != generation:
            blockers.append(f"identity-rebind-generation-mismatch:{candidate.iteration}/{candidate.generation}")
        if rebind.target_main != target_main:
            blockers.append(f"identity-rebind-main-mismatch:{candidate.iteration}/{candidate.generation}")
        if rebind.integrated_commit != integrated_commit:
            blockers.append(f"identity-rebind-commit-mismatch:{candidate.iteration}/{candidate.generation}")
        if rebind.integrated_tree != integrated_tree:
            blockers.append(f"identity-rebind-tree-mismatch:{candidate.iteration}/{candidate.generation}")
        if rebind.principle_sha256 != principle:
            blockers.append(f"identity-rebind-principle-mismatch:{candidate.iteration}/{candidate.generation}")
        candidate_evidence_ids = {
            evidence_id
            for acceptance in candidate.acceptance_evidence
            for evidence_id in acceptance.evidence_ids
        }
        candidate_verification_ids = {
            *candidate.verification_ids,
            *(
                verification_id
                for acceptance in candidate.acceptance_evidence
                for verification_id in acceptance.verification_ids
            ),
        }
        if candidate_evidence_ids.intersection(rebind.evidence_ids):
            blockers.append(f"identity-evidence-not-rebound:{candidate.iteration}/{candidate.generation}")
        if candidate_verification_ids.intersection(rebind.verification_ids):
            blockers.append(f"identity-revalidation-not-new:{candidate.iteration}/{candidate.generation}")

    for source_digest in rebind_by_source:
        if source_digest not in candidate_by_digest:
            blockers.append(f"identity-rebind-source-unknown:{source_digest}")

    verified = not blockers
    provisional = IntegratedCandidate(
        schema_version=INTEGRATION_SCHEMA_V1,
        generation=generation,
        target_main=target_main,
        integrated_commit=integrated_commit,
        integrated_tree=integrated_tree,
        principle_sha256=principle,
        merge_strategy=strategy,
        strategy_declaration_digest=strategy_declaration,
        candidate_digests=tuple(candidate_digests),
        dependency_order=dependency_order,
        preserved_candidate_commits=preserved_commits,
        identity_rebind_digests=tuple(rebind_digests),
        governance_evidence_digest=governance_digest,
        cross_prd_verification_ids=verification_ids,
        integration_evidence_ids=integration_evidence_ids,
        evidence_digest="0" * 64,
        verified=verified,
        blockers=tuple(blockers),
    )
    return IntegratedCandidate(
        **{
            **provisional.__dict__,
            "evidence_digest": integrated_evidence_digest(provisional),
        }
    )


def main_advance_gate(
    evidence: IntegratedCandidate,
    *,
    current_main: str,
    current_integrated_commit: str,
    current_integrated_tree: str,
    current_principle_sha256: str,
    current_candidate_digests: tuple[str, ...],
    current_identity_rebind_digests: tuple[str, ...],
    user_accepted_evidence_digest: str | None,
) -> GateDecision:
    """Require exact, fresh, user-accepted integrated evidence before main CAS."""

    blockers = list(integrated_evidence_gate(evidence).blockers)
    if evidence.target_main != _require_object_id(current_main, "current_main"):
        blockers.append("main-drift")
    if evidence.integrated_commit != _require_object_id(
        current_integrated_commit,
        "current_integrated_commit",
    ):
        blockers.append("integrated-commit-drift")
    if evidence.integrated_tree != _require_object_id(current_integrated_tree, "current_integrated_tree"):
        blockers.append("integrated-tree-drift")
    if evidence.principle_sha256 != _require_digest(current_principle_sha256, "current_principle_sha256"):
        blockers.append("integrated-principle-drift")
    normalized_candidate_digests = tuple(
        _require_digest(item, "current_candidate_digests entry") for item in current_candidate_digests
    )
    if evidence.candidate_digests != normalized_candidate_digests:
        blockers.append("integrated-candidate-set-stale")
    normalized_rebind_digests = tuple(
        _require_digest(item, "current_identity_rebind_digests entry")
        for item in current_identity_rebind_digests
    )
    if evidence.identity_rebind_digests != normalized_rebind_digests:
        blockers.append("integrated-rebind-evidence-stale")
    if user_accepted_evidence_digest != evidence.evidence_digest:
        blockers.append("final-acceptance-missing-or-stale")
    return GateDecision(not blockers, tuple(dict.fromkeys(blockers)))


def default_merge_arguments(candidate_ref: str) -> tuple[str, ...]:
    if not isinstance(candidate_ref, str):
        raise TypeError("candidate_ref must be a string")
    reference = candidate_ref.strip()
    if not reference.startswith("refs/") or any(character.isspace() for character in reference):
        raise ValueError("candidate_ref must be an explicit full ref")
    return ("merge", "--no-ff", "--no-commit", reference)


__all__ = [
    "AcceptanceEvidence",
    "CandidateEvidence",
    "CandidateInput",
    "DEFAULT_MERGE_STRATEGY",
    "GateDecision",
    "IdentityRebindEvidence",
    "IdentityRebindInput",
    "IntegratedCandidate",
    "IntegrationInput",
    "build_candidate",
    "build_identity_rebinding",
    "build_integrated_candidate",
    "candidate_evidence_digest",
    "candidate_evidence_gate",
    "candidate_freshness_gate",
    "candidate_is_current",
    "default_merge_arguments",
    "identity_rebind_evidence_digest",
    "identity_rebind_evidence_gate",
    "integrated_evidence_digest",
    "integrated_evidence_gate",
    "main_advance_gate",
]
