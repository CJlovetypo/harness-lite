from __future__ import annotations

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
for name in ("project_harness", "harness_governance", "harness_progress", "harness_principle_audit"):
    if name in sys.modules:
        continue
    module_path = SCRIPT_DIR / f"{name}.py"
    module_spec = importlib.util.spec_from_file_location(name, module_path)
    assert module_spec and module_spec.loader
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)

CONTROL_SCRIPT = SCRIPT_DIR / "harness_principle_control.py"
CONTROL_SPEC = importlib.util.spec_from_file_location("harness_principle_control_tests", CONTROL_SCRIPT)
assert CONTROL_SPEC and CONTROL_SPEC.loader
control = importlib.util.module_from_spec(CONTROL_SPEC)
sys.modules[CONTROL_SPEC.name] = control
CONTROL_SPEC.loader.exec_module(control)

core = sys.modules["project_harness"]
progress = sys.modules["harness_progress"]
audit = sys.modules["harness_principle_audit"]


OWNER = "<!-- managed-by: harness-lite v1 -->\n"
PRINCIPLE_V1 = OWNER + "# Principles\n\n### P-001: explicit authority\n\nAuthority stays explicit.\n"
PRINCIPLE_V2 = PRINCIPLE_V1 + "\n### P-002: impact audit\n\nEvery drift is audited.\n"
PROGRESS = (
    OWNER
    + "# Progress\n\n"
    + "## S-20260812-01 / OPEN / 2026-08-12T09:00:00+08:00\n\n"
    + "- fact: baseline\n"
)
DEVIATION = OWNER + "# Deviation\n\n- 当前开放偏差：`0`\n"


def prd(principle_hash: str, revision: str) -> str:
    return (
        OWNER
        + "# PRD-001: Controlled feature\n\n"
        + "## 文档元数据\n\n"
        + "- PRD ID：`PRD-001`\n"
        + "- 状态：`实施中`\n"
        + f"- principle_base_hash：`{principle_hash}`\n"
        + f"- 批准依据：用户明确批准 PRD-001 当前产品基线；修订证据 {revision}。\n\n"
        + "## 范围内需求\n\n### R-001-01: governed result\n\n"
        + "## 验收标准\n\n- **AC-001-01**：observable result.\n"
    )


def spec(revision: str) -> str:
    return (
        OWNER
        + "# SPEC-001: Controlled feature\n\n"
        + "## 文档元数据\n\n"
        + "- SPEC ID：`SPEC-001`\n"
        + "- 状态：`实施中`\n"
        + f"- 批准依据：用户明确批准 SPEC-001 当前实施规格；修订证据 {revision}。\n"
        + "- 实施授权：用户明确授权开始实施当前已批准产品与规格。\n\n"
        + "## 需求追踪\n\n| R-001-01 / AC-001-01 | implementation | verification |\n"
    )


