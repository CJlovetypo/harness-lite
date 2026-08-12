#!/usr/bin/env python3
"""Crash-safe append-only progress events for Harness Lite v2.

The semantic three-way union remains in :mod:`harness_governance`.  This
module owns the smaller operation of creating one immutable ``EV-*`` block
inside one worktree.  Planning is read-only; applying is bound to the exact
progress bytes observed by the plan and records recovery state in the Git
common directory shared by all linked worktrees.

This module never commits, merges, pushes, changes refs, or creates/removes a
worktree.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harness_governance import parse_progress_events, plan_progress_union  # noqa: E402


EVENT_SCHEMA = "harness-lite.progress-event/v2"
PLAN_SCHEMA = "harness-lite.progress-append-plan/v2"
JOURNAL_SCHEMA = "harness-lite.progress-append-journal/v2"
RESULT_SCHEMA = "harness-lite.progress-append-result/v2"

PROGRESS_PATH = "harness/progress.md"
REGISTRY_PARTS = ("project-harness", "progress", "v2")
OWNER_MARKERS = (
    b"<!-- managed-by: harness-lite v1 -->",
    b"<!-- managed-by: init-project-harness v1 -->",
)
EVENT_TYPES = frozenset({"OPEN", "DECISION", "CHECKPOINT", "MERGE", "CLOSE"})

OPERATION_ID_RE = re.compile(r"OP-[0-9a-f]{32}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
SESSION_ID_RE = re.compile(r"S-[0-9]{8}-[0-9]{2}")
SCOPE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
EVENT_ID_RE = re.compile(r"EV-[A-Za-z0-9][A-Za-z0-9._-]*")
LEGACY_PARENT_RE = re.compile(
    r"S-[0-9]{8}-[0-9]{2}/(?:OPEN|DECISION|CHECKPOINT|MERGE|CLOSE)"
)
OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")

MAX_PROGRESS_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_EVIDENCE_REFS = 64
MAX_JOURNAL_BYTES = 256 * 1024

EXCLUSIONS = (
    "no commit",
    "no merge",
    "no push",
    "no ref update",
    "no worktree mutation",
)

SAFE_EOL_POLICIES = frozenset({"lf", "crlf"})


class ProgressError(RuntimeError):
    """Raised when an append cannot prove the immutable-history contract."""


class SimulatedCrash(BaseException):
    """Fault-injection signal that deliberately leaves the journal resumable."""


@dataclass(frozen=True)
class ProgressCheckoutPolicy:
    """The exact Git checkout rule proven safe for one progress path."""

    source_commit: str
    autocrlf: str
    core_eol: str
    source_text_attribute: str
    source_eol_attribute: str
    source_filter_attribute: str
    source_working_tree_encoding_attribute: str
    source_ident_attribute: str
    live_text_attribute: str
    live_eol_attribute: str
    live_filter_attribute: str
    live_working_tree_encoding_attribute: str
    live_ident_attribute: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _bounded_line(value: object, label: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if (
        not value
        or len(value) > maximum
        or "\n" in value
        or "\r" in value
        or any(ord(character) < 0x20 and character != "\t" for character in value)
    ):
        raise ProgressError(f"{label} must be a non-empty bounded single line")
    return value


def _validate_operation_id(value: object) -> str:
    if not isinstance(value, str) or OPERATION_ID_RE.fullmatch(value) is None:
        raise ProgressError("operation_id must use OP- plus 32 lowercase hexadecimal characters")
    return value


def _validate_iteration(value: object) -> str:
    if not isinstance(value, str) or ITERATION_RE.fullmatch(value) is None:
        raise ProgressError("iteration must contain at least three decimal digits")
    return value


def _validate_scope(value: object) -> str:
    if not isinstance(value, str) or SCOPE_RE.fullmatch(value) is None:
        raise ProgressError("scope must be a lowercase stable token")
    return value


def _validate_event_type(value: object) -> str:
    if not isinstance(value, str) or value not in EVENT_TYPES:
        raise ProgressError("event_type is not a supported progress event type")
    return value


def _validate_event_key(value: object) -> str:
    key = _bounded_line(value, "event_key", maximum=256)
    if "`" in key:
        raise ProgressError("event_key may not contain a backtick")
    return key


def _validate_ref(value: object) -> str:
    ref = _bounded_line(value, "source_ref", maximum=1024)
    if (
        not ref.startswith("refs/")
        or ref.endswith("/")
        or ref.endswith(".")
        or "//" in ref
        or ".." in ref
        or "@{" in ref
        or any(character in ref for character in " ~^:?*[\\")
        or any(part in {"", "."} or part.endswith(".lock") for part in ref.split("/"))
    ):
        raise ProgressError("source_ref must be a canonical full Git ref")
    return ref


def _validate_oid(value: object) -> str:
    if not isinstance(value, str) or OID_RE.fullmatch(value) is None:
        raise ProgressError("source_commit must be a full lowercase Git object ID")
    return value


def _validate_parent(value: object, *, label: str = "causal_parent") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or (
        EVENT_ID_RE.fullmatch(value) is None and LEGACY_PARENT_RE.fullmatch(value) is None
    ):
        raise ProgressError(f"{label} must be an EV-* ID or an unambiguous legacy session/type identity")
    return value


def _validate_corrects(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or EVENT_ID_RE.fullmatch(value) is None:
        raise ProgressError("corrects must name an immutable EV-* event")
    return value


def _normalize_occurred_at(value: object) -> str:
    text = _bounded_line(value, "occurred_at", maximum=128)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ProgressError("occurred_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProgressError("occurred_at must include an explicit UTC offset")
    return parsed.isoformat(timespec="seconds")


def _validate_evidence_refs(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("evidence_refs must be a sequence of strings")
    if not values or len(values) > MAX_EVIDENCE_REFS:
        raise ProgressError("evidence_refs must contain between 1 and 64 identities")
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        item = _bounded_line(raw, f"evidence_refs[{index}]", maximum=2048)
        if item in seen:
            raise ProgressError("evidence_refs may not contain duplicates")
        seen.add(item)
        result.append(item)
    return tuple(result)


def deterministic_event_id(
    *,
    iteration: str,
    operation_id: str,
    scope: str,
    event_type: str,
    event_key: str,
) -> str:
    """Derive one retry-stable global identity without using wall-clock time."""

    normalized_iteration = _validate_iteration(iteration)
    normalized_operation = _validate_operation_id(operation_id)
    normalized_scope = _validate_scope(scope)
    normalized_type = _validate_event_type(event_type)
    normalized_key = _validate_event_key(event_key)
    stable_identity = {
        "schema_version": EVENT_SCHEMA,
        "iteration": normalized_iteration,
        "operation_id": normalized_operation,
        "scope": normalized_scope,
        "type": normalized_type,
        "event_key": normalized_key,
    }
    digest = sha256_bytes(_canonical_json(stable_identity))
    return f"EV-I{normalized_iteration}-{normalized_scope}-{digest}"


@dataclass(frozen=True)
class ProgressEventV2:
    schema_version: str
    event_id: str
    session_id: str
    iteration: str
    scope: str
    event_type: str
    event_key: str
    occurred_at: str
    source_ref: str
    source_commit: str
    operation_id: str
    causal_parent: str | None
    evidence_refs: tuple[str, ...]
    summary: str
    corrects: str | None = None

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        iteration: str,
        scope: str,
        event_type: str,
        event_key: str,
        occurred_at: str,
        source_ref: str,
        source_commit: str,
        operation_id: str,
        causal_parent: str | None,
        evidence_refs: Sequence[str],
        summary: str,
        corrects: str | None = None,
    ) -> "ProgressEventV2":
        normalized_iteration = _validate_iteration(iteration)
        normalized_operation = _validate_operation_id(operation_id)
        normalized_scope = _validate_scope(scope)
        normalized_type = _validate_event_type(event_type)
        normalized_key = _validate_event_key(event_key)
        if not isinstance(session_id, str) or SESSION_ID_RE.fullmatch(session_id) is None:
            raise ProgressError("session_id must use S-YYYYMMDD-NN")
        normalized_summary = _bounded_line(summary, "summary", maximum=4096)
        event_id = deterministic_event_id(
            iteration=normalized_iteration,
            operation_id=normalized_operation,
            scope=normalized_scope,
            event_type=normalized_type,
            event_key=normalized_key,
        )
        parent = _validate_parent(causal_parent)
        correction = _validate_corrects(corrects)
        if correction == event_id:
            raise ProgressError("an event may not correct itself")
        return cls(
            schema_version=EVENT_SCHEMA,
            event_id=event_id,
            session_id=session_id,
            iteration=normalized_iteration,
            scope=normalized_scope,
            event_type=normalized_type,
            event_key=normalized_key,
            occurred_at=_normalize_occurred_at(occurred_at),
            source_ref=_validate_ref(source_ref),
            source_commit=_validate_oid(source_commit),
            operation_id=normalized_operation,
            causal_parent=parent,
            evidence_refs=_validate_evidence_refs(evidence_refs),
            summary=normalized_summary,
            corrects=correction,
        )

    @classmethod
    def from_dict(cls, value: object) -> "ProgressEventV2":
        required = {
            "schema_version",
            "event_id",
            "session_id",
            "iteration",
            "scope",
            "event_type",
            "event_key",
            "occurred_at",
            "source_ref",
            "source_commit",
            "operation_id",
            "causal_parent",
            "evidence_refs",
            "summary",
            "corrects",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ProgressError("durable progress event fields are invalid")
        if value.get("schema_version") != EVENT_SCHEMA:
            raise ProgressError("durable progress event schema is invalid")
        evidence = value.get("evidence_refs")
        if not isinstance(evidence, list):
            raise ProgressError("durable progress event evidence_refs are invalid")
        event = cls.create(
            session_id=value.get("session_id"),  # type: ignore[arg-type]
            iteration=value.get("iteration"),  # type: ignore[arg-type]
            scope=value.get("scope"),  # type: ignore[arg-type]
            event_type=value.get("event_type"),  # type: ignore[arg-type]
            event_key=value.get("event_key"),  # type: ignore[arg-type]
            occurred_at=value.get("occurred_at"),  # type: ignore[arg-type]
            source_ref=value.get("source_ref"),  # type: ignore[arg-type]
            source_commit=value.get("source_commit"),  # type: ignore[arg-type]
            operation_id=value.get("operation_id"),  # type: ignore[arg-type]
            causal_parent=value.get("causal_parent"),  # type: ignore[arg-type]
            evidence_refs=evidence,
            summary=value.get("summary"),  # type: ignore[arg-type]
            corrects=value.get("corrects"),  # type: ignore[arg-type]
        )
        if value.get("event_id") != event.event_id:
            raise ProgressError("durable progress event ID differs from its stable identity")
        return event

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        return result

    def render(self, newline: bytes = b"\n") -> bytes:
        if newline not in {b"\n", b"\r\n"}:
            raise ProgressError("newline must be LF or CRLF")
        evidence = json.dumps(
            list(self.evidence_refs), ensure_ascii=False, separators=(",", ":")
        )
        summary = json.dumps(self.summary, ensure_ascii=False, separators=(",", ":"))
        lines = (
            f"## {self.event_id} / {self.event_type} / {self.occurred_at}",
            "",
            f"- schema_version: `{self.schema_version}`",
            f"- session_id: `{self.session_id}`",
            f"- iteration: `{self.iteration}`",
            f"- scope: `{self.scope}`",
            f"- operation_id: `{self.operation_id}`",
            f"- event_key: `{self.event_key}`",
            f"- source_ref: `{self.source_ref}`",
            f"- source_commit: `{self.source_commit}`",
            f"- causal_parent: `{self.causal_parent or 'none'}`",
            f"- evidence_refs: {evidence}",
            f"- corrects: `{self.corrects or 'none'}`",
            f"- summary: {summary}",
        )
        raw = newline.join(line.encode("utf-8") for line in lines) + newline
        if len(raw) > MAX_EVENT_BYTES:
            raise ProgressError("rendered progress event exceeds the safe size")
        return raw


def build_progress_event(**values: object) -> ProgressEventV2:
    """Public generic event constructor with strict v2 validation."""

    return ProgressEventV2.create(**values)  # type: ignore[arg-type]


def open_event(
    *,
    open_key: str,
    **values: object,
) -> ProgressEventV2:
    """Build a deterministic EV OPEN event for bundle/lifecycle adoption."""

    key = _validate_event_key(open_key)
    return ProgressEventV2.create(
        scope="lifecycle",
        event_type="OPEN",
        event_key=f"open:{key}",
        **values,  # type: ignore[arg-type]
    )


def workspace_event(
    *,
    workspace_state: str,
    **values: object,
) -> ProgressEventV2:
    """Build a CHECKPOINT for a workspace lifecycle state transition."""

    state = _validate_event_key(workspace_state)
    return ProgressEventV2.create(
        scope="workspace",
        event_type="CHECKPOINT",
        event_key=f"workspace:{state}",
        **values,  # type: ignore[arg-type]
    )


def candidate_event(
    *,
    generation: int,
    candidate_state: str,
    **values: object,
) -> ProgressEventV2:
    """Build a CHECKPOINT bound to one feature-candidate generation/state."""

    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ProgressError("candidate generation must be a positive integer")
    state = _validate_event_key(candidate_state)
    return ProgressEventV2.create(
        scope="candidate",
        event_type="CHECKPOINT",
        event_key=f"candidate:{generation}:{state}",
        **values,  # type: ignore[arg-type]
    )


def integration_event(
    *,
    integration_state: str,
    **values: object,
) -> ProgressEventV2:
    """Build a MERGE event for an integrated-candidate/main-train transition."""

    state = _validate_event_key(integration_state)
    return ProgressEventV2.create(
        scope="integration",
        event_type="MERGE",
        event_key=f"integration:{state}",
        **values,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class ProgressAppendPlan:
    plan_digest: str
    manifest: dict[str, object]
    event: ProgressEventV2
    event_bytes: bytes

    @property
    def operation_id(self) -> str:
        return self.event.operation_id

    @property
    def action(self) -> str:
        return str(self.manifest["action"])

    @property
    def project_root(self) -> str:
        return str(self.manifest["project_root"])

    @property
    def git_common_dir(self) -> str:
        return str(self.manifest["git_common_dir"])

    @property
    def progress_path(self) -> str:
        return str(self.manifest["progress_path"])

    @property
    def before_sha256(self) -> str:
        return str(self.manifest["before_sha256"])

    @property
    def after_sha256(self) -> str:
        return str(self.manifest["after_sha256"])

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLAN_SCHEMA,
            "operation_id": self.operation_id,
            "event_id": self.event.event_id,
            "iteration": self.event.iteration,
            "project_root": self.project_root,
            "git_common_dir": self.git_common_dir,
            "progress_path": self.progress_path,
            "action": self.action,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "plan_digest": self.plan_digest,
            "exclusions": list(EXCLUSIONS),
            "pushed": False,
        }


@dataclass(frozen=True)
class ProgressAppendResult:
    operation_id: str
    event_id: str
    project_root: str
    progress_path: str
    plan_digest: str
    phase: str
    appended: bool
    resumed: bool
    journal_path: str
    result_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESULT_SCHEMA,
            **asdict(self),
            "exclusions": list(EXCLUSIONS),
            "pushed": False,
        }


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(key, None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _git(
    root: Path,
    *arguments: str,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("git")
    if not executable:
        raise ProgressError("git is required for progress event binding")
    result = subprocess.run(
        [executable, "-C", str(root), *arguments],
        check=False,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ProgressError(f"git {' '.join(arguments)} failed: {detail or result.returncode}")
    return result


def _resolve_worktree_root(value: Path | str) -> Path:
    root = Path(value).absolute().resolve()
    if not root.is_dir():
        raise ProgressError(f"project_root is not a directory: {root}")
    result = _git(root, "rev-parse", "--show-toplevel")
    actual = Path(result.stdout.decode("utf-8", errors="strict").strip()).resolve()
    if actual != root:
        raise ProgressError(f"project_root must be the exact worktree root: {actual}")
    return root


def resolve_git_common_dir(project_root: Path | str) -> Path:
    root = Path(project_root).absolute().resolve()
    result = _git(root, "rev-parse", "--git-common-dir")
    raw = result.stdout.decode("utf-8", errors="strict").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    if not common.is_dir() or not (common / "objects").is_dir():
        raise ProgressError(f"Git common directory is invalid: {common}")
    return common


def _canonical_progress_path(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("progress_path must be a string")
    normalized = value.replace("\\", "/")
    if normalized != PROGRESS_PATH:
        raise ProgressError(f"progress_path must be {PROGRESS_PATH}")
    return normalized


def _attribute_state(
    root: Path,
    path: str,
    attribute: str,
    *,
    source_commit: str | None = None,
) -> str:
    arguments = ["check-attr", "-z"]
    if source_commit is not None:
        arguments.append(f"--source={_validate_oid(source_commit)}")
    arguments.extend((attribute, "--", path))
    result = _git(root, *arguments)
    fields = result.stdout.split(b"\0")
    if len(fields) < 4 or fields[0].decode("utf-8", errors="strict") != path:
        raise ProgressError(f"git check-attr returned an invalid {attribute} result")
    if fields[1].decode("utf-8", errors="strict") != attribute:
        raise ProgressError(f"git check-attr returned another attribute for {attribute}")
    return fields[2].decode("utf-8", errors="strict")


def _policy_from_dict(value: object) -> ProgressCheckoutPolicy:
    if not isinstance(value, dict) or set(value) != set(
        ProgressCheckoutPolicy.__dataclass_fields__
    ):
        raise ProgressError("progress append checkout policy is invalid")
    if not all(isinstance(item, str) for item in value.values()):
        raise ProgressError("progress append checkout policy values are invalid")
    policy = ProgressCheckoutPolicy(**value)
    _validate_oid(policy.source_commit)
    for prefix in ("source", "live"):
        text_state = getattr(policy, f"{prefix}_text_attribute")
        eol_state = getattr(policy, f"{prefix}_eol_attribute")
        filter_state = getattr(policy, f"{prefix}_filter_attribute")
        encoding_state = getattr(policy, f"{prefix}_working_tree_encoding_attribute")
        ident_state = getattr(policy, f"{prefix}_ident_attribute")
        if text_state not in {"set", "auto", "unspecified"}:
            raise ProgressError("progress append text policy is invalid")
        if eol_state not in {"lf", "crlf", "unspecified"}:
            raise ProgressError("progress append eol attribute is invalid")
        if filter_state != "unspecified" or encoding_state != "unspecified":
            raise ProgressError("progress append checkout policy contains unsafe filters/encoding")
        if ident_state != "unspecified":
            raise ProgressError("progress append checkout policy contains unsafe ident conversion")
    if policy.autocrlf not in {"true", "input", "false"}:
        raise ProgressError("progress append core.autocrlf policy is invalid")
    if policy.core_eol not in {"native", "lf", "crlf"}:
        raise ProgressError("progress append core.eol policy is invalid")
    return policy


def _config_value(root: Path, key: str) -> str | None:
    result = _git(root, "config", "--get", key, check=False)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ProgressError(f"cannot read Git config {key}: {detail or result.returncode}")
    value = result.stdout.decode("utf-8", errors="strict").strip()
    return value or None


def resolve_progress_checkout_policy(
    project_root: Path | str,
    source_commit: str,
    progress_path: str = PROGRESS_PATH,
) -> ProgressCheckoutPolicy:
    """Prove the only allowed clean/checkout EOL conversion for progress.

    Custom filters and ``working-tree-encoding`` can change arbitrary bytes and
    are therefore never accepted.  Explicit/non-boolean ``text`` attributes are
    likewise rejected.  A pure text path may safely use LF or CRLF in the
    working tree; exact bytes remain bound by the append plan.
    """

    root = _resolve_worktree_root(project_root)
    relative = _canonical_progress_path(progress_path)
    commit = _validate_oid(source_commit)
    attributes = ("text", "eol", "filter", "working-tree-encoding", "ident")
    source = {
        attribute: _attribute_state(
            root,
            relative,
            attribute,
            source_commit=commit,
        )
        for attribute in attributes
    }
    live = {attribute: _attribute_state(root, relative, attribute) for attribute in attributes}
    if source != live:
        raise ProgressError(
            "live progress attributes differ from the pinned source commit; caller attributes cannot authorize conversion"
        )
    autocrlf_raw = (_config_value(root, "core.autocrlf") or "false").lower()
    autocrlf_aliases = {
        "true": "true",
        "yes": "true",
        "on": "true",
        "1": "true",
        "input": "input",
        "false": "false",
        "no": "false",
        "off": "false",
        "0": "false",
    }
    if autocrlf_raw not in autocrlf_aliases:
        raise ProgressError("core.autocrlf has an unsupported value for safe progress checkout")
    core_eol = (_config_value(root, "core.eol") or "native").lower()
    if core_eol not in {"native", "lf", "crlf"}:
        raise ProgressError("core.eol has an unsupported value for safe progress checkout")
    return _policy_from_dict(
        {
            "source_commit": commit,
            "autocrlf": autocrlf_aliases[autocrlf_raw],
            "core_eol": core_eol,
            "source_text_attribute": source["text"],
            "source_eol_attribute": source["eol"],
            "source_filter_attribute": source["filter"],
            "source_working_tree_encoding_attribute": source["working-tree-encoding"],
            "source_ident_attribute": source["ident"],
            "live_text_attribute": live["text"],
            "live_eol_attribute": live["eol"],
            "live_filter_attribute": live["filter"],
            "live_working_tree_encoding_attribute": live["working-tree-encoding"],
            "live_ident_attribute": live["ident"],
        }
    )


def _pure_eol_style(content: bytes, label: str) -> str:
    if b"\r" in content.replace(b"\r\n", b""):
        raise ProgressError(f"{label} contains a bare carriage return")
    crlf = content.count(b"\r\n")
    lf = content.count(b"\n") - crlf
    if crlf and lf:
        raise ProgressError(f"{label} contains mixed LF/CRLF line endings")
    return "crlf" if crlf else "lf"


def checkout_progress_variants(
    root: Path,
    progress_path: str,
    source_blob_oid: str,
    source_progress: bytes,
    policy: ProgressCheckoutPolicy,
) -> dict[str, bytes]:
    """Return exact safe working-tree variants for one committed text blob."""

    if not isinstance(policy, ProgressCheckoutPolicy):
        raise TypeError("policy must be ProgressCheckoutPolicy")
    source_oid = _validate_oid(source_blob_oid)
    relative = _canonical_progress_path(progress_path)
    source_style = _pure_eol_style(source_progress, "source progress history")
    normalized = source_progress.replace(b"\r\n", b"\n")
    candidates = {
        "lf": normalized,
        "crlf": normalized.replace(b"\n", b"\r\n"),
    }
    result: dict[str, bytes] = {}
    for style, candidate in candidates.items():
        cleaned = _git(
            root,
            "hash-object",
            f"--path={relative}",
            "--stdin",
            input_bytes=candidate,
        ).stdout.decode("ascii", errors="strict").strip().lower()
        if cleaned == source_oid:
            result[style] = candidate
    if source_style not in result:
        raise ProgressError("Git clean filtering does not reproduce the pinned progress blob")
    if not result:
        raise ProgressError("no Git-proven progress checkout variant exists")
    return result


def _equivalent_checkout_source(
    root: Path,
    progress_path: str,
    source_blob_oid: str,
    source_progress: bytes,
    current: bytes,
    policy: ProgressCheckoutPolicy,
) -> bytes:
    variants = checkout_progress_variants(
        root,
        progress_path,
        source_blob_oid,
        source_progress,
        policy,
    )
    current_style = _pure_eol_style(current, "working-tree progress history")
    equivalent = variants.get(current_style)
    if equivalent is None:
        raise ProgressError(
            "working-tree progress uses an EOL style not proven equivalent by Git clean filtering"
        )
    return equivalent


def _ensure_target_safe(root: Path, target: Path) -> None:
    resolved_root = root.resolve()
    try:
        target.absolute().relative_to(resolved_root)
        target.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise ProgressError(f"progress path resolves outside the worktree: {target}") from exc
    current = target
    while current != resolved_root:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise ProgressError(f"progress path traverses a symlink or junction: {current}")
        if current.parent == current:
            raise ProgressError(f"cannot prove progress path containment: {target}")
        current = current.parent


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > limit:
            raise ProgressError(f"{label} exceeds the safe size")
        raw = path.read_bytes()
    except OSError as exc:
        raise ProgressError(f"cannot read {label}: {path}") from exc
    if len(raw) != size:
        raise ProgressError(f"{label} changed while it was read")
    return raw


def _has_owner_marker(content: bytes) -> bool:
    payload = content[3:] if content.startswith(b"\xef\xbb\xbf") else content
    return any(payload.startswith(marker) for marker in OWNER_MARKERS)


def _detect_newline(content: bytes) -> bytes:
    crlf = content.count(b"\r\n")
    lf = content.count(b"\n") - crlf
    return b"\r\n" if crlf > lf else b"\n"


def _append_exact_event(content: bytes, event: bytes, newline: bytes) -> bytes:
    if not content:
        return event
    if content.endswith(newline + newline):
        separator = b""
    elif content.endswith(newline):
        separator = newline
    else:
        separator = newline + newline
    return content + separator + event


def append_progress_event_exact(
    content: bytes,
    event: ProgressEventV2,
) -> tuple[bytes, bool]:
    """Return an exact semantic append suitable for an integration snapshot.

    This is the write-free counterpart of :func:`plan_progress_append`.  It is
    intentionally small so a merge-train governance adapter can pre-bind an
    immutable event inside the candidate tree without consulting the live
    checkout or creating a second operation journal.  Existing same-ID/same-
    bytes events are idempotent; same-ID/different-bytes and missing causal
    parents fail closed.
    """

    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if not isinstance(event, ProgressEventV2):
        raise TypeError("event must be ProgressEventV2")
    if len(content) > MAX_PROGRESS_BYTES:
        raise ProgressError("progress history exceeds the safe size")
    style = _pure_eol_style(content, "progress history")
    newline = b"\r\n" if style == "crlf" else b"\n"
    rendered = event.render(newline)
    parsed, existing = _event_by_identity(content, event.event_id)
    if existing is not None:
        if existing.exact_bytes != rendered:
            raise ProgressError(
                f"event ID {event.event_id} already exists with different exact bytes"
            )
        return content, False

    identities = {item.identity for item in parsed.events}
    if event.causal_parent is not None and event.causal_parent not in identities:
        raise ProgressError(
            f"event {event.event_id} causal parent is absent: {event.causal_parent}"
        )
    candidate = _append_exact_event(content, rendered, newline)
    if len(candidate) > MAX_PROGRESS_BYTES:
        raise ProgressError("progress history exceeds the safe size after event append")
    semantic = plan_progress_union(
        branch_base=content,
        latest_main=content,
        branch_candidate=candidate,
    )
    if (
        not semantic.ready
        or semantic.preview != candidate
        or semantic.appended_event_identities != (event.event_id,)
    ):
        detail = _semantic_errors(semantic) or "semantic union did not preserve the exact event"
        raise ProgressError(f"progress event cannot be reconciled exactly: {detail}")
    return candidate, True


def _semantic_errors(plan: object) -> str:
    blockers = getattr(plan, "blockers", ())
    return "; ".join(f"{item.code}: {item.message}" for item in blockers)


def _validate_append_only_base(base: bytes, current: bytes) -> None:
    semantic = plan_progress_union(
        branch_base=base,
        latest_main=base,
        branch_candidate=current,
    )
    if not semantic.ready or semantic.preview != current:
        detail = _semantic_errors(semantic) or "candidate bytes are not an exact append of source history"
        raise ProgressError(f"existing progress history differs from its source commit: {detail}")


def _event_by_identity(content: bytes, identity: str):
    parsed = parse_progress_events(content, source="progress-append-target")
    if parsed.blockers:
        detail = "; ".join(f"{item.code}: {item.message}" for item in parsed.blockers)
        raise ProgressError(f"progress history is not valid: {detail}")
    return parsed, next((item for item in parsed.events if item.identity == identity), None)


def _read_source_progress(root: Path, event: ProgressEventV2, progress_path: str) -> tuple[bytes, str | None]:
    exists = _git(root, "cat-file", "-e", f"{event.source_commit}^{{commit}}", check=False)
    if exists.returncode != 0:
        raise ProgressError("source_commit does not identify an existing commit")
    content = _git(root, "show", f"{event.source_commit}:{progress_path}").stdout
    if len(content) > MAX_PROGRESS_BYTES:
        raise ProgressError("source progress history exceeds the safe size")
    resolved = _git(root, "rev-parse", "--verify", f"{event.source_ref}^{{commit}}", check=False)
    observed: str | None = None
    if resolved.returncode == 0:
        candidate = resolved.stdout.decode("ascii", errors="strict").strip().lower()
        if OID_RE.fullmatch(candidate) is not None:
            observed = candidate
    return content, observed


def source_progress_blob_oid(
    root: Path,
    source_commit: str,
    progress_path: str,
) -> str:
    result = _git(
        root,
        "rev-parse",
        "--verify",
        f"{_validate_oid(source_commit)}:{_canonical_progress_path(progress_path)}",
    )
    oid = result.stdout.decode("ascii", errors="strict").strip().lower()
    if OID_RE.fullmatch(oid) is None:
        raise ProgressError("source progress blob identity is invalid")
    kind = _git(root, "cat-file", "-t", oid).stdout.decode("ascii", errors="strict").strip()
    if kind != "blob":
        raise ProgressError("source progress path does not identify a blob")
    return oid


def _registry_root(common_dir: Path) -> Path:
    return common_dir.joinpath(*REGISTRY_PARTS)


def journal_path(common_dir: Path | str, operation_id: str, event_id: str) -> Path:
    operation = _validate_operation_id(operation_id)
    if not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id) is None:
        raise ProgressError("event_id is invalid")
    # Event IDs deliberately remain descriptive and can be long.  They are
    # authoritative inside the strict journal payload, but using them as a
    # Windows filename can exceed legacy MAX_PATH in otherwise valid nested
    # workspaces.  The filename is therefore a deterministic, non-authoritative
    # locator; the full event identity is still checked after reading.
    locator = sha256_bytes(event_id.encode("utf-8"))
    return _registry_root(Path(common_dir)) / "operations" / operation / f"event-{locator}.json"


def _event_lock_path(common_dir: Path, event: ProgressEventV2) -> Path:
    locator = sha256_bytes(event.event_id.encode("utf-8"))
    return _registry_root(common_dir) / "locks" / f"event-{locator}.lock"


def _target_lock_path(common_dir: Path, root: Path, progress_path: str, iteration: str) -> Path:
    target = root / Path(progress_path)
    identity = os.path.normcase(str(target.resolve(strict=False)))
    digest = sha256_bytes(f"{iteration}\0{identity}".encode("utf-8"))
    return _registry_root(common_dir) / "locks" / f"progress-I{iteration}-{digest}.lock"


def _new_manifest(
    *,
    root: Path,
    common: Path,
    progress_path: str,
    event: ProgressEventV2,
    event_bytes: bytes,
    newline: bytes,
    source_progress: bytes,
    source_progress_blob_oid: str,
    semantic_source_progress: bytes,
    checkout_policy: ProgressCheckoutPolicy,
    allowed_source_variants: Mapping[str, bytes],
    source_ref_observed_commit: str | None,
    before: bytes,
    after: bytes,
    action: str,
) -> dict[str, object]:
    return {
        "schema_version": PLAN_SCHEMA,
        "operation_id": event.operation_id,
        "event_id": event.event_id,
        "iteration": event.iteration,
        "project_root": str(root),
        "git_common_dir": str(common),
        "progress_path": progress_path,
        "target_path": str((root / Path(progress_path)).resolve(strict=False)),
        "action": action,
        "event": event.as_dict(),
        "event_bytes_base64": base64.b64encode(event_bytes).decode("ascii"),
        "event_sha256": sha256_bytes(event_bytes),
        "newline": "CRLF" if newline == b"\r\n" else "LF",
        "source_progress_sha256": sha256_bytes(source_progress),
        "source_progress_blob_oid": source_progress_blob_oid,
        "semantic_source_progress_sha256": sha256_bytes(semantic_source_progress),
        "checkout_policy": checkout_policy.as_dict(),
        "allowed_source_variants": {
            key: sha256_bytes(value) for key, value in sorted(allowed_source_variants.items())
        },
        "source_ref_observed_commit": source_ref_observed_commit,
        "before_sha256": sha256_bytes(before),
        "after_sha256": sha256_bytes(after),
        "exclusions": list(EXCLUSIONS),
    }


def _plan_from_manifest(manifest: object, plan_digest: str) -> ProgressAppendPlan:
    required = {
        "schema_version",
        "operation_id",
        "event_id",
        "iteration",
        "project_root",
        "git_common_dir",
        "progress_path",
        "target_path",
        "action",
        "event",
        "event_bytes_base64",
        "event_sha256",
        "newline",
        "source_progress_sha256",
        "source_progress_blob_oid",
        "semantic_source_progress_sha256",
        "checkout_policy",
        "allowed_source_variants",
        "source_ref_observed_commit",
        "before_sha256",
        "after_sha256",
        "exclusions",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ProgressError("progress append manifest fields are invalid")
    if manifest.get("schema_version") != PLAN_SCHEMA:
        raise ProgressError("progress append manifest schema is invalid")
    if not isinstance(plan_digest, str) or DIGEST_RE.fullmatch(plan_digest) is None:
        raise ProgressError("progress append plan digest is invalid")
    if sha256_bytes(_canonical_json(manifest)) != plan_digest:
        raise ProgressError("progress append manifest differs from its plan digest")
    event = ProgressEventV2.from_dict(manifest.get("event"))
    if (
        manifest.get("operation_id") != event.operation_id
        or manifest.get("event_id") != event.event_id
        or manifest.get("iteration") != event.iteration
    ):
        raise ProgressError("progress append manifest/event identity is inconsistent")
    progress_path = _canonical_progress_path(manifest.get("progress_path"))
    root = Path(str(manifest.get("project_root"))).absolute().resolve()
    common = Path(str(manifest.get("git_common_dir"))).absolute().resolve()
    target = root / Path(progress_path)
    if str(target.resolve(strict=False)) != manifest.get("target_path"):
        raise ProgressError("progress append target path is not canonical")
    if manifest.get("action") not in {"APPEND", "IDEMPOTENT"}:
        raise ProgressError("progress append action is invalid")
    if manifest.get("newline") not in {"LF", "CRLF"}:
        raise ProgressError("progress append newline identity is invalid")
    for field in (
        "event_sha256",
        "source_progress_sha256",
        "semantic_source_progress_sha256",
        "before_sha256",
        "after_sha256",
    ):
        if not isinstance(manifest.get(field), str) or DIGEST_RE.fullmatch(str(manifest[field])) is None:
            raise ProgressError(f"progress append {field} is invalid")
    observed = manifest.get("source_ref_observed_commit")
    if observed is not None and (not isinstance(observed, str) or OID_RE.fullmatch(observed) is None):
        raise ProgressError("progress append observed source ref commit is invalid")
    if not isinstance(manifest.get("source_progress_blob_oid"), str) or OID_RE.fullmatch(
        str(manifest["source_progress_blob_oid"])
    ) is None:
        raise ProgressError("progress append source blob identity is invalid")
    if manifest.get("exclusions") != list(EXCLUSIONS):
        raise ProgressError("progress append exclusions were altered")
    _policy_from_dict(manifest.get("checkout_policy"))
    variants = manifest.get("allowed_source_variants")
    if (
        not isinstance(variants, dict)
        or not variants
        or not set(variants).issubset(SAFE_EOL_POLICIES)
        or any(not isinstance(item, str) or DIGEST_RE.fullmatch(item) is None for item in variants.values())
    ):
        raise ProgressError("progress append allowed source variants are invalid")
    encoded = manifest.get("event_bytes_base64")
    if not isinstance(encoded, str):
        raise ProgressError("progress append event bytes are invalid")
    try:
        event_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ProgressError("progress append event bytes are not canonical base64") from exc
    newline = b"\r\n" if manifest["newline"] == "CRLF" else b"\n"
    if (
        len(event_bytes) > MAX_EVENT_BYTES
        or sha256_bytes(event_bytes) != manifest.get("event_sha256")
        or event.render(newline) != event_bytes
    ):
        raise ProgressError("progress append event bytes differ from the v2 event model")
    if manifest["action"] == "IDEMPOTENT" and manifest["before_sha256"] != manifest["after_sha256"]:
        raise ProgressError("idempotent progress plan unexpectedly changes bytes")
    if manifest["action"] == "APPEND" and manifest["before_sha256"] == manifest["after_sha256"]:
        raise ProgressError("append progress plan does not change bytes")
    return ProgressAppendPlan(plan_digest, dict(manifest), event, event_bytes)


def plan_progress_append(
    *,
    project_root: Path | str,
    event: ProgressEventV2,
    progress_path: str = PROGRESS_PATH,
) -> ProgressAppendPlan:
    """Read and bind one exact append plan without writing any file or Git ref."""

    if not isinstance(event, ProgressEventV2):
        raise TypeError("event must be ProgressEventV2")
    # Round-trip the model so manually constructed dataclass values cannot
    # bypass constructor validation.
    event = ProgressEventV2.from_dict(event.as_dict())
    root = _resolve_worktree_root(project_root)
    common = resolve_git_common_dir(root)
    relative = _canonical_progress_path(progress_path)
    target = root / Path(relative)
    _ensure_target_safe(root, target)
    if not target.is_file():
        raise ProgressError(f"progress file is missing: {relative}")
    current = _read_bounded(target, MAX_PROGRESS_BYTES, "progress file")
    if not _has_owner_marker(current):
        raise ProgressError("progress file is not Harness-managed")
    newline = _detect_newline(current)
    rendered = event.render(newline)
    parsed, existing = _event_by_identity(current, event.event_id)
    if existing is not None and existing.exact_bytes != rendered:
        raise ProgressError(
            f"event ID {event.event_id} already exists with different exact bytes"
        )

    durable_path = journal_path(common, event.operation_id, event.event_id)
    if existing is not None and durable_path.is_file():
        durable = load_progress_append_plan(common, event.operation_id, event.event_id)
        if (
            durable.event != event
            or durable.project_root != str(root)
            or durable.progress_path != relative
            or durable.after_sha256 != sha256_bytes(current)
            or durable.event_bytes != rendered
        ):
            raise ProgressError("durable retry journal conflicts with the existing exact event")
        return durable

    source_progress, observed_source_commit = _read_source_progress(root, event, relative)
    if not _has_owner_marker(source_progress):
        raise ProgressError("source progress history is not Harness-managed")
    source_blob_oid = source_progress_blob_oid(root, event.source_commit, relative)
    checkout_policy = resolve_progress_checkout_policy(root, event.source_commit, relative)
    source_variants = checkout_progress_variants(
        root,
        relative,
        source_blob_oid,
        source_progress,
        checkout_policy,
    )
    semantic_source = _equivalent_checkout_source(
        root,
        relative,
        source_blob_oid,
        source_progress,
        current,
        checkout_policy,
    )
    _validate_append_only_base(semantic_source, current)

    identities = {item.identity for item in parsed.events}
    if event.corrects is not None and event.corrects not in identities:
        raise ProgressError(f"correction target is absent from progress history: {event.corrects}")

    if existing is not None:
        action = "IDEMPOTENT"
        after = current
    else:
        if observed_source_commit != event.source_commit:
            actual = observed_source_commit or "missing"
            raise ProgressError(
                f"source_ref does not resolve to source_commit: expected {event.source_commit}, got {actual}"
            )
        action = "APPEND"
        after = _append_exact_event(current, rendered, newline)
        if len(after) > MAX_PROGRESS_BYTES:
            raise ProgressError("progress append would exceed the safe size")
        semantic = plan_progress_union(
            branch_base=current,
            latest_main=current,
            branch_candidate=after,
        )
        if not semantic.ready or semantic.preview != after:
            detail = _semantic_errors(semantic) or "new event is not a valid immutable append"
            raise ProgressError(f"progress event cannot be appended: {detail}")
        if semantic.appended_event_identities != (event.event_id,):
            raise ProgressError("semantic append did not produce exactly the requested event")

    manifest = _new_manifest(
        root=root,
        common=common,
        progress_path=relative,
        event=event,
        event_bytes=rendered,
        newline=newline,
        source_progress=source_progress,
        source_progress_blob_oid=source_blob_oid,
        semantic_source_progress=semantic_source,
        checkout_policy=checkout_policy,
        allowed_source_variants=source_variants,
        source_ref_observed_commit=observed_source_commit,
        before=current,
        after=after,
        action=action,
    )
    digest = sha256_bytes(_canonical_json(manifest))
    return _plan_from_manifest(manifest, digest)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes_replace(
    path: Path,
    raw: bytes,
    *,
    create_parent: bool,
    before_replace: Callable[[], None] | None = None,
) -> None:
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir():
        raise ProgressError(f"target parent directory is missing: {path.parent}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _atomic_json_replace(path: Path, value: Mapping[str, object]) -> None:
    _atomic_bytes_replace(
        path,
        _canonical_json(value) + b"\n",
        create_parent=True,
    )


@contextlib.contextmanager
def _file_lock(path: Path, *, timeout_seconds: float = 30.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ProgressError(f"timed out waiting for progress lock: {path}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _locks(paths: Sequence[Path]):
    with ExitStack() as stack:
        for path in sorted(set(paths), key=lambda item: str(item)):
            stack.enter_context(_file_lock(path))
        yield


def _read_json(path: Path) -> dict[str, object]:
    raw = _read_bounded(path, MAX_JOURNAL_BYTES, "progress journal")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProgressError(f"progress journal is invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ProgressError(f"progress journal must be an object: {path}")
    return value


def _new_journal(plan: ProgressAppendPlan) -> dict[str, object]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schema_version": JOURNAL_SCHEMA,
        "operation_id": plan.operation_id,
        "event_id": plan.event.event_id,
        "plan_digest": plan.plan_digest,
        "phase": "PLANNED",
        "created_at": now,
        "updated_at": now,
        "manifest": plan.manifest,
        "history": [{"phase": "PLANNED", "at": now}],
        "error": None,
    }


def _validate_journal(value: object, source: Path) -> dict[str, object]:
    required = {
        "schema_version",
        "operation_id",
        "event_id",
        "plan_digest",
        "phase",
        "created_at",
        "updated_at",
        "manifest",
        "history",
        "error",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ProgressError(f"progress journal fields are invalid: {source}")
    if value.get("schema_version") != JOURNAL_SCHEMA:
        raise ProgressError(f"progress journal schema is invalid: {source}")
    _validate_operation_id(value.get("operation_id"))
    if not isinstance(value.get("event_id"), str) or EVENT_ID_RE.fullmatch(str(value["event_id"])) is None:
        raise ProgressError(f"progress journal event ID is invalid: {source}")
    if not isinstance(value.get("plan_digest"), str) or DIGEST_RE.fullmatch(str(value["plan_digest"])) is None:
        raise ProgressError(f"progress journal plan digest is invalid: {source}")
    if value.get("phase") not in {"PLANNED", "APPLYING", "APPLIED", "FAILED_NEEDS_RECONCILE"}:
        raise ProgressError(f"progress journal phase is invalid: {source}")
    if not isinstance(value.get("manifest"), dict) or not isinstance(value.get("history"), list):
        raise ProgressError(f"progress journal payload is invalid: {source}")
    return dict(value)


def _advance_journal(
    path: Path,
    journal: Mapping[str, object],
    phase: str,
    *,
    error: str | None = None,
) -> dict[str, object]:
    updated = dict(journal)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    updated["phase"] = phase
    updated["updated_at"] = now
    updated["error"] = error
    history = list(updated["history"])
    if not history or history[-1].get("phase") != phase:
        entry: dict[str, object] = {"phase": phase, "at": now}
        if error:
            entry["error"] = error
        history.append(entry)
    updated["history"] = history
    _atomic_json_replace(path, updated)
    return updated


def load_progress_append_plan(
    common_dir: Path | str,
    operation_id: str,
    event_id: str,
) -> ProgressAppendPlan:
    """Rehydrate the exact accepted append plan from common-dir recovery state."""

    common = Path(common_dir).absolute().resolve()
    path = journal_path(common, operation_id, event_id)
    journal = _validate_journal(_read_json(path), path)
    if journal["operation_id"] != operation_id or journal["event_id"] != event_id:
        raise ProgressError("progress journal identity differs from its path")
    plan = _plan_from_manifest(journal["manifest"], str(journal["plan_digest"]))
    if plan.git_common_dir != str(common):
        raise ProgressError("progress journal belongs to another Git common directory")
    return plan


def _target_state(target: Path, plan: ProgressAppendPlan) -> str:
    if not target.is_file():
        return "drift"
    current = _read_bounded(target, MAX_PROGRESS_BYTES, "progress file")
    digest = sha256_bytes(current)
    if digest == plan.after_sha256:
        _, event = _event_by_identity(current, plan.event.event_id)
        if event is None or event.exact_bytes != plan.event_bytes:
            return "drift"
        return "after"
    if digest == plan.before_sha256:
        return "before"
    return "drift"


def _validate_plan_context(plan: ProgressAppendPlan) -> tuple[Path, Path, Path]:
    validated = _plan_from_manifest(plan.manifest, plan.plan_digest)
    if validated.event != plan.event or validated.event_bytes != plan.event_bytes:
        raise ProgressError("in-memory progress plan differs from its manifest")
    root = _resolve_worktree_root(plan.project_root)
    common = resolve_git_common_dir(root)
    if str(common) != plan.git_common_dir:
        raise ProgressError("progress plan common directory no longer belongs to its worktree")
    target = root / Path(plan.progress_path)
    _ensure_target_safe(root, target)
    if str(target.resolve(strict=False)) != plan.manifest["target_path"]:
        raise ProgressError("progress plan target identity changed")
    expected_policy = _policy_from_dict(plan.manifest["checkout_policy"])
    actual_policy = resolve_progress_checkout_policy(
        root,
        plan.event.source_commit,
        plan.progress_path,
    )
    if actual_policy != expected_policy:
        raise ProgressError("progress Git checkout/clean policy changed after planning")
    source_progress, _ = _read_source_progress(root, plan.event, plan.progress_path)
    if sha256_bytes(source_progress) != plan.manifest["source_progress_sha256"]:
        raise ProgressError("pinned source progress bytes changed after planning")
    source_blob_oid = source_progress_blob_oid(
        root,
        plan.event.source_commit,
        plan.progress_path,
    )
    if source_blob_oid != plan.manifest["source_progress_blob_oid"]:
        raise ProgressError("pinned source progress blob changed after planning")
    variants = checkout_progress_variants(
        root,
        plan.progress_path,
        source_blob_oid,
        source_progress,
        actual_policy,
    )
    variant_digests = {
        key: sha256_bytes(value) for key, value in sorted(variants.items())
    }
    if variant_digests != plan.manifest["allowed_source_variants"]:
        raise ProgressError("Git-proven progress checkout variants changed after planning")
    return root, common, target


def apply_progress_append(
    plan: ProgressAppendPlan,
    *,
    accept_plan_digest: str,
    fault_injector: Callable[[str, str], None] | None = None,
) -> ProgressAppendResult:
    """Apply or resume one exact append under common-dir and target locks."""

    if not isinstance(plan, ProgressAppendPlan):
        raise TypeError("plan must be ProgressAppendPlan")
    if accept_plan_digest != plan.plan_digest:
        raise ProgressError("accepted plan digest does not match the reviewed progress plan")
    root, common, target = _validate_plan_context(plan)
    durable_path = journal_path(common, plan.operation_id, plan.event.event_id)
    locks = (
        _event_lock_path(common, plan.event),
        _target_lock_path(common, root, plan.progress_path, plan.event.iteration),
    )
    with _locks(locks):
        resumed = durable_path.exists()
        if resumed:
            journal = _validate_journal(_read_json(durable_path), durable_path)
            if (
                journal["operation_id"] != plan.operation_id
                or journal["event_id"] != plan.event.event_id
                or journal["plan_digest"] != plan.plan_digest
                or journal["manifest"] != plan.manifest
            ):
                raise ProgressError("durable progress journal differs from the accepted plan")
            if journal["phase"] == "FAILED_NEEDS_RECONCILE":
                raise ProgressError(f"progress operation requires reconcile: {journal.get('error')}")
        else:
            journal = _new_journal(plan)
            _atomic_json_replace(durable_path, journal)

        state = _target_state(target, plan)
        if state == "after":
            journal = _advance_journal(durable_path, journal, "APPLIED")
            return ProgressAppendResult(
                plan.operation_id,
                plan.event.event_id,
                str(root),
                plan.progress_path,
                plan.plan_digest,
                str(journal["phase"]),
                False,
                resumed,
                str(durable_path),
                plan.after_sha256,
            )
        if state != "before":
            message = "progress bytes changed after planning; immutable history requires a new plan"
            _advance_journal(durable_path, journal, "FAILED_NEEDS_RECONCILE", error=message)
            raise ProgressError(message)
        if plan.action == "IDEMPOTENT":
            message = "idempotent progress plan no longer contains its exact event"
            _advance_journal(durable_path, journal, "FAILED_NEEDS_RECONCILE", error=message)
            raise ProgressError(message)

        _, observed_ref = _read_source_progress(root, plan.event, plan.progress_path)
        if observed_ref != plan.event.source_commit:
            message = "source_ref changed after planning; progress append was not applied"
            _advance_journal(durable_path, journal, "FAILED_NEEDS_RECONCILE", error=message)
            raise ProgressError(message)

        journal = _advance_journal(durable_path, journal, "APPLYING")
        if fault_injector is not None:
            fault_injector("before_replace", str(target))

        newline = b"\r\n" if plan.manifest["newline"] == "CRLF" else b"\n"

        def exact_before_cas() -> None:
            if _target_state(target, plan) != "before":
                raise ProgressError("progress bytes changed during atomic append preparation")

        before = _read_bounded(target, MAX_PROGRESS_BYTES, "progress file")
        after = _append_exact_event(before, plan.event_bytes, newline)
        if sha256_bytes(after) != plan.after_sha256:
            raise ProgressError("reconstructed progress result differs from the accepted plan")
        _atomic_bytes_replace(
            target,
            after,
            create_parent=False,
            before_replace=exact_before_cas,
        )
        if fault_injector is not None:
            fault_injector("after_replace_before_journal", str(target))
        if _target_state(target, plan) != "after":
            message = "atomic progress append verification failed"
            _advance_journal(durable_path, journal, "FAILED_NEEDS_RECONCILE", error=message)
            raise ProgressError(message)
        journal = _advance_journal(durable_path, journal, "APPLIED")
        return ProgressAppendResult(
            plan.operation_id,
            plan.event.event_id,
            str(root),
            plan.progress_path,
            plan.plan_digest,
            str(journal["phase"]),
            True,
            resumed,
            str(durable_path),
            plan.after_sha256,
        )


__all__ = [
    "EVENT_SCHEMA",
    "EXCLUSIONS",
    "JOURNAL_SCHEMA",
    "PLAN_SCHEMA",
    "PROGRESS_PATH",
    "ProgressAppendPlan",
    "ProgressAppendResult",
    "ProgressCheckoutPolicy",
    "ProgressError",
    "ProgressEventV2",
    "RESULT_SCHEMA",
    "SimulatedCrash",
    "apply_progress_append",
    "append_progress_event_exact",
    "build_progress_event",
    "candidate_event",
    "checkout_progress_variants",
    "deterministic_event_id",
    "integration_event",
    "journal_path",
    "load_progress_append_plan",
    "open_event",
    "plan_progress_append",
    "resolve_progress_checkout_policy",
    "resolve_git_common_dir",
    "sha256_bytes",
    "source_progress_blob_oid",
    "workspace_event",
]
