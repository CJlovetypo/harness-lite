from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import harness_train as train
from scripts import harness_workspace as workspace
from scripts.harness_candidate import AcceptanceEvidence, CandidateInput, build_candidate
from scripts.harness_coordinator import CoordinatorError, derive_iteration_authority, plan_route
from scripts import project_harness as core
from scripts.project_harness import apply_operations, build_init_operations


OWNER = "<!-- managed-by: harness-lite v1 -->\n"


class CoordinatorAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="harness-coordinator-test-")
        self.container = Path(self.temp.name)
        self.root = self.container / "primary"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        apply_operations(
            self.root,
            build_init_operations(
                self.root,
                "Coordinator Test",
                datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),
            ),
        )
        self.git("add", ".")
        self.git("commit", "-m", "canonical governance baseline")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.principle_sha = hashlib.sha256(
            self.git_bytes("show", "refs/heads/main:harness/principle.md")
        ).hexdigest()
        self.write_iteration("001", approved=True)
        self.candidate_workspaces: dict[str, dict[str, object]] = {}
        self.git(
            "update-ref",
            "refs/project-harness/iterations/001/base/refs/heads/main",
            self.head,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(
        self,
        *arguments: str,
        check: bool = True,
        input: str | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            ["git", "-C", str(cwd or self.root), *arguments],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=input,
            env=environment,
        )

    def git_bytes(self, *arguments: str) -> bytes:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        ).stdout

    def write_iteration(
        self,
        number: str,
        *,
        approved: bool,
        depends_on: str | None = None,
        conflicts_with: str | None = None,
        prd_approval: str | None = None,
        spec_approval: str | None = None,
        implementation_authorization: str | None = None,
    ) -> None:
        status = "实施中" if approved else "草案"
        prd_evidence = prd_approval or (f"用户明确批准 PRD-{number}" if approved else "待批准")
        spec_evidence = spec_approval or (f"用户明确批准 SPEC-{number}" if approved else "待批准")
        authorization = implementation_authorization or ("用户明确授权开始实施" if approved else "未授权")
        dependency = f"- 依赖 PRD：`{depends_on}`\n" if depends_on else ""
        conflict = f"- 冲突 PRD：`{conflicts_with}`\n" if conflicts_with else ""
        directory = self.root / "harness" / "iterations" / number
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "README.md").write_text(OWNER + f"# Iteration {number}\n", encoding="utf-8")
        (directory / f"deviation-{number}.md").write_text(
            OWNER + f"# PRD-{number} / SPEC-{number} deviations\n\n当前开放偏差：`0`。\n",
            encoding="utf-8",
        )
        (directory / f"prd-{number}.md").write_text(
            OWNER
            + f"# PRD-{number}：Feature {number}\n"
            + f"- 状态：`{status}`\n"
            + f"- 批准依据：{prd_evidence}\n"
            + dependency
            + conflict
            + f"\n## 验收标准\n\n### AC-{number}-01\n\nEvidence required.\n",
            encoding="utf-8",
        )
        (directory / f"spec-{number}.md").write_text(
            OWNER
            + f"# SPEC-{number}：Feature {number}\n"
            + f"- 状态：`{status}`\n"
            + f"- 批准依据：{spec_evidence}\n"
            + f"- 实施授权：{authorization}\n",
            encoding="utf-8",
        )

    def snapshot(self) -> tuple[str, str, tuple[str, ...]]:
        refs = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout
        status = self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout
        files = tuple(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()))
        return refs, hashlib.sha256(status.encode()).hexdigest(), files

    def write_valid_lease(self, iteration: str) -> Path:
        directory = self.root / ".git" / "project-harness" / "workspace" / "v1" / "leases" / "iterations"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{iteration}.json"
        value = {
            "schema_version": workspace.LEGACY_LEASE_SCHEMA,
            "scope": "iteration-writer",
            "state": "active",
            "iteration": iteration,
            "operation_id": "OP-" + "9" * 32,
            "owner": "task-test",
            "generation": 1,
            "execution_topology": "worktree",
            "expected_root": str(self.root),
            "worktree_path": str(self.root),
            "branch_ref": f"refs/heads/prd-{iteration}",
            "base_ref": f"refs/project-harness/v2/iterations/{iteration}/base",
            "base_commit": self.head,
            "principle_sha256": self.principle_sha,
            "runtime_namespace": f"prd-{iteration}",
            "acquired_at": "2026-08-12T01:00:00+00:00",
            "heartbeat": "2026-08-12T01:00:00+00:00",
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    @staticmethod
    def receipt_digest(value: dict[str, object], field: str) -> str:
        payload = dict(value)
        payload.pop(field, None)
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def write_v2_allocation(self, number: str, operation_char: str) -> str:
        operation = "OP-" + operation_char * 31 + number[-1]
        _governance_ref, _governance_commit, governance_snapshot = core.committed_governance_snapshot(
            "git", self.root, "refs/heads/main"
        )
        title = f"Candidate {number}"
        allocation_ref = f"refs/project-harness/v2/allocations/{number}"
        base_ref_value = f"refs/project-harness/v2/iterations/{number}/base"
        manifest = {
            "schema_version": core.OPERATION_PLAN_SCHEMA_V1,
            "operation_id": operation,
            "action": "reserve-iteration",
            "project_root": str(self.root),
            "title": title,
            "base_commit": self.head,
            "base_ref": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.head,
            "governance_snapshot": governance_snapshot,
            "reservation_policy": {
                "strategy": "next-monotonic-v2-cas",
                "collision_policy": "advance-to-current-max-plus-one",
                "max_attempts": core.RESERVATION_MAX_ATTEMPTS,
                "observed_next_iteration": number,
                "observed_allocation_ref": allocation_ref,
                "observed_base_ref": base_ref_value,
                "ref_namespace": core.V2_REF_ROOT,
            },
            "exclusions": [
                "no worktree",
                "no branch",
                "no governance bundle",
                "no progress update",
                "no commit",
                "no push",
            ],
        }
        plan_digest = core.schema_digest(manifest)
        metadata = {
            "schema_version": "harness-lite.allocation-metadata.v1",
            "operation_id": operation,
            "plan_digest": plan_digest,
            "iteration": number,
            "base_commit": self.head,
            "base_branch": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.head,
            "governance_tree": self.git("rev-parse", f"{self.head}^{{tree}}").stdout.strip(),
            "principle_sha256": self.principle_sha,
            "title": title,
        }
        blob = self.git(
            "hash-object",
            "-w",
            "--stdin",
            input=json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ).stdout.strip()
        self.git("update-ref", allocation_ref, blob)
        self.git("update-ref", base_ref_value, self.head)
        timestamp = "2026-08-12T01:00:00+00:00"
        journal = {
            "schema_version": core.OPERATION_JOURNAL_SCHEMA_V1,
            "operation_id": operation,
            "plan_digest": plan_digest,
            "action": "reserve-iteration",
            "phase": "READY",
            "project_root": str(self.root),
            "title": title,
            "base_commit": self.head,
            "base_branch": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.head,
            "principle_sha256": self.principle_sha,
            "created_at": timestamp,
            "updated_at": timestamp,
            "manifest": manifest,
            "expected_refs": [allocation_ref, base_ref_value],
            "iteration": number,
            "allocation_object": blob,
            "created_refs": [allocation_ref, base_ref_value],
            "attempts": [],
            "history": [
                {"phase": "PLANNED", "at": timestamp},
                {"phase": "RESERVED", "at": timestamp},
                {"phase": "READY", "at": timestamp},
            ],
            "error": None,
        }
        path = self.root / ".git" / "project-harness" / "journal" / "v1" / f"{operation}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(journal, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return base_ref_value

    def write_verified_candidate(self, iteration: str, generation: str = "g1") -> tuple[str, str]:
        iteration_root = self.root / "harness" / "iterations"
        saved = {
            path.relative_to(iteration_root).as_posix(): path.read_bytes()
            for path in iteration_root.rglob("*")
            if path.is_file()
        }
        for child in iteration_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)

        def reserve(number: str, operation_char: str) -> str:
            operation = "OP-" + operation_char * 31 + number[-1]
            _governance_ref, _governance_commit, governance_snapshot = core.committed_governance_snapshot(
                "git", self.root, "refs/heads/main"
            )
            title = f"Candidate {number}"
            allocation_ref = f"refs/project-harness/v2/allocations/{number}"
            base_ref_value = f"refs/project-harness/v2/iterations/{number}/base"
            manifest = {
                "schema_version": core.OPERATION_PLAN_SCHEMA_V1,
                "operation_id": operation,
                "action": "reserve-iteration",
                "project_root": str(self.root),
                "title": title,
                "base_commit": self.head,
                "base_ref": "refs/heads/main",
                "governance_ref": "refs/heads/main",
                "governance_commit": self.head,
                "governance_snapshot": governance_snapshot,
                "reservation_policy": {
                    "strategy": "next-monotonic-v2-cas",
                    "collision_policy": "advance-to-current-max-plus-one",
                    "max_attempts": core.RESERVATION_MAX_ATTEMPTS,
                    "observed_next_iteration": number,
                    "observed_allocation_ref": allocation_ref,
                    "observed_base_ref": base_ref_value,
                    "ref_namespace": core.V2_REF_ROOT,
                },
                "exclusions": [
                    "no worktree",
                    "no branch",
                    "no governance bundle",
                    "no progress update",
                    "no commit",
                    "no push",
                ],
            }
            plan_digest = core.schema_digest(manifest)
            metadata = {
                "schema_version": "harness-lite.allocation-metadata.v1",
                "operation_id": operation,
                "plan_digest": plan_digest,
                "iteration": number,
                "base_commit": self.head,
                "base_branch": "refs/heads/main",
                "governance_ref": "refs/heads/main",
                "governance_commit": self.head,
                "governance_tree": self.git("rev-parse", f"{self.head}^{{tree}}").stdout.strip(),
                "principle_sha256": self.principle_sha,
                "title": title,
            }
            blob = self.git(
                "hash-object",
                "-w",
                "--stdin",
                input=json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ).stdout.strip()
            self.git("update-ref", allocation_ref, blob)
            self.git("update-ref", base_ref_value, self.head)
            timestamp = "2026-08-12T01:00:00+00:00"
            journal = {
                "schema_version": core.OPERATION_JOURNAL_SCHEMA_V1,
                "operation_id": operation,
                "plan_digest": plan_digest,
                "action": "reserve-iteration",
                "phase": "READY",
                "project_root": str(self.root),
                "title": title,
                "base_commit": self.head,
                "base_branch": "refs/heads/main",
                "governance_ref": "refs/heads/main",
                "governance_commit": self.head,
                "principle_sha256": self.principle_sha,
                "created_at": timestamp,
                "updated_at": timestamp,
                "manifest": manifest,
                "expected_refs": [allocation_ref, base_ref_value],
                "iteration": number,
                "allocation_object": blob,
                "created_refs": [allocation_ref, base_ref_value],
                "attempts": [],
                "history": [
                    {"phase": "PLANNED", "at": timestamp},
                    {"phase": "RESERVED", "at": timestamp},
                    {"phase": "READY", "at": timestamp},
                ],
                "error": None,
            }
            path = self.root / ".git" / "project-harness" / "journal" / "v1" / f"{operation}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(journal, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            return base_ref_value

        bootstrap_base = reserve("999", "8")
        bootstrap_operation = "OP-" + "9" * 32
        bootstrap = workspace.build_activation_plan(
            self.root,
            iteration="999",
            execution_topology="local",
            base_ref=bootstrap_base,
            branch_ref="refs/heads/main",
            worktree_path=self.root,
            owner="task-bootstrap",
            lease_generation=1,
            operation_id=bootstrap_operation,
        )
        self.assertEqual(bootstrap.blockers, ())
        self.assertEqual(
            workspace.apply_activation(
                self.root,
                iteration="999",
                execution_topology="local",
                base_ref=bootstrap_base,
                branch_ref="refs/heads/main",
                worktree_path=self.root,
                owner="task-bootstrap",
                lease_generation=1,
                operation_id=bootstrap_operation,
                accepted_plan_digest=bootstrap.digest,
            )["blocking_reasons"],
            [],
        )
        base_ref = reserve(iteration, "a")
        workspace_operation = "OP-" + "b" * 31 + iteration[-1]
        feature_worktree = self.container / f"feature-{iteration}"
        feature_ref = f"refs/heads/feature/{iteration}"
        activation = workspace.build_activation_plan(
            self.root,
            iteration=iteration,
            execution_topology="worktree",
            base_ref=base_ref,
            branch_ref=feature_ref,
            worktree_path=feature_worktree,
            owner="task-test",
            lease_generation=1,
            operation_id=workspace_operation,
        )
        self.assertEqual(activation.blockers, ())
        self.assertEqual(
            workspace.apply_activation(
                self.root,
                iteration=iteration,
                execution_topology="worktree",
                base_ref=base_ref,
                branch_ref=feature_ref,
                worktree_path=feature_worktree,
                owner="task-test",
                lease_generation=1,
                operation_id=workspace_operation,
                accepted_plan_digest=activation.digest,
            )["blocking_reasons"],
            [],
        )
        prefix = f"{iteration}/"
        for relative, raw in saved.items():
            if relative.startswith(prefix):
                target = feature_worktree / "harness" / "iterations" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
        feature = feature_worktree / f"feature-{iteration}.txt"
        feature.write_text(f"candidate {iteration}\n", encoding="utf-8")
        self.git("add", "harness", feature.name, cwd=feature_worktree)
        self.git("commit", "-m", f"candidate {iteration}", cwd=feature_worktree)
        for relative, raw in saved.items():
            target = iteration_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        registration = train.plan_register_candidate(
            self.root,
            iteration=iteration,
            generation=generation,
            feature_ref=feature_ref,
            feature_worktree=feature_worktree,
            workspace_owner="task-test",
            workspace_generation=1,
            workspace_operation_id=workspace_operation,
            accepted_workspace_plan_digest=activation.digest,
            acceptance_evidence=(
                AcceptanceEvidence(
                    acceptance_id=f"AC-{iteration}-01",
                    evidence_ids=(f"EV-{iteration}-candidate",),
                    verification_ids=(f"verify-{iteration}",),
                ),
            ),
            verify_commands=(
                train.VerifyCommand(
                    evidence_id=f"verify-{iteration}",
                    argv=(sys.executable, "-c", "import sys; sys.exit(0)"),
                ),
            ),
            operation_id="OP-" + "c" * 31 + iteration[-1],
        )
        self.assertEqual(registration.blockers, ())
        seal = train.prepare_candidate_registration(
            registration,
            accepted_plan_digest=registration.plan_digest,
        )
        self.assertEqual(seal.blockers, ())
        authorization = f"AUTH-CANDIDATE-{iteration}-{generation}"
        token = train.ConfirmationToken(
            schema_version=train.CONFIRM_TOKEN_SCHEMA,
            action="create-candidate-seal",
            subject_digest=seal.seal_plan_digest,
            authorization_id=authorization,
            token_digest=train.confirmation_token_digest(
                "create-candidate-seal",
                seal.seal_plan_digest,
                authorization,
            ),
        )
        registered = train.apply_register_candidate(
            seal,
            accepted_seal_plan_digest=seal.seal_plan_digest,
            confirmation_token=token,
        )
        self.candidate_workspaces[iteration] = {
            "feature_worktree": feature_worktree,
            "feature_ref": feature_ref,
            "workspace_operation": workspace_operation,
            "workspace_plan_digest": activation.digest,
        }
        return registered.candidate_ref, registered.candidate_commit

    def write_next_verified_candidate(self, iteration: str, generation: str) -> tuple[str, str]:
        info = self.candidate_workspaces[iteration]
        feature_worktree = Path(str(info["feature_worktree"]))
        feature_ref = str(info["feature_ref"])
        feature = feature_worktree / f"feature-{iteration}-{generation}.txt"
        feature.write_text(f"candidate {iteration} {generation}\n", encoding="utf-8")
        self.git("add", feature.name, cwd=feature_worktree)
        self.git("commit", "-m", f"candidate {iteration} {generation}", cwd=feature_worktree)
        verification_id = f"verify-{iteration}-{generation}"
        registration = train.plan_register_candidate(
            self.root,
            iteration=iteration,
            generation=generation,
            feature_ref=feature_ref,
            feature_worktree=feature_worktree,
            workspace_owner="task-test",
            workspace_generation=1,
            workspace_operation_id=str(info["workspace_operation"]),
            accepted_workspace_plan_digest=str(info["workspace_plan_digest"]),
            acceptance_evidence=(
                AcceptanceEvidence(
                    acceptance_id=f"AC-{iteration}-01",
                    evidence_ids=(f"EV-{iteration}-{generation}",),
                    verification_ids=(verification_id,),
                ),
            ),
            verify_commands=(
                train.VerifyCommand(
                    evidence_id=verification_id,
                    argv=(sys.executable, "-c", "import sys; sys.exit(0)"),
                ),
            ),
            operation_id="OP-" + "d" * 31 + iteration[-1],
        )
        self.assertEqual(registration.blockers, ())
        seal = train.prepare_candidate_registration(registration, accepted_plan_digest=registration.plan_digest)
        self.assertEqual(seal.blockers, ())
        authorization = f"AUTH-CANDIDATE-{iteration}-{generation}"
        token = train.ConfirmationToken(
            schema_version=train.CONFIRM_TOKEN_SCHEMA,
            action="create-candidate-seal",
            subject_digest=seal.seal_plan_digest,
            authorization_id=authorization,
            token_digest=train.confirmation_token_digest(
                "create-candidate-seal", seal.seal_plan_digest, authorization
            ),
        )
        registered = train.apply_register_candidate(
            seal,
            accepted_seal_plan_digest=seal.seal_plan_digest,
            confirmation_token=token,
        )
        return registered.candidate_ref, registered.candidate_commit

    def test_route_derives_strict_approvals_and_local_topology_without_writing(self) -> None:
        before = self.snapshot()

        plan = plan_route(
            self.root,
            iteration="001",
            read_only=False,
            risk={"localized_impact": True, "straightforward_rollback": True},
            operation_id="OP-" + "1" * 32,
        )

        self.assertEqual(plan.phase, "planned")
        self.assertTrue(plan.authority.prd_approved)
        self.assertTrue(plan.authority.spec_approved)
        self.assertTrue(plan.authority.implementation_authorized)
        self.assertEqual(plan.authority.principle_sha256, self.principle_sha)
        self.assertEqual(plan.decision["effective_execution_topology"], "local")
        self.assertEqual(before, self.snapshot())

    def test_unquoted_colon_rich_approval_evidence_is_not_truncated(self) -> None:
        path = self.root / "harness" / "iterations" / "001" / "spec-001.md"
        spec = path.read_text(encoding="utf-8")
        spec = spec.replace(
            "- 批准依据：用户明确批准 SPEC-001",
            "- 批准依据：用户于 2026-08-12 明确批准 SPEC-001：包含 merge --no-ff 策略",
        )
        path.write_text(spec, encoding="utf-8")

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "6" * 32)

        self.assertTrue(plan.authority.spec_approved)
        self.assertNotIn("spec-not-approved", plan.blocking_reasons)

    def test_negative_or_inferred_evidence_cannot_authorize(self) -> None:
        for index, evidence in enumerate(("拒绝批准 PRD-001", "代理推断用户批准 PRD-001", "未明确批准 PRD-001")):
            with self.subTest(evidence=evidence):
                self.write_iteration("001", approved=True, prd_approval=evidence)
                plan = plan_route(
                    self.root,
                    iteration="001",
                    read_only=False,
                    operation_id="OP-" + f"{index + 2:x}" * 32,
                )
                self.assertFalse(plan.authority.prd_approved)
                self.assertIn("prd-not-approved", plan.blocking_reasons)

    def test_duplicate_approval_field_fails_closed(self) -> None:
        path = self.root / "harness" / "iterations" / "001" / "prd-001.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "- 批准依据：用户明确批准 PRD-001\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(CoordinatorError, "duplicated"):
            plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "a" * 32)

    def test_live_principle_edit_does_not_change_canonical_main_authority(self) -> None:
        principle = self.root / "harness" / "principle.md"
        principle.write_bytes(principle.read_bytes() + b"\nuncommitted proposal\n")

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "b" * 32)

        self.assertEqual(plan.authority.principle_sha256, self.principle_sha)
        self.assertEqual(plan.authority.governance_commit, self.head)

    def test_missing_dependency_candidate_blocks_stacked_start(self) -> None:
        self.write_iteration("002", approved=True)
        self.git(
            "update-ref",
            "refs/project-harness/iterations/002/base/refs/heads/main",
            self.head,
        )
        self.write_iteration("001", approved=True, depends_on="PRD-002")

        blocked = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "3" * 32)

        self.assertIn("dependency-stable-candidate-missing:002", blocked.blocking_reasons)
        self.assertEqual(blocked.phase, "blocked")

    def test_bare_candidate_ref_without_persisted_evidence_does_not_unlock_dependency(self) -> None:
        self.write_iteration("002", approved=True)
        self.git(
            "update-ref",
            "refs/project-harness/iterations/002/base/refs/heads/main",
            self.head,
        )
        self.write_iteration("001", approved=True, depends_on="PRD-002")
        candidate_ref = "refs/project-harness/v2/iterations/002/candidates/g2"
        self.git("update-ref", candidate_ref, self.head)

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "4" * 32)

        self.assertEqual(plan.phase, "blocked")
        self.assertTrue(
            any(reason.startswith("dependency-authority:002:candidate-registration:g2:") for reason in plan.blocking_reasons)
        )
        self.assertIn("dependency-stable-candidate-missing:002", plan.blocking_reasons)
        dependency = plan.authority.depends_on
        self.assertEqual(dependency, ("002",))

    def test_exact_persisted_candidate_evidence_unlocks_dependency_baseline(self) -> None:
        self.write_iteration("002", approved=True)
        self.git(
            "update-ref",
            "refs/project-harness/iterations/002/base/refs/heads/main",
            self.head,
        )
        candidate_ref, candidate_commit = self.write_verified_candidate("002")
        self.write_iteration("001", approved=True, depends_on="PRD-002")

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "c" * 32)

        self.assertEqual(plan.authority.depends_on, ("002",))
        self.assertNotIn("dependency-stable-candidate-missing:002", plan.blocking_reasons)
        self.assertNotIn("dependency-candidate-stale:002", plan.blocking_reasons)
        workspace_step = next(item for item in plan.planned_steps if item["step"] == "workspace-plan")
        self.assertEqual(workspace_step["implementation_ref"], candidate_ref)
        self.assertEqual(workspace_step["implementation_commit"], candidate_commit)
        self.assertEqual(plan.phase, "planned")

    def test_dependency_candidate_change_requires_explicit_refresh_and_revalidation(self) -> None:
        self.write_iteration("002", approved=True)
        candidate_ref_g1, candidate_commit_g1 = self.write_verified_candidate("002")
        self.write_iteration("001", approved=True, depends_on="PRD-002")
        base_ref = self.write_v2_allocation("001", "e")
        route = plan_route(
            self.root,
            iteration="001",
            read_only=False,
            operation_id="OP-" + "e" * 32,
        )
        workspace_step = next(item for item in route.planned_steps if item["step"] == "workspace-plan")
        bindings_g1 = tuple(workspace_step["dependency_bindings"])
        self.assertEqual(bindings_g1[0]["candidate_ref"], candidate_ref_g1)
        self.assertEqual(bindings_g1[0]["candidate_commit"], candidate_commit_g1)

        feature_worktree = self.container / "feature-001-stacked"
        feature_ref = "refs/heads/feature/001-stacked"
        workspace_operation = "OP-" + "f" * 32
        activation = workspace.build_activation_plan(
            self.root,
            iteration="001",
            execution_topology="worktree",
            base_ref=base_ref,
            branch_ref=feature_ref,
            worktree_path=feature_worktree,
            owner="task-test",
            lease_generation=1,
            dependency_bindings=bindings_g1,
            operation_id=workspace_operation,
        )
        self.assertEqual(activation.blockers, ())
        applied = workspace.apply_activation(
            self.root,
            iteration="001",
            execution_topology="worktree",
            base_ref=base_ref,
            branch_ref=feature_ref,
            worktree_path=feature_worktree,
            owner="task-test",
            lease_generation=1,
            operation_id=workspace_operation,
            accepted_plan_digest=activation.digest,
            dependency_bindings=bindings_g1,
        )
        self.assertEqual(applied["blocking_reasons"], [])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=feature_worktree).stdout.strip(), candidate_commit_g1)

        shutil.copytree(
            self.root / "harness" / "iterations" / "001",
            feature_worktree / "harness" / "iterations" / "001",
            dirs_exist_ok=True,
        )
        (feature_worktree / "feature-001.txt").write_text("stacked candidate\n", encoding="utf-8")
        self.git("add", "harness/iterations/001", "feature-001.txt", cwd=feature_worktree)
        self.git("commit", "-m", "stack PRD-001 on PRD-002 g1", cwd=feature_worktree)

        context = workspace.resolve_repository(self.root)
        lease_g1 = workspace.load_lease(context, "001")
        self.assertIsNotNone(lease_g1)
        current_guard, _ = workspace.guard_lease(context, lease_g1)
        self.assertEqual(current_guard, [])
        evidence_ref = str(bindings_g1[0]["candidate_evidence_ref"])
        evidence_oid = str(bindings_g1[0]["candidate_evidence_blob"])
        forged_evidence = self.git("hash-object", "-w", "--stdin", input="{}").stdout.strip()
        self.git("update-ref", evidence_ref, forged_evidence, evidence_oid)
        evidence_guard, _ = workspace.guard_lease(context, lease_g1)
        self.assertTrue(any(item.code == "dependency-candidate-evidence-ref-drift" for item in evidence_guard))
        self.assertTrue(any(item.code == "dependency-baseline-stale" for item in evidence_guard))
        self.git("update-ref", evidence_ref, evidence_oid, forged_evidence)
        restored_guard, _ = workspace.guard_lease(context, lease_g1)
        self.assertEqual(restored_guard, [])

        _candidate_ref_g2, candidate_commit_g2 = self.write_next_verified_candidate("002", "g2")
        stale_guard, _ = workspace.guard_lease(context, lease_g1)
        self.assertTrue(any(item.code == "dependency-baseline-stale" for item in stale_guard))
        status = workspace.status_payload(context)
        by_iteration = {item["iteration"]: item for item in status["writer_leases"]}
        self.assertEqual(by_iteration["001"]["dependency_baseline_state"], "stale")
        self.assertTrue(by_iteration["001"]["dependency_refresh_required"])
        self.assertTrue(by_iteration["002"]["guard_valid"])
        self.assertEqual(by_iteration["002"]["dependency_baseline_state"], "current")

        stale_route = plan_route(
            self.root,
            iteration="001",
            read_only=False,
            operation_id="OP-" + "6" * 32,
        )
        self.assertIn(
            "dependency-baseline-stale:explicit-refresh-required",
            stale_route.blocking_reasons,
        )
        selected_g2 = next(
            item
            for item in derive_iteration_authority(self.root, "002").stable_candidate_bindings
            if item["generation"] == "g2"
        )
        candidate_plan = train.plan_register_candidate(
            self.root,
            iteration="001",
            generation="g2",
            feature_ref=feature_ref,
            feature_worktree=feature_worktree,
            workspace_owner="task-test",
            workspace_generation=1,
            workspace_operation_id=workspace_operation,
            accepted_workspace_plan_digest=activation.digest,
            acceptance_evidence=(
                AcceptanceEvidence(
                    acceptance_id="AC-001-01",
                    evidence_ids=("EV-001-stale",),
                    verification_ids=("verify-001-stale",),
                ),
            ),
            verify_commands=(
                train.VerifyCommand(
                    evidence_id="verify-001-stale",
                    argv=(sys.executable, "-c", "import sys; sys.exit(0)"),
                ),
            ),
            operation_id="OP-" + "5" * 32,
        )
        self.assertTrue(
            any(item.code == "workspace-dependency-baseline-stale" for item in candidate_plan.blockers)
        )
        self.git(
            "merge",
            "--no-ff",
            "-s",
            "ours",
            str(selected_g2["candidate_ref"]),
            "-m",
            "explicitly adopt PRD-002 g2 baseline",
            cwd=feature_worktree,
        )
        (feature_worktree / "feature-002-g2.txt").write_text("candidate 002 g2\n", encoding="utf-8")
        self.git("add", "feature-002-g2.txt", cwd=feature_worktree)
        self.git("commit", "-m", "adapt PRD-001 implementation to PRD-002 g2", cwd=feature_worktree)
        self.assertEqual(
            self.git("merge-base", "--is-ancestor", candidate_commit_g2, "HEAD", cwd=feature_worktree).returncode,
            0,
        )
        dependency_workspace = Path(str(self.candidate_workspaces["002"]["feature_worktree"]))
        dependency_lease_path = workspace.lease_path(context, "002")
        unaffected_before = (
            dependency_lease_path.read_bytes(),
            self.git("rev-parse", "HEAD", cwd=dependency_workspace).stdout,
            self.git("status", "--porcelain=v2", "-z", cwd=dependency_workspace).stdout,
        )
        refresh = workspace.build_dependency_refresh_plan(
            self.root,
            iteration="001",
            owner="task-test",
            lease_generation=1,
            worktree_path=feature_worktree,
            branch_ref=feature_ref,
            base_commit=self.head,
            dependency_bindings=(selected_g2,),
            verification_commands=(
                {
                    "evidence_id": "verify-dependency-refresh",
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('feature-002-g2.txt').read_text(encoding='utf-8') == 'candidate 002 g2\\n'",
                    ],
                },
            ),
            operation_id="OP-" + "4" * 32,
        )
        self.assertEqual(refresh.blockers, ())

        lease_path = workspace.lease_path(context, "001")
        exact_lease = lease_path.read_bytes()
        drifted = json.loads(exact_lease.decode("utf-8"))
        drifted["heartbeat"] = "2026-08-12T02:00:00+00:00"
        lease_path.write_text(json.dumps(drifted), encoding="utf-8")
        cas_blocked = workspace.apply_dependency_refresh(
            refresh,
            accepted_plan_digest=refresh.digest,
        )
        self.assertTrue(
            any(item["code"] == "dependency-refresh-lease-cas" for item in cas_blocked["blocking_reasons"])
        )
        lease_path.write_bytes(exact_lease)
        refreshed = workspace.apply_dependency_refresh(
            refresh,
            accepted_plan_digest=refresh.digest,
        )
        self.assertEqual(refreshed["phase"], "succeeded")
        self.assertEqual(refreshed["blocking_reasons"], [])
        self.assertTrue(refreshed["verification_receipts"])
        lease_g2 = workspace.load_lease(context, "001")
        self.assertEqual(lease_g2["generation"], 2)
        self.assertEqual(lease_g2["dependency_refresh_generation"], 1)
        self.assertEqual(lease_g2["dependency_bindings"], [selected_g2])
        self.assertEqual(lease_g2["implementation_ref"], selected_g2["candidate_ref"])
        self.assertEqual(lease_g2["implementation_commit"], candidate_commit_g2)
        self.assertEqual(lease_g2["reconciliation_ref"], feature_ref)
        self.assertEqual(
            lease_g2["reconciliation_commit"],
            self.git("rev-parse", "HEAD", cwd=feature_worktree).stdout.strip(),
        )
        self.assertNotEqual(lease_g2["reconciliation_commit"], candidate_commit_g2)
        old_generation_guard, _ = workspace.guard_lease(context, lease_g2, generation=1)
        self.assertTrue(any(item.code == "lease-generation-mismatch" for item in old_generation_guard))
        current_generation_guard, _ = workspace.guard_lease(context, lease_g2, generation=2)
        self.assertEqual(current_generation_guard, [])
        replay = workspace.apply_dependency_refresh(
            refresh,
            accepted_plan_digest=refresh.digest,
        )
        self.assertEqual(replay["phase"], "succeeded")
        self.assertTrue(replay["idempotent_replay"])
        unaffected_after = (
            dependency_lease_path.read_bytes(),
            self.git("rev-parse", "HEAD", cwd=dependency_workspace).stdout,
            self.git("status", "--porcelain=v2", "-z", cwd=dependency_workspace).stdout,
        )
        self.assertEqual(unaffected_after, unaffected_before)

    def test_candidate_evidence_tamper_does_not_unlock_dependency(self) -> None:
        self.write_iteration("002", approved=True)
        self.git(
            "update-ref",
            "refs/project-harness/iterations/002/base/refs/heads/main",
            self.head,
        )
        self.write_verified_candidate("002")
        journal = next((self.root / ".git" / "project-harness" / "train" / "v1" / "journal").glob("candidate-*.json"))
        value = json.loads(journal.read_text(encoding="utf-8"))
        value["candidate_evidence"]["verification_ids"] = ["forged"]
        journal.write_text(json.dumps(value), encoding="utf-8")
        self.write_iteration("001", approved=True, depends_on="PRD-002")

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "d" * 32)

        self.assertIn("dependency-stable-candidate-missing:002", plan.blocking_reasons)
        self.assertTrue(
            any(
                reason.startswith("dependency-authority:002:candidate-registration:g1:")
                for reason in plan.blocking_reasons
            )
        )

    def test_declared_active_conflict_uses_strict_workspace_lease(self) -> None:
        self.write_iteration("001", approved=True, conflicts_with="PRD-002")
        self.write_valid_lease("002")

        plan = plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "5" * 32)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("declared-conflict-active:002", plan.blocking_reasons)

    def test_minimal_or_extended_fake_lease_is_rejected_as_corrupt(self) -> None:
        directory = self.root / ".git" / "project-harness" / "workspace" / "v1" / "leases" / "iterations"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "002.json").write_text(
            '{"iteration":"002","dependency_generations":{"001":"g1"}}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(CoordinatorError, "lease registry is invalid"):
            plan_route(self.root, iteration="001", read_only=False, operation_id="OP-" + "7" * 32)

    def test_invalid_operation_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "operation ID"):
            plan_route(self.root, iteration="001", read_only=False, operation_id="../../escape")


if __name__ == "__main__":
    unittest.main()