class PrincipleControlTests(unittest.TestCase):
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
            "[user]\n\tname = Harness Principle Control Tests\n"
            "\temail = principle-control@example.invalid\n",
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
        self.principle_v1_sha = hashlib.sha256(PRINCIPLE_V1.encode()).hexdigest()
        self.write("harness/iterations/001/prd-001.md", prd(self.principle_v1_sha, "initial"))
        self.write("harness/iterations/001/spec-001.md", spec("initial"))
        self.write("harness/iterations/001/deviation-001.md", DEVIATION)
        self.git("add", "--", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "base authority")
        self.base = self.rev("HEAD")
        self.reserve()
        self.write("harness/principle.md", PRINCIPLE_V2)
        self.git("add", "--", "harness/principle.md")
        self.git("commit", "--no-gpg-sign", "-m", "change global principle")
        self.principle_commit = self.rev("HEAD")
        self.principle_v2_sha = hashlib.sha256(PRINCIPLE_V2.encode()).hexdigest()
        self.authority_ref = "refs/heads/prd-001"
        self.git("update-ref", self.authority_ref, self.principle_commit)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def git(self, *arguments: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git_executable, "-C", str(self.root), *arguments],
            input=input_bytes.decode("utf-8") if input_bytes is not None else None,
            check=True,
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

    def reserve(self) -> None:
        operation = f"OP-{1:032x}"
        tree = self.rev(f"{self.base}^{{tree}}")
        metadata = {
            "schema_version": core.ALLOCATION_METADATA_SCHEMA_V1,
            "operation_id": operation,
            "plan_digest": hashlib.sha256(b"allocation-plan-001").hexdigest(),
            "iteration": "001",
            "base_commit": self.base,
            "base_branch": "refs/heads/main",
            "governance_ref": "refs/heads/main",
            "governance_commit": self.base,
            "governance_tree": tree,
            "principle_sha256": self.principle_v1_sha,
            "title": "Iteration 001",
        }
        blob = self.git(
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        ).stdout.strip()
        self.git("update-ref", "refs/project-harness/v2/allocations/001", blob)
        self.git("update-ref", "refs/project-harness/v2/iterations/001/base", self.base)

    def decision(self, disposition: str):
        return audit.PrincipleAuditDecision.create(
            iteration="001",
            authority_ref=self.authority_ref,
            disposition=disposition,
            affected_ids=("R-001-01", "AC-001-01"),
            evidence_ids=(f"EVIDENCE-{disposition}",),
            authorization_ids=(f"AUTH-{disposition}",),
        )

    def last_parent(self) -> str:
        parsed = progress.parse_progress_events(
            (self.root / "harness/progress.md").read_bytes(),
            source="test-current-progress",
        )
        self.assertFalse(parsed.blockers)
        return parsed.events[-1].identity

    def plan(self, disposition: str, ordinal: int):
        return control.plan_principle_control(
            self.root,
            decision=self.decision(disposition),
            session_id=f"S-20260812-{ordinal:02d}",
            occurred_at=f"2026-08-12T10:{ordinal:02d}:00+08:00",
            causal_parent=self.last_parent(),
            operation_id=f"OP-{ordinal:032x}",
        )

    def reapprove(self) -> None:
        self.write("harness/iterations/001/prd-001.md", prd(self.principle_v2_sha, "current-principle"))
        self.write("harness/iterations/001/spec-001.md", spec("current-principle"))
        self.git(
            "add",
            "--",
            "harness/iterations/001/prd-001.md",
            "harness/iterations/001/spec-001.md",
        )
        self.git("commit", "--no-gpg-sign", "-m", "reapprove PRD-001")
        self.git("update-ref", self.authority_ref, self.rev("HEAD"))

    def test_no_impact_plan_is_zero_write_and_completes_with_event(self) -> None:
        common_raw = Path(self.git("rev-parse", "--git-common-dir").stdout.strip())
        common = (common_raw if common_raw.is_absolute() else self.root / common_raw).resolve()
        before_files = sorted(path.relative_to(common) for path in common.rglob("*") if path.is_file())
        before_progress = (self.root / "harness/progress.md").read_bytes()
        plan = self.plan(audit.DISPOSITION_NO_IMPACT, 10)
        self.assertTrue(plan.ready)
        self.assertEqual(before_progress, (self.root / "harness/progress.md").read_bytes())
        self.assertEqual(before_files, sorted(path.relative_to(common) for path in common.rglob("*") if path.is_file()))
        result = control.apply_principle_control(plan, accept_plan_digest=plan.plan_digest)
        self.assertEqual(result.phase, "COMPLETE")
        parsed = progress.parse_progress_events(
            (self.root / "harness/progress.md").read_bytes(), source="after-control"
        )
        self.assertEqual(sum(item.identity == result.progress_event_id for item in parsed.events), 1)
        gate = control.current_principle_control_gate(
            self.root, iteration="001", authority_ref=self.authority_ref
        )
        self.assertTrue(gate.allowed)
        self.assertTrue(gate.evidence_complete)
        self.assertEqual(gate.receipt_digest, result.audit_receipt_digest)

    def test_impact_and_reapproved_generations_each_append_event(self) -> None:
        impact = self.plan(audit.DISPOSITION_IMPACT, 11)
        first = control.apply_principle_control(impact, accept_plan_digest=impact.plan_digest)
        impact_gate = control.current_principle_control_gate(
            self.root, iteration="001", authority_ref=self.authority_ref
        )
        self.assertFalse(impact_gate.allowed)
        self.assertTrue(impact_gate.evidence_complete)
        self.assertIn("principle-reapproval-required", impact_gate.blockers)
        self.reapprove()
        reapproved = self.plan(audit.DISPOSITION_REAPPROVED, 12)
        self.assertEqual(reapproved.audit_plan.generation, 2)
        second = control.apply_principle_control(reapproved, accept_plan_digest=reapproved.plan_digest)
        final_gate = control.current_principle_control_gate(
            self.root, iteration="001", authority_ref=self.authority_ref
        )
        self.assertTrue(final_gate.allowed)
        parsed = progress.parse_progress_events(
            (self.root / "harness/progress.md").read_bytes(), source="two-generations"
        )
        identities = [item.identity for item in parsed.events]
        self.assertIn(first.progress_event_id, identities)
        self.assertIn(second.progress_event_id, identities)

    def test_crash_after_audit_before_progress_recovers_without_duplicate(self) -> None:
        plan = self.plan(audit.DISPOSITION_NO_IMPACT, 13)

        def crash(stage: str) -> None:
            if stage == "after_audit_before_progress":
                raise control.InjectedControlCrash(stage)

        with self.assertRaises(control.InjectedControlCrash):
            control.apply_principle_control(
                plan,
                accept_plan_digest=plan.plan_digest,
                fault_injector=crash,
            )
        interim = control.current_principle_control_gate(
            self.root, iteration="001", authority_ref=self.authority_ref
        )
        self.assertFalse(interim.allowed)
        self.assertFalse(interim.evidence_complete)
        recovered = control.apply_principle_control(plan, accept_plan_digest=plan.plan_digest)
        self.assertEqual(recovered.phase, "COMPLETE")
        parsed = progress.parse_progress_events(
            (self.root / "harness/progress.md").read_bytes(), source="recovered"
        )
        self.assertEqual(sum(item.identity == recovered.progress_event_id for item in parsed.events), 1)

    def test_conflicting_event_after_audit_blocks_composite_current_gate(self) -> None:
        plan = self.plan(audit.DISPOSITION_NO_IMPACT, 14)

        def crash(stage: str) -> None:
            if stage == "after_audit_before_progress":
                raise control.InjectedControlCrash(stage)

        with self.assertRaises(control.InjectedControlCrash):
            control.apply_principle_control(
                plan,
                accept_plan_digest=plan.plan_digest,
                fault_injector=crash,
            )
        journal = control.load_principle_control_journal(self.root, plan.operation_id)
        assert journal and isinstance(journal["event"], dict)
        expected = progress.ProgressEventV2.from_dict(journal["event"])
        conflicting = progress.build_progress_event(
            session_id=expected.session_id,
            iteration=expected.iteration,
            scope=expected.scope,
            event_type=expected.event_type,
            event_key=expected.event_key,
            occurred_at=expected.occurred_at,
            source_ref=expected.source_ref,
            source_commit=expected.source_commit,
            operation_id=expected.operation_id,
            causal_parent=expected.causal_parent,
            evidence_refs=expected.evidence_refs,
            summary=expected.summary + ":tampered",
        )
        conflicting_plan = progress.plan_progress_append(project_root=self.root, event=conflicting)
        progress.apply_progress_append(
            conflicting_plan, accept_plan_digest=conflicting_plan.plan_digest
        )
        with self.assertRaisesRegex(control.PrincipleControlError, "different exact bytes"):
            control.apply_principle_control(plan, accept_plan_digest=plan.plan_digest)
        gate = control.current_principle_control_gate(
            self.root, iteration="001", authority_ref=self.authority_ref
        )
        self.assertFalse(gate.allowed)
        self.assertFalse(gate.evidence_complete)

    def test_repeated_operation_is_idempotent(self) -> None:
        plan = self.plan(audit.DISPOSITION_NO_IMPACT, 15)
        first = control.apply_principle_control(plan, accept_plan_digest=plan.plan_digest)
        second = control.apply_principle_control(plan, accept_plan_digest=plan.plan_digest)
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.audit_receipt_digest, second.audit_receipt_digest)
        self.assertEqual(first.progress_event_id, second.progress_event_id)
        parsed = progress.parse_progress_events(
            (self.root / "harness/progress.md").read_bytes(), source="idempotent"
        )
        self.assertEqual(sum(item.identity == first.progress_event_id for item in parsed.events), 1)


if __name__ == "__main__":
    unittest.main()
