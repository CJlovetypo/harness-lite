#!/usr/bin/env python3
"""Read-only legacy-to-v2 upgrade planning for Harness Lite.

The first migration surface is deliberately conservative: it inventories the
repository, classifies each iteration, and emits an exact deterministic plan.
It never creates refs, branches, worktrees, files, journals, commits, or pushes.
Any future apply command must accept this plan digest and independently recheck
the observed state before its first write.
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


SCHEMA_V1 = "harness-lite.upgrade-plan/v1"
OID_RE = re.compile(r"[0-9a-f]{40,64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")


class UpgradeError(RuntimeError):
    """Raised when a read-only upgrade plan cannot be established safely."""


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
    head: str
    branch_ref: str | None
    dirty: bool
    iterations: tuple[IterationUpgrade, ...]
    planned_actions: tuple[dict[str, object], ...]
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    plan_digest: str
    phase: str
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


def _run_git(root: Path, arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    git = shutil.which("git")
    if not git:
        raise UpgradeError("Git is required to inspect an upgrade")
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
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

    iterations: list[IterationUpgrade] = []
    actions: list[dict[str, object]] = []
    global_blockers: list[str] = []
    warnings: list[str] = []
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
        "head": head,
        "branch_ref": branch_ref,
        "dirty": dirty,
        "iterations": [asdict(item) for item in iterations],
        "planned_actions": actions,
        "blocking_reasons": global_blockers,
        "warnings": warnings,
    }
    plan_digest = hashlib.sha256(_canonical_json(digest_payload)).hexdigest()
    return UpgradePlan(
        schema_version=SCHEMA_V1,
        command="upgrade-dry-run",
        action_level="silent",
        pushed=False,
        project_root=str(root),
        head=head,
        branch_ref=branch_ref,
        dirty=dirty,
        iterations=tuple(iterations),
        planned_actions=tuple(actions),
        blocking_reasons=tuple(global_blockers),
        warnings=tuple(warnings),
        plan_digest=plan_digest,
        phase="blocked" if global_blockers else "planned",
        next_gate="fix-input-or-reconcile" if global_blockers else "review-explicit-adoption-plan",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan = build_upgrade_plan(args.project_root)
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
    payload = plan.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(f"Upgrade plan: {plan.phase}; digest={plan.plan_digest}")
        for item in plan.iterations:
            print(f"- PRD-{item.iteration}: {item.lifecycle} -> {item.disposition}")
    return 0 if plan.phase == "planned" else 2


if __name__ == "__main__":
    raise SystemExit(main())
