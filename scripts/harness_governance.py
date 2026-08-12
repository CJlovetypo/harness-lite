from __future__ import annotations

"""Pure governance reconciliation plans for Harness Lite.

This module deliberately has no filesystem or Git adapter.  Callers provide
already-read immutable snapshots and receive an immutable plan containing
blockers and preview bytes.  Applying a preview, advancing a ref, merging, or
committing belongs to a later orchestration layer.
"""

from dataclasses import dataclass
from bisect import bisect_right
from collections import deque
import hashlib
import re
from typing import Sequence


PRINCIPLE_PLAN_SCHEMA = "harness-lite.principle-reconcile-plan/v1"
PROGRESS_PLAN_SCHEMA = "harness-lite.progress-reconcile-plan/v1"
README_PLAN_SCHEMA = "harness-lite.readme-preview-plan/v1"

# Governance inputs are small Markdown control documents.  Explicit bounds
# prevent an untrusted/corrupt blob from turning a preview into an unbounded
# memory operation.  Callers must not interpret a bound failure as permission
# to truncate: it is a hard reconcile blocker.
MAX_PRINCIPLE_BYTES = 1 * 1024 * 1024
MAX_PROGRESS_BYTES = 16 * 1024 * 1024
MAX_README_BYTES = 4 * 1024 * 1024
MAX_PROGRESS_EVENTS = 10_000
MAX_ROUTING_ITERATIONS = 10_000

FOCUS_START = "<!-- project-harness:focus:start -->"
FOCUS_END = "<!-- project-harness:focus:end -->"
ITERATIONS_START = "<!-- project-harness:iterations:start -->"
ITERATIONS_END = "<!-- project-harness:iterations:end -->"

