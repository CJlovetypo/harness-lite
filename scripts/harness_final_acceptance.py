#!/usr/bin/env python3
"""Public Git-backed registry for exact Harness final acceptance.

Final acceptance is a distinct authority from integrated-candidate evidence.
The canonical JSON blob records the user's exact ``advance-main``
confirmation and the exact ``MainAdvancePlan`` it accepted.  The blob refs are
created in the *same* compare-and-swap transaction as main and the
per-iteration ``integrated``/``final`` refs.  Consequently a public loader can
reconstruct acceptance after every private operation journal has been lost.

This module never pushes, rewrites history, stashes, resets, or cleans.  It is
imported lazily by ``harness_train`` so importing the train remains acyclic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Mapping, Sequence
import uuid

try:
    from . import harness_integrated_evidence as integrated_registry
    from . import harness_train as train
except ImportError:  # pragma: no cover - direct execution
    import harness_integrated_evidence as integrated_registry
    import harness_train as train


EVIDENCE_SCHEMA = "harness-lite.final-acceptance/v1"
PLAN_SCHEMA = "harness-lite.final-acceptance-plan/v1"
REGISTRATION_SCHEMA = "harness-lite.final-acceptance-registration/v1"
JOURNAL_SCHEMA = "harness-lite.final-acceptance-journal/v1"
REF_ROOT = "refs/project-harness/v2"
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024

OID_RE = re.compile(r"[0-9a-f]{40,64}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
ITERATION_RE = re.compile(r"[0-9]{3,}")
OPERATION_RE = re.compile(r"OP-[0-9a-f]{32}")
EVENT_ID_RE = re.compile(r"EV-[A-Za-z0-9][A-Za-z0-9._-]*")


class FinalAcceptanceError(RuntimeError):
    """Raised when final acceptance cannot be authenticated or applied."""


class InjectedCrash(RuntimeError):
    """Focused failure-injection exception used by registry tests."""


@dataclass(frozen=True)
class RefBinding:
    ref_name: str
    object_id: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RefUpdate:
    ref_name: str
    old_object_id: str | None
    new_object_id: str

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class AcceptedCandidate:
    iteration: str
    generation: str
    candidate_ref: str
    candidate_commit: str
    candidate_evidence_ref: str
    candidate_evidence_blob: str
    candidate_registration_digest: str
    principle_gate_binding_digest: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class IterationFinalEvidenceRef:
    iteration: str
    ref_name: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class FinalAcceptanceEnvelope:
    schema_version: str
    operation_id: str
    canonical_operation: str
    main_plan_schema_version: str
    main_plan_digest: str
    main_plan_snapshot: Mapping[str, object]
    integrated_registration_digest: str
    integrated_metadata_digest: str
    integrated_evidence_blob: str
    integrated_commit_ref: str
    integrated_evidence_ref: str
    integrated_iteration_evidence_refs: tuple[RefBinding, ...]
    main_ref: str
    previous_main: str
    accepted_main: str
    accepted_tree: str
    principle_path: str
    principle_sha256: str
    accepted_candidates: tuple[AcceptedCandidate, ...]
    source_ref_bindings: tuple[RefBinding, ...]
    main_ref_updates: tuple[RefUpdate, ...]
    confirmation_action: str
    confirmation_subject_digest: str
    confirmation_authorization_id: str
    confirmation_token_digest: str
    main_advanced_event_ids: tuple[str, ...]
    operation_final_evidence_ref: str
    iteration_final_evidence_refs: tuple[IterationFinalEvidenceRef, ...]
    metadata_digest: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FinalAcceptancePlan:
    schema_version: str
    operation_id: str
    project_root: str
    git_common_dir: str
    metadata_blob: str
    metadata: FinalAcceptanceEnvelope
    journal_path: str
    plan_digest: str
    blockers: tuple[train.Blocker, ...]
    requires_confirmation: bool = True
    pushed: bool = False

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredFinalAcceptance:
    schema_version: str
    operation_id: str
    project_root: str
    evidence_ref: str
    evidence_blob: str
    iteration_evidence_refs: tuple[IterationFinalEvidenceRef, ...]
    metadata: FinalAcceptanceEnvelope
    registration_digest: str
    journal_path: str
    idempotent: bool
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


Failpoint = Callable[[str], None]


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _validate_oid(value: str, label: str) -> str:
    if not isinstance(value, str) or OID_RE.fullmatch(value.strip().lower()) is None:
        raise FinalAcceptanceError(f"{label} must be a full Git object ID")
    return value.strip().lower()


def _validate_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value.strip().lower()) is None:
        raise FinalAcceptanceError(f"{label} must be a SHA-256 digest")
    return value.strip().lower()


def _validate_operation(value: str) -> str:
    if not isinstance(value, str) or OPERATION_RE.fullmatch(value.strip()) is None:
        raise FinalAcceptanceError("operation_id must be OP- followed by 32 lowercase hexadecimal characters")
    return value.strip()


def _validate_iteration(value: str) -> str:
    if not isinstance(value, str):
        raise FinalAcceptanceError("iteration must be a canonical NNN identity")
    number = value.strip()
    if (
        ITERATION_RE.fullmatch(number) is None
        or number != f"{int(number):03d}"
        or int(number) < 1
    ):
        raise FinalAcceptanceError("iteration must be a canonical NNN identity")
    return number


def _validate_ref(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise FinalAcceptanceError(f"{label} must be an explicit full ref")
    reference = value.strip()
    if not reference.startswith("refs/") or any(char.isspace() or ord(char) < 32 for char in reference):
        raise FinalAcceptanceError(f"{label} must be an explicit full ref")
    if any(char in reference for char in "~^:?*[\\") or "@{" in reference or ".." in reference:
        raise FinalAcceptanceError(f"{label} is malformed")
    if reference.endswith(("/", ".")) or any(
        part in {"", ".", ".."} or part.endswith(".lock") for part in reference.split("/")
    ):
        raise FinalAcceptanceError(f"{label} is malformed")
    return reference


def canonical_operation(operation_id: str) -> str:
    return _validate_operation(operation_id).lower()


def operation_final_evidence_ref(operation_id: str) -> str:
    return f"{REF_ROOT}/integrations/{canonical_operation(operation_id)}/final-acceptance"


def iteration_final_evidence_ref(iteration: str) -> str:
    return f"{REF_ROOT}/iterations/{_validate_iteration(iteration)}/final-evidence"


def _git(
    repo: train.Repository,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["PATH"] = repo.git_exec_path + os.pathsep + environment.get("PATH", "")
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    ):
        environment.pop(name, None)
    result = subprocess.run(
        [repo.git, "-C", str(repo.root), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise FinalAcceptanceError(
            f"git {' '.join(arguments)} failed: {detail or 'unknown Git error'}"
        )
    return result


def _resolve_ref(repo: train.Repository, reference: str) -> str | None:
    result = _git(repo, ["rev-parse", "--verify", "--end-of-options", reference], check=False)
    if result.returncode != 0:
        return None
    try:
        return _validate_oid(result.stdout.decode("ascii", errors="strict").strip(), reference)
    except (UnicodeDecodeError, FinalAcceptanceError):
        return None


def _object_type(repo: train.Repository, oid: str) -> str | None:
    result = _git(repo, ["cat-file", "-t", oid], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="replace").strip()


def _blob_bytes(repo: train.Repository, oid: str) -> bytes:
    result = _git(repo, ["cat-file", "blob", _validate_oid(oid, "evidence blob")])
    if len(result.stdout) > MAX_METADATA_BYTES:
        raise FinalAcceptanceError("final acceptance evidence exceeds the size limit")
    return result.stdout


def _hash_blob(repo: train.Repository, raw: bytes, *, write: bool) -> str:
    arguments = ["hash-object"]
    if write:
        arguments.append("-w")
    arguments.append("--stdin")
    result = _git(repo, arguments, input_bytes=raw)
    return _validate_oid(result.stdout.decode("ascii", errors="strict").strip(), "evidence blob")


def _is_ancestor(repo: train.Repository, ancestor: str, descendant: str) -> bool:
    return _git(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode == 0


def _metadata_payload(envelope: FinalAcceptanceEnvelope) -> dict[str, object]:
    value = envelope.as_dict()
    value.pop("metadata_digest", None)
    return value


def metadata_digest(envelope: FinalAcceptanceEnvelope) -> str:
    return digest(_metadata_payload(envelope))


def metadata_bytes(envelope: FinalAcceptanceEnvelope) -> bytes:
    return canonical_json(envelope.as_dict()) + b"\n"


def _plan_payload(plan: FinalAcceptancePlan) -> dict[str, object]:
    value = plan.as_dict()
    value.pop("plan_digest", None)
    return value


def final_acceptance_plan_digest(plan: FinalAcceptancePlan) -> str:
    return digest(_plan_payload(plan))


def _registration_payload(receipt: RegisteredFinalAcceptance) -> dict[str, object]:
    value = receipt.as_dict()
    value.pop("registration_digest", None)
    value.pop("journal_path", None)
    value.pop("idempotent", None)
    return value


def registered_final_acceptance_digest(receipt: RegisteredFinalAcceptance) -> str:
    return digest(_registration_payload(receipt))


def _journal_path(repo: train.Repository, operation_id: str) -> Path:
    return (
        repo.common_dir
        / "project-harness"
        / "v2"
        / "final-acceptance"
        / f"{canonical_operation(operation_id)}.json"
    )


def _lock_path(repo: train.Repository, operation_id: str) -> Path:
    return (
        repo.common_dir
        / "project-harness"
        / "v2"
        / "final-acceptance"
        / "locks"
        / f"{canonical_operation(operation_id)}.lock"
    )


def journal_path(project_root: str | Path, operation_id: str) -> Path:
    return _journal_path(train.open_repository(project_root), operation_id)


@contextlib.contextmanager
def _operation_lock(repo: train.Repository, operation_id: str, timeout: float = 30.0):
    """Crash-releasing OS lock for one final-acceptance operation identity."""

    path = _lock_path(repo, operation_id)
    _assert_journal_parent(path, repo.common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_journal_parent(path.parent, repo.common_dir)
    handle = path.open("a+b")
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - Windows is the primary integration target
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise FinalAcceptanceError(
                        f"final acceptance operation is already active: {operation_id}"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _normalized_main_snapshot(main_plan: train.MainAdvancePlan) -> dict[str, object]:
    return json.loads(canonical_json(main_plan.as_dict()).decode("utf-8"))


def _strict_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise FinalAcceptanceError(f"{label} must be a string")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise FinalAcceptanceError(f"{label} must be a boolean")
    return value


def _strict_main_plan_from_snapshot(value: object) -> train.MainAdvancePlan:
    """Decode exactly the public MainAdvancePlan schema; accept no extra fields."""

    if not isinstance(value, Mapping):
        raise FinalAcceptanceError("main advance plan snapshot is not an object")
    required = {field.name for field in train.MainAdvancePlan.__dataclass_fields__.values()}
    if set(value) != required:
        raise FinalAcceptanceError("main advance plan snapshot fields are unsupported")

    def string_pairs(name: str) -> tuple[tuple[str, str], ...]:
        raw = value[name]
        if not isinstance(raw, list) or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
            for item in raw
        ):
            raise FinalAcceptanceError(f"main advance {name} is malformed")
        return tuple((item[0], item[1]) for item in raw)

    raw_updates = value["ref_updates"]
    if not isinstance(raw_updates, list) or any(
        not isinstance(item, list)
        or len(item) != 3
        or not isinstance(item[0], str)
        or (item[1] is not None and not isinstance(item[1], str))
        or not isinstance(item[2], str)
        for item in raw_updates
    ):
        raise FinalAcceptanceError("main advance ref_updates are malformed")
    updates = tuple((item[0], item[1], item[2]) for item in raw_updates)

    raw_releases = value["local_main_release_receipts"]
    if not isinstance(raw_releases, list) or any(
        not isinstance(item, list)
        or len(item) != 4
        or not isinstance(item[0], str)
        or not isinstance(item[1], str)
        or type(item[2]) is not int
        or not isinstance(item[3], str)
        for item in raw_releases
    ):
        raise FinalAcceptanceError("main advance local release receipts are malformed")
    releases = tuple((item[0], item[1], item[2], item[3]) for item in raw_releases)

    raw_blockers = value["blockers"]
    if not isinstance(raw_blockers, list):
        raise FinalAcceptanceError("main advance blockers are malformed")
    blockers: list[train.Blocker] = []
    for item in raw_blockers:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"code", "message"}
            or not isinstance(item["code"], str)
            or not isinstance(item["message"], str)
        ):
            raise FinalAcceptanceError("main advance blocker is malformed")
        blockers.append(train.Blocker(code=item["code"], message=item["message"]))

    result = train.MainAdvancePlan(
        schema_version=_strict_string(value["schema_version"], "main plan schema_version"),
        operation_id=_strict_string(value["operation_id"], "main plan operation_id"),
        project_root=_strict_string(value["project_root"], "main plan project_root"),
        git_common_dir=_strict_string(value["git_common_dir"], "main plan git_common_dir"),
        integration_worktree=_strict_string(
            value["integration_worktree"], "main plan integration_worktree"
        ),
        main_ref=_strict_string(value["main_ref"], "main plan main_ref"),
        expected_main=_strict_string(value["expected_main"], "main plan expected_main"),
        integrated_commit=_strict_string(
            value["integrated_commit"], "main plan integrated_commit"
        ),
        integrated_tree=_strict_string(value["integrated_tree"], "main plan integrated_tree"),
        integrated_evidence_digest=_strict_string(
            value["integrated_evidence_digest"], "main plan integrated_evidence_digest"
        ),
        integrated_evidence_metadata_digest=_strict_string(
            value["integrated_evidence_metadata_digest"],
            "main plan integrated_evidence_metadata_digest",
        ),
        integrated_evidence_blob=_strict_string(
            value["integrated_evidence_blob"], "main plan integrated_evidence_blob"
        ),
        operation_commit_ref=_strict_string(
            value["operation_commit_ref"], "main plan operation_commit_ref"
        ),
        operation_evidence_ref=_strict_string(
            value["operation_evidence_ref"], "main plan operation_evidence_ref"
        ),
        iteration_evidence_refs=string_pairs("iteration_evidence_refs"),
        principle_path=_strict_string(value["principle_path"], "main plan principle_path"),
        principle_sha256=_strict_string(value["principle_sha256"], "main plan principle_sha256"),
        candidate_refs=string_pairs("candidate_refs"),
        source_ref_bindings=string_pairs("source_ref_bindings"),
        ref_updates=updates,
        integration_commit_result_digest=_strict_string(
            value["integration_commit_result_digest"],
            "main plan integration_commit_result_digest",
        ),
        local_main_release_receipts=releases,
        plan_digest=_strict_string(value["plan_digest"], "main plan plan_digest"),
        blockers=tuple(blockers),
        requires_confirmation=_strict_bool(
            value["requires_confirmation"], "main plan requires_confirmation"
        ),
        pushed=_strict_bool(value["pushed"], "main plan pushed"),
    )
    if result.plan_digest != train.main_advance_plan_digest(result):
        raise FinalAcceptanceError("main advance plan snapshot digest is invalid")
    if result.schema_version != train.ADVANCE_PLAN_SCHEMA:
        raise FinalAcceptanceError("main advance plan snapshot schema is unsupported")
    if result.blockers:
        raise FinalAcceptanceError("main advance plan snapshot was blocked")
    if not result.requires_confirmation or result.pushed:
        raise FinalAcceptanceError("main advance plan action boundary is invalid")
    return result


def _accepted_candidates(
    integrated: integrated_registry.RegisteredIntegratedEvidence,
) -> tuple[AcceptedCandidate, ...]:
    return tuple(
        AcceptedCandidate(
            iteration=_validate_iteration(item.iteration),
            generation=item.generation,
            candidate_ref=_validate_ref(item.candidate_ref, "candidate_ref"),
            candidate_commit=_validate_oid(item.candidate_commit, "candidate_commit"),
            candidate_evidence_ref=_validate_ref(
                item.candidate_evidence_ref,
                "candidate_evidence_ref",
            ),
            candidate_evidence_blob=_validate_oid(
                item.candidate_evidence_blob,
                "candidate_evidence_blob",
            ),
            candidate_registration_digest=_validate_digest(
                item.registration_digest,
                "candidate registration_digest",
            ),
            principle_gate_binding_digest=_validate_digest(
                item.principle_gate_binding.binding_digest,
                "principle gate binding_digest",
            ),
        )
        for item in integrated.metadata.candidate_bindings
    )


def _normalize_event_ids(
    integrated: integrated_registry.RegisteredIntegratedEvidence,
    candidates: Sequence[AcceptedCandidate],
    supplied: Sequence[str],
) -> tuple[str, ...]:
    """Extract hash-shaped EV identities solely from their canonical final refs."""

    expected_refs = tuple(iteration_final_evidence_ref(item.iteration) for item in candidates)
    expected_set = set(expected_refs)
    by_ref: dict[str, str] = {}
    for binding in integrated.metadata.progress_bindings:
        if not binding.ref_name.endswith("/final-evidence"):
            continue
        if binding.ref_name not in expected_set:
            raise FinalAcceptanceError(
                f"main_advanced binding names an unaccepted iteration: {binding.ref_name}"
            )
        if binding.ref_name in by_ref:
            raise FinalAcceptanceError(
                f"main_advanced binding repeats final evidence ref: {binding.ref_name}"
            )
        if EVENT_ID_RE.fullmatch(binding.event_id) is None:
            raise FinalAcceptanceError("main_advanced event IDs must be stable EV-* identities")
        by_ref[binding.ref_name] = binding.event_id
    missing = tuple(reference for reference in expected_refs if reference not in by_ref)
    if missing:
        raise FinalAcceptanceError(
            "main_advanced progress binding is missing for: " + ", ".join(missing)
        )
    extracted = tuple(by_ref[reference] for reference in expected_refs)
    if len(set(extracted)) != len(extracted):
        raise FinalAcceptanceError("main_advanced event IDs must be unique")
    if supplied and tuple(supplied) != extracted:
        raise FinalAcceptanceError(
            "supplied main_advanced event IDs differ from canonical progress bindings"
        )
    return extracted


def _canonical_final_refs(
    operation_id: str,
    candidates: Sequence[AcceptedCandidate],
) -> tuple[str, tuple[IterationFinalEvidenceRef, ...]]:
    operation_ref = operation_final_evidence_ref(operation_id)
    iteration_refs = tuple(
        IterationFinalEvidenceRef(
            iteration=item.iteration,
            ref_name=iteration_final_evidence_ref(item.iteration),
        )
        for item in candidates
    )
    return operation_ref, iteration_refs


def _expected_source_bindings(
    integrated: integrated_registry.RegisteredIntegratedEvidence,
) -> tuple[tuple[str, str], ...]:
    values: dict[str, str] = {}

    def bind(reference: str, oid: str) -> None:
        prior = values.get(reference)
        if prior is not None and prior != oid:
            raise FinalAcceptanceError(f"integrated evidence has conflicting source ref: {reference}")
        values[reference] = oid

    bind(integrated.commit_ref, integrated.metadata.integrated_commit)
    bind(integrated.evidence_ref, integrated.evidence_blob)
    for item in integrated.iteration_evidence_refs:
        bind(item.ref_name, integrated.evidence_blob)
    for item in integrated.metadata.candidate_bindings:
        bind(item.candidate_ref, item.candidate_commit)
        bind(item.candidate_evidence_ref, item.candidate_evidence_blob)
    return tuple(values.items())


def _expected_ref_updates(
    integrated: integrated_registry.RegisteredIntegratedEvidence,
) -> tuple[tuple[str, str | None, str], ...]:
    envelope = integrated.metadata
    values: list[tuple[str, str | None, str]] = [
        (envelope.main_ref, envelope.target_main, envelope.integrated_commit)
    ]
    seen: set[str] = set()
    for item in envelope.candidate_bindings:
        if item.iteration in seen:
            raise FinalAcceptanceError(f"integrated candidate iteration repeats: {item.iteration}")
        seen.add(item.iteration)
        values.extend(
            (
                (
                    f"{REF_ROOT}/iterations/{item.iteration}/integrated",
                    None,
                    envelope.integrated_commit,
                ),
                (
                    f"{REF_ROOT}/iterations/{item.iteration}/final",
                    None,
                    envelope.integrated_commit,
                ),
            )
        )
    return tuple(values)


def _authority_binding_blockers(
    repo: train.Repository,
    envelope: FinalAcceptanceEnvelope,
    integrated: integrated_registry.RegisteredIntegratedEvidence,
) -> tuple[train.Blocker, ...]:
    """Prove the final envelope is the sole projection of public authorities."""

    blockers: list[train.Blocker] = []
    try:
        main_plan = _strict_main_plan_from_snapshot(envelope.main_plan_snapshot)
    except (FinalAcceptanceError, train.TrainError) as exc:
        return (train.Blocker("final-acceptance-main-plan-authority", str(exc)),)

    expected_candidates = _accepted_candidates(integrated)
    expected_candidate_refs = tuple(
        (item.candidate_ref, item.candidate_commit) for item in expected_candidates
    )
    expected_iteration_refs = tuple(
        (item.ref_name, integrated.evidence_blob) for item in integrated.iteration_evidence_refs
    )
    expected_integrated_ref_bindings = tuple(
        RefBinding(reference, oid) for reference, oid in expected_iteration_refs
    )
    try:
        expected_sources = _expected_source_bindings(integrated)
        expected_updates = _expected_ref_updates(integrated)
        expected_events = _normalize_event_ids(integrated, expected_candidates, ())
    except FinalAcceptanceError as exc:
        blockers.append(train.Blocker("final-acceptance-authority-projection", str(exc)))
        expected_sources = ()
        expected_updates = ()
        expected_events = ()

    comparisons: tuple[tuple[object, object, str], ...] = (
        (main_plan.operation_id, integrated.operation_id, "main-plan-operation"),
        (main_plan.project_root, str(repo.root), "main-plan-project-root"),
        (main_plan.git_common_dir, str(repo.common_dir), "main-plan-common-dir"),
        (
            main_plan.integration_worktree,
            str(integrated.metadata.commit_plan_snapshot.get("integration_worktree", "")),
            "main-plan-integration-worktree",
        ),
        (main_plan.main_ref, integrated.metadata.main_ref, "main-plan-main-ref"),
        (main_plan.expected_main, integrated.metadata.target_main, "main-plan-previous-main"),
        (main_plan.integrated_commit, integrated.metadata.integrated_commit, "main-plan-commit"),
        (main_plan.integrated_tree, integrated.metadata.integrated_tree, "main-plan-tree"),
        (
            main_plan.integrated_evidence_digest,
            integrated.registration_digest,
            "main-plan-integrated-registration",
        ),
        (
            main_plan.integrated_evidence_metadata_digest,
            integrated.metadata.metadata_digest,
            "main-plan-integrated-metadata",
        ),
        (
            main_plan.integrated_evidence_blob,
            integrated.evidence_blob,
            "main-plan-integrated-blob",
        ),
        (main_plan.operation_commit_ref, integrated.commit_ref, "main-plan-operation-commit-ref"),
        (
            main_plan.operation_evidence_ref,
            integrated.evidence_ref,
            "main-plan-operation-evidence-ref",
        ),
        (main_plan.iteration_evidence_refs, expected_iteration_refs, "main-plan-iteration-evidence"),
        (main_plan.principle_sha256, integrated.metadata.principle_sha256, "main-plan-principle"),
        (main_plan.candidate_refs, expected_candidate_refs, "main-plan-candidates"),
        (main_plan.source_ref_bindings, expected_sources, "main-plan-source-refs"),
        (main_plan.ref_updates, expected_updates, "main-plan-ref-updates"),
        (
            main_plan.integration_commit_result_digest,
            integrated.metadata.commit_result_digest,
            "main-plan-commit-result",
        ),
        (envelope.operation_id, integrated.operation_id, "envelope-operation"),
        (envelope.main_plan_schema_version, main_plan.schema_version, "envelope-main-plan-schema"),
        (envelope.main_plan_digest, main_plan.plan_digest, "envelope-main-plan-digest"),
        (envelope.integrated_registration_digest, integrated.registration_digest, "envelope-registration"),
        (envelope.integrated_metadata_digest, integrated.metadata.metadata_digest, "envelope-metadata"),
        (envelope.integrated_evidence_blob, integrated.evidence_blob, "envelope-integrated-blob"),
        (envelope.integrated_commit_ref, integrated.commit_ref, "envelope-integrated-commit-ref"),
        (envelope.integrated_evidence_ref, integrated.evidence_ref, "envelope-integrated-evidence-ref"),
        (
            envelope.integrated_iteration_evidence_refs,
            expected_integrated_ref_bindings,
            "envelope-integrated-iteration-refs",
        ),
        (envelope.main_ref, main_plan.main_ref, "envelope-main-ref"),
        (envelope.previous_main, main_plan.expected_main, "envelope-previous-main"),
        (envelope.accepted_main, main_plan.integrated_commit, "envelope-accepted-main"),
        (envelope.accepted_tree, main_plan.integrated_tree, "envelope-accepted-tree"),
        (envelope.principle_path, main_plan.principle_path, "envelope-principle-path"),
        (envelope.principle_sha256, main_plan.principle_sha256, "envelope-principle"),
        (envelope.accepted_candidates, expected_candidates, "envelope-candidates"),
        (
            envelope.source_ref_bindings,
            tuple(RefBinding(reference, oid) for reference, oid in main_plan.source_ref_bindings),
            "envelope-source-refs",
        ),
        (
            envelope.main_ref_updates,
            tuple(RefUpdate(reference, old, new) for reference, old, new in main_plan.ref_updates),
            "envelope-ref-updates",
        ),
        (envelope.main_advanced_event_ids, expected_events, "envelope-main-events"),
    )
    for observed, expected, label in comparisons:
        if observed != expected:
            blockers.append(train.Blocker("final-acceptance-authority-binding", label))

    expected_operation_ref, expected_final_refs = _canonical_final_refs(
        integrated.operation_id,
        expected_candidates,
    )
    if envelope.operation_final_evidence_ref != expected_operation_ref:
        blockers.append(train.Blocker("final-acceptance-authority-binding", "operation-final-ref"))
    if envelope.iteration_final_evidence_refs != expected_final_refs:
        blockers.append(train.Blocker("final-acceptance-authority-binding", "iteration-final-refs"))
    try:
        _blob_oid, principle_raw = train._blob_at(
            repo,
            main_plan.expected_main,
            main_plan.principle_path,
        )
    except train.TrainError as exc:
        blockers.append(train.Blocker("final-acceptance-principle-authority", str(exc)))
    else:
        if hashlib.sha256(principle_raw).hexdigest() != main_plan.principle_sha256:
            blockers.append(
                train.Blocker(
                    "final-acceptance-principle-authority",
                    "principle bytes at previous main differ from the accepted plan",
                )
            )
    return tuple(dict.fromkeys(blockers))


def _refs_state(
    repo: train.Repository,
    envelope: FinalAcceptanceEnvelope,
    metadata_blob: str,
) -> str:
    final_expected = {
        envelope.operation_final_evidence_ref: metadata_blob,
        **{item.ref_name: metadata_blob for item in envelope.iteration_final_evidence_refs},
    }
    final_observed = {ref: _resolve_ref(repo, ref) for ref in final_expected}
    updates_exact = all(
        _resolve_ref(repo, item.ref_name) == item.new_object_id
        for item in envelope.main_ref_updates
    )
    initial_exact = all(
        _resolve_ref(repo, item.ref_name) == item.old_object_id
        for item in envelope.main_ref_updates
    )
    if all(value is None for value in final_observed.values()) and initial_exact:
        return "absent"
    if (
        all(final_observed[ref] == oid for ref, oid in final_expected.items())
        and updates_exact
    ):
        return "exact"
    return "mismatch"


def plan_final_acceptance(
    project_root: str | Path,
    *,
    main_plan: train.MainAdvancePlan,
    integrated: integrated_registry.RegisteredIntegratedEvidence,
    confirmation: train.ConfirmationToken,
    main_advanced_events: Sequence[str] = (),
) -> FinalAcceptancePlan:
    """Build the exact final acceptance envelope before any main-ref CAS."""

    if not isinstance(main_plan, train.MainAdvancePlan):
        raise FinalAcceptanceError("main_plan must be MainAdvancePlan")
    if not isinstance(integrated, integrated_registry.RegisteredIntegratedEvidence):
        raise FinalAcceptanceError("integrated must be RegisteredIntegratedEvidence")
    repo = train.open_repository(project_root)
    blockers: list[train.Blocker] = []
    if main_plan.project_root != str(repo.root) or integrated.project_root != str(repo.root):
        blockers.append(train.Blocker("final-acceptance-project-root", "inputs belong to another project"))
    if main_plan.plan_digest != train.main_advance_plan_digest(main_plan):
        blockers.append(train.Blocker("final-acceptance-main-plan-digest", "main plan was changed"))
    blockers.extend(main_plan.blockers)
    blockers.extend(
        train.confirmation_token_gate(
            confirmation,
            action="advance-main",
            subject_digest=main_plan.plan_digest,
        )
    )
    blockers.extend(integrated_registry.registered_integrated_evidence_gate(repo.root, integrated))
    try:
        loaded, load_blockers = integrated_registry.load_registered_integrated_evidence(
            repo.root,
            operation_id=main_plan.operation_id,
        )
    except integrated_registry.IntegratedEvidenceError as exc:
        loaded, load_blockers = None, (train.Blocker("final-acceptance-integrated-load", str(exc)),)
    blockers.extend(load_blockers)
    if loaded is None or loaded.registration_digest != integrated.registration_digest:
        blockers.append(
            train.Blocker(
                "final-acceptance-integrated-identity",
                "supplied integrated evidence is not the canonical public registration",
            )
        )
    comparisons = (
        (main_plan.operation_id, integrated.operation_id, "operation"),
        (main_plan.integrated_commit, integrated.metadata.integrated_commit, "commit"),
        (main_plan.integrated_tree, integrated.metadata.integrated_tree, "tree"),
        (main_plan.integrated_evidence_digest, integrated.registration_digest, "registration"),
        (main_plan.integrated_evidence_metadata_digest, integrated.metadata.metadata_digest, "metadata"),
        (main_plan.integrated_evidence_blob, integrated.evidence_blob, "blob"),
        (main_plan.operation_commit_ref, integrated.commit_ref, "commit-ref"),
        (main_plan.operation_evidence_ref, integrated.evidence_ref, "evidence-ref"),
        (main_plan.expected_main, integrated.metadata.target_main, "previous-main"),
        (main_plan.principle_sha256, integrated.metadata.principle_sha256, "principle"),
    )
    for observed, expected, label in comparisons:
        if observed != expected:
            blockers.append(train.Blocker("final-acceptance-integrated-binding", label))

    candidates = _accepted_candidates(integrated)
    if main_plan.candidate_refs != tuple(
        (item.candidate_ref, item.candidate_commit) for item in candidates
    ):
        blockers.append(train.Blocker("final-acceptance-candidate-binding", "main plan candidates differ"))
    source_bindings = tuple(
        RefBinding(_validate_ref(reference, "source ref"), _validate_oid(oid, "source object"))
        for reference, oid in main_plan.source_ref_bindings
    )
    updates = tuple(
        RefUpdate(
            _validate_ref(reference, "main update ref"),
            None if old is None else _validate_oid(old, "old ref object"),
            _validate_oid(new, "new ref object"),
        )
        for reference, old, new in main_plan.ref_updates
    )
    if any(item.ref_name.endswith("/final-evidence") for item in updates):
        blockers.append(
            train.Blocker(
                "final-acceptance-ref-owned-by-registry",
                "main plan must not pre-bind final-evidence to integrated evidence",
            )
        )
    try:
        event_ids = _normalize_event_ids(integrated, candidates, main_advanced_events)
    except FinalAcceptanceError as exc:
        blockers.append(train.Blocker("final-acceptance-main-events", str(exc)))
        event_ids = ()
    operation_ref, iteration_refs = _canonical_final_refs(main_plan.operation_id, candidates)
    snapshot = _normalized_main_snapshot(main_plan)
    provisional_metadata = FinalAcceptanceEnvelope(
        schema_version=EVIDENCE_SCHEMA,
        operation_id=_validate_operation(main_plan.operation_id),
        canonical_operation=canonical_operation(main_plan.operation_id),
        main_plan_schema_version=main_plan.schema_version,
        main_plan_digest=_validate_digest(main_plan.plan_digest, "main plan_digest"),
        main_plan_snapshot=snapshot,
        integrated_registration_digest=_validate_digest(
            integrated.registration_digest,
            "integrated registration_digest",
        ),
        integrated_metadata_digest=_validate_digest(
            integrated.metadata.metadata_digest,
            "integrated metadata_digest",
        ),
        integrated_evidence_blob=_validate_oid(integrated.evidence_blob, "integrated evidence blob"),
        integrated_commit_ref=_validate_ref(integrated.commit_ref, "integrated commit ref"),
        integrated_evidence_ref=_validate_ref(integrated.evidence_ref, "integrated evidence ref"),
        integrated_iteration_evidence_refs=tuple(
            RefBinding(item.ref_name, integrated.evidence_blob)
            for item in integrated.iteration_evidence_refs
        ),
        main_ref=_validate_ref(main_plan.main_ref, "main_ref"),
        previous_main=_validate_oid(main_plan.expected_main, "previous_main"),
        accepted_main=_validate_oid(main_plan.integrated_commit, "accepted_main"),
        accepted_tree=_validate_oid(main_plan.integrated_tree, "accepted_tree"),
        principle_path=main_plan.principle_path,
        principle_sha256=_validate_digest(main_plan.principle_sha256, "principle_sha256"),
        accepted_candidates=candidates,
        source_ref_bindings=source_bindings,
        main_ref_updates=updates,
        confirmation_action=confirmation.action,
        confirmation_subject_digest=confirmation.subject_digest,
        confirmation_authorization_id=confirmation.authorization_id,
        confirmation_token_digest=_validate_digest(confirmation.token_digest, "confirmation token_digest"),
        main_advanced_event_ids=event_ids,
        operation_final_evidence_ref=operation_ref,
        iteration_final_evidence_refs=iteration_refs,
        metadata_digest="0" * 64,
    )
    metadata = replace(
        provisional_metadata,
        metadata_digest=metadata_digest(provisional_metadata),
    )
    blockers.extend(_envelope_structural_blockers(metadata))
    blockers.extend(_authority_binding_blockers(repo, metadata, integrated))
    raw = metadata_bytes(metadata)
    if len(raw) > MAX_METADATA_BYTES:
        raise FinalAcceptanceError("final acceptance evidence exceeds the size limit")
    metadata_blob = _hash_blob(repo, raw, write=False)
    state = _refs_state(repo, metadata, metadata_blob)
    if state == "mismatch":
        blockers.append(
            train.Blocker(
                "final-acceptance-ref-state",
                "main/final evidence refs are partial or name another identity",
            )
        )
    provisional = FinalAcceptancePlan(
        schema_version=PLAN_SCHEMA,
        operation_id=main_plan.operation_id,
        project_root=str(repo.root),
        git_common_dir=str(repo.common_dir),
        metadata_blob=metadata_blob,
        metadata=metadata,
        journal_path=str(_journal_path(repo, main_plan.operation_id)),
        plan_digest="0" * 64,
        blockers=tuple(dict.fromkeys(blockers)),
    )
    return replace(provisional, plan_digest=final_acceptance_plan_digest(provisional))


def _envelope_structural_blockers(
    envelope: FinalAcceptanceEnvelope,
) -> tuple[train.Blocker, ...]:
    blockers: list[train.Blocker] = []
    if envelope.schema_version != EVIDENCE_SCHEMA:
        blockers.append(train.Blocker("final-acceptance-schema", "schema is unsupported"))
    if envelope.metadata_digest != metadata_digest(envelope):
        blockers.append(train.Blocker("final-acceptance-metadata-digest", "metadata was changed"))
        return tuple(blockers)
    try:
        expected_operation = canonical_operation(envelope.operation_id)
        expected_operation_ref = operation_final_evidence_ref(envelope.operation_id)
        expected_iteration_refs = tuple(
            IterationFinalEvidenceRef(item.iteration, iteration_final_evidence_ref(item.iteration))
            for item in envelope.accepted_candidates
        )
    except FinalAcceptanceError as exc:
        blockers.append(train.Blocker("final-acceptance-operation", str(exc)))
        return tuple(blockers)
    if envelope.canonical_operation != expected_operation:
        blockers.append(train.Blocker("final-acceptance-operation", envelope.operation_id))
    if envelope.operation_final_evidence_ref != expected_operation_ref:
        blockers.append(train.Blocker("final-acceptance-operation-ref", envelope.operation_final_evidence_ref))
    if envelope.iteration_final_evidence_refs != expected_iteration_refs:
        blockers.append(train.Blocker("final-acceptance-iteration-refs", "refs are not canonical"))
    try:
        main_plan = _strict_main_plan_from_snapshot(envelope.main_plan_snapshot)
    except (FinalAcceptanceError, train.TrainError) as exc:
        blockers.append(train.Blocker("final-acceptance-main-plan-snapshot", str(exc)))
        main_plan = None
    if main_plan is not None and (
        envelope.main_plan_digest != main_plan.plan_digest
        or envelope.main_plan_schema_version != main_plan.schema_version
    ):
        blockers.append(
            train.Blocker("final-acceptance-main-plan-snapshot", "envelope differs from snapshot")
        )
    try:
        expected_confirmation = train.confirmation_token_digest(
            envelope.confirmation_action,
            envelope.confirmation_subject_digest,
            envelope.confirmation_authorization_id,
        )
    except train.TrainError as exc:
        blockers.append(train.Blocker("final-acceptance-confirmation", str(exc)))
        expected_confirmation = None
    if (
        envelope.confirmation_action != "advance-main"
        or envelope.confirmation_subject_digest != envelope.main_plan_digest
        or expected_confirmation is None
        or envelope.confirmation_token_digest != expected_confirmation
    ):
        blockers.append(train.Blocker("final-acceptance-confirmation", "confirmation identity differs"))
    if len(set(envelope.main_advanced_event_ids)) != len(envelope.main_advanced_event_ids):
        blockers.append(train.Blocker("final-acceptance-main-events", "event IDs repeat"))
    return tuple(blockers)


def _live_blockers(
    repo: train.Repository,
    envelope: FinalAcceptanceEnvelope,
    *,
    require_refs: bool,
) -> tuple[train.Blocker, ...]:
    blockers = list(_envelope_structural_blockers(envelope))
    try:
        integrated, values = integrated_registry.load_registered_integrated_evidence(
            repo.root,
            operation_id=envelope.operation_id,
        )
    except integrated_registry.IntegratedEvidenceError as exc:
        integrated, values = None, (train.Blocker("final-acceptance-integrated-load", str(exc)),)
    blockers.extend(values)
    if integrated is None:
        blockers.append(train.Blocker("final-acceptance-integrated-missing", envelope.operation_id))
    else:
        comparisons = (
            (integrated.registration_digest, envelope.integrated_registration_digest, "registration"),
            (integrated.metadata.metadata_digest, envelope.integrated_metadata_digest, "metadata"),
            (integrated.evidence_blob, envelope.integrated_evidence_blob, "blob"),
            (integrated.commit_ref, envelope.integrated_commit_ref, "commit-ref"),
            (integrated.evidence_ref, envelope.integrated_evidence_ref, "evidence-ref"),
            (integrated.metadata.integrated_commit, envelope.accepted_main, "accepted-main"),
            (integrated.metadata.integrated_tree, envelope.accepted_tree, "accepted-tree"),
        )
        for observed, expected, label in comparisons:
            if observed != expected:
                blockers.append(train.Blocker("final-acceptance-integrated-binding", label))
        try:
            blockers.extend(_authority_binding_blockers(repo, envelope, integrated))
        except (FinalAcceptanceError, train.TrainError, TypeError, ValueError) as exc:
            blockers.append(train.Blocker("final-acceptance-authority-binding", str(exc)))
    for item in envelope.source_ref_bindings:
        if _resolve_ref(repo, item.ref_name) != item.object_id:
            blockers.append(train.Blocker("final-acceptance-source-ref-drift", item.ref_name))
    if _object_type(repo, envelope.accepted_main) != "commit":
        blockers.append(train.Blocker("final-acceptance-main-object", envelope.accepted_main))
    elif require_refs:
        current = _resolve_ref(repo, envelope.main_ref)
        if current is None or not _is_ancestor(repo, envelope.accepted_main, current):
            blockers.append(train.Blocker("final-acceptance-main-not-reachable", envelope.main_ref))
    if require_refs:
        for update in envelope.main_ref_updates:
            current = _resolve_ref(repo, update.ref_name)
            if update.ref_name == envelope.main_ref:
                if current is None or not _is_ancestor(repo, update.new_object_id, current):
                    blockers.append(train.Blocker("final-acceptance-main-update-drift", update.ref_name))
            elif current != update.new_object_id:
                blockers.append(train.Blocker("final-acceptance-target-ref-drift", update.ref_name))
    else:
        # ``apply_final_acceptance`` is itself a public mutation entry point.
        # Revalidate the same main-release authority that the train wrapper
        # checked while planning; callers may not bypass it by invoking this
        # registry directly after checkout/lease state changes.
        if train._ref_checked_out(repo, envelope.main_ref):
            blockers.append(
                train.Blocker(
                    "final-acceptance-main-checked-out",
                    f"{envelope.main_ref} is checked out in a worktree",
                )
            )
        expected_releases_raw = envelope.main_plan_snapshot.get(
            "local_main_release_receipts"
        )
        if not isinstance(expected_releases_raw, list) or any(
            not isinstance(item, list) or len(item) != 4
            for item in expected_releases_raw
        ):
            blockers.append(
                train.Blocker(
                    "final-acceptance-main-release-snapshot",
                    "main plan release receipts are malformed",
                )
            )
        else:
            expected_releases = tuple(
                (str(item[0]), str(item[1]), int(item[2]), str(item[3]))
                for item in expected_releases_raw
            )
            current_releases, release_blockers = train._local_main_release_gate(repo)
            blockers.extend(
                train.Blocker(f"final-acceptance-{item.code}", item.message)
                for item in release_blockers
            )
            if current_releases != expected_releases:
                blockers.append(
                    train.Blocker(
                        "final-acceptance-main-release-drift",
                        "Local main-release authority changed after planning",
                    )
                )
        lease = train._read_json(train._lease_path(repo), repo)
        if (
            not isinstance(lease, Mapping)
            or lease.get("schema_version") != train.LEASE_SCHEMA
            or lease.get("operation_id") != envelope.operation_id
            or lease.get("expected_main") != envelope.previous_main
        ):
            blockers.append(
                train.Blocker(
                    "final-acceptance-integration-lease",
                    "matching main integration lease is absent or changed",
                )
            )
    return tuple(dict.fromkeys(blockers))


def _final_plan_binding_blockers(
    repo: train.Repository,
    plan: FinalAcceptancePlan,
) -> tuple[train.Blocker, ...]:
    """Bind the outer plan and its metadata to canonical public authority.

    This gate runs before a recovery journal is created.  A self-consistent
    but forged plan must therefore be unable to reserve the canonical
    operation journal and permanently block the legitimate accepted plan.
    """

    blockers: list[train.Blocker] = []
    if plan.schema_version != PLAN_SCHEMA:
        blockers.append(train.Blocker("final-acceptance-plan-schema", "schema is unsupported"))
    if plan.plan_digest != final_acceptance_plan_digest(plan):
        blockers.append(train.Blocker("final-acceptance-plan-digest", "plan was changed"))
    if plan.blockers:
        blockers.extend(plan.blockers)
    expected_journal = _journal_path(repo, plan.metadata.operation_id)
    comparisons = (
        (plan.operation_id, plan.metadata.operation_id, "operation"),
        (os.path.normcase(plan.project_root), os.path.normcase(str(repo.root)), "project-root"),
        (os.path.normcase(plan.git_common_dir), os.path.normcase(str(repo.common_dir)), "common-dir"),
        (
            Path(plan.journal_path).resolve(strict=False),
            expected_journal.resolve(strict=False),
            "journal-path",
        ),
        (plan.metadata_blob, _hash_blob(repo, metadata_bytes(plan.metadata), write=False), "metadata-blob"),
    )
    for observed, expected, label in comparisons:
        if observed != expected:
            blockers.append(train.Blocker("final-acceptance-plan-binding", label))
    if not plan.requires_confirmation or plan.pushed:
        blockers.append(
            train.Blocker("final-acceptance-plan-action-boundary", "confirm/local-only contract changed")
        )
    blockers.extend(_envelope_structural_blockers(plan.metadata))
    try:
        integrated, integrated_blockers = (
            integrated_registry.load_registered_integrated_evidence(
                repo.root,
                operation_id=plan.metadata.operation_id,
            )
        )
    except integrated_registry.IntegratedEvidenceError as exc:
        integrated = None
        integrated_blockers = (
            train.Blocker("final-acceptance-integrated-load", str(exc)),
        )
    blockers.extend(integrated_blockers)
    if integrated is None:
        blockers.append(
            train.Blocker(
                "final-acceptance-integrated-missing",
                plan.metadata.operation_id,
            )
        )
    else:
        try:
            blockers.extend(
                _authority_binding_blockers(repo, plan.metadata, integrated)
            )
        except (FinalAcceptanceError, train.TrainError, TypeError, ValueError) as exc:
            blockers.append(
                train.Blocker("final-acceptance-authority-binding", str(exc))
            )
    return tuple(dict.fromkeys(blockers))


def _assert_journal_parent(path: Path, common_dir: Path) -> None:
    common = common_dir.resolve()
    try:
        path.resolve(strict=False).relative_to(common)
    except ValueError as exc:
        raise FinalAcceptanceError("final acceptance journal escapes Git common directory") from exc
    current = common
    for part in path.parent.relative_to(common).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise FinalAcceptanceError(f"final acceptance journal traverses a link: {current}")


def _journal_payload(value: Mapping[str, object]) -> dict[str, object]:
    result = dict(value)
    result.pop("journal_digest", None)
    return result


def _expected_journal(plan: FinalAcceptancePlan, *, status: str) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": JOURNAL_SCHEMA,
        "kind": "final-acceptance",
        "operation_id": plan.operation_id,
        "plan_digest": plan.plan_digest,
        "main_plan_digest": plan.metadata.main_plan_digest,
        "integrated_registration_digest": plan.metadata.integrated_registration_digest,
        "confirmation_authorization_id": plan.metadata.confirmation_authorization_id,
        "confirmation_token_digest": plan.metadata.confirmation_token_digest,
        "metadata_digest": plan.metadata.metadata_digest,
        "metadata_blob": plan.metadata_blob,
        # Recovery must resume the exact reviewed plan.  Persisting only its
        # digest would force a live re-plan and could silently accept changed
        # repository observations after a crash.
        "plan_snapshot": json.loads(canonical_json(plan.as_dict()).decode("utf-8")),
        "status": status,
    }
    value["journal_digest"] = digest(value)
    return value


def _replace_json(path: Path, value: Mapping[str, object], common_dir: Path) -> None:
    _assert_journal_parent(path, common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_journal_parent(path, common_dir)
    raw = canonical_json(value) + b"\n"
    if len(raw) > MAX_JOURNAL_BYTES:
        raise FinalAcceptanceError("final acceptance journal exceeds the size limit")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_journal(path: Path, common_dir: Path) -> dict[str, object] | None:
    _assert_journal_parent(path, common_dir)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise FinalAcceptanceError("final acceptance journal is not a regular file")
    raw = path.read_bytes()
    if len(raw) > MAX_JOURNAL_BYTES:
        raise FinalAcceptanceError("final acceptance journal exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalAcceptanceError("final acceptance journal is invalid JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) + b"\n" != raw:
        raise FinalAcceptanceError("final acceptance journal is not canonical JSON")
    if value.get("journal_digest") != digest(_journal_payload(value)):
        raise FinalAcceptanceError("final acceptance journal digest is invalid")
    return value


def _apply_ref_transaction(
    repo: train.Repository,
    envelope: FinalAcceptanceEnvelope,
    metadata_blob: str,
) -> None:
    sources: dict[str, str] = {}
    for item in (
        *envelope.source_ref_bindings,
        RefBinding(envelope.integrated_commit_ref, envelope.accepted_main),
        RefBinding(envelope.integrated_evidence_ref, envelope.integrated_evidence_blob),
        *envelope.integrated_iteration_evidence_refs,
    ):
        prior = sources.get(item.ref_name)
        if prior is not None and prior != item.object_id:
            raise FinalAcceptanceError(f"conflicting source identity: {item.ref_name}")
        sources[item.ref_name] = item.object_id
    lines = ["start"]
    lines.extend(f"verify {reference} {oid}" for reference, oid in sources.items())
    for item in envelope.main_ref_updates:
        if item.old_object_id is None:
            lines.append(f"create {item.ref_name} {item.new_object_id}")
        else:
            lines.append(
                f"update {item.ref_name} {item.new_object_id} {item.old_object_id}"
            )
    lines.append(f"create {envelope.operation_final_evidence_ref} {metadata_blob}")
    lines.extend(
        f"create {item.ref_name} {metadata_blob}"
        for item in envelope.iteration_final_evidence_refs
    )
    lines.extend(("prepare", "commit", ""))
    result = _git(
        repo,
        ["update-ref", "--stdin"],
        input_bytes="\n".join(lines).encode("ascii"),
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise FinalAcceptanceError(
            f"final acceptance/main CAS transaction failed: {detail or 'unknown Git error'}"
        )


def _receipt(
    plan: FinalAcceptancePlan,
    *,
    idempotent: bool,
) -> RegisteredFinalAcceptance:
    provisional = RegisteredFinalAcceptance(
        schema_version=REGISTRATION_SCHEMA,
        operation_id=plan.operation_id,
        project_root=plan.project_root,
        evidence_ref=plan.metadata.operation_final_evidence_ref,
        evidence_blob=plan.metadata_blob,
        iteration_evidence_refs=plan.metadata.iteration_final_evidence_refs,
        metadata=plan.metadata,
        registration_digest="0" * 64,
        journal_path=plan.journal_path,
        idempotent=idempotent,
    )
    return replace(
        provisional,
        registration_digest=registered_final_acceptance_digest(provisional),
    )


def _trigger(failpoint: Failpoint | None, stage: str) -> None:
    if failpoint is not None:
        failpoint(stage)


def _apply_final_acceptance_locked(
    plan: FinalAcceptancePlan,
    *,
    accepted_plan_digest: str,
    confirmation: train.ConfirmationToken,
    failpoint: Failpoint | None = None,
) -> RegisteredFinalAcceptance:
    """Create final evidence and advance main in one exact ref transaction."""

    if not isinstance(plan, FinalAcceptancePlan):
        raise FinalAcceptanceError("plan must be FinalAcceptancePlan")
    if plan.schema_version != PLAN_SCHEMA or plan.plan_digest != final_acceptance_plan_digest(plan):
        raise FinalAcceptanceError("final acceptance plan is unsupported or changed")
    if accepted_plan_digest != plan.plan_digest:
        raise FinalAcceptanceError("accepted digest differs from final acceptance plan")
    if plan.blockers:
        raise FinalAcceptanceError(
            "final acceptance plan is blocked: "
            + "; ".join(item.code for item in plan.blockers)
        )
    token_blockers = train.confirmation_token_gate(
        confirmation,
        action="advance-main",
        subject_digest=plan.metadata.main_plan_digest,
    )
    if token_blockers:
        raise FinalAcceptanceError(
            "main confirmation is invalid: " + "; ".join(item.code for item in token_blockers)
        )
    if (
        confirmation.authorization_id != plan.metadata.confirmation_authorization_id
        or confirmation.token_digest != plan.metadata.confirmation_token_digest
    ):
        raise FinalAcceptanceError("main confirmation identity differs from final acceptance plan")
    repo = train.open_repository(plan.project_root)
    if os.path.normcase(str(repo.common_dir)) != os.path.normcase(plan.git_common_dir):
        raise FinalAcceptanceError("final acceptance plan belongs to another Git common directory")
    plan_blockers = _final_plan_binding_blockers(repo, plan)
    if plan_blockers:
        raise FinalAcceptanceError(
            "final acceptance plan authority is invalid: "
            + "; ".join(f"{item.code}:{item.message}" for item in plan_blockers)
        )
    raw = metadata_bytes(plan.metadata)
    if _hash_blob(repo, raw, write=False) != plan.metadata_blob:
        raise FinalAcceptanceError("planned final acceptance blob identity differs")

    path = Path(plan.journal_path)
    journal = _read_journal(path, repo.common_dir)
    if journal is not None:
        status = journal.get("status")
        if status not in {"planned", "metadata-written", "complete"}:
            raise FinalAcceptanceError("final acceptance journal status is unsupported")
        if canonical_json(journal) != canonical_json(_expected_journal(plan, status=str(status))):
            raise FinalAcceptanceError("old or foreign journal does not authorize this plan")

    state = _refs_state(repo, plan.metadata, plan.metadata_blob)
    if state == "exact":
        receipt = _receipt(plan, idempotent=True)
        blockers = registered_final_acceptance_gate(repo.root, receipt)
        if blockers:
            raise FinalAcceptanceError(
                "existing final acceptance failed its public gate: "
                + "; ".join(item.code for item in blockers)
            )
        _replace_json(path, _expected_journal(plan, status="complete"), repo.common_dir)
        return receipt
    if state != "absent":
        raise FinalAcceptanceError("final acceptance/main refs are partial or name another identity")
    live = _live_blockers(repo, plan.metadata, require_refs=False)
    if live:
        raise FinalAcceptanceError(
            "final acceptance source identity is stale: " + "; ".join(item.code for item in live)
        )
    # A first attempt owns the canonical recovery journal only after both the
    # public authority projection and the live mutation boundary are valid.
    # Otherwise a stale checkout/lease/ref observation could poison the
    # operation name and prevent a subsequently valid plan from proceeding.
    if journal is None:
        _replace_json(path, _expected_journal(plan, status="planned"), repo.common_dir)
    _trigger(failpoint, "final-acceptance-after-journal")
    written = _hash_blob(repo, raw, write=True)
    if written != plan.metadata_blob or _blob_bytes(repo, written) != raw:
        raise FinalAcceptanceError("Git wrote a different final acceptance blob")
    _replace_json(path, _expected_journal(plan, status="metadata-written"), repo.common_dir)
    _trigger(failpoint, "final-acceptance-after-blob")
    live = _live_blockers(repo, plan.metadata, require_refs=False)
    if live:
        raise FinalAcceptanceError(
            "final acceptance source changed before CAS: " + "; ".join(item.code for item in live)
        )
    _apply_ref_transaction(repo, plan.metadata, plan.metadata_blob)
    _trigger(failpoint, "final-acceptance-after-refs")
    receipt = _receipt(plan, idempotent=False)
    blockers = registered_final_acceptance_gate(repo.root, receipt)
    if blockers:
        raise FinalAcceptanceError(
            "published final acceptance failed its public gate: "
            + "; ".join(item.code for item in blockers)
        )
    _replace_json(path, _expected_journal(plan, status="complete"), repo.common_dir)
    return receipt


def apply_final_acceptance(
    plan: FinalAcceptancePlan,
    *,
    accepted_plan_digest: str,
    confirmation: train.ConfirmationToken,
    failpoint: Failpoint | None = None,
) -> RegisteredFinalAcceptance:
    """Serialize one operation, then publish evidence/main through one CAS."""

    if not isinstance(plan, FinalAcceptancePlan):
        raise FinalAcceptanceError("plan must be FinalAcceptancePlan")
    repo = train.open_repository(plan.project_root)
    if os.path.normcase(str(repo.common_dir)) != os.path.normcase(plan.git_common_dir):
        raise FinalAcceptanceError("final acceptance plan belongs to another Git common directory")
    with _operation_lock(repo, plan.operation_id):
        return _apply_final_acceptance_locked(
            plan,
            accepted_plan_digest=accepted_plan_digest,
            confirmation=confirmation,
            failpoint=failpoint,
        )


def _ref_binding_from_dict(value: object) -> RefBinding:
    if not isinstance(value, Mapping) or set(value) != {"ref_name", "object_id"}:
        raise FinalAcceptanceError("ref binding is malformed")
    return RefBinding(str(value["ref_name"]), str(value["object_id"]))


def _ref_update_from_dict(value: object) -> RefUpdate:
    if not isinstance(value, Mapping) or set(value) != {
        "ref_name",
        "old_object_id",
        "new_object_id",
    }:
        raise FinalAcceptanceError("ref update is malformed")
    return RefUpdate(
        str(value["ref_name"]),
        None if value["old_object_id"] is None else str(value["old_object_id"]),
        str(value["new_object_id"]),
    )


def _candidate_from_dict(value: object) -> AcceptedCandidate:
    if not isinstance(value, Mapping) or set(value) != {
        field.name for field in AcceptedCandidate.__dataclass_fields__.values()
    }:
        raise FinalAcceptanceError("accepted candidate is malformed")
    return AcceptedCandidate(**{key: str(item) for key, item in value.items()})


def _iteration_ref_from_dict(value: object) -> IterationFinalEvidenceRef:
    if not isinstance(value, Mapping) or set(value) != {"iteration", "ref_name"}:
        raise FinalAcceptanceError("iteration final evidence ref is malformed")
    return IterationFinalEvidenceRef(str(value["iteration"]), str(value["ref_name"]))


def _envelope_from_dict(value: object) -> FinalAcceptanceEnvelope:
    if not isinstance(value, Mapping):
        raise FinalAcceptanceError("final acceptance metadata is not an object")
    required = {field.name for field in FinalAcceptanceEnvelope.__dataclass_fields__.values()}
    if set(value) != required:
        raise FinalAcceptanceError("final acceptance metadata fields are unsupported")
    sequence_names = {
        "integrated_iteration_evidence_refs",
        "accepted_candidates",
        "source_ref_bindings",
        "main_ref_updates",
        "main_advanced_event_ids",
        "iteration_final_evidence_refs",
    }
    if any(not isinstance(value[name], list) for name in sequence_names):
        raise FinalAcceptanceError("final acceptance metadata sequences are malformed")
    if not isinstance(value["main_plan_snapshot"], Mapping):
        raise FinalAcceptanceError("main plan snapshot is malformed")
    return FinalAcceptanceEnvelope(
        schema_version=str(value["schema_version"]),
        operation_id=str(value["operation_id"]),
        canonical_operation=str(value["canonical_operation"]),
        main_plan_schema_version=str(value["main_plan_schema_version"]),
        main_plan_digest=str(value["main_plan_digest"]),
        main_plan_snapshot=dict(value["main_plan_snapshot"]),
        integrated_registration_digest=str(value["integrated_registration_digest"]),
        integrated_metadata_digest=str(value["integrated_metadata_digest"]),
        integrated_evidence_blob=str(value["integrated_evidence_blob"]),
        integrated_commit_ref=str(value["integrated_commit_ref"]),
        integrated_evidence_ref=str(value["integrated_evidence_ref"]),
        integrated_iteration_evidence_refs=tuple(
            _ref_binding_from_dict(item) for item in value["integrated_iteration_evidence_refs"]
        ),
        main_ref=str(value["main_ref"]),
        previous_main=str(value["previous_main"]),
        accepted_main=str(value["accepted_main"]),
        accepted_tree=str(value["accepted_tree"]),
        principle_path=str(value["principle_path"]),
        principle_sha256=str(value["principle_sha256"]),
        accepted_candidates=tuple(
            _candidate_from_dict(item) for item in value["accepted_candidates"]
        ),
        source_ref_bindings=tuple(
            _ref_binding_from_dict(item) for item in value["source_ref_bindings"]
        ),
        main_ref_updates=tuple(
            _ref_update_from_dict(item) for item in value["main_ref_updates"]
        ),
        confirmation_action=str(value["confirmation_action"]),
        confirmation_subject_digest=str(value["confirmation_subject_digest"]),
        confirmation_authorization_id=str(value["confirmation_authorization_id"]),
        confirmation_token_digest=str(value["confirmation_token_digest"]),
        main_advanced_event_ids=tuple(str(item) for item in value["main_advanced_event_ids"]),
        operation_final_evidence_ref=str(value["operation_final_evidence_ref"]),
        iteration_final_evidence_refs=tuple(
            _iteration_ref_from_dict(item) for item in value["iteration_final_evidence_refs"]
        ),
        metadata_digest=str(value["metadata_digest"]),
    )


def final_acceptance_plan_from_dict(value: object) -> FinalAcceptancePlan:
    """Strictly reconstruct one durable final-acceptance plan snapshot."""

    if not isinstance(value, Mapping):
        raise FinalAcceptanceError("final acceptance plan snapshot is not an object")
    required = {field.name for field in FinalAcceptancePlan.__dataclass_fields__.values()}
    if set(value) != required:
        raise FinalAcceptanceError("final acceptance plan snapshot fields are unsupported")
    blockers_raw = value["blockers"]
    if not isinstance(blockers_raw, list):
        raise FinalAcceptanceError("final acceptance plan blockers are malformed")
    blockers: list[train.Blocker] = []
    for item in blockers_raw:
        if not isinstance(item, Mapping) or set(item) != {"code", "message"}:
            raise FinalAcceptanceError("final acceptance plan blocker is malformed")
        blockers.append(train.Blocker(code=str(item["code"]), message=str(item["message"])))
    plan = FinalAcceptancePlan(
        schema_version=str(value["schema_version"]),
        operation_id=str(value["operation_id"]),
        project_root=str(value["project_root"]),
        git_common_dir=str(value["git_common_dir"]),
        metadata_blob=str(value["metadata_blob"]),
        metadata=_envelope_from_dict(value["metadata"]),
        journal_path=str(value["journal_path"]),
        plan_digest=str(value["plan_digest"]),
        blockers=tuple(blockers),
        requires_confirmation=bool(value["requires_confirmation"]),
        pushed=bool(value["pushed"]),
    )
    if plan.schema_version != PLAN_SCHEMA:
        raise FinalAcceptanceError("final acceptance plan schema is unsupported")
    if plan.plan_digest != final_acceptance_plan_digest(plan):
        raise FinalAcceptanceError("final acceptance plan snapshot digest is invalid")
    if plan.metadata.metadata_digest != metadata_digest(plan.metadata):
        raise FinalAcceptanceError("final acceptance plan metadata digest is invalid")
    return plan


def load_final_acceptance_plan(
    project_root: str | Path,
    *,
    operation_id: str,
) -> FinalAcceptancePlan | None:
    """Load the exact pre-CAS plan from its private durable recovery journal.

    The returned plan is recovery input, not final authority.  Once public refs
    exist, callers must additionally use ``load_registered_final_acceptance``.
    No live plan is synthesized here.
    """

    repo = train.open_repository(project_root)
    operation = _validate_operation(operation_id)
    path = _journal_path(repo, operation)
    journal = _read_journal(path, repo.common_dir)
    if journal is None:
        return None
    snapshot = journal.get("plan_snapshot")
    plan = final_acceptance_plan_from_dict(snapshot)
    if (
        plan.operation_id != operation
        or os.path.normcase(plan.project_root) != os.path.normcase(str(repo.root))
        or os.path.normcase(plan.git_common_dir) != os.path.normcase(str(repo.common_dir))
        or Path(plan.journal_path).resolve(strict=False) != path.resolve(strict=False)
        or journal.get("plan_digest") != plan.plan_digest
        or journal.get("metadata_digest") != plan.metadata.metadata_digest
        or journal.get("metadata_blob") != plan.metadata_blob
    ):
        raise FinalAcceptanceError("final acceptance journal and plan snapshot identities differ")
    plan_blockers = _final_plan_binding_blockers(repo, plan)
    if plan_blockers:
        raise FinalAcceptanceError(
            "durable final acceptance plan authority is invalid: "
            + "; ".join(f"{item.code}:{item.message}" for item in plan_blockers)
        )
    return plan


def _project_main_advance_result(
    receipt: RegisteredFinalAcceptance,
    *,
    idempotent: bool,
) -> train.MainAdvanceResult:
    """Project an already public-gated receipt without repeating Git reads."""
    snapshot = receipt.metadata.main_plan_snapshot
    required = {
        "operation_id",
        "project_root",
        "integration_worktree",
        "main_ref",
        "expected_main",
        "integrated_commit",
        "ref_updates",
    }
    if not required.issubset(snapshot):
        raise FinalAcceptanceError("final acceptance main plan snapshot is incomplete")
    raw_updates = snapshot["ref_updates"]
    if not isinstance(raw_updates, list):
        raise FinalAcceptanceError("final acceptance main ref updates are malformed")
    updated_refs = tuple(str(item[0]) for item in raw_updates if isinstance(item, list) and item)
    updated_refs += (receipt.evidence_ref,)
    updated_refs += tuple(item.ref_name for item in receipt.iteration_evidence_refs)
    repo = train.open_repository(receipt.project_root)
    advance_journal = (
        repo.common_dir
        / "project-harness"
        / "train"
        / "v1"
        / "journal"
        / f"advance-{_validate_operation(receipt.operation_id)}.json"
    )
    return train.MainAdvanceResult(
        schema_version=train.ADVANCE_RESULT_SCHEMA,
        operation_id=str(snapshot["operation_id"]),
        project_root=str(snapshot["project_root"]),
        integration_worktree=str(snapshot["integration_worktree"]),
        main_ref=str(snapshot["main_ref"]),
        previous_main=str(snapshot["expected_main"]),
        current_main=str(snapshot["integrated_commit"]),
        updated_refs=updated_refs,
        journal_path=str(advance_journal),
        cleanup_worktree="pending-explicit-cleanup",
        idempotent=idempotent,
        final_acceptance_digest=receipt.registration_digest,
        final_acceptance_evidence_blob=receipt.evidence_blob,
        final_acceptance_evidence_ref=receipt.evidence_ref,
        final_acceptance_iteration_evidence_refs=tuple(
            item.ref_name for item in receipt.iteration_evidence_refs
        ),
    )


def main_advance_result_from_final_acceptance(
    receipt: RegisteredFinalAcceptance,
    *,
    idempotent: bool = True,
) -> train.MainAdvanceResult:
    """Publicly gate then project one final receipt for cleanup."""

    blockers = registered_final_acceptance_gate(receipt.project_root, receipt)
    if blockers:
        raise FinalAcceptanceError(
            "cannot project invalid final acceptance: "
            + "; ".join(item.code for item in blockers)
        )
    return _project_main_advance_result(receipt, idempotent=idempotent)


def registered_final_acceptance_gate(
    project_root: str | Path,
    receipt: RegisteredFinalAcceptance,
) -> tuple[train.Blocker, ...]:
    """Authenticate final acceptance from public Git objects and refs only."""

    if not isinstance(receipt, RegisteredFinalAcceptance):
        return (train.Blocker("final-acceptance-registration-type", "receipt is not structured"),)
    blockers: list[train.Blocker] = []
    if receipt.schema_version != REGISTRATION_SCHEMA:
        blockers.append(train.Blocker("final-acceptance-registration-schema", "schema is unsupported"))
    if receipt.registration_digest != registered_final_acceptance_digest(receipt):
        blockers.append(train.Blocker("final-acceptance-registration-digest", "receipt was changed"))
    repo = train.open_repository(project_root)
    if os.path.normcase(str(repo.root)) != os.path.normcase(receipt.project_root):
        blockers.append(train.Blocker("final-acceptance-project-root", "receipt belongs elsewhere"))
        return tuple(blockers)
    envelope = receipt.metadata
    if receipt.operation_id != envelope.operation_id:
        blockers.append(train.Blocker("final-acceptance-operation-binding", receipt.operation_id))
    if receipt.evidence_ref != envelope.operation_final_evidence_ref:
        blockers.append(train.Blocker("final-acceptance-ref-binding", receipt.evidence_ref))
    if receipt.iteration_evidence_refs != envelope.iteration_final_evidence_refs:
        blockers.append(train.Blocker("final-acceptance-iteration-ref-binding", "receipt differs"))
    if _resolve_ref(repo, receipt.evidence_ref) != receipt.evidence_blob:
        blockers.append(train.Blocker("final-acceptance-ref-drift", receipt.evidence_ref))
    elif _object_type(repo, receipt.evidence_blob) != "blob":
        blockers.append(train.Blocker("final-acceptance-ref-type", receipt.evidence_ref))
    for item in receipt.iteration_evidence_refs:
        if _resolve_ref(repo, item.ref_name) != receipt.evidence_blob:
            blockers.append(train.Blocker("final-acceptance-iteration-ref-drift", item.ref_name))
    try:
        raw = _blob_bytes(repo, receipt.evidence_blob)
    except FinalAcceptanceError as exc:
        blockers.append(train.Blocker("final-acceptance-blob-unreadable", str(exc)))
    else:
        if raw != metadata_bytes(envelope):
            blockers.append(train.Blocker("final-acceptance-blob-content", "blob differs from metadata"))
    blockers.extend(_live_blockers(repo, envelope, require_refs=True))
    return tuple(dict.fromkeys(blockers))


def load_registered_final_acceptance(
    project_root: str | Path,
    *,
    operation_id: str,
) -> tuple[RegisteredFinalAcceptance | None, tuple[train.Blocker, ...]]:
    """Reconstruct final acceptance from its canonical operation ref."""

    repo = train.open_repository(project_root)
    operation = _validate_operation(operation_id)
    evidence_ref = operation_final_evidence_ref(operation)
    evidence_blob = _resolve_ref(repo, evidence_ref)
    if evidence_blob is None:
        return None, (train.Blocker("final-acceptance-missing", operation),)
    if _object_type(repo, evidence_blob) != "blob":
        return None, (train.Blocker("final-acceptance-ref-object-type", evidence_ref),)
    try:
        raw = _blob_bytes(repo, evidence_blob)
        value = json.loads(raw.decode("utf-8"))
        if canonical_json(value) + b"\n" != raw:
            raise FinalAcceptanceError("final acceptance blob is not canonical JSON")
        envelope = _envelope_from_dict(value)
    except (UnicodeDecodeError, json.JSONDecodeError, FinalAcceptanceError) as exc:
        return None, (train.Blocker("final-acceptance-metadata-invalid", str(exc)),)
    if envelope.operation_id != operation:
        return None, (train.Blocker("final-acceptance-operation-mismatch", operation),)
    provisional = RegisteredFinalAcceptance(
        schema_version=REGISTRATION_SCHEMA,
        operation_id=operation,
        project_root=str(repo.root),
        evidence_ref=evidence_ref,
        evidence_blob=evidence_blob,
        iteration_evidence_refs=envelope.iteration_final_evidence_refs,
        metadata=envelope,
        registration_digest="0" * 64,
        journal_path=str(_journal_path(repo, operation)),
        idempotent=True,
    )
    receipt = replace(
        provisional,
        registration_digest=registered_final_acceptance_digest(provisional),
    )
    blockers = registered_final_acceptance_gate(repo.root, receipt)
    return (receipt if not blockers else None), blockers


def load_main_advance_result_from_final_acceptance(
    project_root: str | Path,
    *,
    operation_id: str,
) -> tuple[
    train.MainAdvanceResult | None,
    RegisteredFinalAcceptance | None,
    tuple[train.Blocker, ...],
]:
    """Public-gate once, then reconstruct the cleanup-capable result."""

    receipt, blockers = load_registered_final_acceptance(
        project_root,
        operation_id=operation_id,
    )
    if receipt is None or blockers:
        return None, receipt, blockers
    try:
        result = _project_main_advance_result(receipt, idempotent=True)
    except FinalAcceptanceError as exc:
        values = (*blockers, train.Blocker("final-acceptance-result-projection", str(exc)))
        return None, receipt, tuple(dict.fromkeys(values))
    return result, receipt, ()


__all__ = [
    "AcceptedCandidate",
    "EVIDENCE_SCHEMA",
    "FinalAcceptanceEnvelope",
    "FinalAcceptanceError",
    "FinalAcceptancePlan",
    "InjectedCrash",
    "IterationFinalEvidenceRef",
    "JOURNAL_SCHEMA",
    "PLAN_SCHEMA",
    "REGISTRATION_SCHEMA",
    "RefBinding",
    "RefUpdate",
    "RegisteredFinalAcceptance",
    "apply_final_acceptance",
    "canonical_operation",
    "final_acceptance_plan_digest",
    "final_acceptance_plan_from_dict",
    "iteration_final_evidence_ref",
    "journal_path",
    "load_registered_final_acceptance",
    "load_final_acceptance_plan",
    "load_main_advance_result_from_final_acceptance",
    "main_advance_result_from_final_acceptance",
    "metadata_digest",
    "operation_final_evidence_ref",
    "plan_final_acceptance",
    "registered_final_acceptance_digest",
    "registered_final_acceptance_gate",
]
