#!/usr/bin/env python3
"""Authoritative v2 request routing and lifecycle coordination.

The coordinator derives governance, authorization, dependency, allocation, and
workspace facts from repository authority. It plans only: no files, refs,
indexes, leases, branches, or worktrees are mutated here. Facts that cannot be
authenticated by the currently persisted schemas fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

try:
    from . import harness_workspace as workspace
    from . import harness_train as train
    from . import project_harness as core
    from .harness_decision import AuthorizationState, DecisionInput, RiskVector, classify
except ImportError:  # pragma: no cover - direct script execution
    import harness_workspace as workspace
    import harness_train as train
    import project_harness as core
    from harness_decision import AuthorizationState, DecisionInput, RiskVector, classify


SCHEMA_V1 = "harness-lite.coordinator-plan/v1"
ITERATION_RE = re.compile(r"[0-9]{3,}")
OID_RE = re.compile(r"[0-9a-f]{40,64}")
OP_RE = re.compile(r"OP-[0-9a-f]{32}")
STATUS_LINE = re.compile(
    r"^- (?P<label>[^：:\r\n]+)[：:]\s*(?:`(?P<quoted>[^`\r\n]+)`|(?P<plain>[^\r\n]+))\s*$",
    re.MULTILINE,
)
PRD_STATUSES = {"草案", "待批准", "已批准", "实施中", "待验收", "已验收", "已取代", "已取消"}
SPEC_STATUSES = {"受 PRD 阻塞", "草案", "待批准", "已批准", "实施中", "已完成", "已取代", "已取消"}
GIT_ENVIRONMENT_OVERRIDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


class CoordinatorError(RuntimeError):
    """Raised when authority cannot be derived without trusting caller claims."""


@dataclass(frozen=True)
class IterationAuthority:
    iteration: str
    title: str
    prd_status: str
    spec_status: str
    prd_approved: bool
    spec_approved: bool
    implementation_authorized: bool
    governance_ref: str
    governance_commit: str
    governance_tree: str
    principle_sha256: str
    base_ref: str | None
    base_commit: str | None
    source_base_ref: str | None
    depends_on: tuple[str, ...]
    conflicts_with: tuple[str, ...]
    candidate_refs: tuple[str, ...]
    candidate_objects: tuple[tuple[str, str], ...]
    verified_candidate_refs: tuple[str, ...]
    stable_candidate_bindings: tuple[dict[str, str], ...]
    integrated: bool
    integrated_object: str | None
    active_writer: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class CoordinatorPlan:
    schema_version: str
    command: str
    action_level: str
    pushed: bool
    operation_id: str
    project_root: str
    head: str
    branch_ref: str | None
    authority: IterationAuthority | None
    decision: dict[str, object]
    planned_steps: tuple[dict[str, object], ...]
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    plan_digest: str
    phase: str
    next_gate: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git(root: Path, arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("git")
    if not executable:
        raise CoordinatorError("Git is required")
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
        raise CoordinatorError(message or f"Git exited with {result.returncode}")
    return result


def _text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="strict").strip()


def resolve_root(value: str | Path) -> Path:
    supplied = Path(value).expanduser().resolve()
    if not supplied.is_dir():
        raise CoordinatorError(f"project root is not an existing directory: {supplied}")
    result = _git(supplied, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise CoordinatorError("project root is not a Git worktree")
    actual = Path(_text(result)).resolve()
    if os.path.normcase(str(actual)) != os.path.normcase(str(supplied)):
        raise CoordinatorError(f"project root must name the exact worktree root: {actual}")
    return actual


def _read_utf8(path: Path, maximum: int = 2 * 1024 * 1024) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CoordinatorError(f"cannot read authority file {path}: {exc}") from exc
    if len(raw) > maximum:
        raise CoordinatorError(f"authority file exceeds safe size limit: {path}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CoordinatorError(f"authority file is not UTF-8: {path}") from exc
    if not core.has_owner_marker(text):
        raise CoordinatorError(f"authority file lacks a Harness ownership marker: {path}")
    return text


def _value(text: str, label: str) -> str | None:
    clean = core.strip_html_comments(text)
    values: list[str] = []
    for match in STATUS_LINE.finditer(clean):
        if match.group("label").strip() == label:
            values.append((match.group("quoted") or match.group("plain")).strip())
    if len(values) > 1:
        raise CoordinatorError(f"authority field is duplicated: {label}")
    return values[0] if values else None


def _ids_from_field(text: str, label: str) -> tuple[str, ...]:
    value = _value(text, label)
    if not value or value.strip().casefold() in {"无", "none", "n/a", "尚无"}:
        return ()
    return tuple(dict.fromkeys(re.findall(r"(?:PRD-)?([0-9]{3,})", value)))


def _refs(root: Path) -> dict[str, str]:
    result = _git(root, ["for-each-ref", "--format=%(refname)%00%(objectname)"])
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        try:
            raw_name, raw_oid = line.split(b"\0", 1)
            name = raw_name.decode("utf-8")
            oid = raw_oid.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise CoordinatorError("Git returned malformed refs") from exc
        if not OID_RE.fullmatch(oid):
            raise CoordinatorError(f"Git returned malformed object ID for {name}")
        refs[name] = oid
    return refs


def _common_dir(root: Path) -> Path:
    raw = Path(_text(_git(root, ["rev-parse", "--git-common-dir"])))
    return (raw if raw.is_absolute() else root / raw).resolve()


def _workspace_lease_snapshots(root: Path) -> dict[str, dict[str, object]]:
    executable = shutil.which("git")
    if not executable:
        raise CoordinatorError("Git is required")
    context = workspace.RepositoryContext(
        git=executable,
        project_root=root,
        common_dir=_common_dir(root),
    )
    try:
        leases, blockers = workspace.load_active_leases(context)
    except workspace.WorkspaceError as exc:
        raise CoordinatorError(f"workspace lease registry is invalid: {exc}") from exc
    if blockers:
        detail = "; ".join(f"{item.code}: {item.message}" for item in blockers)
        raise CoordinatorError("workspace lease registry is invalid: " + detail)
    return {str(item["iteration"]): item for item in leases}


def _active_workspace_iterations(root: Path) -> set[str]:
    return set(_workspace_lease_snapshots(root))


def _object_type(root: Path, oid: str) -> str | None:
    result = _git(root, ["cat-file", "-t", oid], check=False)
    return _text(result) if result.returncode == 0 else None


def _generation_key(reference: str) -> tuple[int, int | str, str]:
    generation = reference.rsplit("/", 1)[-1]
    numeric = re.fullmatch(r"g([0-9]+)", generation)
    if numeric:
        return (0, int(numeric.group(1)), generation)
    return (1, generation, generation)


def _v2_base_identity(
    root: Path, number: str, refs: Mapping[str, str]
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    blockers: list[str] = []
    allocation_ref = f"refs/project-harness/v2/allocations/{number}"
    base_ref = f"refs/project-harness/v2/iterations/{number}/base"
    allocation_oid = refs.get(allocation_ref)
    base_commit = refs.get(base_ref)
    if allocation_oid is None and base_commit is None:
        return None, None, None, ()
    if allocation_oid is None or base_commit is None:
        return base_ref if base_commit else None, base_commit, None, ("partial-v2-identity",)
    if _object_type(root, allocation_oid) != "blob" or _object_type(root, base_commit) != "commit":
        return base_ref, base_commit, None, ("v2-identity-object-type-invalid",)
    git = shutil.which("git") or "git"
    try:
        metadata = core.read_allocation_metadata(git, root, allocation_oid)
    except core.HarnessError as exc:
        return base_ref, base_commit, None, (f"v2-allocation-metadata-invalid:{exc}",)
    if metadata.get("iteration") != number or metadata.get("base_commit") != base_commit:
        blockers.append("v2-allocation-base-mismatch")
    owner = str(metadata.get("operation_id"))
    try:
        journal, _ = core.load_operation_journal(_common_dir(root), owner)
    except core.HarnessError as exc:
        blockers.append(f"v2-reservation-owner-invalid:{exc}")
    else:
        if (
            journal.phase != "READY"
            or journal.plan_digest != metadata.get("plan_digest")
            or journal.iteration != number
            or journal.allocation_object != allocation_oid
            or journal.base_commit != base_commit
            or journal.base_branch != metadata.get("base_branch")
        ):
            blockers.append("v2-reservation-owner-mismatch")
    return base_ref, base_commit, str(metadata.get("base_branch")), tuple(blockers)


def _candidate_observations(
    root: Path,
    number: str,
    refs: Mapping[str, str],
    *,
    principle_sha256: str,
) -> tuple[
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[dict[str, str], ...],
]:
    del refs  # the dependency registry is re-read atomically enough for CAS-style drift detection below
    candidate_prefix = f"refs/project-harness/v2/iterations/{number}/candidates/"
    evidence_prefix = f"refs/project-harness/v2/iterations/{number}/candidate-evidence/"
    observations: list[tuple[str, str]] = []
    verified: list[str] = []
    bindings: list[dict[str, str]] = []
    blockers: list[str] = []
    executable = shutil.which("git")
    if not executable:
        raise CoordinatorError("Git is required")
    context = workspace.RepositoryContext(git=executable, project_root=root, common_dir=_common_dir(root))
    registry = workspace.dependency_registry_snapshot(context, number)
    registry_digest = str(registry["digest"])
    registry_entries = registry.get("refs")
    if not isinstance(registry_entries, list):
        raise CoordinatorError(f"candidate registry for PRD-{number} is unreadable")
    registry_refs = {
        str(item["ref"]): str(item["oid"])
        for item in registry_entries
        if isinstance(item, Mapping) and set(item) == {"ref", "oid"}
    }
    if len(registry_refs) != len(registry_entries):
        raise CoordinatorError(f"candidate registry for PRD-{number} contains malformed or duplicate refs")
    candidate_names = tuple(
        sorted((name for name in registry_refs if name.startswith(candidate_prefix)), key=_generation_key)
    )
    generations = {
        name[len(candidate_prefix) :]
        for name in candidate_names
    } | {
        name[len(evidence_prefix) :]
        for name in registry_refs
        if name.startswith(evidence_prefix)
    }
    ordered_generations = tuple(
        reference.rsplit("/", 1)[-1]
        for reference in sorted(
            (f"{candidate_prefix}{generation}" for generation in generations),
            key=_generation_key,
        )
    )
    for generation in ordered_generations:
        reference = f"{candidate_prefix}{generation}"
        oid = registry_refs.get(reference)
        if oid is not None:
            observations.append((reference, oid))
        registered, candidate_blockers = train.load_registered_candidate(
            root,
            iteration=number,
            generation=generation,
            current_principle_sha256=principle_sha256,
        )
        blockers.extend(
            f"candidate-registration:{generation}:{item.code}:{item.message}"
            for item in candidate_blockers
        )
        if registered is None:
            blockers.append(f"candidate-stable-registration-missing:{generation}")
            continue
        verified.append(registered.candidate_ref)
        bindings.append(
            {
                "schema_version": workspace.DEPENDENCY_BINDING_SCHEMA,
                "iteration": registered.iteration,
                "generation": registered.generation,
                "candidate_ref": registered.candidate_ref,
                "candidate_commit": registered.candidate_commit,
                "candidate_tree": registered.candidate_tree,
                "candidate_evidence_ref": registered.candidate_evidence_ref,
                "candidate_evidence_blob": registered.candidate_evidence_blob,
                "candidate_evidence_digest": registered.candidate_evidence.evidence_digest,
                "candidate_evidence_metadata_digest": registered.candidate_evidence_metadata_digest,
                "registration_digest": registered.registration_digest,
                "registry_digest": registry_digest,
            }
        )
    final_registry = workspace.dependency_registry_snapshot(context, number)
    if final_registry["digest"] != registry_digest:
        blockers.append("candidate-registry-changed-during-read")
        verified.clear()
        bindings.clear()
    return candidate_names, tuple(observations), tuple(blockers), tuple(verified), tuple(bindings)


def derive_iteration_authority(root: Path, iteration: str) -> IterationAuthority:
    """Read approvals/dependencies from canonical files and identities from Git."""

    number = iteration.strip()
    if not ITERATION_RE.fullmatch(number) or number != f"{int(number):03d}":
        raise CoordinatorError("iteration must be a canonical NNN identity")
    directory = root / "harness" / "iterations" / number
    expected = {
        "readme": directory / "README.md",
        "prd": directory / f"prd-{number}.md",
        "spec": directory / f"spec-{number}.md",
        "deviation": directory / f"deviation-{number}.md",
    }
    missing = [str(path) for path in expected.values() if not path.is_file()]
    if missing:
        raise CoordinatorError("iteration bundle is incomplete: " + ", ".join(missing))
    for path in expected.values():
        _read_utf8(path)
    prd = _read_utf8(expected["prd"])
    spec = _read_utf8(expected["spec"])
    prd_status = _value(prd, "状态") or ""
    spec_status = _value(spec, "状态") or ""
    blockers: list[str] = []
    if prd_status not in PRD_STATUSES:
        blockers.append("invalid-prd-status")
    if spec_status not in SPEC_STATUSES:
        blockers.append("invalid-spec-status")
    prd_approved = prd_status in {"已批准", "实施中", "待验收", "已验收"} and core.explicit_user_baseline_approval(
        _value(prd, "批准依据"), f"PRD-{number}"
    )
    spec_approved = spec_status in {"已批准", "实施中", "已完成"} and core.explicit_user_baseline_approval(
        _value(spec, "批准依据"), f"SPEC-{number}"
    )
    implementation_authorized = core.explicit_user_implementation_authorization(_value(spec, "实施授权"))
    git = shutil.which("git") or "git"
    try:
        governance_ref, governance_commit, snapshot = core.committed_governance_snapshot(
            git, root, "refs/heads/main"
        )
    except core.HarnessError as exc:
        raise CoordinatorError(f"canonical main governance is invalid: {exc}") from exc
    refs = _refs(root)
    base_ref, base_commit, source_base_ref, v2_blockers = _v2_base_identity(root, number, refs)
    blockers.extend(v2_blockers)
    if base_ref is None:
        legacy_prefix = f"refs/project-harness/iterations/{number}/base/"
        legacy = [(name, oid) for name, oid in refs.items() if name.startswith(legacy_prefix)]
        if len(legacy) == 1 and _object_type(root, legacy[0][1]) == "commit":
            base_ref, base_commit = legacy[0]
            source_base_ref = legacy[0][0][len(legacy_prefix) :]
        else:
            blockers.append("immutable-base-missing-or-ambiguous")
    (
        candidate_refs,
        candidate_objects,
        candidate_blockers,
        verified_candidate_refs,
        stable_candidate_bindings,
    ) = _candidate_observations(
        root,
        number,
        refs,
        principle_sha256=str(snapshot["principle_sha256"]),
    )
    blockers.extend(candidate_blockers)
    integrated_ref = f"refs/project-harness/v2/iterations/{number}/integrated"
    integrated_object = refs.get(integrated_ref)
    integrated = False
    if integrated_object is not None:
        if _object_type(root, integrated_object) != "commit":
            blockers.append("integrated-object-not-commit")
        else:
            blockers.append("integrated-evidence-envelope-missing")
    title_match = re.search(rf"^#\s+PRD-{re.escape(number)}[：:]\s*(.+?)\s*$", prd, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else f"PRD-{number}"
    active = _active_workspace_iterations(root)
    return IterationAuthority(
        iteration=number,
        title=title,
        prd_status=prd_status,
        spec_status=spec_status,
        prd_approved=prd_approved,
        spec_approved=spec_approved,
        implementation_authorized=implementation_authorized,
        governance_ref=governance_ref,
        governance_commit=governance_commit,
        governance_tree=str(snapshot["tree"]),
        principle_sha256=str(snapshot["principle_sha256"]),
        base_ref=base_ref,
        base_commit=base_commit,
        source_base_ref=source_base_ref,
        depends_on=_ids_from_field(prd, "依赖 PRD") or _ids_from_field(prd, "depends_on"),
        conflicts_with=_ids_from_field(prd, "冲突 PRD") or _ids_from_field(prd, "conflicts_with"),
        candidate_refs=candidate_refs,
        candidate_objects=candidate_objects,
        verified_candidate_refs=verified_candidate_refs,
        stable_candidate_bindings=stable_candidate_bindings,
        integrated=integrated,
        integrated_object=integrated_object,
        active_writer=number in active,
        blockers=tuple(blockers),
    )


def _risk(value: Mapping[str, object]) -> RiskVector:
    allowed = set(RiskVector.__dataclass_fields__)
    unknown = set(value) - allowed
    if unknown:
        raise CoordinatorError("unknown risk fields: " + ", ".join(sorted(unknown)))
    normalized = dict(value)
    if "unknowns" in normalized:
        raw = normalized["unknowns"]
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise CoordinatorError("risk.unknowns must be an array of strings")
        normalized["unknowns"] = tuple(raw)
    return RiskVector(**normalized)


def _declared_dependencies(root: Path, iteration: str) -> tuple[str, ...]:
    number = iteration.strip()
    path = root / "harness" / "iterations" / number / f"prd-{number}.md"
    if not path.is_file():
        raise CoordinatorError(f"declared dependency iteration is missing: PRD-{number}")
    prd = _read_utf8(path)
    return _ids_from_field(prd, "依赖 PRD") or _ids_from_field(prd, "depends_on")


def _dependency_dag_blockers(root: Path, iteration: str) -> tuple[str, ...]:
    blockers: list[str] = []
    visited: set[str] = set()
    visiting: list[str] = []

    def visit(number: str) -> None:
        if number in visiting:
            cycle = visiting[visiting.index(number) :] + [number]
            blockers.append("dependency-cycle:" + "->".join(cycle))
            return
        if number in visited:
            return
        visiting.append(number)
        try:
            dependencies = _declared_dependencies(root, number)
        except CoordinatorError as exc:
            blockers.append(f"dependency-authority-missing:{number}:{exc}")
            visiting.pop()
            visited.add(number)
            return
        for dependency in dependencies:
            visit(dependency)
        visiting.pop()
        visited.add(number)

    visit(iteration)
    return tuple(dict.fromkeys(blockers))


def plan_route(
    project_root: str | Path,
    *,
    iteration: str,
    read_only: bool,
    ambiguities: tuple[str, ...] = (),
    risk: Mapping[str, object] | None = None,
    operation_id: str | None = None,
) -> CoordinatorPlan:
    root = resolve_root(project_root)
    operation = operation_id or f"OP-{uuid.uuid4().hex}"
    if not OP_RE.fullmatch(operation):
        raise CoordinatorError("operation ID must use canonical OP- plus 32 lowercase hexadecimal characters")
    authority = derive_iteration_authority(root, iteration)
    leases = _workspace_lease_snapshots(root)
    active = set(leases)
    blockers = list(authority.blockers)
    blockers.extend(_dependency_dag_blockers(root, authority.iteration))
    dependency_candidates: dict[str, tuple[dict[str, str], ...]] = {}
    selected_dependency_bindings: list[dict[str, str]] = []
    for dependency in authority.depends_on:
        dep = derive_iteration_authority(root, dependency)
        dependency_candidates[dependency] = dep.stable_candidate_bindings
        blockers.extend(f"dependency-authority:{dependency}:{reason}" for reason in dep.blockers)
        if not dep.stable_candidate_bindings:
            blockers.append(f"dependency-stable-candidate-missing:{dependency}")
        else:
            selected_dependency_bindings.append(dict(dep.stable_candidate_bindings[-1]))
    if selected_dependency_bindings:
        executable = shutil.which("git")
        if not executable:
            raise CoordinatorError("Git is required")
        dependency_context = workspace.RepositoryContext(
            git=executable,
            project_root=root,
            common_dir=_common_dir(root),
        )
        order_blockers = workspace.dependency_order_blockers(
            dependency_context,
            selected_dependency_bindings,
        )
        blockers.extend(f"dependency-order:{item.code}:{item.message}" for item in order_blockers)
    current_lease = leases.get(authority.iteration)
    if current_lease is not None:
        current_bindings = current_lease.get("dependency_bindings")
        if not isinstance(current_bindings, list) or current_bindings != selected_dependency_bindings:
            blockers.append("dependency-baseline-stale:explicit-refresh-required")
    for conflict in authority.conflicts_with:
        if conflict in active:
            blockers.append(f"declared-conflict-active:{conflict}")
    decision = classify(
        DecisionInput(
            read_only=read_only,
            ambiguities=tuple(ambiguities),
            risk=_risk(risk or {}),
            active_writers=len(active - {authority.iteration}),
            depends_on=authority.depends_on,
            conflicts_with=authority.conflicts_with,
            authorization=AuthorizationState(
                prd_approved=authority.prd_approved,
                spec_approved=authority.spec_approved,
                implementation_authorized=authority.implementation_authorized,
                integration_authorized=bool(authority.verified_candidate_refs),
                finally_accepted=authority.integrated,
            ),
        )
    )
    blockers.extend(decision.blocking_reasons)
    if not read_only and not authority.prd_approved:
        blockers.append("prd-not-approved")
    if not read_only and not authority.spec_approved:
        blockers.append("spec-not-approved")
    if not read_only and not authority.implementation_authorized:
        blockers.append("implementation-not-authorized")
    topology = decision.execution_topology
    steps: list[dict[str, object]] = [
        {"step": "authority-preflight", "writes": False},
        {"step": "three-axis-classification", "writes": False},
    ]
    if not read_only and not blockers:
        steps.extend(
            (
                {
                    "step": "workspace-plan",
                    "topology": topology,
                    "base_ref": authority.base_ref,
                    "base_commit": authority.base_commit,
                    "source_base_ref": authority.source_base_ref,
                    "implementation_ref": (
                        selected_dependency_bindings[-1]["candidate_ref"]
                        if selected_dependency_bindings
                        else "refs/heads/main"
                    ),
                    "implementation_commit": (
                        selected_dependency_bindings[-1]["candidate_commit"]
                        if selected_dependency_bindings
                        else authority.governance_commit
                    ),
                    "dependency_bindings": selected_dependency_bindings,
                    "dependency_bindings_digest": workspace.dependency_bindings_digest(
                        selected_dependency_bindings
                    ),
                    "writes": False,
                },
                {
                    "step": "workspace-apply",
                    "writes": True,
                    "action_level": "notify" if "worktree" in topology else "silent",
                },
            )
        )
    head = _text(_git(root, ["rev-parse", "--verify", "HEAD^{commit}"]))
    branch_result = _git(root, ["symbolic-ref", "-q", "HEAD"], check=False)
    payload = {
        "schema_version": SCHEMA_V1,
        "operation_id": operation,
        "project_root": str(root),
        "head": head,
        "branch_ref": _text(branch_result) if branch_result.returncode == 0 else None,
        "authority": asdict(authority),
        "decision": decision.as_dict(),
        "dependency_candidates": dependency_candidates,
        "selected_dependency_bindings": selected_dependency_bindings,
        "planned_steps": steps,
        "blocking_reasons": sorted(set(blockers)),
    }
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    blocked = bool(blockers)
    return CoordinatorPlan(
        schema_version=SCHEMA_V1,
        command="route-iteration",
        action_level="silent",
        pushed=False,
        operation_id=operation,
        project_root=str(root),
        head=head,
        branch_ref=payload["branch_ref"],
        authority=authority,
        decision={**decision.as_dict(), "effective_execution_topology": topology},
        planned_steps=tuple(steps),
        blocking_reasons=tuple(sorted(set(blockers))),
        warnings=(),
        plan_digest=digest,
        phase="blocked" if blocked else "planned",
        next_gate=decision.authorization_gate if blocked else "review-and-apply-workspace-plan",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--iteration", required=True)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--ambiguity", action="append", default=[])
    parser.add_argument("--risk-json", default="{}")
    parser.add_argument("--operation-id")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        risk = json.loads(args.risk_json)
        if not isinstance(risk, dict):
            raise CoordinatorError("risk JSON must be an object")
        plan = plan_route(
            args.project_root,
            iteration=args.iteration,
            read_only=args.read_only,
            ambiguities=tuple(args.ambiguity),
            risk=risk,
            operation_id=args.operation_id,
        )
        payload = plan.as_dict()
    except (CoordinatorError, json.JSONDecodeError, ValueError) as exc:
        payload = {
            "schema_version": SCHEMA_V1,
            "command": "route-iteration",
            "action_level": "silent",
            "pushed": False,
            "phase": "blocked",
            "blocking_reasons": [str(exc)],
            "next_gate": "fix-authority-or-input",
        }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(f"ROUTE {payload.get('phase')} -> {payload.get('next_gate')}")
    return 0 if payload.get("phase") == "planned" else 2


if __name__ == "__main__":
    raise SystemExit(main())
