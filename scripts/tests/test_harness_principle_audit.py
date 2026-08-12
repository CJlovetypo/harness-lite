from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
for name in ("project_harness", "harness_governance", "harness_progress"):
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

SCRIPT = SCRIPT_DIR / "harness_principle_audit.py"
SPEC = importlib.util.spec_from_file_location("harness_principle_audit_tests", SCRIPT)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)

core = sys.modules["project_harness"]


OWNER = "<!-- managed-by: harness-lite v1 -->\n"
PRINCIPLE_V1 = (
    OWNER
    + "# Principles\n\n## Approved\n\n### P-001: preserve authority\n\n"
    + "Product authority remains explicit.\n"
)
PRINCIPLE_V2 = (
    PRINCIPLE_V1
    + "\n### P-002: durable audit\n\nEvery open PRD receives an impact audit.\n"
)
PROGRESS = (
    OWNER
    + "# Progress\n\n"
    + "## S-20260812-01 / OPEN / 2026-08-12T09:00:00+08:00\n\n"
    + "- fact: baseline\n"
)
DEVIATION = (
    OWNER
    + "# Deviation\n\n- 当前开放偏差：`0`\n"
)


def prd(number: str, *, principle_hash: str, revision: str) -> str:
    return (
        OWNER
        + f"# PRD-{number}: Product change\n\n"
        + "## 文档元数据\n\n"
        + f"- PRD ID：`PRD-{number}`\n"
        + "- 状态：`实施中`\n"
        + f"- principle_base_hash：`{principle_hash}`\n"
        + f"- 批准依据：用户明确批准 PRD-{number} 当前产品基线；修订证据 {revision}。\n\n"
        + "## 范围内需求\n\n"
        + f"### R-{number}-01: governed result\n\n"
        + "## 验收标准\n\n"
        + f"- **AC-{number}-01**：observable result.\n"
    )


def spec(number: str, *, revision: str) -> str:
    return (
        OWNER
        + f"# SPEC-{number}: Product change\n\n"
        + "## 文档元数据\n\n"
        + f"- SPEC ID：`SPEC-{number}`\n"
        + "- 状态：`实施中`\n"
        + f"- 批准依据：用户明确批准 SPEC-{number} 当前实施规格；修订证据 {revision}。\n"
        + "- 实施授权：用户明确授权开始实施当前已批准产品与规格。\n\n"
        + "## 需求追踪\n\n"
        + f"| R-{number}-01 / AC-{number}-01 | implementation | verification |\n"
    )


class PrincipleAuditGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.git_executable = shutil.which("git")
        if not self.git_executable:
            self.skipTest("git is required")
        self.temporary = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "main repo"
        self.root.mkdir()
        self.git_config = self.sandbox / "gitconfig"
        self.git_config.write_text(
            "[user]\n\tname = Harness Principle Audit Tests\n"
            "\temail = principle-audit@example.invalid\n",
            encoding="utf-8",
        )
        self.environment = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(self.git_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=False,
        )
        self.environment.start()
        self.git("init", "-b", "main")
        self.write("harness/principle.md", PRINCIPLE_V1)
        self.write("harness/progress.md", PROGRESS)
        for number in ("001", "002"):
            self.write(
                f"harness/iterations/{number}/prd-{number}.md",
                prd(number, principle_hash=hashlib.sha256(PRINCIPLE_V1.encode()).hexdigest(), revision="initial"),
            )
            self.write(
                f"harness/iterations/{number}/spec-{number}.md",
                spec(number, revision="initial"),
            )
            self.write(f"harness/iterations/{number}/deviation-{number}.md", DEVIATION)
        self.git("add", "--", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "base authority")
        self.base = self.rev("HEAD")
        self.principle_v1_sha = hashlib.sha256(PRINCIPLE_V1.encode()).hexdigest()
        for index, number in enumerate(("001", "002"), start=1):
            self.reserve(number, f"OP-{index:032x}")
        self.write("harness/principle.md", PRINCIPLE_V2)
        self.git("add", "--", "harness/principle.md")
        self.git("commit", "--no-gpg-sign", "-m", "change global principle")
        self.principle_commit = self.rev("HEAD")
        self.principle_v2_sha = hashlib.sha256(PRINCIPLE_V2.encode()).hexdigest()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def git(
        self,
        *arguments: str,
        check: bool = True,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(self.root), *arguments],
            input=input_bytes.decode("utf-8") if input_bytes is not None else None,
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )

    def rev(self, value: str) -> str:
        return self.git("rev-parse", value).stdout.strip()

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def reserve(self, number: str, operation_id: str) -> None:
        tree = self.rev(f"{self.base}^{{tree}}")
        metadata = {
            "schema_version": core.ALLOCATION_METADATA_SCHEMA_V1,
            "operation_id": operation_id,
            "plan_digest": hashlib.sha256(f"plan-{number}".encode()).hexdigest(),
            "iteration": number,
            "base_commit": self.base,
            "base_branch": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.base,
            "governance_tree": tree,
            "principle_sha256": self.principle_v1_sha,
            "title": f"Iteration {number}",
        }
        object_name = self.git(
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        ).stdout.strip()
        self.git("update-ref", f"refs/project-harness/v2/allocations/{number}", object_name)
        self.git("update-ref", f"refs/project-harness/v2/iterations/{number}/base", self.base)

    def feature_ref(self, number: str) -> str:
        ref = f"refs/heads/prd-{number}"
        self.git("branch", f"prd-{number}", self.principle_commit)
        return ref

    def decision(
        self,
        number: str,
        *,
        disposition: str,
        authority_ref: str | None = None,
        affected_ids: tuple[str, ...] | None = None,
        evidence_ids: tuple[str, ...] = ("EVIDENCE-impact-analysis-v1",),
        authorization_ids: tuple[str, ...] = ("AUTH-principle-audit-v1",),
    ):
        return audit.PrincipleAuditDecision.create(
            iteration=number,
            authority_ref=authority_ref or self.feature_ref(number),
            disposition=disposition,
            affected_ids=affected_ids or (f"R-{number}-01", f"AC-{number}-01"),
            evidence_ids=evidence_ids,
            authorization_ids=authorization_ids,
        )

    def reapprove(self, number: str, ref: str) -> str:
        self.write(
            f"harness/iterations/{number}/prd-{number}.md",
            prd(number, principle_hash=self.principle_v2_sha, revision="exact-current-principle"),
        )
        self.write(
            f"harness/iterations/{number}/spec-{number}.md",
            spec(number, revision="exact-current-principle"),
        )
        self.git(
            "add",
            "--",
            f"harness/iterations/{number}/prd-{number}.md",
            f"harness/iterations/{number}/spec-{number}.md",
        )
        self.git("commit", "--no-gpg-sign", "-m", f"reapprove PRD-{number}")
        commit = self.rev("HEAD")
        self.git("update-ref", ref, commit)
        return commit

    def test_two_open_prds_are_discovered_and_exactly_covered(self) -> None:
        # A raw commit-shaped final ref is not closure authority without the
        # exact completed merge-train main-advance journal.
        self.git(
            "update-ref",
            "refs/project-harness/v2/iterations/002/final",
            self.principle_commit,
        )
        open_iterations = audit.discover_open_v2_iterations(self.root)
        self.assertEqual([item.iteration for item in open_iterations], ["001", "002"])
        self.assertTrue(all(item.principle_base_sha256 == self.principle_v1_sha for item in open_iterations))
        decisions = {
            "001": self.decision("001", disposition=audit.DISPOSITION_NO_IMPACT),
            "002": self.decision("002", disposition=audit.DISPOSITION_IMPACT),
        }
        plans = audit.plan_open_principle_impact_audits(
            self.root,
            decisions=decisions,
            operation_ids={"001": f"OP-{101:032x}", "002": f"OP-{102:032x}"},
        )
        self.assertEqual([plan.iteration for plan in plans], ["001", "002"])
        self.assertTrue(all(plan.ready for plan in plans))
        with self.assertRaisesRegex(audit.PrincipleAuditError, "cover exactly"):
            audit.plan_open_principle_impact_audits(
                self.root,
                decisions={"001": decisions["001"]},
            )

    def test_no_evidence_is_blocked_and_plan_is_zero_write(self) -> None:
        common = Path(self.git("rev-parse", "--git-common-dir").stdout.strip()).resolve()
        before = sorted(path.relative_to(common) for path in common.rglob("*") if path.is_file())
        decision = self.decision(
            "001",
            disposition=audit.DISPOSITION_NO_IMPACT,
            evidence_ids=(),
        )
        plan = audit.plan_principle_impact_audit(
            self.root,
            decision=decision,
            operation_id=f"OP-{103:032x}",
        )
        after = sorted(path.relative_to(common) for path in common.rglob("*") if path.is_file())
        self.assertEqual(before, after)
        self.assertIn("audit-evidence-missing", [item.code for item in plan.blockers])
        with self.assertRaisesRegex(audit.PrincipleAuditError, "blocked"):
            audit.apply_principle_impact_audit(plan, accept_plan_digest=plan.plan_digest)

    def test_no_impact_exact_receipt_clears_only_current_drift(self) -> None:
        ref = self.feature_ref("001")
        plan = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_NO_IMPACT,
                authority_ref=ref,
            ),
            operation_id=f"OP-{104:032x}",
        )
        self.assertTrue(plan.ready)
        result = audit.apply_principle_impact_audit(
            plan, accept_plan_digest=plan.plan_digest
        )
        self.assertTrue(result.receipt.clears_drift)
        self.assertFalse(result.receipt.progress_checkpoint["write_progress"])
        self.assertIn(
            f"audit-receipt:{result.receipt.receipt_digest}",
            result.receipt.progress_evidence_refs,
        )
        gate = audit.current_principle_gate(
            self.root, iteration="001", authority_ref=ref
        )
        self.assertTrue(gate.allowed)
        other = audit.current_principle_gate(self.root, iteration="002")
        self.assertFalse(other.allowed)
        self.assertIn("principle-impact-audit-required", other.blockers)

    def test_impact_receipt_is_durable_but_remains_blocked(self) -> None:
        ref = self.feature_ref("001")
        plan = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_IMPACT,
                authority_ref=ref,
            ),
            operation_id=f"OP-{105:032x}",
        )
        result = audit.apply_principle_impact_audit(plan, accept_plan_digest=plan.plan_digest)
        self.assertFalse(result.receipt.clears_drift)
        gate = audit.current_principle_gate(self.root, iteration="001", authority_ref=ref)
        self.assertFalse(gate.allowed)
        self.assertIn("principle-reapproval-required", gate.blockers)

    def test_reapproved_requires_exact_committed_approved_authority(self) -> None:
        ref = self.feature_ref("001")
        blocked = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_REAPPROVED,
                authority_ref=ref,
            ),
            operation_id=f"OP-{106:032x}",
        )
        self.assertIn(
            "reapproval-principle-baseline-stale",
            [item.code for item in blocked.blockers],
        )
        # An uncommitted edit cannot satisfy the proof because authority_ref
        # still resolves to the older exact commit.
        self.write(
            "harness/iterations/001/prd-001.md",
            prd("001", principle_hash=self.principle_v2_sha, revision="uncommitted"),
        )
        still_blocked = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_REAPPROVED,
                authority_ref=ref,
            ),
            operation_id=f"OP-{107:032x}",
        )
        self.assertIn(
            "reapproval-principle-baseline-stale",
            [item.code for item in still_blocked.blockers],
        )
        self.git("restore", "--worktree", "--", "harness/iterations/001/prd-001.md")
        self.reapprove("001", ref)
        ready = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_REAPPROVED,
                authority_ref=ref,
            ),
            operation_id=f"OP-{108:032x}",
        )
        self.assertTrue(ready.ready, [item.code for item in ready.blockers])
        result = audit.apply_principle_impact_audit(ready, accept_plan_digest=ready.plan_digest)
        self.assertTrue(result.receipt.clears_drift)
        self.assertTrue(
            audit.current_principle_gate(self.root, iteration="001", authority_ref=ref).allowed
        )

    def test_main_authority_and_allocation_ref_drift_block_apply_or_gate(self) -> None:
        ref = self.feature_ref("001")
        plan = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_NO_IMPACT,
                authority_ref=ref,
            ),
            operation_id=f"OP-{109:032x}",
        )
        # Move main while keeping the working tree clean enough for the test.
        self.write("new.txt", "main moved\n")
        self.git("add", "--", "new.txt")
        self.git("commit", "--no-gpg-sign", "-m", "advance main")
        with self.assertRaisesRegex(audit.PrincipleAuditError, "drifted"):
            audit.apply_principle_impact_audit(plan, accept_plan_digest=plan.plan_digest)

        fresh = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_NO_IMPACT,
                authority_ref=ref,
            ),
            operation_id=f"OP-{110:032x}",
        )
        result = audit.apply_principle_impact_audit(fresh, accept_plan_digest=fresh.plan_digest)
        self.assertTrue(result.receipt.clears_drift)
        original_base = self.rev("refs/project-harness/v2/iterations/001/base")
        self.git(
            "update-ref",
            "refs/project-harness/v2/iterations/001/base",
            self.rev("refs/heads/main"),
            original_base,
        )
        with self.assertRaises(audit.PrincipleAuditError):
            audit.current_principle_gate(self.root, iteration="001", authority_ref=ref)

    def test_authority_ref_drift_invalidates_receipt(self) -> None:
        ref = self.feature_ref("001")
        plan = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_NO_IMPACT,
                authority_ref=ref,
            ),
            operation_id=f"OP-{111:032x}",
        )
        audit.apply_principle_impact_audit(plan, accept_plan_digest=plan.plan_digest)
        self.write("authority-extra.txt", "authority moved\n")
        self.git("add", "--", "authority-extra.txt")
        self.git("commit", "--no-gpg-sign", "-m", "move authority ref")
        self.git("update-ref", ref, self.rev("HEAD"))
        # An unrelated fast-forward (including the future progress event
        # commit) keeps the exact PRD/SPEC authority and principle bytes.
        gate = audit.current_principle_gate(self.root, iteration="001", authority_ref=ref)
        self.assertTrue(gate.allowed, gate.blockers)
        self.write(
            "harness/iterations/001/prd-001.md",
            prd("001", principle_hash=self.principle_v1_sha, revision="changed-authority"),
        )
        self.git("add", "--", "harness/iterations/001/prd-001.md")
        self.git("commit", "--no-gpg-sign", "-m", "change exact PRD authority")
        self.git("update-ref", ref, self.rev("HEAD"))
        gate = audit.current_principle_gate(self.root, iteration="001", authority_ref=ref)
        self.assertFalse(gate.allowed)
        self.assertIn("principle-audit-authority-drift", gate.blockers)

    def test_same_operation_retry_and_crash_after_receipt_recover(self) -> None:
        ref = self.feature_ref("001")
        plan = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_NO_IMPACT,
                authority_ref=ref,
            ),
            operation_id=f"OP-{112:032x}",
        )
        first = audit.apply_principle_impact_audit(plan, accept_plan_digest=plan.plan_digest)
        second = audit.apply_principle_impact_audit(plan, accept_plan_digest=plan.plan_digest)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.receipt.receipt_digest, second.receipt.receipt_digest)

        ref2 = self.feature_ref("002")
        plan2 = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "002",
                disposition=audit.DISPOSITION_NO_IMPACT,
                authority_ref=ref2,
            ),
            operation_id=f"OP-{113:032x}",
        )

        def crash(stage: str) -> None:
            if stage == "after_receipt_before_journal":
                raise audit.SimulatedCrash(stage)

        with self.assertRaises(audit.SimulatedCrash):
            audit.apply_principle_impact_audit(
                plan2,
                accept_plan_digest=plan2.plan_digest,
                fault_injector=crash,
            )
        loaded = audit.load_principle_impact_audit_plan(
            Path(plan2.git_common_dir), plan2.operation_id
        )
        recovered = audit.apply_principle_impact_audit(
            loaded, accept_plan_digest=loaded.plan_digest
        )
        self.assertTrue(recovered.idempotent)
        self.assertTrue(recovered.resumed)
        self.assertTrue(
            audit.current_principle_gate(self.root, iteration="002", authority_ref=ref2).allowed
        )

    def test_same_current_hash_different_audit_is_immutable_conflict(self) -> None:
        ref = self.feature_ref("001")
        operation = f"OP-{114:032x}"
        first = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_NO_IMPACT,
                authority_ref=ref,
                evidence_ids=("EVIDENCE-first",),
            ),
            operation_id=operation,
        )
        conflicting = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_NO_IMPACT,
                authority_ref=ref,
                evidence_ids=("EVIDENCE-different",),
            ),
            operation_id=operation,
        )
        audit.apply_principle_impact_audit(first, accept_plan_digest=first.plan_digest)
        with self.assertRaisesRegex(audit.PrincipleAuditError, "journal differs"):
            audit.apply_principle_impact_audit(
                conflicting,
                accept_plan_digest=conflicting.plan_digest,
            )
        persisted = audit.load_principle_impact_audit(
            Path(first.git_common_dir), "001", first.current_principle_sha256
        )
        assert persisted is not None
        self.assertEqual(persisted.operation_id, first.operation_id)

    def test_impact_then_exact_reapproval_supersedes_without_losing_history(self) -> None:
        ref = self.feature_ref("001")
        impact = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_IMPACT,
                authority_ref=ref,
            ),
            operation_id=f"OP-{116:032x}",
        )
        impact_result = audit.apply_principle_impact_audit(
            impact, accept_plan_digest=impact.plan_digest
        )
        self.assertEqual(impact_result.receipt.generation, 1)
        self.assertIsNone(impact_result.receipt.supersedes)
        self.assertFalse(
            audit.current_principle_gate(self.root, iteration="001", authority_ref=ref).allowed
        )

        self.reapprove("001", ref)
        reapproved = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_REAPPROVED,
                authority_ref=ref,
            ),
            operation_id=f"OP-{117:032x}",
        )
        self.assertTrue(reapproved.ready, [item.code for item in reapproved.blockers])
        self.assertEqual(reapproved.generation, 2)
        self.assertEqual(reapproved.supersedes, impact_result.receipt.receipt_digest)
        reapproved_result = audit.apply_principle_impact_audit(
            reapproved, accept_plan_digest=reapproved.plan_digest
        )
        self.assertTrue(reapproved_result.receipt.clears_drift)
        self.assertNotEqual(impact_result.receipt_path, reapproved_result.receipt_path)
        self.assertTrue(Path(impact_result.receipt_path).is_file())
        self.assertTrue(Path(reapproved_result.receipt_path).is_file())
        tip = audit.load_principle_impact_audit(
            Path(reapproved.git_common_dir), "001", self.principle_v2_sha
        )
        assert tip is not None
        self.assertEqual(tip.receipt_digest, reapproved_result.receipt.receipt_digest)
        historical = audit.load_principle_impact_audit_receipt(
            Path(reapproved.git_common_dir),
            "001",
            self.principle_v2_sha,
            impact_result.receipt.receipt_digest,
        )
        assert historical is not None
        self.assertEqual(historical.disposition, audit.DISPOSITION_IMPACT)
        self.assertEqual(historical.generation, 1)
        self.assertTrue(
            audit.current_principle_gate(self.root, iteration="001", authority_ref=ref).allowed
        )

    def test_concurrent_reapproval_successors_admit_only_one_causal_tip(self) -> None:
        ref = self.feature_ref("001")
        impact = audit.plan_principle_impact_audit(
            self.root,
            decision=self.decision(
                "001",
                disposition=audit.DISPOSITION_IMPACT,
                authority_ref=ref,
            ),
            operation_id=f"OP-{118:032x}",
        )
        impact_result = audit.apply_principle_impact_audit(
            impact, accept_plan_digest=impact.plan_digest
        )
        self.reapprove("001", ref)
        plans = tuple(
            audit.plan_principle_impact_audit(
                self.root,
                decision=self.decision(
                    "001",
                    disposition=audit.DISPOSITION_REAPPROVED,
                    authority_ref=ref,
                    evidence_ids=(f"EVIDENCE-reapproval-{index}",),
                    authorization_ids=(f"AUTH-reapproval-{index}",),
                ),
                operation_id=f"OP-{118 + index:032x}",
            )
            for index in (1, 2)
        )
        self.assertTrue(all(plan.ready and plan.generation == 2 for plan in plans))

        def apply_one(plan):
            try:
                return audit.apply_principle_impact_audit(
                    plan, accept_plan_digest=plan.plan_digest
                )
            except audit.PrincipleAuditError as exc:
                return exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(apply_one, plans))
        successes = [item for item in outcomes if isinstance(item, audit.PrincipleAuditApplyResult)]
        failures = [item for item in outcomes if isinstance(item, audit.PrincipleAuditError)]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(failures), 1, outcomes)
        tip = audit.load_principle_impact_audit(
            Path(plans[0].git_common_dir), "001", self.principle_v2_sha
        )
        assert tip is not None
        self.assertEqual(tip.generation, 2)
        self.assertEqual(tip.supersedes, impact_result.receipt.receipt_digest)
        historical = audit.load_principle_impact_audit_receipt(
            Path(plans[0].git_common_dir),
            "001",
            self.principle_v2_sha,
            impact_result.receipt.receipt_digest,
        )
        self.assertIsNotNone(historical)


if __name__ == "__main__":
    unittest.main()
