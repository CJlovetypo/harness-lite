from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parents[1] / "project_harness.py"
V2_ALLOCATION_PREFIX = "refs/project-harness/v2/allocations"
V2_ITERATION_PREFIX = "refs/project-harness/v2/iterations"
LEGACY_ITERATION_PREFIX = "refs/project-harness/iterations"


class ParallelHarnessCliTests(unittest.TestCase):
    """Black-box contracts for the first parallel-lifecycle CLI slice."""

    maxDiff = None

    def setUp(self) -> None:
        git = shutil.which("git")
        if not git:
            self.skipTest("git is required")
        self.git_executable = git
        self.temporary = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "project"
        self.root.mkdir()
        self.linked_worktrees = self.sandbox / "worktrees"
        self.linked_worktrees.mkdir()
        self.git_config = self.sandbox / "gitconfig"
        self.plans_by_operation: dict[str, dict[str, Any]] = {}
        subprocess.run(
            [git, "config", "--file", str(self.git_config), "user.name", "Harness Parallel Tests"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [git, "config", "--file", str(self.git_config), "user.email", "parallel@example.invalid"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": str(self.git_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        self.initial_head = self.initialize_repository()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialize_repository(self) -> str:
        self.git("init", "-b", "main")
        source = self.root / "src" / "app.txt"
        source.parent.mkdir()
        source.write_text("baseline\n", encoding="utf-8")
        self.git("add", "--", "src/app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "baseline")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def git(
        self,
        *arguments: str,
        check: bool = True,
        cwd: Path | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(cwd or self.root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            input=input_text,
        )

    def initialize_harness(self) -> None:
        result = self.run_cli(
            "init",
            "--project-root",
            str(self.root),
            "--project-name",
            "Parallel Harness Test",
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        self.git("add", "--", "AGENTS.md", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "committed governance baseline")
        self.initial_head = self.git("rev-parse", "HEAD").stdout.strip()

    def cli_command(self, *arguments: str) -> list[str]:
        return [sys.executable, str(SCRIPT), *arguments]

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.cli_command(*arguments),
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )

    def parse_payload(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                "CLI --json output must be one JSON object even on blocked operations; "
                f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}: {exc}"
            )
        self.assertIsInstance(payload, dict)
        return payload

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        payload = self.parse_payload(result)
        self.assertEqual(
            result.returncode,
            0,
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        return payload

    def assert_common_json(
        self,
        payload: dict[str, Any],
        *,
        command: str,
        action_level: str,
    ) -> None:
        self.assertEqual(payload.get("schema_version"), "harness-lite.operation/v1")
        self.assertEqual(payload.get("command"), command)
        self.assertEqual(payload.get("action_level"), action_level)
        self.assertIs(payload.get("pushed"), False)
        self.assertEqual(Path(payload.get("project_root", "")).resolve(), self.root.resolve())

    @staticmethod
    def reservation_number(reservation: dict[str, Any]) -> str | None:
        value = reservation.get("iteration", reservation.get("observed_next_iteration"))
        return value if isinstance(value, str) else None

    @staticmethod
    def reservation_base(reservation: dict[str, Any]) -> str | None:
        value = reservation.get("base_oid", reservation.get("base_commit"))
        return value if isinstance(value, str) else None

    def status(self) -> dict[str, Any]:
        result = self.run_cli(
            "status",
            "--project-root",
            str(self.root),
            "--all-worktrees",
            "--json",
        )
        payload = self.assert_success(result)
        self.assert_common_json(payload, command="status", action_level="silent")
        return payload

    def plan_reservation(self, title: str) -> dict[str, Any]:
        result = self.run_cli(
            "plan",
            "reserve-iteration",
            "--project-root",
            str(self.root),
            "--title",
            title,
            "--base-ref",
            "refs/heads/main",
            "--governance-ref",
            "refs/heads/main",
            "--json",
        )
        payload = self.assert_success(result)
        self.assert_common_json(payload, command="reserve-iteration", action_level="silent")
        self.assertEqual(payload.get("phase"), "planned")
        self.assertIsInstance(payload.get("operation_id"), str)
        self.assertTrue(payload["operation_id"])
        self.assertIsInstance(payload.get("reservation"), dict)
        self.assertEqual(self.reservation_base(payload["reservation"]), self.initial_head)
        digest = payload.get("plan_digest", payload["reservation"].get("digest"))
        self.assertRegex(digest or "", r"^[0-9a-f]{64}$")
        self.plans_by_operation[payload["operation_id"]] = payload
        return payload

    def reserve(
        self,
        title: str,
        operation_id: str,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        plan = self.plans_by_operation[operation_id]
        result = self.run_cli(
            "reserve-iteration",
            "--project-root",
            str(self.root),
            "--title",
            title,
            "--operation-id",
            operation_id,
            "--base-ref",
            "refs/heads/main",
            "--governance-ref",
            "refs/heads/main",
            "--accept-plan-digest",
            str(plan["plan_digest"]),
            "--json",
        )
        payload = self.parse_payload(result)
        return result, payload

    def assert_reservation_payload(
        self,
        payload: dict[str, Any],
        *,
        title: str,
        operation_id: str,
    ) -> str:
        self.assert_common_json(payload, command="reserve-iteration", action_level="notify")
        self.assertEqual(payload.get("phase"), "succeeded")
        self.assertEqual(payload.get("operation_id"), operation_id)
        reservation = payload.get("reservation")
        self.assertIsInstance(reservation, dict)
        number = self.reservation_number(reservation)
        self.assertRegex(number or "", r"^\d{3,}$")
        rendered_title = reservation.get("title", payload.get("title"))
        if rendered_title is not None:
            self.assertEqual(rendered_title, title)
        self.assertEqual(self.reservation_base(reservation), self.initial_head)
        self.assertEqual(
            reservation.get("allocation_ref"),
            f"{V2_ALLOCATION_PREFIX}/{number}",
        )
        self.assertEqual(
            reservation.get("base_ref"),
            f"{V2_ITERATION_PREFIX}/{number}/base",
        )
        self.assertEqual(reservation.get("governance_ref"), "refs/heads/main")
        self.assertEqual(reservation.get("governance_commit"), self.initial_head)
        self.assertRegex(str(reservation.get("governance_tree", "")), r"^[0-9a-f]{40,64}$")
        self.assertRegex(str(reservation.get("principle_sha256", "")), r"^[0-9a-f]{64}$")
        return number

    def allocation_metadata(self, allocation_object: str) -> dict[str, Any]:
        self.assertEqual(self.git("cat-file", "-t", allocation_object).stdout.strip(), "blob")
        return json.loads(self.git("cat-file", "-p", allocation_object).stdout)

    def refs(self, prefix: str | None = None) -> dict[str, str]:
        arguments = ["for-each-ref", "--format=%(refname) %(objectname)"]
        if prefix:
            arguments.append(prefix)
        result = self.git(*arguments)
        refs: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            name, oid = line.split(" ", 1)
            refs[name] = oid
        return refs

    def working_tree_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or ".git" in path.relative_to(self.root).parts:
                continue
            relative = path.relative_to(self.root).as_posix()
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snapshot

    def repository_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root).as_posix()
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snapshot

    def worktree_snapshot(self) -> str:
        return self.git("worktree", "list", "--porcelain").stdout

    def branch_snapshot(self) -> dict[str, str]:
        return self.refs("refs/heads")

    def status_snapshot(self) -> str:
        return self.git("status", "--porcelain=v1", "-uall").stdout

    def git_common_dir(self) -> Path:
        raw = self.git("rev-parse", "--git-common-dir").stdout.strip()
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    def journal_for(self, operation_id: str) -> Path:
        journal_root = self.git_common_dir() / "project-harness"
        self.assertTrue(journal_root.is_dir(), f"missing operational journal root: {journal_root}")
        matches = list(journal_root.rglob(f"{operation_id}.json"))
        self.assertEqual(len(matches), 1, f"journal matches: {matches}")
        if not matches:
            self.fail(f"no journal found for {operation_id} under {journal_root}")
        return matches[0]

    def iteration_entries(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        entries = payload.get("iterations")
        self.assertIsInstance(entries, list)
        by_number: dict[str, dict[str, Any]] = {}
        for entry in entries:
            self.assertIsInstance(entry, dict)
            number = entry.get("iteration", entry.get("number"))
            self.assertRegex(number or "", r"^\d{3,}$")
            by_number[number] = entry
        return by_number

    def worktree_entries(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        entries = payload.get("worktrees")
        self.assertIsInstance(entries, list)
        for entry in entries:
            self.assertIsInstance(entry, dict)
            self.assertTrue(entry.get("path", entry.get("worktree")))
            self.assertTrue(entry.get("head_oid", entry.get("HEAD")))
        return entries

    @staticmethod
    def worktree_path(entry: dict[str, Any]) -> str:
        value = entry.get("path", entry.get("worktree"))
        return value if isinstance(value, str) else ""

    @staticmethod
    def worktree_head(entry: dict[str, Any]) -> str:
        value = entry.get("head_oid", entry.get("HEAD"))
        return value if isinstance(value, str) else ""

    def test_status_recognizes_empty_repository_without_writing(self) -> None:
        before = self.repository_snapshot()

        payload = self.status()

        self.assertEqual(self.iteration_entries(payload), {})
        worktrees = self.worktree_entries(payload)
        self.assertEqual(len(worktrees), 1)
        self.assertEqual(Path(self.worktree_path(worktrees[0])).resolve(), self.root.resolve())
        self.assertEqual(self.worktree_head(worktrees[0]), self.initial_head)
        self.assertEqual(self.repository_snapshot(), before)

    def test_status_reads_legacy_nested_base_and_v2_refs_with_linked_worktree(self) -> None:
        self.initialize_harness()
        legacy_base = f"{LEGACY_ITERATION_PREFIX}/001/base/refs/heads/main"
        legacy_final = f"{LEGACY_ITERATION_PREFIX}/001/final"
        self.git("update-ref", legacy_base, self.initial_head)
        self.git("update-ref", legacy_final, self.initial_head)

        plan = self.plan_reservation("status fixture")
        self.assertEqual(self.reservation_number(plan["reservation"]), "002")
        reserve_result, reserve_payload = self.reserve("status fixture", plan["operation_id"])
        self.assertEqual(reserve_result.returncode, 0, reserve_result.stderr)
        self.assert_reservation_payload(
            reserve_payload,
            title="status fixture",
            operation_id=plan["operation_id"],
        )
        allocation_ref = f"{V2_ALLOCATION_PREFIX}/002"
        v2_base = f"{V2_ITERATION_PREFIX}/002/base"
        allocation_object = self.refs("refs/project-harness")[allocation_ref]
        linked = self.linked_worktrees / "prd-002-status"
        self.git("worktree", "add", "-b", "prd/002-status", str(linked), self.initial_head)

        payload = self.status()

        iterations = self.iteration_entries(payload)
        self.assertEqual(set(iterations), {"001", "002"})
        self.assertEqual(iterations["001"].get("lifecycle", iterations["001"].get("base_format")), "legacy")
        self.assertEqual(iterations["001"].get("base_ref"), legacy_base)
        self.assertEqual(iterations["001"].get("base_oid", iterations["001"].get("base_commit")), self.initial_head)
        self.assertEqual(iterations["002"].get("lifecycle", iterations["002"].get("base_format")), "v2")
        self.assertEqual(iterations["002"].get("allocation_ref"), allocation_ref)
        self.assertEqual(iterations["002"].get("allocation_object"), allocation_object)
        self.assertEqual(iterations["002"].get("base_ref"), v2_base)
        self.assertEqual(iterations["002"].get("base_oid", iterations["002"].get("base_commit")), self.initial_head)

        worktrees = self.worktree_entries(payload)
        linked_entry = next(
            entry for entry in worktrees if Path(self.worktree_path(entry)).resolve() == linked.resolve()
        )
        self.assertEqual(linked_entry.get("branch"), "refs/heads/prd/002-status")
        self.assertEqual(self.worktree_head(linked_entry), self.initial_head)
        self.assertNotIn(f"{LEGACY_ITERATION_PREFIX}/002/base", self.refs())

    def test_plan_reserve_iteration_is_a_zero_write_operation(self) -> None:
        self.initialize_harness()
        before_repository = self.repository_snapshot()
        before_refs = self.refs()
        before_worktrees = self.worktree_snapshot()
        before_status = self.status_snapshot()

        payload = self.plan_reservation("Parallel reservation")

        reservation = payload["reservation"]
        self.assertEqual(self.reservation_number(reservation), "001")
        self.assertEqual(
            reservation.get("allocation_ref"),
            f"{V2_ALLOCATION_PREFIX}/001",
        )
        self.assertEqual(
            reservation.get("base_ref"),
            f"{V2_ITERATION_PREFIX}/001/base",
        )
        self.assertRegex(payload.get("plan_digest", reservation.get("digest", "")), r"^[0-9a-f]{64}$")
        self.assertEqual(self.repository_snapshot(), before_repository)
        self.assertEqual(self.refs(), before_refs)
        self.assertEqual(self.worktree_snapshot(), before_worktrees)
        self.assertEqual(self.status_snapshot(), before_status)

    def test_reservation_atomically_creates_v2_allocation_base_and_journal_only(self) -> None:
        self.initialize_harness()
        title = "First parallel reservation"
        plan = self.plan_reservation(title)
        operation_id = plan["operation_id"]
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        before_files = self.working_tree_snapshot()
        before_status = self.status_snapshot()
        before_worktrees = self.worktree_snapshot()
        before_branches = self.branch_snapshot()

        result, payload = self.reserve(title, operation_id)

        self.assertEqual(result.returncode, 0, result.stderr)
        number = self.assert_reservation_payload(
            payload,
            title=title,
            operation_id=operation_id,
        )
        allocation_ref = f"{V2_ALLOCATION_PREFIX}/{number}"
        base_ref = f"{V2_ITERATION_PREFIX}/{number}/base"
        project_refs = self.refs("refs/project-harness")
        allocation_object = project_refs.get(allocation_ref)
        self.assertRegex(allocation_object or "", r"^[0-9a-f]{40,64}$")
        self.assertEqual(project_refs.get(base_ref), self.initial_head)
        self.assertNotIn(f"{LEGACY_ITERATION_PREFIX}/{number}/base", project_refs)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before_head)
        self.assertEqual(self.working_tree_snapshot(), before_files)
        self.assertEqual(self.status_snapshot(), before_status)
        self.assertEqual(self.worktree_snapshot(), before_worktrees)
        self.assertEqual(self.branch_snapshot(), before_branches)

        journal_path = self.journal_for(operation_id)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(journal.get("operation_id"), operation_id)
        self.assertEqual(journal.get("phase", journal.get("state")), "READY")
        journal_digest = journal.get("plan_digest", journal.get("operation_digest"))
        self.assertRegex(journal_digest or "", r"^[0-9a-f]{64}$")
        journal_iteration = journal.get("iteration")
        if journal_iteration is None and isinstance(journal.get("reservation"), dict):
            journal_iteration = journal["reservation"].get("iteration")
        self.assertEqual(journal_iteration, number)
        journal_allocation = journal.get("allocation_object")
        if journal_allocation is not None:
            self.assertEqual(journal_allocation, allocation_object)
        metadata = self.allocation_metadata(allocation_object or "")
        self.assertEqual(metadata.get("operation_id"), operation_id)
        self.assertEqual(metadata.get("plan_digest"), journal_digest)
        self.assertEqual(metadata.get("iteration"), number)
        self.assertEqual(metadata.get("base_commit"), self.initial_head)
        leftovers = [
            path
            for path in (self.git_common_dir() / "project-harness").rglob("*")
            if path.is_file() and path.suffix.lower() in {".tmp", ".partial"}
        ]
        self.assertEqual(leftovers, [])
        lock_files = list((self.git_common_dir() / "project-harness" / "locks").rglob("*.lock"))
        self.assertEqual(len(lock_files), 1)

    def test_same_operation_retry_is_idempotent(self) -> None:
        self.initialize_harness()
        title = "Idempotent reservation"
        plan = self.plan_reservation(title)
        operation_id = plan["operation_id"]
        first_result, first = self.reserve(title, operation_id)
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        number = self.assert_reservation_payload(first, title=title, operation_id=operation_id)
        before_refs = self.refs()
        journal_path = self.journal_for(operation_id)
        before_journal = journal_path.read_bytes()

        retry_result, retry = self.reserve(title, operation_id)

        self.assertEqual(retry_result.returncode, 0, retry_result.stderr)
        retry_number = self.assert_reservation_payload(
            retry,
            title=title,
            operation_id=operation_id,
        )
        self.assertEqual(retry_number, number)
        self.assertEqual(self.refs(), before_refs)
        self.assertEqual(journal_path.read_bytes(), before_journal)

    def test_same_operation_with_different_digest_is_blocked_without_mutation(self) -> None:
        self.initialize_harness()
        title = "Digest-bound reservation"
        plan = self.plan_reservation(title)
        operation_id = plan["operation_id"]
        first_result, first = self.reserve(title, operation_id)
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        self.assert_reservation_payload(first, title=title, operation_id=operation_id)
        before_refs = self.refs()
        before_files = self.working_tree_snapshot()
        before_worktrees = self.worktree_snapshot()
        journal_path = self.journal_for(operation_id)
        before_journal = journal_path.read_bytes()

        result, payload = self.reserve("Different title changes digest", operation_id)

        self.assertNotEqual(result.returncode, 0)
        self.assert_common_json(payload, command="reserve-iteration", action_level="notify")
        self.assertIn(payload.get("phase"), {"blocked", "stale"})
        reason_codes = {
            reason.get("code")
            for reason in payload.get("blocking_reasons", [])
            if isinstance(reason, dict)
        }
        self.assertTrue(
            {
                "operation_digest_mismatch",
                "operation-digest-mismatch",
                "operation-title-mismatch",
            }
            & reason_codes,
            reason_codes,
        )
        self.assertEqual(self.refs(), before_refs)
        self.assertEqual(self.working_tree_snapshot(), before_files)
        self.assertEqual(self.worktree_snapshot(), before_worktrees)
        self.assertEqual(journal_path.read_bytes(), before_journal)

    def test_bad_journal_json_is_preserved_and_requires_reconcile(self) -> None:
        self.initialize_harness()
        title = "Corrupt journal reservation"
        plan = self.plan_reservation(title)
        operation_id = plan["operation_id"]
        first_result, first = self.reserve(title, operation_id)
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        self.assert_reservation_payload(first, title=title, operation_id=operation_id)
        journal_path = self.journal_for(operation_id)
        corrupt_bytes = b'{"operation_id": "unterminated"'
        journal_path.write_bytes(corrupt_bytes)
        before_refs = self.refs()
        before_files = self.working_tree_snapshot()
        before_worktrees = self.worktree_snapshot()

        result, payload = self.reserve(title, operation_id)

        self.assertNotEqual(result.returncode, 0)
        self.assert_common_json(payload, command="reserve-iteration", action_level="notify")
        self.assertIn(payload.get("phase"), {"blocked", "failed_needs_reconcile"})
        reason_codes = {
            reason.get("code")
            for reason in payload.get("blocking_reasons", [])
            if isinstance(reason, dict)
        }
        self.assertTrue(
            {"journal_invalid_json", "journal-invalid-json", "corrupt-operation-journal"} & reason_codes,
            reason_codes,
        )
        self.assertEqual(journal_path.read_bytes(), corrupt_bytes)
        self.assertEqual(self.refs(), before_refs)
        self.assertEqual(self.working_tree_snapshot(), before_files)
        self.assertEqual(self.worktree_snapshot(), before_worktrees)

    def test_competing_operations_allocate_distinct_next_ids_and_atomic_bases(self) -> None:
        self.initialize_harness()
        legacy_base = f"{LEGACY_ITERATION_PREFIX}/001/base/refs/heads/main"
        self.git("update-ref", legacy_base, self.initial_head)
        existing_plan = self.plan_reservation("existing v2 fixture")
        self.assertEqual(self.reservation_number(existing_plan["reservation"]), "002")
        existing_result, existing_payload = self.reserve(
            "existing v2 fixture", existing_plan["operation_id"]
        )
        self.assertEqual(existing_result.returncode, 0, existing_result.stderr)
        self.assert_reservation_payload(
            existing_payload,
            title="existing v2 fixture",
            operation_id=existing_plan["operation_id"],
        )
        title_a = "Competing reservation A"
        title_b = "Competing reservation B"
        plan_a = self.plan_reservation(title_a)
        plan_b = self.plan_reservation(title_b)
        self.assertEqual(self.reservation_number(plan_a["reservation"]), "003")
        self.assertEqual(self.reservation_number(plan_b["reservation"]), "003")
        before_head = self.git("rev-parse", "HEAD").stdout.strip()
        before_files = self.working_tree_snapshot()
        before_status = self.status_snapshot()
        before_worktrees = self.worktree_snapshot()
        before_branches = self.branch_snapshot()
        barrier = threading.Barrier(2)

        def invoke(title: str, operation_id: str) -> subprocess.CompletedProcess[str]:
            barrier.wait(timeout=10)
            plan = self.plans_by_operation[operation_id]
            return self.run_cli(
                "reserve-iteration",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--operation-id",
                operation_id,
                "--base-ref",
                "refs/heads/main",
                "--governance-ref",
                "refs/heads/main",
                "--accept-plan-digest",
                str(plan["plan_digest"]),
                "--json",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(invoke, title_a, plan_a["operation_id"])
            future_b = executor.submit(invoke, title_b, plan_b["operation_id"])
            results = [future_a.result(timeout=30), future_b.result(timeout=30)]

        payloads = [self.assert_success(result) for result in results]
        numbers = {
            self.assert_reservation_payload(
                payload,
                title=title,
                operation_id=operation_id,
            )
            for payload, title, operation_id in (
                (payloads[0], title_a, plan_a["operation_id"]),
                (payloads[1], title_b, plan_b["operation_id"]),
            )
        }
        self.assertEqual(numbers, {"003", "004"})
        project_refs = self.refs("refs/project-harness")
        for number in numbers:
            allocation_ref = f"{V2_ALLOCATION_PREFIX}/{number}"
            base_ref = f"{V2_ITERATION_PREFIX}/{number}/base"
            allocation_object = project_refs.get(allocation_ref)
            self.assertRegex(allocation_object or "", r"^[0-9a-f]{40,64}$")
            metadata = self.allocation_metadata(allocation_object or "")
            self.assertEqual(metadata.get("iteration"), number)
            self.assertEqual(metadata.get("base_commit"), self.initial_head)
            self.assertEqual(project_refs.get(base_ref), self.initial_head)
            self.assertNotIn(f"{LEGACY_ITERATION_PREFIX}/{number}/base", project_refs)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before_head)
        self.assertEqual(self.working_tree_snapshot(), before_files)
        self.assertEqual(self.status_snapshot(), before_status)
        self.assertEqual(self.worktree_snapshot(), before_worktrees)
        self.assertEqual(self.branch_snapshot(), before_branches)

    def test_plan_digest_blocks_main_drift_before_journal_or_refs(self) -> None:
        self.initialize_harness()
        title = "Digest rejects main drift"
        plan = self.plan_reservation(title)
        source = self.root / "src" / "app.txt"
        source.write_text("baseline\nmain moved\n", encoding="utf-8")
        self.git("add", "--", "src/app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "move main after plan")
        before_refs = self.refs("refs/project-harness/v2")

        result, payload = self.reserve(title, plan["operation_id"])

        self.assertNotEqual(result.returncode, 0)
        self.assert_common_json(payload, command="reserve-iteration", action_level="notify")
        reason_codes = {
            reason.get("code")
            for reason in payload.get("blocking_reasons", [])
            if isinstance(reason, dict)
        }
        self.assertIn("accepted-plan-digest-mismatch", reason_codes)
        self.assertEqual(self.refs("refs/project-harness/v2"), before_refs)
        journal_root = self.git_common_dir() / "project-harness" / "journal"
        self.assertFalse(journal_root.exists() and any(journal_root.rglob("*.json")))

    def test_explicit_base_ref_never_inherits_caller_branch_head(self) -> None:
        self.initialize_harness()
        self.git("switch", "-c", "prd/a-local")
        source = self.root / "src" / "app.txt"
        source.write_text("baseline\nA-only committed state\n", encoding="utf-8")
        self.git("add", "--", "src/app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "A-only state")
        self.assertNotEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.initial_head)

        plan = self.plan_reservation("Independent B")

        self.assertEqual(self.reservation_base(plan["reservation"]), self.initial_head)
        self.assertEqual(plan["reservation"].get("source_base_ref"), "refs/heads/main")
        self.assertEqual(plan["reservation"].get("governance_commit"), self.initial_head)

    def test_plan_validates_governance_from_main_commit_not_caller_worktree(self) -> None:
        self.initialize_harness()
        valid_governance_commit = self.initial_head
        self.git("switch", "-c", "prd/a-valid-governance")
        self.git("switch", "main")
        (self.root / "harness" / "README.md").write_text(
            "<!-- managed-by: harness-lite v1 -->\n\n# Structurally broken but owned\n",
            encoding="utf-8",
        )
        self.git("add", "--", "harness/README.md")
        self.git("commit", "--no-gpg-sign", "-m", "break main governance")
        broken_main = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(broken_main, valid_governance_commit)
        self.git("switch", "prd/a-valid-governance")
        self.assertTrue((self.root / "harness" / "README.md").is_file())
        before_refs = self.refs("refs/project-harness/v2")

        result = self.run_cli(
            "plan",
            "reserve-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "B must obey main governance",
            "--base-ref",
            "refs/heads/main",
            "--governance-ref",
            "refs/heads/main",
            "--json",
        )
        payload = self.parse_payload(result)

        self.assertNotEqual(result.returncode, 0)
        self.assert_common_json(payload, command="reserve-iteration", action_level="silent")
        self.assertEqual(payload.get("phase"), "blocked")
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertIn("Committed governance snapshot failed validation", rendered)
        self.assertIn("managed-markers", rendered)
        self.assertEqual(self.refs("refs/project-harness/v2"), before_refs)

    def test_same_operation_is_serialized_across_processes(self) -> None:
        self.initialize_harness()
        title = "Same operation race"
        plan = self.plan_reservation(title)
        barrier = threading.Barrier(2)

        def invoke() -> subprocess.CompletedProcess[str]:
            barrier.wait(timeout=10)
            return self.run_cli(
                "reserve-iteration",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--operation-id",
                plan["operation_id"],
                "--base-ref",
                "refs/heads/main",
                "--governance-ref",
                "refs/heads/main",
                "--accept-plan-digest",
                str(plan["plan_digest"]),
                "--json",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result(timeout=45) for future in (executor.submit(invoke), executor.submit(invoke))]

        payloads = [self.assert_success(result) for result in results]
        self.assertEqual(
            {self.reservation_number(payload["reservation"]) for payload in payloads},
            {"001"},
        )
        self.assertEqual(
            sorted(bool(payload["reservation"].get("created_now")) for payload in payloads),
            [False, True],
        )
        journal = json.loads(self.journal_for(plan["operation_id"]).read_text(encoding="utf-8"))
        self.assertEqual([event["phase"] for event in journal["history"]], ["PLANNED", "RESERVED", "READY"])

    def test_different_operation_can_plan_while_another_is_incomplete(self) -> None:
        self.initialize_harness()
        plan_a = self.plan_reservation("Operation A")
        result_a, _ = self.reserve("Operation A", plan_a["operation_id"])
        self.assertEqual(result_a.returncode, 0, result_a.stderr)
        journal_path = self.journal_for(plan_a["operation_id"])
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["phase"] = "PLANNED"
        journal["created_refs"] = []
        journal["history"] = [journal["history"][0]]
        journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        plan_b = self.plan_reservation("Operation B")
        result_b, payload_b = self.reserve("Operation B", plan_b["operation_id"])

        self.assertEqual(self.reservation_number(plan_b["reservation"]), "002")
        self.assertEqual(result_b.returncode, 0, result_b.stderr)
        self.assertEqual(
            self.assert_reservation_payload(
                payload_b,
                title="Operation B",
                operation_id=plan_b["operation_id"],
            ),
            "002",
        )

    def test_retry_recovers_refs_written_before_journal_advance_after_main_moves(self) -> None:
        self.initialize_harness()
        title = "Recover accepted reservation"
        plan = self.plan_reservation(title)
        first_result, first_payload = self.reserve(title, plan["operation_id"])
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        self.assert_reservation_payload(
            first_payload,
            title=title,
            operation_id=plan["operation_id"],
        )
        journal_path = self.journal_for(plan["operation_id"])
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["phase"] = "PLANNED"
        journal["created_refs"] = []
        journal["history"] = [journal["history"][0]]
        journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        source = self.root / "src" / "app.txt"
        source.write_text("baseline\nmain moved after ref CAS\n", encoding="utf-8")
        self.git("add", "--", "src/app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "move main after reservation CAS")
        blocked_status = self.run_cli(
            "status", "--project-root", str(self.root), "--all-worktrees", "--json"
        )
        self.assertNotEqual(blocked_status.returncode, 0)

        retry_result, retry_payload = self.reserve(title, plan["operation_id"])

        self.assertEqual(retry_result.returncode, 0, retry_result.stderr)
        self.assertFalse(retry_payload["reservation"]["created_now"])
        recovered = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(recovered["phase"], "READY")
        self.assertEqual(
            [event["phase"] for event in recovered["history"]],
            ["PLANNED", "RESERVED", "READY"],
        )

    def test_source_ref_verify_blocks_initial_cas_after_main_moves(self) -> None:
        self.initialize_harness()
        title = "Source ref CAS guard"
        plan = self.plan_reservation(title)
        first_result, first_payload = self.reserve(title, plan["operation_id"])
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        number = self.assert_reservation_payload(
            first_payload,
            title=title,
            operation_id=plan["operation_id"],
        )
        self.git("update-ref", "-d", f"{V2_ALLOCATION_PREFIX}/{number}")
        self.git("update-ref", "-d", f"{V2_ITERATION_PREFIX}/{number}/base")
        journal_path = self.journal_for(plan["operation_id"])
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["phase"] = "PLANNED"
        journal["created_refs"] = []
        journal["history"] = [journal["history"][0]]
        journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        source = self.root / "src" / "app.txt"
        source.write_text("baseline\nmain moved before initial CAS\n", encoding="utf-8")
        self.git("add", "--", "src/app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "move main before reservation CAS")

        retry_result, retry_payload = self.reserve(title, plan["operation_id"])

        self.assertNotEqual(retry_result.returncode, 0)
        self.assert_common_json(retry_payload, command="reserve-iteration", action_level="notify")
        failed = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["phase"], "FAILED_NEEDS_RECONCILE")
        self.assertIn("source base ref changed", failed["error"])
        self.assertNotIn(f"{V2_ALLOCATION_PREFIX}/{number}", self.refs())
        self.assertNotIn(f"{V2_ITERATION_PREFIX}/{number}/base", self.refs())

    def test_orphan_v2_base_is_a_status_blocker(self) -> None:
        self.initialize_harness()
        self.git("update-ref", f"{V2_ITERATION_PREFIX}/001/base", self.initial_head)

        result = self.run_cli(
            "status", "--project-root", str(self.root), "--all-worktrees", "--json"
        )
        payload = self.parse_payload(result)

        self.assertNotEqual(result.returncode, 0)
        reason_codes = {
            reason.get("code")
            for reason in payload.get("blocking_reasons", [])
            if isinstance(reason, dict)
        }
        self.assertIn("iteration-ref-inconsistent", reason_codes)

    def test_new_json_commands_return_one_json_error_envelope(self) -> None:
        self.initialize_harness()
        result = self.run_cli(
            "reserve-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Invalid operation id",
            "--operation-id",
            "../../escape",
            "--base-ref",
            "refs/heads/main",
            "--governance-ref",
            "refs/heads/main",
            "--accept-plan-digest",
            "0" * 64,
            "--json",
        )

        payload = self.parse_payload(result)
        self.assertNotEqual(result.returncode, 0)
        self.assert_common_json(payload, command="reserve-iteration", action_level="notify")
        self.assertEqual(payload.get("phase"), "blocked")

    def test_legacy_new_iteration_is_blocked_after_v2_identity_exists(self) -> None:
        self.initialize_harness()
        title = "V2 identity owns the lifecycle"
        plan = self.plan_reservation(title)
        reserve_result, _ = self.reserve(title, plan["operation_id"])
        self.assertEqual(reserve_result.returncode, 0, reserve_result.stderr)
        before_refs = self.refs()
        before_files = self.working_tree_snapshot()

        result = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Must not collide",
            "--dry-run",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("v2 identity", result.stderr)
        self.assertEqual(self.refs(), before_refs)
        self.assertEqual(self.working_tree_snapshot(), before_files)


if __name__ == "__main__":
    unittest.main()
