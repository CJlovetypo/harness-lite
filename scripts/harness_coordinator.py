#!/usr/bin/env python3
"""Authoritative v2 request routing and lifecycle coordination.

The coordinator is the only public layer allowed to convert repository facts
into low-level Harness operations.  Its first slice closes the unsafe gap where
callers could reserve an identity and then manually assert approval booleans:
it derives governance, authorization, dependency, allocation, and workspace
facts from the target project and emits one versioned route/start plan.

This module currently plans only.  Mutating adapters continue to require their
own accepted digest/journal and are invoked in later phases by an authorized
coordinator.  A plan never writes files, refs, indexes, leases, or worktrees.
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
    from .harness_decision import AuthorizationState, DecisionInput, RiskVector, classify
except ImportError:  # pragma: no cover - direct script execution
    from harness_decision import AuthorizationState, DecisionInput, RiskVector, classify


SCHEMA_V1 = "harness-lite.coordinator-plan/v1"
ITERATION_RE = re.compile(r"[0-9]{3,}")
OID_RE = re.compile(r"[0-9a-f]{40,64}")
STATUS_LINE = re.compile(
    r"^- (?P<label>[^：:\r\n]+)[：:]\s*(?:`(?P<quoted>[^`\r\n]+)`|(?P<plain>[^\r\n]+))\s*$",
    re.MULTILINE,
)
PRD_STATUSES = {"草案", "待批准", "已批准", "实施中", "待验收", "已验收", "已取代", "已取消"}
SPEC_STATUSES = {"受 PRD 阻塞", "草案", "待批准", "已批准", "实施中", "已完成", "已取代", "已取消"}


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
    principle_sha256: str
    base_ref: str | None
    base_commit: str | None
    depends_on: tuple[str, ...]
    conflicts_with: tuple[str, ...]
    candidate_refs: tuple[str, ...]
    integrated: bool
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
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CoordinatorError(f"authority file is not UTF-8: {path}") from exc


def _value(text: str, label: str) -> str | None:
    for match in STATUS_LINE.finditer(text):
        if match.group("label").strip() == label:
            value = match.group("quoted") or match.group("plain")
            return value.strip()
    return None


def _truthy_authorization(value: str | None) -> bool:
    if value is None:
        return False
    lowered = value.strip().lower()
    negative = ("待批准", "未批准", "未授权", "尚未", "无", "n/a", "todo")
    return bool(lowered and not any(fragment in lowered for fragment in negative))


def _ids_from_field(text: str, label: str) -> tuple[str, ...]:
    value = _value(text, label)
    if not value:
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


def _active_workspace_iterations(root: Path) -> set[str]:
    directory = _common_dir(root) / "project-harness" / "workspace" / "v1" / "leases" / "iterations"
    if not directory.is_dir():
        return set()
    result: set[str] = set()
    for path in directory.glob("*.json"):
        if not ITERATION_RE.fullmatch(path.stem):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoordinatorError(f"workspace lease is corrupt: {path}: {exc}") from exc
        if not isinstance(value, Mapping) or value.get("iteration") != path.stem:
            raise CoordinatorError(f"workspace lease identity mismatch: {path}")
        result.add(path.stem)
    return result


def _workspace_lease_snapshots(root: Path) -> dict[str, dict[str, object]]:
    directory = _common_dir(root) / "project-harness" / "workspace" / "v1" / "leases" / "iterations"
    if not directory.is_dir():
        return {}
    result: dict[str, dict[str, object]] = {}
    for path in directory.glob("*.json"):
        if not ITERATION_RE.fullmatch(path.stem):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoordinatorError(f"workspace lease is corrupt: {path}: {exc}") from exc
        if not isinstance(value, dict) or value.get("iteration") != path.stem:
            raise CoordinatorError(f"workspace lease identity mismatch: {path}")
        result[path.stem] = value
    return result


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
    prd = _read_utf8(expected["prd"])
    spec = _read_utf8(expected["spec"])
    principle = (root / "harness" / "principle.md").read_bytes()
    prd_status = _value(prd, "状态") or ""
    spec_status = _value(spec, "状态") or ""
    blockers: list[str] = []
    if prd_status not in PRD_STATUSES:
        blockers.append("invalid-prd-status")
    if spec_status not in SPEC_STATUSES:
        blockers.append("invalid-spec-status")
    prd_approved = prd_status in {"已批准", "实施中", "待验收", "已验收"} and _truthy_authorization(
        _value(prd, "批准依据")
    )
    spec_approved = spec_status in {"已批准", "实施中", "已完成"} and _truthy_authorization(
        _value(spec, "批准依据")
    )
    implementation_authorized = _truthy_authorization(_value(spec, "实施授权"))
    refs = _refs(root)
    v2_base = f"refs/project-harness/v2/iterations/{number}/base"
    legacy_prefix = f"refs/project-harness/iterations/{number}/base/"
    legacy = [(name, oid) for name, oid in refs.items() if name.startswith(legacy_prefix)]
    base_ref: str | None = None
    base_commit: str | None = None
    if v2_base in refs:
        base_ref, base_commit = v2_base, refs[v2_base]
    elif len(legacy) == 1:
        base_ref, base_commit = legacy[0]
    else:
        blockers.append("immutable-base-missing-or-ambiguous")
    candidate_prefix = f"refs/project-harness/v2/iterations/{number}/candidates/"
    candidates = tuple(sorted(name for name in refs if name.startswith(candidate_prefix)))
    integrated = f"refs/project-harness/v2/iterations/{number}/integrated" in refs
    title_match = re.search(rf"^#\s+PRD-{re.escape(number)}[：:]\s*(.+?)\s*$", prd, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else f"PRD-{number}"
    return IterationAuthority(
        iteration=number,
        title=title,
        prd_status=prd_status,
        spec_status=spec_status,
        prd_approved=prd_approved,
        spec_approved=spec_approved,
        implementation_authorized=implementation_authorized,
        principle_sha256=hashlib.sha256(principle).hexdigest(),
        base_ref=base_ref,
        base_commit=base_commit,
        depends_on=_ids_from_field(prd, "依赖 PRD") or _ids_from_field(prd, "depends_on"),
        conflicts_with=_ids_from_field(prd, "冲突 PRD") or _ids_from_field(prd, "conflicts_with"),
        candidate_refs=candidates,
        integrated=integrated,
        active_writer=number in _active_workspace_iterations(root),
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
    authority = derive_iteration_authority(root, iteration)
    active = _active_workspace_iterations(root)
    blockers = list(authority.blockers)
    dependency_candidates: dict[str, tuple[str, ...]] = {}
    for dependency in authority.depends_on:
        dep = derive_iteration_authority(root, dependency)
        dependency_candidates[dependency] = dep.candidate_refs
        if not dep.candidate_refs and not dep.integrated:
            blockers.append(f"dependency-stable-candidate-missing:{dependency}")
        elif dep.candidate_refs:
            leases = _workspace_lease_snapshots(root)
            current_generation = dep.candidate_refs[-1].rsplit("/", 1)[-1]
            dependent_lease = leases.get(authority.iteration)
            if dependent_lease is not None:
                bound = dependent_lease.get("dependency_generations")
                if isinstance(bound, Mapping):
                    accepted_generation = bound.get(dependency)
                    if accepted_generation is not None and accepted_generation != current_generation:
                        blockers.append(f"dependency-candidate-stale:{dependency}")
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
                integration_authorized=bool(authority.candidate_refs),
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
    if decision.execution_topology == "stacked-worktree" and not active:
        # A dependent PRD still needs an isolated path even if it is the first
        # local writer, because its base is not canonical main.
        topology = "stacked-worktree"
    else:
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
                    "writes": False,
                },
                {"step": "workspace-apply", "writes": True, "action_level": "notify" if "worktree" in topology else "silent"},
            )
        )
    head = _text(_git(root, ["rev-parse", "--verify", "HEAD^{commit}"]))
    branch_result = _git(root, ["symbolic-ref", "-q", "HEAD"], check=False)
    operation = operation_id or f"OP-{uuid.uuid4().hex}"
    payload = {
        "schema_version": SCHEMA_V1,
        "operation_id": operation,
        "project_root": str(root),
        "head": head,
        "branch_ref": _text(branch_result) if branch_result.returncode == 0 else None,
        "authority": asdict(authority),
        "decision": decision.as_dict(),
        "dependency_candidates": dependency_candidates,
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
