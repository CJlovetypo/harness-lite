from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.harness_upgrade import build_upgrade_plan


class HarnessUpgradePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="harness-upgrade-test-")
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness@example.invalid")
        (self.root / "harness" / "iterations" / "001").mkdir(parents=True)
        (self.root / "harness" / "iterations" / "001" / "README.md").write_text("001\n", encoding="utf-8")
        self.git("add", "harness/iterations/001/README.md")
        self.git("commit", "-m", "baseline")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    def test_completed_legacy_iteration_is_preserved_without_actions(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        final = "refs/project-harness/iterations/001/final"
        self.git("update-ref", base, self.head)
        self.git("update-ref", final, self.head)

        before_refs = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout
        plan = build_upgrade_plan(self.root)
        after_refs = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout

        self.assertEqual(plan.phase, "planned")
        self.assertEqual(plan.iterations[0].lifecycle, "legacy-complete")
        self.assertEqual(plan.iterations[0].disposition, "preserve-legacy-history")
        self.assertEqual(plan.planned_actions, ())
        self.assertEqual(before_refs, after_refs)

    def test_clean_active_legacy_iteration_only_proposes_explicit_adoption(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        self.git("update-ref", base, self.head)

        first = build_upgrade_plan(self.root)
        second = build_upgrade_plan(self.root)

        self.assertEqual(first.phase, "planned")
        self.assertEqual(first.plan_digest, second.plan_digest)
        self.assertEqual(first.iterations[0].lifecycle, "legacy-active-clean")
        self.assertEqual(first.planned_actions[0]["authorization"], "confirm")
        self.assertFalse(first.planned_actions[0]["writes"])

    def test_dirty_active_iteration_fails_closed_without_migration(self) -> None:
        base = "refs/project-harness/iterations/001/base/refs/heads/main"
        self.git("update-ref", base, self.head)
        (self.root / "implementation.txt").write_text("dirty\n", encoding="utf-8")

        plan = build_upgrade_plan(self.root)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("001:dirty-active-iteration-requires-explicit-adoption", plan.blocking_reasons)
        self.assertEqual(plan.planned_actions, ())

    def test_partial_v2_identity_is_blocked_and_not_repaired(self) -> None:
        self.git("update-ref", "refs/project-harness/v2/iterations/001/base", self.head)
        before = self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout

        plan = build_upgrade_plan(self.root)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("001:partial-v2-identity", plan.blocking_reasons)
        self.assertEqual(before, self.git("for-each-ref", "--format=%(refname) %(objectname)").stdout)

    def test_bundle_without_identity_requires_reconcile(self) -> None:
        plan = build_upgrade_plan(self.root)

        self.assertEqual(plan.phase, "blocked")
        self.assertIn("001:iteration-bundle-has-no-base-identity", plan.blocking_reasons)


if __name__ == "__main__":
    unittest.main()
