"""Deterministic, evidence-bound merge-train ordering for Harness Lite.

This module is deliberately plan-only.  It authenticates every supplied
candidate against the public train registry, derives a stable dependency
order, and returns the exact ordered receipts consumed by the Git adapter.
It neither creates a worktree nor mutates a ref.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence

try:
    from . import harness_train as train
except ImportError:  # pragma: no cover - direct execution
    import harness_train as train


PLAN_SCHEMA = "harness-lite.merge-train-order/v1"
PRIORITY_RE = re.compile(r"-?[0-9]{1,9}\Z")
QUEUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class MergeTrainOrderError(RuntimeError):
    """Raised for malformed plan inputs, never for ordinary plan blockers."""


@dataclass(frozen=True)
class QueueEntry:
    iteration: str
    priority: int
    queued_identity: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateAuthority:
    iteration: str
    generation: str
    candidate_ref: str
    candidate_commit: str
    candidate_tree: str
    evidence_ref: str
    evidence_blob: str
    registration_digest: str
    depends_on: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MergeTrainOrderPlan:
    schema_version: str
    project_root: str
    principle_sha256: str
    candidate_authorities: tuple[CandidateAuthority, ...]
    queue_entries: tuple[QueueEntry, ...]
    dependency_edges: tuple[tuple[str, str], ...]
    ordered_iterations: tuple[str, ...]
    ordered_candidates: tuple[train.RegisteredCandidate, ...]
    blockers: tuple[train.Blocker, ...]
    plan_digest: str
    pushed: bool = False

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _plan_payload(plan: MergeTrainOrderPlan) -> dict[str, object]:
    value = plan.as_dict()
    value.pop("plan_digest", None)
    return value


def merge_train_order_plan_digest(plan: MergeTrainOrderPlan) -> str:
    return _digest(_plan_payload(plan))


def _queue_entries(
    candidates: Sequence[train.RegisteredCandidate],
    queue_metadata: Mapping[str, Mapping[str, object]] | None,
) -> tuple[tuple[QueueEntry, ...], tuple[train.Blocker, ...]]:
    entries: list[QueueEntry] = []
    blockers: list[train.Blocker] = []
    metadata = queue_metadata or {}
    supplied = set(metadata)
    iterations = {item.iteration for item in candidates}
    for unknown in sorted(supplied - iterations):
        blockers.append(train.Blocker("merge-train-queue-unknown", f"PRD-{unknown}"))
    for candidate in candidates:
        raw = metadata.get(candidate.iteration, {})
        if not isinstance(raw, Mapping):
            raise MergeTrainOrderError("queue metadata entries must be objects")
        if set(raw) - {"priority", "queued_identity"}:
            raise MergeTrainOrderError("queue metadata fields are unsupported")
        priority_raw = raw.get("priority", 0)
        if type(priority_raw) is not int or PRIORITY_RE.fullmatch(str(priority_raw)) is None:
            raise MergeTrainOrderError("queue priority must be a bounded integer")
        queued = raw.get("queued_identity", f"iteration:{candidate.iteration}")
        if not isinstance(queued, str) or QUEUE_RE.fullmatch(queued) is None:
            raise MergeTrainOrderError("queued_identity is invalid")
        entries.append(QueueEntry(candidate.iteration, priority_raw, queued))
    return tuple(sorted(entries, key=lambda item: item.iteration)), tuple(blockers)


def _authority(candidate: train.RegisteredCandidate) -> CandidateAuthority:
    return CandidateAuthority(
        iteration=candidate.iteration,
        generation=candidate.generation,
        candidate_ref=candidate.candidate_ref,
        candidate_commit=candidate.candidate_commit,
        candidate_tree=candidate.candidate_tree,
        evidence_ref=candidate.candidate_evidence_ref,
        evidence_blob=candidate.candidate_evidence_blob,
        registration_digest=candidate.registration_digest,
        depends_on=candidate.depends_on,
    )


def plan_merge_train_order(
    project_root: str | Path,
    *,
    candidates: Sequence[train.RegisteredCandidate],
    current_principle_sha256: str,
    queue_metadata: Mapping[str, Mapping[str, object]] | None = None,
) -> MergeTrainOrderPlan:
    """Authenticate and stably topologically order one candidate set."""

    repo = train.open_repository(project_root)
    normalized = tuple(candidates)
    blockers: list[train.Blocker] = []
    if not normalized:
        blockers.append(train.Blocker("merge-train-candidate-missing", "no candidates supplied"))
    by_iteration: dict[str, train.RegisteredCandidate] = {}
    for item in normalized:
        if not isinstance(item, train.RegisteredCandidate):
            raise MergeTrainOrderError("candidates must be RegisteredCandidate values")
        if item.iteration in by_iteration:
            blockers.append(
                train.Blocker("merge-train-candidate-duplicate", f"PRD-{item.iteration}")
            )
        else:
            by_iteration[item.iteration] = item
        blockers.extend(
            train.registered_candidate_gate(
                repo.root,
                item,
                current_principle_sha256=current_principle_sha256,
            )
        )
    queue, queue_blockers = _queue_entries(normalized, queue_metadata)
    blockers.extend(queue_blockers)
    queue_by_iteration = {item.iteration: item for item in queue}

    edges: set[tuple[str, str]] = set()
    indegree = {iteration: 0 for iteration in by_iteration}
    dependents: dict[str, set[str]] = {iteration: set() for iteration in by_iteration}
    for candidate in by_iteration.values():
        for dependency in candidate.depends_on:
            if dependency not in by_iteration:
                # Dependencies absent from this exact train are validated by
                # the integration adapter against latest main/final refs.
                continue
            edge = (dependency, candidate.iteration)
            if edge in edges:
                continue
            edges.add(edge)
            indegree[candidate.iteration] += 1
            dependents[dependency].add(candidate.iteration)

    def ready_key(iteration: str) -> tuple[int, str, int]:
        entry = queue_by_iteration[iteration]
        return (-entry.priority, entry.queued_identity, int(iteration))

    ready = sorted((item for item, degree in indegree.items() if degree == 0), key=ready_key)
    order: list[str] = []
    while ready:
        iteration = ready.pop(0)
        order.append(iteration)
        for child in sorted(dependents[iteration], key=ready_key):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=ready_key)
    if len(order) != len(by_iteration):
        blockers.append(train.Blocker("merge-train-dependency-cycle", "candidate DAG is cyclic"))

    ordered = tuple(by_iteration[item] for item in order)
    authorities = tuple(sorted((_authority(item) for item in normalized), key=lambda x: x.iteration))
    provisional = MergeTrainOrderPlan(
        schema_version=PLAN_SCHEMA,
        project_root=str(repo.root),
        principle_sha256=current_principle_sha256,
        candidate_authorities=authorities,
        queue_entries=queue,
        dependency_edges=tuple(sorted(edges)),
        ordered_iterations=tuple(order),
        ordered_candidates=ordered,
        blockers=tuple(dict.fromkeys(blockers)),
        plan_digest="0" * 64,
    )
    return replace(provisional, plan_digest=merge_train_order_plan_digest(provisional))


def merge_train_order_gate(
    plan: MergeTrainOrderPlan,
) -> tuple[train.Blocker, ...]:
    """Re-run public gates and prove the ordered plan was not changed."""

    blockers = list(plan.blockers)
    if plan.schema_version != PLAN_SCHEMA:
        blockers.append(train.Blocker("merge-train-plan-schema", "schema is unsupported"))
    if plan.plan_digest != merge_train_order_plan_digest(plan):
        blockers.append(train.Blocker("merge-train-plan-digest", "plan was changed"))
    if plan.pushed:
        blockers.append(train.Blocker("merge-train-push-forbidden", "ordering never pushes"))
    try:
        recomputed = plan_merge_train_order(
            plan.project_root,
            candidates=plan.ordered_candidates,
            current_principle_sha256=plan.principle_sha256,
            queue_metadata={
                item.iteration: {
                    "priority": item.priority,
                    "queued_identity": item.queued_identity,
                }
                for item in plan.queue_entries
            },
        )
    except (MergeTrainOrderError, train.TrainError) as exc:
        blockers.append(train.Blocker("merge-train-plan-recompute", str(exc)))
    else:
        if recomputed != plan:
            blockers.append(
                train.Blocker(
                    "merge-train-order-recomputed-drift",
                    "dependency/queue ordering differs from the canonical public plan",
                )
            )
    for candidate, authority in zip(plan.ordered_candidates, sorted(
        plan.candidate_authorities,
        key=lambda item: plan.ordered_iterations.index(item.iteration)
        if item.iteration in plan.ordered_iterations else len(plan.ordered_iterations),
    )):
        if _authority(candidate) != authority:
            blockers.append(
                train.Blocker("merge-train-candidate-authority", f"PRD-{candidate.iteration}")
            )
        blockers.extend(
            train.registered_candidate_gate(
                plan.project_root,
                candidate,
                current_principle_sha256=plan.principle_sha256,
            )
        )
    return tuple(dict.fromkeys(blockers))


__all__ = [
    "CandidateAuthority",
    "MergeTrainOrderError",
    "MergeTrainOrderPlan",
    "PLAN_SCHEMA",
    "QueueEntry",
    "merge_train_order_gate",
    "merge_train_order_plan_digest",
    "plan_merge_train_order",
]
