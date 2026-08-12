#!/usr/bin/env python3
"""Versioned, low-noise interaction envelopes for Harness Lite actions.

This module does not execute an action.  It validates the minimum facts that
must be visible before or after Silent/Notify/Confirm operations so callers do
not accidentally hide a Git mutation behind a generic success message.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal, Mapping

try:
    from .harness_decision import action_level
except ImportError:  # pragma: no cover - direct script/module loading
    from harness_decision import action_level


SCHEMA_V1 = "harness-lite.interaction/v1"
HEX_OBJECT = re.compile(r"[0-9a-f]{40,64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
Phase = Literal["before", "after"]


class InteractionError(ValueError):
    """Raised when a notification would conceal a required fact."""


@dataclass(frozen=True)
class ActionFacts:
    action: str
    phase: Phase
    iteration: str | None = None
    operation_id: str | None = None
    project_root: str | None = None
    base_commit: str | None = None
    branch_ref: str | None = None
    worktree_path: str | None = None
    paths: tuple[str, ...] = ()
    message: str | None = None
    verification_ids: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    resulting_commit: str | None = None
    remote: str | None = None
    source_ref: str | None = None
    target_ref: str | None = None
    commit_range: str | None = None
    force: bool = False
    pushed: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class InteractionEnvelope:
    schema_version: str
    action: str
    action_level: str
    phase: str
    summary: str
    facts: dict[str, object]
    facts_digest: str
    requires_user_response: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


WORKTREE_ACTIONS = {"create-worktree", "remove-clean-worktree", "bind-local-branch"}
GIT_CONFIRM_ACTIONS = {
    "commit",
    "push",
    "main-advance",
    "merge",
    "rebase",
    "cherry-pick",
    "delete-branch",
}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    result = value.strip()
    if not result or "\n" in result or "\r" in result:
        raise InteractionError("interaction fields must be non-empty single-line strings")
    return result


def _strings(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        value = _clean(raw)
        if value is not None and value not in result:
            result.append(value)
    if any(len(value) > 4096 for value in result):
        raise InteractionError(f"{label} contains an overlong value")
    return tuple(result)


def _require(value: str | None, label: str) -> str:
    result = _clean(value)
    if result is None:
        raise InteractionError(f"{label} is required for this interaction")
    return result


def _facts_payload(value: ActionFacts) -> dict[str, object]:
    action = _require(value.action, "action").lower()
    if value.phase not in {"before", "after"}:
        raise InteractionError("phase must be before or after")
    iteration = _clean(value.iteration)
    operation = _clean(value.operation_id)
    project_root = _clean(value.project_root)
    base = _clean(value.base_commit)
    branch = _clean(value.branch_ref)
    worktree = _clean(value.worktree_path)
    paths = _strings(value.paths, "paths")
    verification = _strings(value.verification_ids, "verification_ids")
    excluded = _strings(value.excluded_paths, "excluded_paths")
    result_commit = _clean(value.resulting_commit)
    message = _clean(value.message)
    remote = _clean(value.remote)
    source_ref = _clean(value.source_ref)
    target_ref = _clean(value.target_ref)
    commit_range = _clean(value.commit_range)
    reason = _clean(value.reason)

    if base is not None and not HEX_OBJECT.fullmatch(base.lower()):
        raise InteractionError("base_commit must be a full Git object ID")
    if result_commit is not None and not HEX_OBJECT.fullmatch(result_commit.lower()):
        raise InteractionError("resulting_commit must be a full Git object ID")
    if action in WORKTREE_ACTIONS:
        for label, item in (
            ("iteration", iteration),
            ("operation_id", operation),
            ("project_root", project_root),
            ("base_commit", base),
            ("branch_ref", branch),
            ("worktree_path", worktree),
            ("reason", reason),
        ):
            if item is None:
                raise InteractionError(f"{label} is required for {action}")
    if action == "commit":
        if not paths or message is None or not verification:
            raise InteractionError("commit requires exact paths, message, and verification IDs")
        if value.phase == "after" and result_commit is None:
            raise InteractionError("commit after-notification requires the resulting hash")
        if value.pushed:
            raise InteractionError("commit notification cannot claim a push")
    if action == "push":
        for label, item in (
            ("remote", remote),
            ("source_ref", source_ref),
            ("target_ref", target_ref),
            ("commit_range", commit_range),
        ):
            if item is None:
                raise InteractionError(f"{label} is required for push")
        if value.force:
            raise InteractionError("Harness Lite has no silent or normal force-push path")
        if value.phase == "before" and value.pushed:
            raise InteractionError("a before-notification cannot claim push completion")

    return {
        "iteration": iteration,
        "operation_id": operation,
        "project_root": project_root,
        "base_commit": base.lower() if base else None,
        "branch_ref": branch,
        "worktree_path": worktree,
        "paths": list(paths),
        "message": message,
        "verification_ids": list(verification),
        "excluded_paths": list(excluded),
        "resulting_commit": result_commit.lower() if result_commit else None,
        "remote": remote,
        "source_ref": source_ref,
        "target_ref": target_ref,
        "commit_range": commit_range,
        "force": bool(value.force),
        "pushed": bool(value.pushed),
        "reason": reason,
    }


def interaction(value: ActionFacts) -> InteractionEnvelope:
    """Validate and build a stable before/after interaction envelope."""

    action = _require(value.action, "action").lower()
    level = action_level(action)
    facts = _facts_payload(value)
    digest = hashlib.sha256(
        json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if not SHA256.fullmatch(digest):  # pragma: no cover - defensive invariant
        raise AssertionError("invalid interaction digest")
    phase_word = "will" if value.phase == "before" else "did"
    summary = f"Harness {phase_word} {action}"
    if facts.get("iteration"):
        summary += f" for PRD-{facts['iteration']}"
    return InteractionEnvelope(
        schema_version=SCHEMA_V1,
        action=action,
        action_level=level,
        phase=value.phase,
        summary=summary,
        facts=facts,
        facts_digest=digest,
        requires_user_response=level == "confirm" and value.phase == "before",
    )


def is_exact_reply_to(before: InteractionEnvelope, accepted_facts_digest: str | None) -> bool:
    """Return whether a user response is bound to this exact Confirm card."""

    if before.phase != "before" or before.action_level != "confirm":
        return False
    supplied = (accepted_facts_digest or "").strip().lower()
    return bool(SHA256.fullmatch(supplied) and supplied == before.facts_digest)


def public_projection(value: Mapping[str, object]) -> dict[str, object]:
    """Whitelist the stable fields safe for a user-facing machine response."""

    allowed = {
        "schema_version",
        "action",
        "action_level",
        "phase",
        "summary",
        "facts",
        "facts_digest",
        "requires_user_response",
    }
    return {key: value[key] for key in allowed if key in value}
