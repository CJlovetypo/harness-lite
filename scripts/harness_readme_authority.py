#!/usr/bin/env python3
"""Authoritative, deterministic L0/L1 README derivation for merge-train state.

README bytes are an output of this module, never an input assertion supplied by
the merge-train caller.  The derivation authenticates exact latest-main and
candidate governance blobs, the semantic progress snapshot, public candidate /
integrated refs, and a small validated projection of writer workspaces.  Its
digest binds every selected input and every emitted byte.

This module is read-only.  It never changes a file, ref, index, worktree, lease,
commit, or remote.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterator, Mapping, Sequence

try:
    from . import harness_final_acceptance as final_registry
    from . import harness_governance as governance
    from . import harness_integrated_evidence as integrated_registry
    from . import harness_reconcile as reconcile
    from . import harness_train as train
    from . import harness_workspace as workspace
    from . import project_harness as core
except ImportError:  # pragma: no cover - direct script execution
    import harness_final_acceptance as final_registry
    import harness_governance as governance
    import harness_integrated_evidence as integrated_registry
    import harness_reconcile as reconcile
    import harness_train as train
    import harness_workspace as workspace
    import project_harness as core


AUTHORITY_SCHEMA = "harness-lite.readme-authority/v2"
DOCUMENT_SCHEMA = "harness-lite.readme-authority-document/v1"
INPUT_SCHEMA = "harness-lite.readme-authority-input/v1"
L1_START = "<!-- project-harness:iteration-routing:start -->"
L1_END = "<!-- project-harness:iteration-routing:end -->"
OWNER = b"<!-- managed-by: harness-lite v1 -->"

ITERATION_RE = re.compile(r"[0-9]{3,}")
ITERATION_PATH_RE = re.compile(
    r"harness/iterations/([0-9]{3,})/(README\.md|prd-\1\.md|spec-\1\.md|deviation-\1\.md)"
)
FIELD_RE = re.compile(
    r"^-\s*(?P<label>[^\uff1a:\r\n]+?)\s*[\uff1a:]\s*(?P<value>.*?)\s*$",
    re.MULTILINE,
)
EVENT_FIELD_RE = re.compile(
    r"^-\s*(?P<label>[a-z_]+)\s*:\s*(?P<value>.*?)\s*$",
    re.MULTILINE,
)
CANDIDATE_PUBLIC_REF_RE = re.compile(
    r"refs/project-harness/v2/iterations/([0-9]{3,})/"
    r"(candidates|candidate-evidence)/([^/]+)"
)
INTEGRATION_OPERATION_REF_RE = re.compile(
    r"refs/project-harness/v2/integrations/op-([0-9a-f]{32})/(commit|evidence)"
)
FINAL_OPERATION_REF_RE = re.compile(
    r"refs/project-harness/v2/integrations/op-([0-9a-f]{32})/final-acceptance"
)
ITERATION_INTEGRATED_REF_RE = re.compile(
    r"refs/project-harness/v2/iterations/([0-9]{3,})/"
    r"(integrated|final|final-evidence|integrated-evidence/[^/]+)"
)

MAX_ITERATIONS = 1_000
MAX_LEASES = 256
MAX_RELEVANT_REFS = 20_000
MAX_RECENT_EVENTS = 3


class ReadmeAuthorityError(RuntimeError):
    """The derived view could not be authenticated from bounded authority."""


@dataclass(frozen=True)
class BlobBinding:
    source: str
    path: str
    mode: str
    object_id: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateBinding:
    iteration: str
    generation: str
    candidate_ref: str
    candidate_commit: str
    candidate_tree: str
    implementation_commit: str
    evidence_ref: str
    evidence_blob: str
    evidence_digest: str
    registration_digest: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceProjection:
    iteration: str
    topology: str
    generation: int
    branch_ref: str
    base_commit: str
    implementation_commit: str
    runtime_namespace: str
    primary: bool
    head_commit: str
    worktree_path_sha256: str
    owner_sha256: str
    status_sha256: str
    tracked: int
    untracked: int
    ignored: int
    lease_digest: str
    actual_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IterationProjection:
    number: str
    title: str
    prd_status: str
    spec_status: str
    open_deviations: int
    depends_on: tuple[str, ...]
    workspace: str
    governance_gate: str
    candidate_state: str
    integration_state: str
    result: str
    next_step: str
    recent_events: tuple[tuple[str, str, str], ...]
    source_commit: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DerivedReadmeDocument:
    schema_version: str
    path: str
    content: bytes
    source_sha256: str
    content_sha256: str
    size: int

    def manifest_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "path": self.path,
            "source_sha256": self.source_sha256,
            "content_sha256": self.content_sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class DerivedReadmeAuthority:
    schema_version: str
    project_root: str
    operation_id: str
    train_plan_digest: str
    input_digest: str
    authority_id: str
    target_main: str
    main_tree: str
    principle_sha256: str
    progress_sha256: str
    semantic_snapshot_digest: str
    relevant_refs_digest: str
    operational_projection_digest: str
    topology_phase: str
    committed_blobs: tuple[BlobBinding, ...]
    candidate_bindings: tuple[CandidateBinding, ...]
    workspace_projections: tuple[WorkspaceProjection, ...]
    iteration_projections: tuple[IterationProjection, ...]
    documents: tuple[DerivedReadmeDocument, ...]
    authority_digest: str
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_root": self.project_root,
            "operation_id": self.operation_id,
            "train_plan_digest": self.train_plan_digest,
            "input_digest": self.input_digest,
            "authority_id": self.authority_id,
            "target_main": self.target_main,
            "main_tree": self.main_tree,
            "principle_sha256": self.principle_sha256,
            "progress_sha256": self.progress_sha256,
            "semantic_snapshot_digest": self.semantic_snapshot_digest,
            "relevant_refs_digest": self.relevant_refs_digest,
            "operational_projection_digest": self.operational_projection_digest,
            "topology_phase": self.topology_phase,
            "committed_blobs": [item.as_dict() for item in self.committed_blobs],
            "candidate_bindings": [item.as_dict() for item in self.candidate_bindings],
            "workspace_projections": [item.as_dict() for item in self.workspace_projections],
            "iteration_projections": [item.as_dict() for item in self.iteration_projections],
            "documents": [item.manifest_dict() for item in self.documents],
            "authority_digest": self.authority_digest,
            "pushed": self.pushed,
        }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    return _sha256(_canonical_json(value))


def _one_line(value: object, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ReadmeAuthorityError(f"{label} must be a string")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or "\n" in result
        or "\r" in result
        or any(ord(character) < 0x20 and character != "\t" for character in result)
    ):
        raise ReadmeAuthorityError(f"{label} must be one bounded non-empty line")
    return result


def _decode(raw: bytes, path: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReadmeAuthorityError(f"governance authority is not UTF-8: {path}") from exc


def _field(raw: bytes, label: str) -> str | None:
    text = core.strip_html_comments(_decode(raw, label))
    matches = [
        (item.group("value").strip().strip("`").strip())
        for item in FIELD_RE.finditer(text)
        if item.group("label").strip() == label
    ]
    if len(matches) > 1:
        raise ReadmeAuthorityError(f"governance field is duplicated: {label}")
    return matches[0] if matches else None


def _ids(raw: bytes, *labels: str) -> tuple[str, ...]:
    for label in labels:
        value = _field(raw, label)
        if value and value.casefold() not in {"无", "none", "n/a", "尚无"}:
            return tuple(dict.fromkeys(re.findall(r"(?:PRD-)?([0-9]{3,})", value)))
    return ()


def _title(raw: bytes, number: str) -> str:
    text = _decode(raw, f"PRD-{number}")
    match = re.search(
        rf"^#\s+PRD-{re.escape(number)}(?:\s*[:\uff1a]|\s+[\u2014-])\s*(.+?)\s*$",
        text,
        re.MULTILINE,
    )
    return _one_line(match.group(1) if match else f"PRD-{number}", "iteration title")


def _open_deviations(raw: bytes, number: str) -> int:
    text = core.strip_html_comments(_decode(raw, f"deviation-{number}"))
    matches = re.findall(r"(?:当前)?开放偏差\s*[\uff1a:]\s*`?([0-9]+)`?", text)
    if len(matches) != 1:
        raise ReadmeAuthorityError(
            f"deviation-{number} must declare exactly one current open-deviation count"
        )
    value = int(matches[0])
    if value > 1_000_000:
        raise ReadmeAuthorityError(f"deviation-{number} open count exceeds safe range")
    return value


def _blob_manifest(
    *, source: str, entries: Mapping[str, tuple[str, str, bytes]]
) -> tuple[BlobBinding, ...]:
    return tuple(
        BlobBinding(source, path, mode, object_id, len(raw), _sha256(raw))
        for path, (mode, object_id, raw) in sorted(entries.items())
        if path == "AGENTS.md" or path.startswith("harness/")
    )


def _snapshot_digest(snapshot: reconcile.GovernanceSnapshot) -> str:
    return _digest(
        {
            "source_id": snapshot.source_id,
            "files": [
                {"path": item.path, "size": len(item.content), "sha256": _sha256(item.content)}
                for item in snapshot.files
            ],
        }
    )


def _relevant_ref_manifest(refs: Mapping[str, str], main_ref: str) -> tuple[dict[str, str], ...]:
    selected: list[dict[str, str]] = []
    for reference, oid in sorted(refs.items()):
        relevant = reference == main_ref or (
            reference.startswith("refs/project-harness/v2/iterations/")
            and any(
                token in reference
                for token in (
                    "/candidates/",
                    "/candidate-evidence/",
                    "/integrated",
                    "/final",
                )
            )
        ) or reference.startswith("refs/project-harness/v2/integrations/")
        if relevant:
            selected.append({"ref": reference, "object": oid})
    if len(selected) > MAX_RELEVANT_REFS:
        raise ReadmeAuthorityError("README authority ref projection exceeds its safe limit")
    return tuple(selected)


def _candidate_projection(
    root: Path,
    plan: train.IntegrationPreparePlan,
    validation: train.AuthorityValidationContext,
) -> tuple[tuple[train.RegisteredCandidate, ...], tuple[CandidateBinding, ...]]:
    loaded_values: list[train.RegisteredCandidate] = []
    bindings: list[CandidateBinding] = []
    seen_iterations: set[str] = set()
    for expected in plan.candidates:
        if expected.iteration in seen_iterations:
            raise ReadmeAuthorityError(
                f"integration plan repeats PRD-{expected.iteration} candidate authority"
            )
        seen_iterations.add(expected.iteration)
        loaded, blockers = train.load_registered_candidate(
            root,
            iteration=expected.iteration,
            generation=expected.generation,
            current_principle_sha256=plan.principle_sha256,
        )
        if loaded is None or blockers:
            detail = ", ".join(item.code for item in blockers) or "missing"
            raise ReadmeAuthorityError(
                f"public candidate authority is invalid for PRD-{expected.iteration}: {detail}"
            )
        expected_identity = (
            expected.candidate_ref,
            expected.candidate_commit,
            expected.candidate_tree,
            expected.implementation_commit,
            expected.candidate_evidence_ref,
            expected.candidate_evidence_blob,
            expected.candidate_evidence.evidence_digest,
            expected.registration_digest,
        )
        loaded_identity = (
            loaded.candidate_ref,
            loaded.candidate_commit,
            loaded.candidate_tree,
            loaded.implementation_commit,
            loaded.candidate_evidence_ref,
            loaded.candidate_evidence_blob,
            loaded.candidate_evidence.evidence_digest,
            loaded.registration_digest,
        )
        if loaded_identity != expected_identity:
            raise ReadmeAuthorityError(
                f"integration plan candidate differs from public authority: {expected.candidate_ref}"
            )
        if validation.refs.get(loaded.candidate_ref) != loaded.candidate_commit:
            raise ReadmeAuthorityError(f"candidate ref drifted: {loaded.candidate_ref}")
        if validation.refs.get(loaded.candidate_evidence_ref) != loaded.candidate_evidence_blob:
            raise ReadmeAuthorityError(
                f"candidate evidence ref drifted: {loaded.candidate_evidence_ref}"
            )
        loaded_values.append(loaded)
        bindings.append(
            CandidateBinding(
                iteration=loaded.iteration,
                generation=loaded.generation,
                candidate_ref=loaded.candidate_ref,
                candidate_commit=loaded.candidate_commit,
                candidate_tree=loaded.candidate_tree,
                implementation_commit=loaded.implementation_commit,
                evidence_ref=loaded.candidate_evidence_ref,
                evidence_blob=loaded.candidate_evidence_blob,
                evidence_digest=loaded.candidate_evidence.evidence_digest,
                registration_digest=loaded.registration_digest,
            )
        )
    return tuple(loaded_values), tuple(bindings)


def _generation_key(value: str) -> tuple[int, int | str, str]:
    numeric = re.fullmatch(r"g([0-9]+)", value)
    return (0, int(numeric.group(1)), value) if numeric else (1, value, value)


def _validate_public_candidate_registry(
    root: Path,
    refs: Mapping[str, str],
    *,
    principle_sha256: str,
) -> tuple[
    dict[str, tuple[train.RegisteredCandidate, ...]],
    tuple[dict[str, str], ...],
]:
    """Authenticate every public candidate/evidence ref pair, fail closed.

    Merely finding a generation ref is deliberately insufficient.  Orphan,
    malformed, stale, or principle-drifted generations block the derived view
    rather than being rendered as a verified candidate.
    """

    prefix = "refs/project-harness/v2/iterations/"
    identities: dict[tuple[str, str], set[str]] = {}
    for reference in refs:
        if not reference.startswith(prefix) or not any(
            token in reference for token in ("/candidates/", "/candidate-evidence/")
        ):
            continue
        match = CANDIDATE_PUBLIC_REF_RE.fullmatch(reference)
        if match is None:
            raise ReadmeAuthorityError(
                f"public candidate ref is not canonical: {reference}"
            )
        identities.setdefault((match.group(1), match.group(3)), set()).add(
            match.group(2)
        )
    if len(identities) > MAX_RELEVANT_REFS:
        raise ReadmeAuthorityError("public candidate projection exceeds its safe limit")

    by_iteration: dict[str, list[train.RegisteredCandidate]] = {}
    manifest: list[dict[str, str]] = []
    for (iteration, generation), kinds in sorted(
        identities.items(), key=lambda item: (int(item[0][0]), _generation_key(item[0][1]))
    ):
        try:
            loaded, blockers = train.load_registered_candidate(
                root,
                iteration=iteration,
                generation=generation,
                current_principle_sha256=principle_sha256,
            )
        except train.TrainError as exc:
            raise ReadmeAuthorityError(
                f"public candidate authority is invalid for PRD-{iteration}/{generation}: {exc}"
            ) from exc
        if loaded is None or blockers or kinds != {"candidates", "candidate-evidence"}:
            detail = ", ".join(item.code for item in blockers) or "orphan-public-ref"
            raise ReadmeAuthorityError(
                f"public candidate authority is invalid for PRD-{iteration}/{generation}: {detail}"
            )
        by_iteration.setdefault(iteration, []).append(loaded)
        manifest.append(
            {
                "iteration": iteration,
                "generation": generation,
                "candidate_ref": loaded.candidate_ref,
                "candidate_commit": loaded.candidate_commit,
                "evidence_ref": loaded.candidate_evidence_ref,
                "evidence_blob": loaded.candidate_evidence_blob,
                "registration_digest": loaded.registration_digest,
            }
        )
    return (
        {
            iteration: tuple(
                sorted(values, key=lambda item: _generation_key(item.generation))
            )
            for iteration, values in by_iteration.items()
        },
        tuple(manifest),
    )


def _validate_public_integration_registry(
    root: Path,
    refs: Mapping[str, str],
    *,
    current_main_ref: str,
    current_main: str,
    principle_sha256: str,
) -> tuple[
    tuple[dict[str, object], ...],
    dict[str, tuple[dict[str, str], ...]],
    dict[str, dict[str, str]],
]:
    results: list[dict[str, object]] = []
    integration_operations: dict[str, set[str]] = {}
    final_operations: set[str] = set()
    for reference in refs:
        integrated_match = INTEGRATION_OPERATION_REF_RE.fullmatch(reference)
        if integrated_match:
            integration_operations.setdefault(
                f"OP-{integrated_match.group(1)}", set()
            ).add(integrated_match.group(2))
            continue
        final_match = FINAL_OPERATION_REF_RE.fullmatch(reference)
        if final_match:
            final_operations.add(f"OP-{final_match.group(1)}")
            continue
        if reference.startswith("refs/project-harness/v2/integrations/"):
            raise ReadmeAuthorityError(
                f"public integration ref is not canonical: {reference}"
            )
    if len(integration_operations) + len(final_operations) > MAX_ITERATIONS:
        raise ReadmeAuthorityError("integrated-evidence projection exceeds its safe limit")

    integrated_receipts: list[integrated_registry.RegisteredIntegratedEvidence] = []
    covered_integrated_refs: set[str] = set()
    integrated_by_iteration: dict[str, list[dict[str, str]]] = {}
    for operation, kinds in sorted(integration_operations.items()):
        receipt, blockers = integrated_registry.load_registered_integrated_evidence(
            root, operation_id=operation
        )
        if receipt is None or blockers or kinds != {"commit", "evidence"}:
            detail = ", ".join(item.code for item in blockers) or "orphan-public-ref"
            raise ReadmeAuthorityError(
                f"public integrated evidence is invalid for {operation}: {detail}"
            )
        integrated_receipts.append(receipt)
        covered_integrated_refs.update(
            item.ref_name for item in receipt.iteration_evidence_refs
        )
        current = _integrated_receipt_is_current(
            receipt,
            current_main_ref=current_main_ref,
            current_main=current_main,
            principle_sha256=principle_sha256,
        )
        if current:
            for binding in receipt.metadata.candidate_bindings:
                integrated_by_iteration.setdefault(binding.iteration, []).append(
                    {
                        "operation_id": operation,
                        "generation": receipt.metadata.generation,
                        "commit": receipt.metadata.integrated_commit,
                        "tree": receipt.metadata.integrated_tree,
                        "registration_digest": receipt.registration_digest,
                    }
                )
        results.append(
            {
                "kind": "integrated",
                "operation_id": operation,
                "commit": receipt.metadata.integrated_commit,
                "tree": receipt.metadata.integrated_tree,
                "evidence_blob": receipt.evidence_blob,
                "registration_digest": receipt.registration_digest,
                "current": current,
            }
        )

    final_by_iteration: dict[str, dict[str, str]] = {}
    covered_final_refs: set[str] = set()
    for operation in sorted(final_operations):
        receipt, blockers = final_registry.load_registered_final_acceptance(
            root, operation_id=operation
        )
        if receipt is None or blockers:
            detail = ", ".join(item.code for item in blockers) or "missing"
            raise ReadmeAuthorityError(
                f"public final acceptance is invalid for {operation}: {detail}"
            )
        covered_final_refs.update(item.ref_name for item in receipt.iteration_evidence_refs)
        accepted_iterations: list[str] = []
        for candidate in receipt.metadata.accepted_candidates:
            iteration = candidate.iteration
            integrated_ref = (
                f"refs/project-harness/v2/iterations/{iteration}/integrated"
            )
            final_ref = f"refs/project-harness/v2/iterations/{iteration}/final"
            covered_final_refs.update((integrated_ref, final_ref))
            value = {
                "operation_id": operation,
                "commit": receipt.metadata.accepted_main,
                "tree": receipt.metadata.accepted_tree,
                "registration_digest": receipt.registration_digest,
            }
            previous = final_by_iteration.get(iteration)
            if previous is not None and previous != value:
                raise ReadmeAuthorityError(
                    f"multiple final authorities conflict for PRD-{iteration}"
                )
            final_by_iteration[iteration] = value
            accepted_iterations.append(iteration)
        results.append(
            {
                "kind": "final",
                "operation_id": operation,
                "commit": receipt.metadata.accepted_main,
                "tree": receipt.metadata.accepted_tree,
                "evidence_blob": receipt.evidence_blob,
                "registration_digest": receipt.registration_digest,
                "iterations": accepted_iterations,
            }
        )

    for reference in refs:
        match = ITERATION_INTEGRATED_REF_RE.fullmatch(reference)
        if match is None:
            continue
        kind = match.group(2)
        covered = (
            reference in covered_integrated_refs
            if kind.startswith("integrated-evidence/")
            else reference in covered_final_refs
        )
        if not covered:
            raise ReadmeAuthorityError(
                f"iteration integration ref lacks authenticated public evidence: {reference}"
            )

    for iteration, values in integrated_by_iteration.items():
        if len(values) > 1:
            raise ReadmeAuthorityError(
                f"multiple current integrated authorities conflict for PRD-{iteration}"
            )

    return (
        tuple(results),
        {
            iteration: tuple(sorted(values, key=lambda item: item["operation_id"]))
            for iteration, values in integrated_by_iteration.items()
        },
        final_by_iteration,
    )


def _integrated_receipt_is_current(
    receipt: integrated_registry.RegisteredIntegratedEvidence,
    *,
    current_main_ref: str,
    current_main: str,
    principle_sha256: str,
) -> bool:
    metadata = receipt.metadata
    return (
        metadata.main_ref == current_main_ref
        and metadata.target_main == current_main
        and metadata.principle_sha256 == principle_sha256
    )


def _workspace_projection(
    repo: train.Repository,
) -> tuple[str, tuple[WorkspaceProjection, ...], dict[str, object]]:
    context = workspace.RepositoryContext(repo.git, repo.root, repo.common_dir)
    leases, blockers = workspace.load_active_leases(context)
    if blockers:
        raise ReadmeAuthorityError(
            "writer lease registry is invalid: "
            + "; ".join(f"{item.code}: {item.message}" for item in blockers)
        )
    if len(leases) > MAX_LEASES:
        raise ReadmeAuthorityError("writer lease projection exceeds its safe limit")
    projections: list[WorkspaceProjection] = []
    for lease in sorted(leases, key=lambda item: str(item["iteration"])):
        guard_blockers, actual = workspace.guard_lease(context, lease)
        if guard_blockers:
            raise ReadmeAuthorityError(
                f"PRD-{lease['iteration']} writer projection is stale: "
                + "; ".join(f"{item.code}: {item.message}" for item in guard_blockers)
            )
        status = actual.get("status")
        if not isinstance(status, Mapping) or status.get("readable") is not True:
            raise ReadmeAuthorityError(
                f"PRD-{lease['iteration']} workspace status is unreadable"
            )
        path = _one_line(str(lease["worktree_path"]), "worktree path", maximum=32_768)
        owner = _one_line(str(lease["owner"]), "writer owner")
        head = str(actual.get("head_oid", ""))
        if train.OID_RE.fullmatch(head) is None:
            raise ReadmeAuthorityError(
                f"PRD-{lease['iteration']} workspace HEAD is invalid"
            )
        compact_actual = {
            "path_sha256": _sha256(os.path.normcase(path).encode("utf-8")),
            "head_oid": head,
            "branch_ref": actual.get("branch_ref"),
            "primary": bool(actual.get("primary")),
            "detached": bool(actual.get("detached")),
            "locked": bool(actual.get("locked")),
            "prunable": bool(actual.get("prunable")),
            "status": dict(status),
        }
        projections.append(
            WorkspaceProjection(
                iteration=str(lease["iteration"]),
                topology=str(lease["execution_topology"]),
                generation=int(lease["generation"]),
                branch_ref=str(lease["branch_ref"]),
                base_commit=str(lease["base_commit"]),
                implementation_commit=str(
                    lease.get("implementation_commit", lease["base_commit"])
                ),
                runtime_namespace=str(lease["runtime_namespace"]),
                primary=bool(actual.get("primary")),
                head_commit=head,
                worktree_path_sha256=compact_actual["path_sha256"],
                owner_sha256=_sha256(owner.encode("utf-8")),
                status_sha256=str(status["sha256"]),
                tracked=int(status["tracked"]),
                untracked=int(status["untracked"]),
                ignored=int(status["ignored"]),
                lease_digest=workspace.digest(workspace.lease_projection(lease)),
                actual_digest=workspace.digest(compact_actual),
            )
        )

    prior = workspace.load_topology_state(context)
    derived = workspace.derive_topology(leases, prior)
    topology = {
        "schema_version": derived["schema_version"],
        "epoch": derived["epoch"],
        "phase": derived["phase"],
        "active_count": derived["active_count"],
        "high_watermark": derived["high_watermark"],
    }
    if prior is not None:
        for field in ("schema_version", "epoch", "phase", "active_count", "high_watermark"):
            if prior[field] != topology[field]:
                raise ReadmeAuthorityError(
                    f"workspace topology registry differs from validated leases: {field}"
                )
    return str(topology["phase"]), tuple(projections), topology


def _event_value(raw: bytes, label: str) -> str | None:
    text = _decode(raw, f"progress event {label}")
    matches = [
        item.group("value").strip().strip("`").strip()
        for item in EVENT_FIELD_RE.finditer(text)
        if item.group("label") == label
    ]
    return matches[0] if len(matches) == 1 else None


def _recent_events(progress: bytes, iteration: str) -> tuple[tuple[str, str, str], ...]:
    parsed = governance.parse_progress_events(progress, source="README semantic progress")
    if parsed.blockers:
        raise ReadmeAuthorityError(
            "semantic progress is invalid: "
            + "; ".join(f"{item.code}: {item.message}" for item in parsed.blockers)
        )
    selected: list[tuple[str, str, str]] = []
    for event in parsed.events:
        declared = _event_value(event.exact_bytes, "iteration")
        if declared != iteration:
            continue
        summary_raw = _event_value(event.exact_bytes, "summary")
        summary = event.event_type
        if summary_raw:
            try:
                decoded = json.loads(summary_raw)
                if isinstance(decoded, str):
                    summary = _one_line(decoded, "progress summary")
            except json.JSONDecodeError:
                summary = _one_line(summary_raw, "progress summary")
        selected.append((event.occurred_at[:10], event.identity, summary))
    return tuple(selected[-MAX_RECENT_EVENTS:])


def _single_verified_candidate(
    values: Sequence[train.RegisteredCandidate],
) -> tuple[str, str] | None:
    if len(values) != 1:
        return None
    selected = values[0]
    return selected.candidate_ref, selected.candidate_commit


def _iteration_gate(
    *,
    prd_status: str,
    spec_status: str,
    active: bool,
    candidate: tuple[str, str] | None,
    candidate_count: int,
    in_train: bool,
    integrated_oid: str | None,
    open_deviations: int,
) -> tuple[str, str, str]:
    if integrated_oid is not None:
        return (
            "final/integrated evidence",
            "exact integrated result is registered",
            "verify acceptance/final identity and close the writer lifecycle",
        )
    if open_deviations:
        return (
            "deviation disposition",
            f"{open_deviations} material deviation(s) remain open",
            "dispose every material deviation before acceptance",
        )
    if in_train:
        return (
            "latest-main integrated verification",
            "exact candidates are in the serialized merge train",
            "verify and confirm the exact integrated candidate",
        )
    if candidate_count > 1:
        return (
            "candidate selection",
            f"{candidate_count} public verified candidate generations require an exact selection",
            "select one exact candidate identity before entering the merge train",
        )
    if candidate is not None:
        return (
            "integration pending",
            "a public verified feature candidate is available",
            "enter the latest-main merge train",
        )
    if active:
        return (
            "candidate evidence",
            "implementation has an authenticated writer workspace",
            "complete verification and seal a feature candidate",
        )
    if prd_status not in {"已批准", "实施中", "待验收", "已验收"}:
        return ("PRD approval", "product baseline is not approved", "approve the exact PRD baseline")
    if spec_status not in {"已批准", "实施中", "已完成"}:
        return ("SPEC approval", "implementation baseline is not approved", "approve the exact SPEC baseline")
    return (
        "implementation authorization / activation",
        "approved governance is not currently writable",
        "authorize implementation and activate the routed workspace",
    )


def _l1_body(state: IterationProjection, principle_sha256: str) -> bytes:
    dependencies = "、".join(state.depends_on) if state.depends_on else "无"
    recent = ["| 日期 | 事件 | 摘要 |", "|---|---|---|"]
    if state.recent_events:
        recent.extend(
            f"| {date} | `{identity}` | {summary.replace('|', r'\|')} |"
            for date, identity, summary in state.recent_events
        )
    else:
        recent.append("| — | — | 当前语义 progress 中无该迭代的 v2 事件 |")
    lines = [
        "## 状态卡",
        "",
        f"- 迭代：`{state.number}`",
        f"- PRD 状态：`{state.prd_status}`",
        f"- SPEC 状态：`{state.spec_status}`",
        f"- 开放偏差：`{state.open_deviations}`",
        f"- 当前原则：`sha256:{principle_sha256}`",
        f"- 执行拓扑：{state.workspace}",
        f"- depends_on：{dependencies}",
        f"- candidate：{state.candidate_state}",
        f"- integration：{state.integration_state}",
        f"- 下一道门禁：{state.governance_gate}",
        "",
        "## 当前结果",
        "",
        state.result,
        "",
        "## 最近进展",
        "",
        *recent,
        "",
        "## 开放事项与下一步",
        "",
        f"- 开放偏差：`{state.open_deviations}`。",
        f"- 下一步：{state.next_step}。",
    ]
    return "\n".join(lines).encode("utf-8") + b"\n"


def _render_l1(source: bytes, state: IterationProjection, principle_sha256: str) -> bytes:
    body = _l1_body(state, principle_sha256)
    start_count = source.count(L1_START.encode("utf-8"))
    end_count = source.count(L1_END.encode("utf-8"))
    if start_count == end_count == 1:
        preview = governance.preview_managed_markdown(
            source,
            authority_id=f"iteration:{state.number}:{state.source_commit}",
            sections=(
                governance.ManagedSection(
                    "iteration-routing", L1_START, L1_END, body
                ),
            ),
        )
        if not preview.ready or preview.preview is None:
            raise ReadmeAuthorityError(
                f"L1 managed rebuild is blocked for PRD-{state.number}: "
                + "; ".join(item.code for item in preview.blockers)
            )
        return preview.preview
    if start_count or end_count:
        raise ReadmeAuthorityError(
            f"L1 managed markers are partial/duplicated for PRD-{state.number}"
        )
    if not source.startswith((OWNER, b"\xef\xbb\xbf" + OWNER)):
        raise ReadmeAuthorityError(f"L1 lacks Harness ownership for PRD-{state.number}")
    # Without a prior boundary there is no sound way to distinguish legacy
    # derived prose from user-authored sections.  Replacing the whole document
    # would silently discard those bytes, so migration must be explicit.
    raise ReadmeAuthorityError(
        f"L1 lacks bounded routing markers for PRD-{state.number}; "
        "run an explicit byte-preserving README migration before integration"
    )


def _authority_payload(value: DerivedReadmeAuthority) -> dict[str, object]:
    payload = value.as_dict()
    payload.pop("authority_digest", None)
    payload.pop("pushed", None)
    return payload


def validate_derived_readme_authority(value: DerivedReadmeAuthority) -> None:
    if not isinstance(value, DerivedReadmeAuthority):
        raise TypeError("value must be DerivedReadmeAuthority")
    if value.schema_version != AUTHORITY_SCHEMA:
        raise ReadmeAuthorityError("README authority schema is unsupported")
    if value.pushed:
        raise ReadmeAuthorityError("README derivation cannot claim a push")
    paths: set[str] = set()
    for document in value.documents:
        if document.schema_version != DOCUMENT_SCHEMA or document.path in paths:
            raise ReadmeAuthorityError("README authority documents are malformed/duplicated")
        paths.add(document.path)
        if (
            document.content_sha256 != _sha256(document.content)
            or document.size != len(document.content)
        ):
            raise ReadmeAuthorityError(f"derived README bytes changed: {document.path}")
    expected = {"harness/README.md"} | {
        f"harness/iterations/{item.number}/README.md"
        for item in value.iteration_projections
    }
    if paths != expected:
        raise ReadmeAuthorityError("README authority output coverage is incomplete")
    if value.authority_digest != _digest(_authority_payload(value)):
        raise ReadmeAuthorityError("README authority digest changed")


def _derive_with_validation(
    plan: train.IntegrationPreparePlan,
    *,
    semantic_snapshot: reconcile.GovernanceSnapshot,
    governance_context: train.GovernanceContext | None,
    validation: train.AuthorityValidationContext,
) -> DerivedReadmeAuthority:
    repo = validation.repo
    root = repo.root
    if plan.schema_version != train.PREPARE_PLAN_SCHEMA:
        raise ReadmeAuthorityError("integration prepare schema is unsupported")
    if plan.plan_digest != train.integration_prepare_plan_digest(plan) or plan.blockers:
        raise ReadmeAuthorityError("integration prepare plan is blocked or changed")
    if os.path.normcase(plan.project_root) != os.path.normcase(str(root)):
        raise ReadmeAuthorityError("integration plan belongs to another project root")
    if validation.refs.get(plan.main_ref) != plan.target_main:
        raise ReadmeAuthorityError("latest-main changed before README derivation")
    if governance_context is not None:
        if (
            governance_context.operation_id != plan.operation_id
            or governance_context.target_main != plan.target_main
            or governance_context.candidate_digests
            != tuple(item.candidate_evidence.evidence_digest for item in plan.candidates)
        ):
            raise ReadmeAuthorityError("governance context differs from integration plan")

    semantic = semantic_snapshot.as_mapping()
    for path in (reconcile.PRINCIPLE_PATH, reconcile.PROGRESS_PATH, reconcile.L0_PATH):
        if path not in semantic:
            raise ReadmeAuthorityError(f"semantic snapshot lacks {path}")
    principle_sha256 = _sha256(semantic[reconcile.PRINCIPLE_PATH])
    if principle_sha256 != plan.principle_sha256:
        raise ReadmeAuthorityError("semantic principle differs from accepted integration authority")
    progress_raw = semantic[reconcile.PROGRESS_PATH]
    parsed = governance.parse_progress_events(progress_raw, source="README semantic progress")
    if parsed.blockers:
        raise ReadmeAuthorityError(
            "semantic progress is invalid: "
            + "; ".join(f"{item.code}: {item.message}" for item in parsed.blockers)
        )

    try:
        main_entries = core.read_committed_governance_entries(
            repo.git, root, plan.target_main
        )
    except core.HarnessError as exc:
        raise ReadmeAuthorityError(f"latest-main governance cannot be read: {exc}") from exc
    main_tree = train._commit_tree(repo, plan.target_main)
    loaded_candidates, candidate_bindings = _candidate_projection(root, plan, validation)
    committed_blobs: list[BlobBinding] = list(
        _blob_manifest(source=f"main:{plan.target_main}", entries=main_entries)
    )
    candidate_entries: dict[str, Mapping[str, tuple[str, str, bytes]]] = {}
    for candidate in loaded_candidates:
        try:
            base_entries = core.read_committed_governance_entries(
                repo.git, root, candidate.implementation_commit
            )
            entries = core.read_committed_governance_entries(
                repo.git, root, candidate.candidate_commit
            )
        except core.HarnessError as exc:
            raise ReadmeAuthorityError(
                f"candidate governance cannot be read: {candidate.candidate_ref}: {exc}"
            ) from exc
        for path in sorted(set(base_entries) | set(entries)):
            match = ITERATION_PATH_RE.fullmatch(path)
            if match and match.group(1) != candidate.iteration:
                before = base_entries.get(path)
                after = entries.get(path)
                if before != after:
                    raise ReadmeAuthorityError(
                        f"candidate {candidate.candidate_ref} changed PRD-{match.group(1)} derived/authority bundle"
                    )
        candidate_entries[candidate.iteration] = entries
        committed_blobs.extend(
            _blob_manifest(
                source=f"candidate:{candidate.candidate_ref}:{candidate.candidate_commit}",
                entries=entries,
            )
        )

    public_candidates, public_candidate_manifest = _validate_public_candidate_registry(
        root,
        validation.refs,
        principle_sha256=principle_sha256,
    )
    relevant_refs = _relevant_ref_manifest(validation.refs, plan.main_ref)
    (
        existing_integrated,
        integrated_by_iteration,
        final_by_iteration,
    ) = _validate_public_integration_registry(
        root,
        validation.refs,
        current_main_ref=plan.main_ref,
        current_main=plan.target_main,
        principle_sha256=principle_sha256,
    )
    topology_phase, workspace_values, topology = _workspace_projection(repo)
    workspace_by_iteration = {item.iteration: item for item in workspace_values}
    in_train = {item.iteration: item for item in loaded_candidates}

    numbers = {
        match.group(1)
        for path in main_entries
        if (match := ITERATION_PATH_RE.fullmatch(path)) is not None
    } | set(candidate_entries)
    if not numbers or len(numbers) > MAX_ITERATIONS:
        raise ReadmeAuthorityError("README authority has no iterations or exceeds its safe limit")
    ordered_numbers = sorted(numbers, key=lambda value: (int(value), value))
    projections: list[IterationProjection] = []
    sources: dict[str, bytes] = {}
    for number in ordered_numbers:
        selected = candidate_entries.get(number, main_entries)
        source_commit = (
            in_train[number].candidate_commit if number in in_train else plan.target_main
        )
        prefix = f"harness/iterations/{number}"
        paths = {
            "readme": f"{prefix}/README.md",
            "prd": f"{prefix}/prd-{number}.md",
            "spec": f"{prefix}/spec-{number}.md",
            "deviation": f"{prefix}/deviation-{number}.md",
        }
        missing = [path for path in paths.values() if path not in selected]
        if missing:
            raise ReadmeAuthorityError(
                f"PRD-{number} authoritative bundle is incomplete: {', '.join(missing)}"
            )
        prd = selected[paths["prd"]][2]
        spec = selected[paths["spec"]][2]
        deviation = selected[paths["deviation"]][2]
        readme_path = paths["readme"]
        if readme_path not in semantic:
            raise ReadmeAuthorityError(
                f"semantic merged snapshot lacks authoritative README shell: {readme_path}"
            )
        sources[number] = semantic[readme_path]
        prd_status = _field(prd, "状态") or "未知"
        spec_status = _field(spec, "状态") or "未知"
        open_count = _open_deviations(deviation, number)
        lease = workspace_by_iteration.get(number)
        workspace_label = "未激活"
        if lease is not None:
            workspace_label = (
                "Local (primary checkout)"
                if lease.topology == "local"
                else "linked worktree"
            )
        candidate_values = public_candidates.get(number, ())
        train_candidate = in_train.get(number)
        selected_candidate = (
            (train_candidate.candidate_ref, train_candidate.candidate_commit)
            if train_candidate is not None
            else _single_verified_candidate(candidate_values)
        )
        candidate_state = (
            "none"
            if not candidate_values
            else (
                f"verified:{selected_candidate[0].rsplit('/', 1)[-1]}@{selected_candidate[1][:12]}"
                if selected_candidate is not None
                else f"verified-generations:{len(candidate_values)}:selection-required"
            )
        )
        final_authority = final_by_iteration.get(number)
        prior_integrated = integrated_by_iteration.get(number, ())
        integrated_oid = (
            final_authority["commit"]
            if final_authority is not None
            else prior_integrated[-1]["commit"]
            if prior_integrated
            else None
        )
        if number in in_train:
            integration_state = (
                f"integrated-candidate:{plan.generation}:plan-"
                f"{plan.plan_digest[:12]}"
            )
        elif final_authority is not None:
            integration_state = f"accepted@{final_authority['commit'][:12]}"
        elif prior_integrated:
            integration_state = (
                f"verified-integrated-evidence:{len(prior_integrated)}@"
                f"{prior_integrated[-1]['commit'][:12]}"
            )
        else:
            integration_state = "not-integrated"
        gate, result, next_step = _iteration_gate(
            prd_status=prd_status,
            spec_status=spec_status,
            active=lease is not None,
            candidate=selected_candidate,
            candidate_count=len(candidate_values),
            in_train=number in in_train,
            integrated_oid=integrated_oid,
            open_deviations=open_count,
        )
        projections.append(
            IterationProjection(
                number=number,
                title=_title(prd, number),
                prd_status=prd_status,
                spec_status=spec_status,
                open_deviations=open_count,
                depends_on=_ids(prd, "depends_on", "\u4f9d\u8d56 PRD"),
                workspace=workspace_label,
                governance_gate=gate,
                candidate_state=candidate_state,
                integration_state=integration_state,
                result=result,
                next_step=next_step,
                recent_events=_recent_events(progress_raw, number),
                source_commit=source_commit,
            )
        )

    semantic_digest = _snapshot_digest(semantic_snapshot)
    operational_payload = {
        "topology": topology,
        "workspaces": [item.as_dict() for item in workspace_values],
    }
    input_payload = {
        "schema_version": INPUT_SCHEMA,
        "operation_id": plan.operation_id,
        "train_plan_digest": plan.plan_digest,
        "target_main": plan.target_main,
        "main_tree": main_tree,
        "pre_governance_tree": (
            governance_context.pre_governance_tree if governance_context is not None else None
        ),
        "principle_sha256": principle_sha256,
        "progress_sha256": _sha256(progress_raw),
        "semantic_snapshot_digest": semantic_digest,
        "committed_blobs": [item.as_dict() for item in committed_blobs],
        "candidate_bindings": [item.as_dict() for item in candidate_bindings],
        "public_candidates": list(public_candidate_manifest),
        "relevant_refs": list(relevant_refs),
        "existing_integrated": list(existing_integrated),
        "operational_projection": operational_payload,
        "iterations": [item.as_dict() for item in projections],
    }
    input_digest = _digest(input_payload)
    authority_id = f"readme-input:{input_digest}"
    routing_states = tuple(
        governance.IterationRoutingState(
            number=item.number,
            title=item.title,
            prd_status=item.prd_status,
            spec_status=item.spec_status,
            open_deviations=item.open_deviations,
            workspace=item.workspace,
            governance_gate=item.governance_gate,
            candidate_state=item.candidate_state,
            integration_state=item.integration_state,
            result=item.result,
            next_step=item.next_step,
            depends_on=item.depends_on,
        )
        for item in projections
    )
    current = next(
        (number for number in plan.dependency_order if number in numbers),
        projections[0].number,
    )
    root_authority = governance.RootRoutingAuthority(
        authority_id=authority_id,
        current_iteration=current,
        global_gate="latest-main integrated verification",
        next_step="verify and confirm the exact integrated candidate",
        iterations=routing_states,
    )
    root_preview = governance.preview_root_readme(
        semantic[reconcile.L0_PATH], authority=root_authority
    )
    if not root_preview.ready or root_preview.preview is None:
        raise ReadmeAuthorityError(
            "L0 authoritative rebuild is blocked: "
            + "; ".join(f"{item.code}: {item.message}" for item in root_preview.blockers)
        )
    documents: list[DerivedReadmeDocument] = [
        DerivedReadmeDocument(
            DOCUMENT_SCHEMA,
            reconcile.L0_PATH,
            root_preview.preview,
            _sha256(semantic[reconcile.L0_PATH]),
            _sha256(root_preview.preview),
            len(root_preview.preview),
        )
    ]
    for projection in projections:
        source = sources[projection.number]
        content = _render_l1(source, projection, principle_sha256)
        documents.append(
            DerivedReadmeDocument(
                DOCUMENT_SCHEMA,
                f"harness/iterations/{projection.number}/README.md",
                content,
                _sha256(source),
                _sha256(content),
                len(content),
            )
        )

    provisional = DerivedReadmeAuthority(
        schema_version=AUTHORITY_SCHEMA,
        project_root=str(root),
        operation_id=plan.operation_id,
        train_plan_digest=plan.plan_digest,
        input_digest=input_digest,
        authority_id=authority_id,
        target_main=plan.target_main,
        main_tree=main_tree,
        principle_sha256=principle_sha256,
        progress_sha256=_sha256(progress_raw),
        semantic_snapshot_digest=semantic_digest,
        relevant_refs_digest=_digest(relevant_refs),
        operational_projection_digest=_digest(operational_payload),
        topology_phase=topology_phase,
        committed_blobs=tuple(committed_blobs),
        candidate_bindings=candidate_bindings,
        workspace_projections=workspace_values,
        iteration_projections=tuple(projections),
        documents=tuple(documents),
        authority_digest="0" * 64,
    )
    result = replace(provisional, authority_digest=_digest(_authority_payload(provisional)))
    validate_derived_readme_authority(result)
    return result


def derive_train_readme_authority(
    train_plan: train.IntegrationPreparePlan,
    *,
    semantic_snapshot: reconcile.GovernanceSnapshot,
    governance_context: train.GovernanceContext | None = None,
) -> DerivedReadmeAuthority:
    """Derive exact L0/L1 bytes from independently authenticated authority.

    ``semantic_snapshot`` must be the adapter's post-union principle/progress
    snapshot.  It contributes facts, never caller-selected README output.
    The authority snapshot is always opened and closed inside this call.  A
    caller cannot inject or reuse a context after its end-of-scope drift check.
    """

    if not isinstance(train_plan, train.IntegrationPreparePlan):
        raise TypeError("train_plan must be IntegrationPreparePlan")
    if not isinstance(semantic_snapshot, reconcile.GovernanceSnapshot):
        raise TypeError("semantic_snapshot must be GovernanceSnapshot")
    if governance_context is not None and not isinstance(
        governance_context, train.GovernanceContext
    ):
        raise TypeError("governance_context must be GovernanceContext")
    root = Path(train_plan.project_root).resolve()
    try:
        with train.authority_validation_context(root) as validation:
            validation.assert_unchanged()
            result = _derive_with_validation(
                train_plan,
                semantic_snapshot=semantic_snapshot,
                governance_context=governance_context,
                validation=validation,
            )
            validation.assert_unchanged()
            return result
    except train.TrainError as exc:
        raise ReadmeAuthorityError(f"README authority snapshot failed: {exc}") from exc


def documents_by_path(value: DerivedReadmeAuthority) -> dict[str, bytes]:
    validate_derived_readme_authority(value)
    return {item.path: item.content for item in value.documents}


__all__ = [
    "AUTHORITY_SCHEMA",
    "DOCUMENT_SCHEMA",
    "DerivedReadmeAuthority",
    "DerivedReadmeDocument",
    "IterationProjection",
    "L1_END",
    "L1_START",
    "ReadmeAuthorityError",
    "WorkspaceProjection",
    "derive_train_readme_authority",
    "documents_by_path",
    "validate_derived_readme_authority",
]
