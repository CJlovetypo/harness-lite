"""Product-level Harness Lite lifecycle proof.

This suite intentionally exercises public orchestration boundaries in a real
temporary Git repository.  It never mutates the Harness Lite source checkout,
never configures a remote, and never pushes.  Final acceptance is a required
public product boundary; the proof never substitutes a test-only authority.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

from scripts import harness_bundle as bundle
from scripts import harness_coordinator as coordinator
from scripts import harness_final_acceptance as final_acceptance
from scripts import harness_governance as governance
from scripts import harness_integrated_evidence as integrated_registry
from scripts import harness_lifecycle as lifecycle
from scripts import harness_progress as progress
from scripts import harness_train as train
from scripts import harness_train_governance as train_governance
from scripts import harness_workspace as workspace
from scripts import project_harness as core
from scripts.harness_candidate import AcceptanceEvidence


class HarnessProductE2E(unittest.TestCase):
    """One exact 0 -> 3 PRD -> train -> accepted-main identity chain."""

    maxDiff = None

    def setUp(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is required")
        self.git_executable = git
        self.temporary = tempfile.TemporaryDirectory(prefix="hl-product-e2e-")
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "product"
        self.root.mkdir()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(cwd or self.root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment,
        )

    @staticmethod
    def operation(label: str) -> str:
        return "OP-" + hashlib.sha256(label.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def confirmation(
        action: str,
        subject_digest: str,
        authorization_id: str,
    ) -> train.ConfirmationToken:
        """Model an exact token explicitly supplied by the test user."""

        return train.ConfirmationToken(
            schema_version=train.CONFIRM_TOKEN_SCHEMA,
            action=action,
            subject_digest=subject_digest,
            authorization_id=authorization_id,
            token_digest=train.confirmation_token_digest(
                action,
                subject_digest,
                authorization_id,
            ),
        )

    @staticmethod
    def request(*, iteration: str | None = None) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": lifecycle.REQUEST_SCHEMA,
            "summary": "Implement one approved product change",
            "risk": {
                "localized_impact": True,
                "straightforward_rollback": True,
            },
        }
        if iteration is not None:
            value["iteration"] = iteration
        return value

    @staticmethod
    def assert_no_push_contract(value: object) -> None:
        if isinstance(value, dict):
            assert value.get("pushed", False) is False
        else:
            assert getattr(value, "pushed", False) is False

    def progress_id_counts(self, checkout: Path) -> dict[str, int]:
        parsed = progress.parse_progress_events(
            (checkout / "harness" / "progress.md").read_bytes(),
            source=str(checkout / "harness" / "progress.md"),
        )
        self.assertFalse(parsed.blockers, parsed.blockers)
        counts: dict[str, int] = {}
        for event in parsed.events:
            counts[event.identity] = counts.get(event.identity, 0) + 1
        return counts

    @staticmethod
    def stage(label: str) -> None:
        # Product fixture commits below are test-only construction of exact
        # Git facts; this marker keeps long Windows runs observable.
        print(f"PRODUCT_E2E_STAGE {label}", file=sys.stderr, flush=True)

    def oid(self, revision: str, *, cwd: Path | None = None) -> str:
        return self.git("rev-parse", "--verify", revision, cwd=cwd).stdout.strip()

    @property
    def common_dir(self) -> Path:
        raw = Path(self.git("rev-parse", "--git-common-dir").stdout.strip())
        return (raw if raw.is_absolute() else self.root / raw).resolve()

    def write(self, relative: str, text: str, *, root: Path | None = None) -> None:
        path = (root or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def approve_bundle(self, number: str, *, dependency: str | None = None) -> None:
        """Turn a production-generated bundle into an explicit approved baseline."""

        directory = self.root / "harness" / "iterations" / number
        prd_path = directory / f"prd-{number}.md"
        spec_path = directory / f"spec-{number}.md"
        readme_path = directory / "README.md"

        prd = prd_path.read_text(encoding="utf-8-sig")
        prd = prd.replace("- 状态：`草案`", "- 状态：`实施中`")
        prd = prd.replace(
            "- 批准依据：尚无；当前仅建立草案。",
            f"- 批准依据：用户明确批准 PRD-{number} 产品基线。",
        )
        for placeholder in core.PRD_TEMPLATE_PLACEHOLDERS:
            prd = prd.replace(
                placeholder,
                "本轮产品范围已明确，结果必须可观察并且可依据 AC 完成验证。",
            )
        prd = prd.replace(
            f"### R-{number}-01：待定义",
            f"### R-{number}-01：交付经批准的隔离实现",
        )
        if dependency is not None:
            prd = prd.replace("- depends_on：无。", f"- depends_on：PRD-{dependency}。")

        spec = spec_path.read_text(encoding="utf-8-sig")
        spec = spec.replace("- 状态：`受 PRD 阻塞`", "- 状态：`实施中`")
        spec = spec.replace(
            f"- 当前批准基线：尚无；等待 PRD-{number} 批准。",
            f"- 当前批准基线：用户已批准 PRD-{number} 产品基线。",
        )
        spec = spec.replace(
            "- 批准依据：尚无；当前仅建立规格草案。",
            f"- 批准依据：用户明确批准 SPEC-{number} 实施基线。",
        )
        spec = spec.replace("- 实施授权：尚无。", "- 实施授权：用户明确授权开始实施。")
        for placeholder in core.SPEC_TEMPLATE_PLACEHOLDERS:
            spec = spec.replace(
                placeholder,
                "实现遵循已批准 PRD，保持可回滚，并以自动化验证证明对应验收标准。",
            )
        spec = spec.replace(
            "| 待定义 | 待定义 |",
            "| lifecycle facade | automated test evidence |",
        )

        readme = readme_path.read_text(encoding="utf-8-sig")
        readme = readme.replace("- PRD 状态：`草案`", "- PRD 状态：`实施中`")
        readme = readme.replace("- SPEC 状态：`受 PRD 阻塞`", "- SPEC 状态：`实施中`")
        if dependency is not None:
            readme = readme.replace(
                "- depends_on / conflicts_with：无 / 无。",
                f"- depends_on / conflicts_with：{dependency} / 无。",
            )

        prd_path.write_bytes(prd.encode("utf-8"))
        spec_path.write_bytes(spec.encode("utf-8"))
        readme_path.write_bytes(readme.encode("utf-8"))

        for relative in ("harness/README.md", "harness/progress.md"):
            path = self.root / relative
            text = path.read_text(encoding="utf-8-sig")
            lines: list[str] = []
            for line in text.splitlines(keepends=True):
                if f"[{number}]" in line and line.lstrip().startswith("|"):
                    line = line.replace(
                        "| 草案 | 受 PRD 阻塞 |",
                        "| 实施中 | 实施中 |",
                    )
                lines.append(line)
            path.write_bytes("".join(lines).encode("utf-8"))

        self.git(
            "add",
            "--",
            f"harness/iterations/{number}",
            "harness/README.md",
            "harness/progress.md",
        )
        self.git(
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            f"approve PRD-{number}",
        )

    def bootstrap_authority(self) -> None:
        """Create three authorities through the real reserve/bundle flow."""

        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Product E2E")
        self.git("config", "user.email", "product-e2e@example.invalid")
        self.git("config", "core.autocrlf", "false")
        core.apply_operations(
            self.root,
            core.build_init_operations(
                self.root,
                "Harness Product E2E",
                datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc),
            ),
        )
        principle = self.root / "harness" / "principle.md"
        principle.write_text(
            principle.read_text(encoding="utf-8-sig")
            + (
                "\n## P-E2E：全局产品控制\n\n"
                "- 状态：`已批准`\n"
                "- 批准依据：用户明确批准 P-E2E。\n\n"
                "所有 PRD 必须保持 intent → spec → implementation → evidence 的精确身份链。\n"
            ),
            encoding="utf-8",
        )
        self.write("app.txt", "baseline\n")
        self.git("add", "--", "AGENTS.md", "harness", "app.txt")
        self.git(
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "approved global principle baseline",
        )

        for expected, title, dependency in (
            ("001", "Product A", None),
            ("002", "Product B", None),
            ("003", "Product C", "002"),
        ):
            reserve = core.build_reserve_iteration_plan(
                self.root,
                self.git_executable,
                title=title,
                operation_id=self.operation(f"reserve-{expected}"),
                base_ref="refs/heads/main",
                governance_ref="refs/heads/main",
            )
            journal, _created = core.reserve_iteration(
                reserve,
                self.git_executable,
                self.root,
            )
            number = str(journal.iteration)
            self.assertEqual(number, expected)
            planned_at = datetime.fromisoformat(
                self.git(
                    "show",
                    "-s",
                    "--format=%cI",
                    str(journal.base_commit),
                ).stdout.strip()
            )
            bundle_operation = self.operation(f"bundle-{number}")
            bundle_plan = bundle.plan_bundle(
                self.root,
                iteration=number,
                operation_id=bundle_operation,
                planned_at=planned_at,
            )
            bundled = bundle.apply_bundle(
                self.root,
                iteration=number,
                operation_id=bundle_operation,
                accepted_plan_digest=bundle_plan.plan_digest,
                planned_at=planned_at,
            )
            self.assertEqual(bundled["phase"], "succeeded", bundled)
            self.approve_bundle(number, dependency=dependency)

    def activate(
        self,
        number: str,
        *,
        owner: str,
        notifications: list[dict[str, object]] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        operation = self.operation(f"activate-{number}")
        request = self.request(iteration=number)
        request["owner"] = owner
        plan = lifecycle.plan_start(
            self.root,
            request,
            title=f"Product {number}",
            operation_id=operation,
        )
        self.assertEqual(plan["phase"], "planned", plan)
        result = lifecycle.start_lifecycle(
            self.root,
            request,
            title=f"Product {number}",
            operation_id=operation,
            accepted_plan_digest=str(plan["plan_digest"]),
            notify=notifications.append if notifications is not None else None,
        )
        self.assertEqual(result["child_result"]["phase"], "succeeded", result)
        self.assert_no_push_contract(result)
        return plan, result

    def candidate_binding(self, candidate: train.RegisteredCandidate) -> dict[str, str]:
        authority = coordinator.derive_iteration_authority(self.root, candidate.iteration)
        self.assertTrue(authority.stable_candidate_bindings, authority)
        binding = dict(authority.stable_candidate_bindings[-1])
        self.assertEqual(binding["registration_digest"], candidate.registration_digest)
        return binding

    def acceptance(self, number: str, evidence_id: str) -> tuple[AcceptanceEvidence, ...]:
        return (
            AcceptanceEvidence(
                acceptance_id=f"AC-{number}-01",
                evidence_ids=(f"product-evidence:{number}",),
                verification_ids=(evidence_id,),
            ),
        )

    def candidate_plan(
        self,
        number: str,
        generation: str,
        checkout: Path,
        *,
        lease_generation: int | None = None,
        feature_worktree: Path | None = None,
    ) -> train.CandidateRegistrationPlan:
        context = workspace.resolve_repository(self.root)
        lease = workspace.load_lease(context, number)
        self.assertIsNotNone(lease)
        assert lease is not None
        workspace_journal = workspace.load_journal(
            context,
            str(lease["operation_id"]),
        )
        self.assertIsNotNone(workspace_journal)
        assert workspace_journal is not None
        self.assertEqual(workspace_journal["phase"], "READY", workspace_journal)
        evidence_id = f"verify:{number}:{generation}"
        return train.plan_register_candidate(
            self.root,
            iteration=number,
            generation=generation,
            feature_ref=str(lease["branch_ref"]),
            feature_worktree=feature_worktree or checkout,
            workspace_owner=str(lease["owner"]),
            workspace_generation=(
                int(lease["generation"])
                if lease_generation is None
                else lease_generation
            ),
            workspace_operation_id=str(lease["operation_id"]),
            accepted_workspace_plan_digest=str(workspace_journal["plan_digest"]),
            acceptance_evidence=self.acceptance(number, evidence_id),
            verify_commands=(
                train.VerifyCommand(
                    evidence_id=evidence_id,
                    argv=(sys.executable, "-c", "import sys; sys.exit(0)"),
                ),
            ),
            operation_id=self.operation(f"candidate-{number}-{generation}"),
        )

    def register_candidate(
        self,
        number: str,
        generation: str,
        checkout: Path,
    ) -> train.RegisteredCandidate:
        registration = self.candidate_plan(number, generation, checkout)
        self.assertEqual(registration.blockers, (), registration.blockers)
        facade_operation = self.operation(f"candidate-facade-{number}-{generation}")
        pre_plan = lifecycle.plan_candidate_preverification_stage(
            registration,
            lifecycle_operation_id=facade_operation,
        )
        pre_result = lifecycle.apply_candidate_preverification_stage(
            pre_plan,
            registration,
            accepted_plan_digest=pre_plan.plan_digest,
        )
        seal = pre_result.child_result
        self.assertIsInstance(seal, train.CandidateSealPlan)
        self.assertEqual(seal.blockers, ())
        seal_plan = lifecycle.plan_candidate_registration_stage(
            seal,
            lifecycle_operation_id=facade_operation,
        )
        token = self.confirmation(
            "create-candidate-seal",
            seal.seal_plan_digest,
            f"AUTH-CANDIDATE-{number}-{generation}",
        )
        sealed = lifecycle.apply_candidate_registration_stage(
            seal_plan,
            seal,
            accepted_plan_digest=seal_plan.plan_digest,
            confirmation_token=token,
        )
        candidate = sealed.child_result
        self.assertIsInstance(candidate, train.RegisteredCandidate)
        self.assertFalse(candidate.pushed)
        return candidate

    def readme_authority(self) -> train_governance.ReadmeRebuildAuthority:
        identity = "routing:e2e:001-002-003"
        root = governance.RootRoutingAuthority(
            authority_id=identity,
            current_iteration="001",
            global_gate="latest-main integrated verification",
            next_step="confirm exact final acceptance",
            iterations=tuple(
                governance.IterationRoutingState(
                    number,
                    f"Product {number}",
                    "待验收",
                    "已完成",
                    0,
                    "integration",
                    "evidence ready",
                    "candidate ready",
                    "queued",
                    f"PRD-{number} rebuilt",
                    "accept integrated result",
                    ("002",) if number == "003" else (),
                )
                for number in ("001", "002", "003")
            ),
        )
        l1 = tuple(
            train_governance.DerivedReadme(
                path=f"harness/iterations/{number}/README.md",
                content=(
                    "<!-- managed-by: harness-lite v1 -->\n"
                    f"# Iteration {number} — Product {number}\n\n"
                    "- PRD 状态：`待验收`\n"
                    "- SPEC 状态：`已完成`\n"
                    "- 派生路由：`E2E-REBUILT`\n"
                    "- 下一步：确认 exact integrated candidate。\n"
                ).encode("utf-8"),
                authority_ref=f"routing:{number}:e2e",
            )
            for number in ("001", "002", "003")
        )
        return train_governance.build_readme_rebuild_authority(
            authority_id=identity,
            root=root,
            l1_documents=l1,
        )

    def test_three_prd_product_lifecycle_isolated_integrated_and_accepted(self) -> None:
        """Final scenario; kept explicit so every authority transition is reviewable."""

        self.stage("bootstrap")
        self.bootstrap_authority()
        baseline_main = self.oid("refs/heads/main")

        # 0 -> 1: A is Local.  No branch/worktree is created.
        self.stage("0-to-1-local")
        a_plan, a_activation = self.activate("001", owner="task-a")
        self.assertEqual(a_plan["expected_topology"], "local")
        self.assertEqual(len(self.git("worktree", "list", "--porcelain").stdout.split("worktree ")) - 1, 1)
        a_lease = workspace.load_lease(workspace.resolve_repository(self.root), "001")
        self.assertIsNotNone(a_lease)
        assert a_lease is not None
        self.assertEqual(a_lease["branch_ref"], "refs/heads/main")

        # A owns dirty work and an index entry before the second PRD appears.
        self.write("feature-001.txt", "A implementation\n")
        self.write("a-index.txt", "A staged identity\n")
        self.git("add", "--", "a-index.txt")
        a_status_before = self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout
        a_index_before = self.git("ls-files", "--stage", "-z").stdout
        a_feature_before = (self.root / "feature-001.txt").read_bytes()

        # 1 -> 2: B is a lazily-created linked worktree and A is untouched.
        self.stage("1-to-2-worktree")
        b_notifications: list[dict[str, object]] = []
        b_plan, _b_activation = self.activate(
            "002",
            owner="task-b",
            notifications=b_notifications,
        )
        b_path = Path(b_plan["accepted_child"]["parameters"]["worktree_path"])
        self.assertEqual([item["phase"] for item in b_notifications], ["before", "after"])
        self.assertEqual(len({item["notification_id"] for item in b_notifications}), 2)
        self.assertEqual(self.git("status", "--porcelain=v2", "-z", "--untracked-files=all").stdout, a_status_before)
        self.assertEqual(self.git("ls-files", "--stage", "-z").stdout, a_index_before)
        self.assertEqual((self.root / "feature-001.txt").read_bytes(), a_feature_before)
        self.assertEqual(self.oid("HEAD"), baseline_main)
        self.assertFalse((b_path / "feature-001.txt").exists())
        self.assertEqual((b_path / "app.txt").read_text(encoding="utf-8"), "baseline\n")

        # B g1 is a real sealed candidate with two zero-exit verification receipts.
        self.stage("candidate-B-g1")
        self.write("feature-002.txt", "B generation one\n", root=b_path)
        self.git("add", "--", "feature-002.txt", "harness/progress.md", cwd=b_path)
        self.git("commit", "--no-gpg-sign", "--no-verify", "-m", "implement B g1", cwd=b_path)
        candidate_b1 = self.register_candidate("002", "g1", b_path)
        binding_b1 = self.candidate_binding(candidate_b1)

        # 2 -> 3: C declares B and starts from that exact stable candidate.
        self.stage("2-to-3-stacked-C")
        c_notifications: list[dict[str, object]] = []
        c_plan, _c_activation = self.activate(
            "003",
            owner="task-c",
            notifications=c_notifications,
        )
        c_parameters = c_plan["accepted_child"]["parameters"]
        c_path = Path(c_parameters["worktree_path"])
        self.assertEqual(
            c_plan["route"]["axes"]["execution_topology"],
            "stacked-worktree",
        )
        self.assertEqual(c_plan["expected_topology"], "worktree")
        self.assertEqual(c_parameters["implementation_ref"], candidate_b1.candidate_ref)
        self.assertEqual(c_parameters["implementation_commit"], candidate_b1.candidate_commit)
        self.assertEqual(c_parameters["dependency_bindings"], [binding_b1])
        self.assertEqual(
            c_notifications[0]["facts"]["reason_code"],
            "stable-dependency-stacked-worktree",
        )
        self.assertEqual(self.oid("HEAD", cwd=c_path), candidate_b1.candidate_commit)
        self.assertEqual((c_path / "feature-002.txt").read_text(encoding="utf-8"), "B generation one\n")
        self.write("feature-003.txt", "C consumes exact B\n", root=c_path)
        self.git("add", "--", "feature-003.txt", "harness/progress.md", cwd=c_path)
        self.git("commit", "--no-gpg-sign", "--no-verify", "-m", "implement C on B g1", cwd=c_path)

        context = workspace.resolve_repository(self.root)
        leases, lease_blockers = workspace.load_active_leases(context)
        self.assertEqual(lease_blockers, [])
        self.assertEqual({item["iteration"] for item in leases}, {"001", "002", "003"})
        self.assertEqual(len({item["worktree_path"] for item in leases}), 3)
        self.assertEqual(len({item["runtime_namespace"] for item in leases}), 3)
        self.assertEqual(len({(item["iteration"], item["generation"]) for item in leases}), 3)

        # B advances to g2.  C's exact g1 lease must become stale before any ref write.
        self.stage("B-g2-stales-C")
        self.git("merge", "--ff-only", candidate_b1.candidate_ref, cwd=b_path)
        self.write("feature-002-v2.txt", "B generation two\n", root=b_path)
        self.git("add", "--", "feature-002-v2.txt", cwd=b_path)
        self.git("commit", "--no-gpg-sign", "--no-verify", "-m", "implement B g2", cwd=b_path)
        candidate_b2 = self.register_candidate("002", "g2", b_path)
        binding_b2 = self.candidate_binding(candidate_b2)
        refs_before_stale = self.git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/project-harness/v2/iterations/003",
        ).stdout
        stale_c = self.candidate_plan("003", "g1", c_path)
        self.assertTrue(stale_c.blockers)
        self.assertTrue(
            any("dependency" in item.code for item in stale_c.blockers),
            stale_c.blockers,
        )
        self.assertEqual(
            self.git(
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/project-harness/v2/iterations/003",
            ).stdout,
            refs_before_stale,
        )

        # Bring C to B g2, semantic-union the two append-only histories, then
        # refresh only the reviewed binding with a real zero-exit command.
        self.stage("refresh-C-to-B-g2")
        branch_base_progress = self.git(
            "show", f"{candidate_b1.candidate_commit}:harness/progress.md"
        ).stdout.encode("utf-8")
        c_progress = (c_path / "harness" / "progress.md").read_bytes()
        b2_progress = self.git(
            "show", f"{candidate_b2.candidate_commit}:harness/progress.md"
        ).stdout.encode("utf-8")
        union = governance.plan_progress_union(
            branch_base=branch_base_progress,
            latest_main=c_progress,
            branch_candidate=b2_progress,
        )
        self.assertTrue(union.ready, union.blockers)
        merge = self.git(
            "merge",
            "--no-ff",
            "--no-commit",
            candidate_b2.candidate_ref,
            cwd=c_path,
            check=False,
        )
        self.assertIn(merge.returncode, (0, 1), merge.stderr)
        assert union.preview is not None
        (c_path / "harness" / "progress.md").write_bytes(union.preview)
        self.git("add", "--all", cwd=c_path)
        self.git("commit", "--no-gpg-sign", "--no-verify", "-m", "stack C on exact B g2", cwd=c_path)
        c_lease = workspace.load_lease(context, "003")
        assert c_lease is not None
        refresh = workspace.build_dependency_refresh_plan(
            self.root,
            iteration="003",
            owner=str(c_lease["owner"]),
            lease_generation=int(c_lease["generation"]),
            worktree_path=c_path,
            branch_ref=str(c_lease["branch_ref"]),
            base_commit=self.oid("refs/project-harness/v2/iterations/003/base"),
            dependency_bindings=(binding_b2,),
            verification_commands=(
                {
                    "evidence_id": "verify:C-after-B-g2",
                    "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
                },
            ),
            operation_id=self.operation("refresh-c-to-b2"),
        )
        self.assertEqual(refresh.blockers, (), refresh.blockers)
        refreshed = workspace.apply_dependency_refresh(
            refresh,
            accepted_plan_digest=refresh.digest,
        )
        self.assertEqual(refreshed["phase"], "succeeded", refreshed)
        refreshed_c_lease = workspace.load_lease(context, "003")
        assert refreshed_c_lease is not None
        c_reconciliation_head = self.oid("HEAD", cwd=c_path)
        self.assertEqual(refreshed_c_lease["implementation_ref"], candidate_b2.candidate_ref)
        self.assertEqual(refreshed_c_lease["implementation_commit"], candidate_b2.candidate_commit)
        self.assertEqual(refreshed_c_lease["reconciliation_ref"], str(c_lease["branch_ref"]))
        self.assertEqual(refreshed_c_lease["reconciliation_commit"], c_reconciliation_head)
        self.assertNotEqual(c_reconciliation_head, candidate_b2.candidate_commit)
        candidate_c = self.register_candidate("003", "g1", c_path)
        self.assertEqual(candidate_c.implementation_commit, candidate_b2.candidate_commit)
        self.assertEqual(
            candidate_c.workspace_guard.reconciliation_commit,
            c_reconciliation_head,
        )

        # Release main in place for A without moving cwd, files, or index.
        self.stage("bind-A-and-candidate")
        a_lease = workspace.load_lease(context, "001")
        assert a_lease is not None
        bind = workspace.build_bind_local_branch_plan(
            self.root,
            iteration="001",
            owner=str(a_lease["owner"]),
            lease_generation=int(a_lease["generation"]),
            worktree_path=self.root,
            base_commit=self.oid("refs/project-harness/v2/iterations/001/base"),
            new_branch_ref="refs/heads/harness/prd-001",
            operation_id=self.operation("bind-a-in-place"),
        )
        self.assertEqual(bind.blockers, (), bind.blockers)
        root_progress = progress.parse_progress_events(
            (self.root / "harness" / "progress.md").read_bytes(),
            source="product-e2e-before-main-release",
        )
        self.assertFalse(root_progress.blockers, root_progress.blockers)
        release_parent = root_progress.events[-1].identity if root_progress.events else None
        release_session = "S-20260812-09"
        release_time = "2026-08-12T12:00:00+08:00"
        release_stage = lifecycle.plan_local_main_release_stage(
            bind,
            lifecycle_operation_id=self.operation("release-main-a-facade"),
            session_id=release_session,
            occurred_at=release_time,
            causal_parent=release_parent,
        )
        self.assertTrue(release_stage.ready, release_stage.blockers)
        release_notifications: list[dict[str, object]] = []
        main_before_release = self.oid("refs/heads/main")
        head_before_release = self.oid("HEAD")
        raw_index_before_release = (self.common_dir / "index").read_bytes()
        released_stage = lifecycle.apply_local_main_release_stage(
            release_stage,
            bind,
            accepted_plan_digest=release_stage.plan_digest,
            session_id=release_session,
            occurred_at=release_time,
            causal_parent=release_parent,
            notify=release_notifications.append,
        )
        bound = released_stage.child_result
        self.assertEqual(bound["phase"], "succeeded", bound)
        self.assertEqual(
            [item["phase"] for item in release_notifications],
            ["before", "after"],
        )
        self.assertTrue(all(bound["workspace"]["preservation"].values()))
        release_event_id = bound["progress"]["event_id"]
        self.assertEqual(self.progress_id_counts(self.root).get(release_event_id), 1)
        self.assertEqual(self.oid("refs/heads/main"), main_before_release)
        self.assertEqual(self.oid("HEAD"), head_before_release)
        self.assertEqual((self.common_dir / "index").read_bytes(), raw_index_before_release)
        self.assertEqual(self.git("ls-files", "--stage", "-z").stdout, a_index_before)
        self.assertEqual((self.root / "feature-001.txt").read_bytes(), a_feature_before)
        self.assert_no_push_contract(released_stage)
        self.git("add", "--", "feature-001.txt", "harness/progress.md")
        self.git("commit", "--no-gpg-sign", "--no-verify", "-m", "implement A")

        # Wrong lease generation and wrong worktree both block in a read-only plan.
        a_refs_before_guard = self.git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/project-harness/v2/iterations/001",
        ).stdout
        wrong_lease = self.candidate_plan("001", "g1", self.root, lease_generation=1)
        wrong_path = self.candidate_plan("001", "g1", self.root, feature_worktree=b_path)
        self.assertTrue(wrong_lease.blockers)
        self.assertTrue(wrong_path.blockers)
        self.assertEqual(
            self.git(
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/project-harness/v2/iterations/001",
            ).stdout,
            a_refs_before_guard,
        )
        candidate_a = self.register_candidate("001", "g1", self.root)

        # Latest-main no-ff train through the public lifecycle facade.
        self.stage("latest-main-train-prepare")
        facade_operation = self.operation("integration-facade-i1")
        ordered = lifecycle.plan_ordered_integration_preparation_stage(
            self.root,
            lifecycle_operation_id=facade_operation,
            generation="i1",
            # Deliberately supply readiness order, not dependency/queue order.
            candidates=(candidate_c, candidate_a, candidate_b2),
            verify_commands=(
                train.VerifyCommand(
                    evidence_id="verify:integration:i1",
                    argv=(sys.executable, "-c", "import sys; sys.exit(0)"),
                ),
            ),
            queue_metadata={
                "001": {"priority": 0, "queued_identity": "queue:a"},
                "002": {"priority": 30, "queued_identity": "queue:b"},
                "003": {"priority": 20, "queued_identity": "queue:c"},
            },
            operation_id=self.operation("integration-i1"),
        )
        self.assertEqual(ordered.ordered_iterations, ("002", "003", "001"))
        self.assertFalse(ordered.pushed)
        train_plan = ordered.integration_plan
        self.assertEqual(
            tuple(item.iteration for item in train_plan.candidates),
            ordered.ordered_iterations,
        )
        self.assertEqual(train_plan.blockers, (), train_plan.blockers)
        routing_authority = self.readme_authority()
        prepare_stage = ordered.lifecycle_plan
        prepare_token = self.confirmation(
            "prepare-integration",
            train_plan.plan_digest,
            "AUTH-PREPARE-I1",
        )
        train_notifications: list[object] = []
        prepared_stage = lifecycle.apply_ordered_integration_preparation_stage(
            ordered,
            accepted_plan_digest=ordered.plan_digest,
            confirmation_token=prepare_token,
            readme_authority=routing_authority,
            notify=train_notifications.append,
        )
        prepared = prepared_stage.child_result
        self.assertIsInstance(prepared, train.IntegrationPreparationResult)
        self.assertTrue(prepared.ready_for_commit, prepared.blockers)
        commit_plan = prepared.commit_plan
        assert commit_plan is not None
        governance_context = train.GovernanceContext(
            schema_version=train.GOVERNANCE_RECEIPT_SCHEMA,
            operation_id=train_plan.operation_id,
            project_root=train_plan.project_root,
            integration_worktree=train_plan.worktree_path,
            target_main=train_plan.target_main,
            principle_sha256=train_plan.principle_sha256,
            candidate_digests=tuple(
                item.candidate_evidence.evidence_digest for item in train_plan.candidates
            ),
            pre_governance_tree=commit_plan.governance_receipt.input_tree,
        )
        preview = train_governance.build_governance_callback(
            train_plan,
            readme_authority=routing_authority,
        ).preview(governance_context)
        self.assertTrue(preview.ready, preview.blockers)
        self.assertEqual(len(preview.progress_events), 9)
        self.assertEqual(sum(item.conditional for item in preview.progress_events), 6)
        derived_readme = preview.readme_authority
        self.assertIsNotNone(derived_readme)
        assert derived_readme is not None
        public_readme = train_governance.harness_readme_authority
        public_readme.validate_derived_readme_authority(derived_readme)
        readme_step = next(
            index
            for index, label in enumerate(preview.reconciliation_labels)
            if label.startswith("readme-")
        )
        independently_derived = public_readme.derive_train_readme_authority(
            train_plan,
            semantic_snapshot=preview.reconciliation_snapshots[readme_step - 1],
            governance_context=governance_context,
        )
        self.assertEqual(
            derived_readme.authority_digest,
            independently_derived.authority_digest,
        )
        rebuilt_readmes = public_readme.documents_by_path(derived_readme)
        self.assertEqual(
            set(rebuilt_readmes),
            {
                "harness/README.md",
                "harness/iterations/001/README.md",
                "harness/iterations/002/README.md",
                "harness/iterations/003/README.md",
            },
        )
        integration_path = Path(train_plan.worktree_path)
        for relative, exact_bytes in rebuilt_readmes.items():
            self.assertEqual((integration_path / relative).read_bytes(), exact_bytes)
            self.assertNotIn(b"E2E-REBUILT", exact_bytes)
        self.assertTrue(all(count == 1 for count in self.progress_id_counts(integration_path).values()))

        self.stage("integration-commit-and-registry")
        commit_stage = lifecycle.plan_integration_commit_stage(
            commit_plan,
            lifecycle_operation_id=facade_operation,
        )
        commit_token = self.confirmation(
            "create-integration-commit",
            commit_plan.commit_plan_digest,
            "AUTH-INTEGRATION-COMMIT-I1",
        )
        committed_stage = lifecycle.apply_integration_commit_stage(
            commit_stage,
            commit_plan,
            accepted_plan_digest=commit_stage.plan_digest,
            confirmation_token=commit_token,
        )
        committed = committed_stage.child_result
        self.assertIsInstance(committed, train.IntegrationCommitResult)
        self.assertTrue(committed.evidence_ready, committed.blockers)
        conditional_bindings = tuple(
            (item.event.event_id, item.evidence_ref)
            for item in preview.progress_events
            if item.conditional
        )
        canonical_final_refs = {
            f"refs/project-harness/v2/iterations/{number}/final-evidence"
            for number in ("001", "002", "003")
        }
        self.assertEqual(len(conditional_bindings), 6)
        self.assertEqual(
            {
                reference
                for _event_id, reference in conditional_bindings
                if reference.endswith("/final-evidence")
            },
            canonical_final_refs,
        )
        registry_stage = lifecycle.plan_integrated_evidence_stage(
            committed,
            lifecycle_operation_id=facade_operation,
            commit_confirmation_token=commit_token,
            progress_bindings=conditional_bindings,
        )
        registered_stage = lifecycle.apply_integrated_evidence_stage(
            registry_stage,
            committed,
            accepted_plan_digest=registry_stage.plan_digest,
            commit_confirmation_token=commit_token,
            progress_bindings=conditional_bindings,
        )
        registered = registered_stage.child_result
        self.assertIsInstance(registered, integrated_registry.RegisteredIntegratedEvidence)
        self.assertEqual(
            integrated_registry.registered_integrated_evidence_gate(self.root, registered),
            (),
        )

        # Exact final acceptance owns the only main CAS.  Crash after refs,
        # then recover from the durable pre-CAS child without a second CAS.
        self.stage("final-CAS-crash-recovery")
        main_plan = train.plan_main_advance(registered)
        self.assertEqual(main_plan.blockers, (), main_plan.blockers)
        main_token = self.confirmation(
            "advance-main",
            main_plan.plan_digest,
            "AUTH-FINAL-I1",
        )
        final_stage = lifecycle.plan_final_acceptance_stage(
            registered,
            lifecycle_operation_id=facade_operation,
            main_confirmation_token=main_token,
        )

        def crash_after_refs(stage: str) -> None:
            if stage == "final-acceptance-after-refs":
                raise RuntimeError("simulated crash after final refs")

        with self.assertRaisesRegex(RuntimeError, "after final refs"):
            lifecycle.apply_final_acceptance_stage(
                final_stage,
                registered,
                accepted_plan_digest=final_stage.plan_digest,
                main_confirmation_token=main_token,
                failpoint=crash_after_refs,
            )
        refs_after_final_cas = self.git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/project-harness/v2",
        ).stdout
        final_result_stage = lifecycle.apply_final_acceptance_stage(
            final_stage,
            registered,
            accepted_plan_digest=final_stage.plan_digest,
            main_confirmation_token=main_token,
        )
        main_result = final_result_stage.child_result
        self.assertIsInstance(main_result, train.MainAdvanceResult)
        self.assertTrue(main_result.idempotent)
        self.assertEqual(
            self.git(
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/project-harness/v2",
            ).stdout,
            refs_after_final_cas,
        )
        final_receipt, final_blockers = final_acceptance.load_registered_final_acceptance(
            self.root,
            operation_id=registered.operation_id,
        )
        self.assertEqual(final_blockers, ())
        self.assertIsNotNone(final_receipt)
        assert final_receipt is not None
        self.assertEqual(
            final_acceptance.registered_final_acceptance_gate(self.root, final_receipt),
            (),
        )
        self.assertEqual(self.oid("refs/heads/main"), committed.integrated_commit)
        for number in ("001", "002", "003"):
            authority = coordinator.derive_iteration_authority(self.root, number)
            self.assertTrue(authority.integrated, authority.blockers)
            self.assertEqual(authority.integrated_object, committed.integrated_commit)

        # All six conditional progress transitions are now publicly materialized.
        event_ids_by_ref = {
            item.ref_name: (item.event_id,)
            for item in registered.metadata.progress_bindings
        }
        final_ids = final_receipt.metadata.main_advanced_event_ids
        self.assertEqual(
            final_ids,
            tuple(
                event_ids_by_ref[
                    f"refs/project-harness/v2/iterations/{candidate.iteration}/final-evidence"
                ][0]
                for candidate in final_receipt.metadata.accepted_candidates
            ),
        )

        def resolve_progress(reference: str):
            oid = self.git("rev-parse", "--verify", reference, check=False).stdout.strip()
            if not oid:
                return None
            if reference.endswith("/final-evidence"):
                evidence_digest = final_receipt.registration_digest
            else:
                evidence_digest = registered.registration_digest
            return train_governance.ProgressEvidenceResolution(
                schema_version=train_governance.PROGRESS_EVIDENCE_RESOLUTION_SCHEMA,
                ref_name=reference,
                object_id=oid,
                evidence_digest=evidence_digest,
                event_ids=event_ids_by_ref[reference],
            )

        materialized = train_governance.materialize_train_progress_events(
            preview.progress_events,
            resolver=resolve_progress,
        )
        self.assertEqual(sum(item.conditional and item.materialized for item in materialized), 6)
        self.assertTrue(all(item.materialized for item in materialized))
        integrated_progress = self.git(
            "show", f"{committed.integrated_commit}:harness/progress.md"
        ).stdout.encode("utf-8")
        parsed = progress.parse_progress_events(integrated_progress, source="integrated-main")
        self.assertFalse(parsed.blockers, parsed.blockers)
        identities = [item.identity for item in parsed.events]
        self.assertEqual(len(identities), len(set(identities)))

        # Explicit cleanup removes only the integration worktree.  Releasing A
        # and B leaves C sticky in place (3 -> 1); no migration is attempted.
        self.stage("cleanup-and-3-to-1")
        cleanup_stage = lifecycle.plan_integration_cleanup_stage(
            main_result,
            lifecycle_operation_id=facade_operation,
        )
        cleanup_notifications: list[object] = []
        cleaned_stage = lifecycle.apply_integration_cleanup_stage(
            cleanup_stage,
            main_result,
            accepted_plan_digest=cleanup_stage.plan_digest,
            notify=cleanup_notifications.append,
        )
        self.assertTrue(cleaned_stage.child_result.removed)
        self.assertFalse(integration_path.exists())
        for number in ("001", "002"):
            lease = workspace.load_lease(context, number)
            assert lease is not None
            release = workspace.build_release_plan(
                self.root,
                iteration=number,
                owner=str(lease["owner"]),
                lease_generation=int(lease["generation"]),
                worktree_path=str(lease["worktree_path"]),
                branch_ref=str(lease["branch_ref"]),
                base_commit=self.oid(f"refs/project-harness/v2/iterations/{number}/base"),
                operation_id=self.operation(f"release-{number}"),
            )
            self.assertEqual(release.blockers, (), release.blockers)
            released = workspace.apply_release(
                self.root,
                iteration=number,
                owner=str(lease["owner"]),
                lease_generation=int(lease["generation"]),
                worktree_path=str(lease["worktree_path"]),
                branch_ref=str(lease["branch_ref"]),
                base_commit=self.oid(f"refs/project-harness/v2/iterations/{number}/base"),
                operation_id=release.operation_id,
                accepted_plan_digest=release.digest,
            )
            self.assertEqual(released["phase"], "succeeded", released)
        surviving = workspace.load_lease(context, "003")
        self.assertIsNotNone(surviving)
        assert surviving is not None
        self.assertEqual(Path(str(surviving["worktree_path"])), c_path)
        self.assertTrue(c_path.is_dir())
        topology = workspace.write_topology(context)
        self.assertEqual(topology["phase"], "DRAINING")
        self.assertEqual(self.git("remote", "-v").stdout, "")
        self.assertFalse(main_result.pushed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