EVENT_TYPES = frozenset({"OPEN", "DECISION", "CHECKPOINT", "MERGE", "CLOSE"})
_EVENT_HEADING = re.compile(
    rb"^##[ \t]+(?P<event_id>S-[0-9]{8}-[0-9]{2}|EV-[A-Za-z0-9][A-Za-z0-9._-]*)"
    rb"[ \t]*/[ \t]*(?P<event_type>OPEN|DECISION|CHECKPOINT|MERGE|CLOSE)"
    rb"[ \t]*/[ \t]*(?P<occurred_at>[^\r\n]+?)[ \t]*(?:\r?\n)?$"
)
_EVENT_LIKE_HEADING = re.compile(rb"^##[ \t]+(?:S-[0-9]|EV-)")
_ANY_LEVEL_TWO_HEADING = re.compile(rb"^##(?:[ \t]|$)")
_ITERATION_NUMBER = re.compile(r"[0-9]{3,}")
_PARENT_LINE = re.compile(
    r"^[ \t]*-[ \t]*(?:causal_parent|causal-parent|因果父事件)[ \t]*(?::|：)[ \t]*(.*?)[ \t]*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_PARENT_ID = re.compile(
    r"(?:EV-[A-Za-z0-9][A-Za-z0-9._-]*|S-[0-9]{8}-[0-9]{2}(?:/(?:OPEN|DECISION|CHECKPOINT|MERGE|CLOSE))?)"
)


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 identity of exact bytes."""

    return hashlib.sha256(value).hexdigest()


def _require_bytes(name: str, value: object) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    return value


def _document_blockers(
    value: bytes,
    *,
    subject: str,
    maximum_bytes: int,
    code_prefix: str,
) -> list[Blocker]:
    blockers: list[Blocker] = []
    if len(value) > maximum_bytes:
        blockers.append(
            Blocker(
                f"{code_prefix}-input-too-large",
                f"{subject} is {len(value)} bytes; the maximum is {maximum_bytes} bytes.",
                subject,
            )
        )
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        blockers.append(
            Blocker(
                f"{code_prefix}-input-not-utf8",
                f"{subject} is not valid UTF-8.",
                subject,
            )
        )
    return blockers


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str
    subject: str | None = None


@dataclass(frozen=True)
class PrincipleApproval:
    """Approval bound to exact before/after principle bytes."""

    change_id: str
    evidence_ref: str
    exact_before: bytes
    exact_after: bytes


@dataclass(frozen=True)
class PrincipleReconcilePlan:
    schema: str
    status: str
    action: str
    base_sha256: str
    latest_main_sha256: str
    candidate_sha256: str
    result_sha256: str | None
    change_id: str | None
    evidence_ref: str | None
    blockers: tuple[Blocker, ...]
    preview: bytes | None

    @property
    def ready(self) -> bool:
        return self.status == "READY"


def plan_principle_reconciliation(
    *,
    branch_base: bytes,
    latest_main: bytes,
    branch_candidate: bytes,
    approval: PrincipleApproval | None = None,
) -> PrincipleReconcilePlan:
    """Plan the three-way principle authority gate without applying anything.

    ``latest_main`` is the only currently effective principle set.  A branch
    with no principle proposal simply adopts it.  A proposal may be previewed
    only when its exact before/after bytes were approved and main has not
    drifted from the branch base.  Any main drift blocks a branch proposal even
    when textual hunks might appear independent.
    """

    base = _require_bytes("branch_base", branch_base)
    main = _require_bytes("latest_main", latest_main)
    candidate = _require_bytes("branch_candidate", branch_candidate)
    base_hash = sha256_bytes(base)
    main_hash = sha256_bytes(main)
    candidate_hash = sha256_bytes(candidate)

    input_blockers: list[Blocker] = []
    input_blockers.extend(
        _document_blockers(
            base,
            subject="branch-base principle",
            maximum_bytes=MAX_PRINCIPLE_BYTES,
            code_prefix="principle",
        )
    )
    input_blockers.extend(
        _document_blockers(
            main,
            subject="latest-main principle",
            maximum_bytes=MAX_PRINCIPLE_BYTES,
            code_prefix="principle",
        )
    )
    input_blockers.extend(
        _document_blockers(
            candidate,
            subject="branch-candidate principle",
            maximum_bytes=MAX_PRINCIPLE_BYTES,
            code_prefix="principle",
        )
    )
    if input_blockers:
        return PrincipleReconcilePlan(
            schema=PRINCIPLE_PLAN_SCHEMA,
            status="BLOCKED",
            action="BLOCKED",
            base_sha256=base_hash,
            latest_main_sha256=main_hash,
            candidate_sha256=candidate_hash,
            result_sha256=None,
            change_id=approval.change_id.strip() if isinstance(approval, PrincipleApproval) else None,
            evidence_ref=approval.evidence_ref.strip() if isinstance(approval, PrincipleApproval) else None,
            blockers=tuple(input_blockers),
            preview=None,
        )

    if candidate == base:
        return PrincipleReconcilePlan(
            schema=PRINCIPLE_PLAN_SCHEMA,
            status="READY",
            action="ADOPT_LATEST_MAIN",
            base_sha256=base_hash,
            latest_main_sha256=main_hash,
            candidate_sha256=candidate_hash,
            result_sha256=main_hash,
            change_id=None,
            evidence_ref=None,
            blockers=(),
            preview=main,
        )

    blockers: list[Blocker] = []
    change_id: str | None = None
    evidence_ref: str | None = None
    if approval is None:
        blockers.append(
            Blocker(
                "principle-approval-required",
                "The branch proposes a principle change without exact approval.",
            )
        )
    else:
        if not isinstance(approval, PrincipleApproval):
            raise TypeError("approval must be PrincipleApproval or None")
        change_id = approval.change_id.strip()
        evidence_ref = approval.evidence_ref.strip()
        _require_bytes("approval.exact_before", approval.exact_before)
        _require_bytes("approval.exact_after", approval.exact_after)
        if not change_id:
            blockers.append(
                Blocker("principle-change-id-missing", "Exact principle approval requires a stable change ID.")
            )
        if not evidence_ref:
            blockers.append(
                Blocker("principle-evidence-missing", "Exact principle approval requires an evidence reference.")
            )
        if approval.exact_before != base:
            blockers.append(
                Blocker(
                    "principle-approved-before-mismatch",
                    "Approved before-bytes do not match the branch principle base.",
                )
            )
        if approval.exact_after != candidate:
            blockers.append(
                Blocker(
                    "principle-approved-after-mismatch",
                    "Approved after-bytes do not match the branch principle candidate.",
                )
            )

    if main != base:
        blockers.append(
            Blocker(
                "principle-main-drift",
                "Latest main differs from the approved branch base; the final combined principle text must be reviewed and re-approved.",
            )
        )

    if blockers:
        return PrincipleReconcilePlan(
            schema=PRINCIPLE_PLAN_SCHEMA,
            status="BLOCKED",
            action="BLOCKED",
            base_sha256=base_hash,
            latest_main_sha256=main_hash,
            candidate_sha256=candidate_hash,
            result_sha256=None,
            change_id=change_id,
            evidence_ref=evidence_ref,
            blockers=tuple(blockers),
            preview=None,
        )

    return PrincipleReconcilePlan(
        schema=PRINCIPLE_PLAN_SCHEMA,
        status="READY",
        action="APPLY_APPROVED_EXACT_CHANGE",
        base_sha256=base_hash,
        latest_main_sha256=main_hash,
        candidate_sha256=candidate_hash,
        result_sha256=candidate_hash,
        change_id=change_id,
        evidence_ref=evidence_ref,
        blockers=(),
        preview=candidate,
    )


@dataclass(frozen=True)
class ProgressEvent:
    event_id: str
    identity: str
    event_type: str
    occurred_at: str
    causal_parent: str | None
    exact_bytes: bytes
    ordinal: int


@dataclass(frozen=True)
class ParsedProgress:
    source: str
    events: tuple[ProgressEvent, ...]
    blockers: tuple[Blocker, ...]


@dataclass(frozen=True)
class ProgressReconcilePlan:
    schema: str
    status: str
    base_sha256: str
    latest_main_sha256: str
    candidate_sha256: str
    result_sha256: str | None
    base_event_identities: tuple[str, ...]
    main_event_identities: tuple[str, ...]
    candidate_event_identities: tuple[str, ...]
    appended_event_identities: tuple[str, ...]
    deduplicated_event_identities: tuple[str, ...]
    blockers: tuple[Blocker, ...]
    preview: bytes | None

    @property
    def ready(self) -> bool:
        return self.status == "READY"


def _line_without_ending(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def _event_identity(event_id: str, event_type: str) -> str:
    # Legacy S-* is a session identity and existing logs legitimately contain
    # one OPEN and one CLOSE for the same session.  The immutable block key is
    # therefore session/type.  New EV-* identities are globally unique alone.
    return event_id if event_id.startswith("EV-") else f"{event_id}/{event_type}"


def _parse_causal_parent(
    block: bytes,
    source: str,
    identity: str,
) -> tuple[str | None, bool, list[Blocker]]:
    try:
        text = block.decode("utf-8")
    except UnicodeDecodeError:
        return None, False, [
            Blocker(
                "progress-event-not-utf8",
                f"Event {identity} in {source} is not valid UTF-8.",
                identity,
            )
        ]
    matches = _PARENT_LINE.findall(text)
    if len(matches) > 1:
        return None, True, [
            Blocker(
                "progress-duplicate-causal-parent",
                f"Event {identity} in {source} declares causal_parent more than once.",
                identity,
            )
        ]
    if not matches:
        return None, False, []
    value = matches[0].strip().strip("`").strip()
    if value.lower() in {"", "none", "null", "n/a"} or value == "无":
        return None, True, []
    if not _PARENT_ID.fullmatch(value):
        return None, True, [
            Blocker(
                "progress-invalid-causal-parent",
                f"Event {identity} in {source} has an invalid causal_parent: {value!r}.",
                identity,
            )
        ]
    return value, True, []


def parse_progress_events(content: bytes, *, source: str) -> ParsedProgress:
    """Parse immutable progress event blocks while preserving their bytes."""

    raw = _require_bytes("content", content)
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")

    blockers = _document_blockers(
        raw,
        subject=source,
        maximum_bytes=MAX_PROGRESS_BYTES,
        code_prefix="progress",
    )

    lines = raw.splitlines(keepends=True)
    line_offsets: list[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line)

    canonical: dict[int, re.Match[bytes]] = {}
    level_two_indexes: list[int] = []
    for index, line in enumerate(lines):
        bare = _line_without_ending(line)
        if _ANY_LEVEL_TWO_HEADING.match(bare):
            level_two_indexes.append(index)
        match = _EVENT_HEADING.match(line)
        if match:
            canonical[index] = match
        elif _EVENT_LIKE_HEADING.match(bare):
            heading = bare.decode("utf-8", errors="replace")
            blockers.append(
                Blocker(
                    "progress-malformed-event-heading",
                    f"Malformed event heading in {source}: {heading}",
                )
            )

    if len(canonical) > MAX_PROGRESS_EVENTS:
        blockers.append(
            Blocker(
                "progress-event-count-exceeded",
                f"{source} contains {len(canonical)} events; the maximum is {MAX_PROGRESS_EVENTS}.",
                source,
            )
        )
        return ParsedProgress(source=source, events=(), blockers=tuple(blockers))

    events: list[ProgressEvent] = []
    identities: set[str] = set()
    for ordinal, start_line in enumerate(sorted(canonical)):
        match = canonical[start_line]
        next_h2_index = bisect_right(level_two_indexes, start_line)
        next_h2 = (
            level_two_indexes[next_h2_index]
            if next_h2_index < len(level_two_indexes)
            else len(lines)
        )
        content_end_line = next_h2
        while content_end_line > start_line + 1:
            trailing = _line_without_ending(lines[content_end_line - 1])
            if trailing.strip(b" \t"):
                break
            content_end_line -= 1
        start_offset = line_offsets[start_line]
        if content_end_line >= len(lines):
            end_offset = len(raw)
        else:
            end_offset = line_offsets[content_end_line]
        block = raw[start_offset:end_offset]
        event_id = match.group("event_id").decode("ascii")
        event_type = match.group("event_type").decode("ascii")
        occurred_at = match.group("occurred_at").decode("utf-8", errors="replace").strip()
        identity = _event_identity(event_id, event_type)
        parent, parent_declared, parent_blockers = _parse_causal_parent(block, source, identity)
        blockers.extend(parent_blockers)
        if event_id.startswith("EV-") and not parent_declared:
            blockers.append(
                Blocker(
                    "progress-causal-parent-missing",
                    f"New event {identity} in {source} must explicitly declare causal_parent (use none for a root event).",
                    identity,
                )
            )
        if identity in identities:
            blockers.append(
                Blocker(
                    "progress-duplicate-event-identity",
                    f"Event identity {identity} occurs more than once in {source}.",
                    identity,
                )
            )
        identities.add(identity)
        events.append(
            ProgressEvent(
                event_id=event_id,
                identity=identity,
                event_type=event_type,
                occurred_at=occurred_at,
                causal_parent=parent,
                exact_bytes=block,
                ordinal=ordinal,
            )
        )

    return ParsedProgress(source=source, events=tuple(events), blockers=tuple(blockers))


def _check_base_history(
    base: Sequence[ProgressEvent],
    current: Sequence[ProgressEvent],
    *,
    current_name: str,
) -> list[Blocker]:
    blockers: list[Blocker] = []
    if len(current) < len(base):
        blockers.append(
            Blocker(
                f"progress-{current_name}-deletes-base-history",
                f"{current_name} is missing one or more immutable base events.",
            )
        )
    prefix_count = min(len(base), len(current))
    for index in range(prefix_count):
        expected = base[index]
        actual = current[index]
        if actual.identity != expected.identity:
            blockers.append(
                Blocker(
                    f"progress-{current_name}-reorders-base-history",
                    f"{current_name} event {index + 1} is {actual.identity}, expected immutable base event {expected.identity}.",
                    expected.identity,
                )
            )
            continue
        if actual.exact_bytes != expected.exact_bytes:
            blockers.append(
                Blocker(
                    f"progress-{current_name}-rewrites-base-event",
                    f"{current_name} changed the exact bytes of immutable base event {expected.identity}.",
                    expected.identity,
                )
            )
    return blockers


def _resolve_parent_identity(
    reference: str,
    events: Sequence[ProgressEvent],
) -> tuple[str | None, bool]:
    by_identity = {event.identity: event.identity for event in events}
    if reference in by_identity:
        return reference, False
    aliases: dict[str, list[str]] = {}
    for event in events:
        aliases.setdefault(event.event_id, []).append(event.identity)
    matches = aliases.get(reference, [])
    if len(matches) == 1:
        return matches[0], False
    return None, len(matches) > 1


def _check_causal_order(events: Sequence[ProgressEvent]) -> list[Blocker]:
    blockers: list[Blocker] = []
    positions = {event.identity: index for index, event in enumerate(events)}
    resolved_parents: dict[str, str] = {}
    for index, event in enumerate(events):
        if event.causal_parent is None:
            continue
        parent, ambiguous = _resolve_parent_identity(event.causal_parent, events)
        if ambiguous:
            blockers.append(
                Blocker(
                    "progress-ambiguous-causal-parent",
                    f"Event {event.identity} refers to ambiguous legacy parent {event.causal_parent}.",
                    event.identity,
                )
            )
        elif parent is None:
            blockers.append(
                Blocker(
                    "progress-missing-causal-parent",
                    f"Event {event.identity} refers to missing parent {event.causal_parent}.",
                    event.identity,
                )
            )
        else:
            resolved_parents[event.identity] = parent
            if positions[parent] >= index:
                blockers.append(
                    Blocker(
                        "progress-causal-order-invalid",
                        f"Event {event.identity} appears before or at its causal parent {parent}.",
                        event.identity,
                    )
                )

    completed: set[str] = set()
    reported_cycles: set[frozenset[str]] = set()
    for start in positions:
        if start in completed:
            continue
        path: list[str] = []
        path_positions: dict[str, int] = {}
        current: str | None = start
        while current is not None and current not in completed and current not in path_positions:
            path_positions[current] = len(path)
            path.append(current)
            current = resolved_parents.get(current)
        if current is not None and current in path_positions:
            cycle = [*path[path_positions[current] :], current]
            key = frozenset(cycle)
            if key not in reported_cycles:
                reported_cycles.add(key)
                blockers.append(
                    Blocker(
                        "progress-causal-cycle",
                        "Progress causal dependencies contain a cycle: " + " -> ".join(cycle) + ".",
                        current,
                    )
                )
        completed.update(path)
    return blockers


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


def plan_progress_union(
    *,
    branch_base: bytes,
    latest_main: bytes,
    branch_candidate: bytes,
) -> ProgressReconcilePlan:
    """Plan an append-only semantic union of progress event blocks."""

    base_raw = _require_bytes("branch_base", branch_base)
    main_raw = _require_bytes("latest_main", latest_main)
    candidate_raw = _require_bytes("branch_candidate", branch_candidate)
    base = parse_progress_events(base_raw, source="branch-base")
    main = parse_progress_events(main_raw, source="latest-main")
    candidate = parse_progress_events(candidate_raw, source="branch-candidate")
    blockers = [*base.blockers, *main.blockers, *candidate.blockers]
    blockers.extend(_check_base_history(base.events, main.events, current_name="main"))
    blockers.extend(_check_base_history(base.events, candidate.events, current_name="candidate"))

    main_by_identity = {event.identity: event for event in main.events}
    base_identities = {event.identity for event in base.events}
    appended: list[ProgressEvent] = []
    deduplicated: list[str] = []
    for event in candidate.events[len(base.events) :]:
        existing = main_by_identity.get(event.identity)
        if existing is None:
            appended.append(event)
            continue
        if existing.exact_bytes != event.exact_bytes:
            blockers.append(
                Blocker(
                    "progress-same-id-different-bytes",
                    f"Event {event.identity} has different bytes in latest-main and branch-candidate.",
                    event.identity,
                )
            )
        elif event.identity not in base_identities:
            deduplicated.append(event.identity)

    final_events = [*main.events, *appended]
    blockers.extend(_check_causal_order(final_events))

    if blockers:
        return ProgressReconcilePlan(
            schema=PROGRESS_PLAN_SCHEMA,
            status="BLOCKED",
            base_sha256=sha256_bytes(base_raw),
            latest_main_sha256=sha256_bytes(main_raw),
            candidate_sha256=sha256_bytes(candidate_raw),
            result_sha256=None,
            base_event_identities=tuple(event.identity for event in base.events),
            main_event_identities=tuple(event.identity for event in main.events),
            candidate_event_identities=tuple(event.identity for event in candidate.events),
            appended_event_identities=tuple(event.identity for event in appended),
            deduplicated_event_identities=tuple(deduplicated),
            blockers=tuple(blockers),
            preview=None,
        )

    preview = main_raw
    newline = _detect_newline(main_raw)
    for event in appended:
        preview = _append_exact_event(preview, event.exact_bytes, newline)
    preview_blockers = _document_blockers(
        preview,
        subject="merged progress preview",
        maximum_bytes=MAX_PROGRESS_BYTES,
        code_prefix="progress-preview",
    )
    if preview_blockers:
        return ProgressReconcilePlan(
            schema=PROGRESS_PLAN_SCHEMA,
            status="BLOCKED",
            base_sha256=sha256_bytes(base_raw),
            latest_main_sha256=sha256_bytes(main_raw),
            candidate_sha256=sha256_bytes(candidate_raw),
            result_sha256=None,
            base_event_identities=tuple(event.identity for event in base.events),
            main_event_identities=tuple(event.identity for event in main.events),
            candidate_event_identities=tuple(event.identity for event in candidate.events),
            appended_event_identities=tuple(event.identity for event in appended),
            deduplicated_event_identities=tuple(deduplicated),
            blockers=tuple(preview_blockers),
            preview=None,
        )
    return ProgressReconcilePlan(
        schema=PROGRESS_PLAN_SCHEMA,
        status="READY",
        base_sha256=sha256_bytes(base_raw),
        latest_main_sha256=sha256_bytes(main_raw),
        candidate_sha256=sha256_bytes(candidate_raw),
        result_sha256=sha256_bytes(preview),
        base_event_identities=tuple(event.identity for event in base.events),
        main_event_identities=tuple(event.identity for event in main.events),
        candidate_event_identities=tuple(event.identity for event in candidate.events),
        appended_event_identities=tuple(event.identity for event in appended),
        deduplicated_event_identities=tuple(deduplicated),
        blockers=(),
        preview=preview,
    )


@dataclass(frozen=True)
class ManagedSection:
    name: str
    start_marker: str
    end_marker: str
    body: bytes


@dataclass(frozen=True)
class ManagedSectionChange:
    name: str
    before_sha256: str
    after_sha256: str
    changed: bool


@dataclass(frozen=True)
class ReadmePreviewPlan:
    schema: str
    status: str
    authority_id: str | None
    original_sha256: str
    result_sha256: str | None
    changed: bool
    sections: tuple[ManagedSectionChange, ...]
    blockers: tuple[Blocker, ...]
    preview: bytes | None

    @property
    def ready(self) -> bool:
        return self.status == "READY"


@dataclass(frozen=True)
class IterationRoutingState:
    number: str
    title: str
    prd_status: str
    spec_status: str
    open_deviations: int
    workspace: str
    governance_gate: str
    candidate_state: str
    integration_state: str
    result: str
    next_step: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class RootRoutingAuthority:
    authority_id: str
    current_iteration: str | None
    global_gate: str
    next_step: str
    iterations: tuple[IterationRoutingState, ...]


def _marker_line_span(content: bytes, marker: str) -> tuple[int, int] | None:
    marker_bytes = marker.encode("utf-8")
    matches: list[tuple[int, int]] = []
    offset = 0
    for line in content.splitlines(keepends=True):
        if _line_without_ending(line) == marker_bytes:
            matches.append((offset, offset + len(line)))
        offset += len(line)
    if not content.endswith((b"\n", b"\r")) and offset < len(content):
        # splitlines(keepends=True) already includes a final unterminated line;
        # this branch is defensive for alternate bytes implementations.
        final = content[offset:]
        if final == marker_bytes:
            matches.append((offset, len(content)))
    if len(matches) != 1:
        return None
    return matches[0]


def preview_managed_markdown(
    document: bytes,
    *,
    sections: Sequence[ManagedSection],
    authority_id: str | None = None,
) -> ReadmePreviewPlan:
    """Replace only uniquely bounded managed sections and return preview bytes."""

    raw = _require_bytes("document", document)
    blockers = _document_blockers(
        raw,
        subject="README document",
        maximum_bytes=MAX_README_BYTES,
        code_prefix="readme",
    )
    if blockers:
        return ReadmePreviewPlan(
            schema=README_PLAN_SCHEMA,
            status="BLOCKED",
            authority_id=authority_id,
            original_sha256=sha256_bytes(raw),
            result_sha256=None,
            changed=False,
            sections=(),
            blockers=tuple(blockers),
            preview=None,
        )
    ranges: list[tuple[int, int, int, int, ManagedSection]] = []
    names: set[str] = set()
    markers: set[str] = set()
    for section in sections:
        if not isinstance(section, ManagedSection):
            raise TypeError("sections must contain ManagedSection values")
        _require_bytes(f"section {section.name!r} body", section.body)
        blockers.extend(
            _document_blockers(
                section.body,
                subject=f"managed README section {section.name!r}",
                maximum_bytes=MAX_README_BYTES,
                code_prefix="readme-section",
            )
        )
        if not section.name.strip() or section.name in names:
            blockers.append(
                Blocker("readme-managed-section-name-invalid", "Managed section names must be non-empty and unique.")
            )
        names.add(section.name)
        if (
            not section.start_marker.strip()
            or not section.end_marker.strip()
            or section.start_marker == section.end_marker
            or section.start_marker in markers
            or section.end_marker in markers
        ):
            blockers.append(
                Blocker(
                    "readme-managed-marker-invalid",
                    f"Managed section {section.name!r} must use unique, distinct markers.",
                    section.name,
                )
            )
            continue
        markers.update({section.start_marker, section.end_marker})
        start = _marker_line_span(raw, section.start_marker)
        end = _marker_line_span(raw, section.end_marker)
        if start is None or end is None:
            blockers.append(
                Blocker(
                    "readme-managed-marker-missing-or-duplicate",
                    f"Managed section {section.name!r} does not have exactly one start and one end marker.",
                    section.name,
                )
            )
            continue
        if start[0] >= end[0]:
            blockers.append(
                Blocker(
                    "readme-managed-marker-order-invalid",
                    f"Managed section {section.name!r} has reversed markers.",
                    section.name,
                )
            )
            continue
        ranges.append((start[0], start[1], end[0], end[1], section))

    ranges.sort(key=lambda item: item[0])
    for previous, current in zip(ranges, ranges[1:]):
        if current[0] < previous[3]:
            blockers.append(
                Blocker(
                    "readme-managed-sections-overlap",
                    f"Managed sections {previous[4].name!r} and {current[4].name!r} overlap.",
                )
            )

    if blockers:
        return ReadmePreviewPlan(
            schema=README_PLAN_SCHEMA,
            status="BLOCKED",
            authority_id=authority_id,
            original_sha256=sha256_bytes(raw),
            result_sha256=None,
            changed=False,
            sections=(),
            blockers=tuple(blockers),
            preview=None,
        )

    output = bytearray()
    changes: list[ManagedSectionChange] = []
    cursor = 0
    for start_offset, body_start, body_end, _end_offset, section in ranges:
        output.extend(raw[cursor:body_start])
        before = raw[body_start:body_end]
        newline = _detect_newline(raw[start_offset:body_start] or raw)
        normalized_body = section.body.rstrip(b"\r\n")
        after = normalized_body + newline if normalized_body else b""
        output.extend(after)
        changes.append(
            ManagedSectionChange(
                name=section.name,
                before_sha256=sha256_bytes(before),
                after_sha256=sha256_bytes(after),
                changed=before != after,
            )
        )
        cursor = body_end
    output.extend(raw[cursor:])
    preview = bytes(output)
    preview_blockers = _document_blockers(
        preview,
        subject="rebuilt README preview",
        maximum_bytes=MAX_README_BYTES,
        code_prefix="readme-preview",
    )
    if preview_blockers:
        return ReadmePreviewPlan(
            schema=README_PLAN_SCHEMA,
            status="BLOCKED",
            authority_id=authority_id,
            original_sha256=sha256_bytes(raw),
            result_sha256=None,
            changed=False,
            sections=tuple(changes),
            blockers=tuple(preview_blockers),
            preview=None,
        )
    return ReadmePreviewPlan(
        schema=README_PLAN_SCHEMA,
        status="READY",
        authority_id=authority_id,
        original_sha256=sha256_bytes(raw),
        result_sha256=sha256_bytes(preview),
        changed=preview != raw,
        sections=tuple(changes),
        blockers=(),
        preview=preview,
    )


def _one_line(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text.replace("|", r"\|")


def _validate_routing_authority(authority: RootRoutingAuthority) -> tuple[list[IterationRoutingState], list[Blocker]]:
    if not isinstance(authority, RootRoutingAuthority):
        raise TypeError("authority must be RootRoutingAuthority")
    blockers: list[Blocker] = []
    if not authority.authority_id.strip():
        blockers.append(Blocker("readme-authority-id-missing", "README rebuild requires an authority snapshot ID."))
    iterations = list(authority.iterations)
    if len(iterations) > MAX_ROUTING_ITERATIONS:
        blockers.append(
            Blocker(
                "readme-iteration-count-exceeded",
                f"Routing authority contains {len(iterations)} iterations; the maximum is {MAX_ROUTING_ITERATIONS}.",
            )
        )
        return [], blockers
    by_number: dict[str, IterationRoutingState] = {}
    known_numbers = {
        iteration.number
        for iteration in iterations
        if isinstance(iteration, IterationRoutingState)
        and isinstance(iteration.number, str)
        and _ITERATION_NUMBER.fullmatch(iteration.number)
    }
    seen_numeric: set[int] = set()
    for iteration in iterations:
        if not isinstance(iteration, IterationRoutingState):
            raise TypeError("authority.iterations must contain IterationRoutingState values")
        if not _ITERATION_NUMBER.fullmatch(iteration.number):
            blockers.append(
                Blocker(
                    "readme-iteration-number-invalid",
                    f"Iteration number {iteration.number!r} must be at least three decimal digits.",
                    iteration.number,
                )
            )
            continue
        if iteration.number in by_number:
            blockers.append(
                Blocker(
                    "readme-iteration-number-duplicate",
                    f"Iteration number {iteration.number} occurs more than once.",
                    iteration.number,
                )
            )
        by_number[iteration.number] = iteration
        numeric = int(iteration.number)
        if numeric in seen_numeric:
            blockers.append(
                Blocker(
                    "readme-iteration-number-duplicate",
                    f"Iteration decimal identity {numeric} occurs more than once.",
                    iteration.number,
                )
            )
        seen_numeric.add(numeric)
        if iteration.open_deviations < 0:
            blockers.append(
                Blocker(
                    "readme-open-deviation-count-invalid",
                    f"Iteration {iteration.number} has a negative open deviation count.",
                    iteration.number,
                )
            )
        required = {
            "title": iteration.title,
            "prd_status": iteration.prd_status,
            "spec_status": iteration.spec_status,
            "workspace": iteration.workspace,
            "governance_gate": iteration.governance_gate,
            "candidate_state": iteration.candidate_state,
            "integration_state": iteration.integration_state,
            "result": iteration.result,
            "next_step": iteration.next_step,
        }
        for field_name, value in required.items():
            if not str(value).strip():
                blockers.append(
                    Blocker(
                        "readme-routing-field-missing",
                        f"Iteration {iteration.number} is missing {field_name}.",
                        iteration.number,
                    )
                )
        for dependency in iteration.depends_on:
            if not _ITERATION_NUMBER.fullmatch(dependency):
                blockers.append(
                    Blocker(
                        "readme-dependency-invalid",
                        f"Iteration {iteration.number} has invalid dependency {dependency!r}.",
                        iteration.number,
                    )
                )
            elif dependency not in known_numbers:
                blockers.append(
                    Blocker(
                        "readme-dependency-missing",
                        f"Iteration {iteration.number} depends on absent iteration {dependency}.",
                        iteration.number,
                    )
                )

    dependency_graph = {
        iteration.number: tuple(
            dependency for dependency in iteration.depends_on if dependency in by_number
        )
        for iteration in iterations
        if isinstance(iteration, IterationRoutingState) and iteration.number in by_number
    }
    incoming = {number: 0 for number in dependency_graph}
    for dependencies in dependency_graph.values():
        for dependency in dependencies:
            incoming[dependency] += 1
    ready = deque(sorted((number for number, count in incoming.items() if count == 0), key=lambda value: (int(value), value)))
    visited = 0
    while ready:
        number = ready.popleft()
        visited += 1
        for dependency in dependency_graph.get(number, ()):
            incoming[dependency] -= 1
            if incoming[dependency] == 0:
                ready.append(dependency)
    if visited != len(dependency_graph):
        remaining = sorted(
            (number for number, count in incoming.items() if count > 0),
            key=lambda value: (int(value), value),
        )
        blockers.append(
            Blocker(
                "readme-dependency-cycle",
                "Iteration routing dependencies cannot be topologically ordered; cyclic remainder: "
                + ", ".join(remaining)
                + ".",
                remaining[0] if remaining else None,
            )
        )
    iterations.sort(key=lambda item: (int(item.number) if item.number.isdigit() else 10**30, item.number))
    if authority.current_iteration is not None:
        current = next((item for item in iterations if item.number == authority.current_iteration), None)
        if current is None:
            blockers.append(
                Blocker(
                    "readme-current-iteration-missing",
                    f"Current iteration {authority.current_iteration!r} is absent from authoritative routing state.",
                    authority.current_iteration,
                )
            )
    if not authority.global_gate.strip():
        blockers.append(Blocker("readme-global-gate-missing", "README rebuild requires a global gate."))
    if not authority.next_step.strip():
        blockers.append(Blocker("readme-next-step-missing", "README rebuild requires a next step."))
    return iterations, blockers


def preview_root_readme(
    document: bytes,
    *,
    authority: RootRoutingAuthority,
) -> ReadmePreviewPlan:
    """Deterministically rebuild the L0 focus and iteration registry blocks."""

    raw = _require_bytes("document", document)
    iterations, blockers = _validate_routing_authority(authority)
    if blockers:
        return ReadmePreviewPlan(
            schema=README_PLAN_SCHEMA,
            status="BLOCKED",
            authority_id=authority.authority_id,
            original_sha256=sha256_bytes(raw),
            result_sha256=None,
            changed=False,
            sections=(),
            blockers=tuple(blockers),
            preview=None,
        )

    current = next(
        (item for item in iterations if item.number == authority.current_iteration),
        None,
    )
    if current is None:
        focus_lines = [
            "- 当前迭代：无活跃迭代。",
            f"- 当前门禁：{_one_line(authority.global_gate)}。",
            f"- 下一步：{_one_line(authority.next_step)}。",
        ]
    else:
        focus_lines = [
            f"- 当前迭代：[{current.number}](iterations/{current.number}/README.md) — {_one_line(current.title)}。",
            f"- 执行位置：{_one_line(current.workspace)}。",
            f"- 当前门禁：{_one_line(authority.global_gate)}。",
            f"- 下一步：{_one_line(authority.next_step)}。",
        ]

    rows: list[str] = []
    for iteration in iterations:
        dependencies = "、".join(iteration.depends_on) if iteration.depends_on else "无"
        result = "；".join(
            (
                _one_line(iteration.result),
                f"工作区：{_one_line(iteration.workspace)}",
                f"候选：{_one_line(iteration.candidate_state)}",
                f"集成：{_one_line(iteration.integration_state)}",
            )
        )
        next_step = "；".join(
            (
                f"门禁：{_one_line(iteration.governance_gate)}",
                f"依赖：{_one_line(dependencies)}",
                _one_line(iteration.next_step),
            )
        )
        rows.append(
            f"| [{iteration.number}](iterations/{iteration.number}/README.md) | {_one_line(iteration.title)} | "
            f"{_one_line(iteration.prd_status)} | {_one_line(iteration.spec_status)} | "
            f"{iteration.open_deviations} | {result} | {next_step} | "
            f"[进入](iterations/{iteration.number}/README.md) |"
        )

    newline = _detect_newline(raw).decode("ascii")
    return preview_managed_markdown(
        raw,
        authority_id=authority.authority_id,
        sections=(
            ManagedSection(
                name="focus",
                start_marker=FOCUS_START,
                end_marker=FOCUS_END,
                body=newline.join(focus_lines).encode("utf-8"),
            ),
            ManagedSection(
                name="iterations",
                start_marker=ITERATIONS_START,
                end_marker=ITERATIONS_END,
                body=newline.join(rows).encode("utf-8"),
            ),
        ),
    )


__all__ = [
    "Blocker",
    "FOCUS_END",
    "FOCUS_START",
    "ITERATIONS_END",
    "ITERATIONS_START",
    "IterationRoutingState",
    "ManagedSection",
    "ManagedSectionChange",
    "ParsedProgress",
    "PrincipleApproval",
    "PrincipleReconcilePlan",
    "ProgressEvent",
    "ProgressReconcilePlan",
    "ReadmePreviewPlan",
    "RootRoutingAuthority",
    "parse_progress_events",
    "plan_principle_reconciliation",
    "plan_progress_union",
    "preview_managed_markdown",
    "preview_root_readme",
    "sha256_bytes",
]
