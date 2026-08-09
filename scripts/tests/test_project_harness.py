from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "project_harness.py"
SPEC = importlib.util.spec_from_file_location("project_harness", SCRIPT)
assert SPEC and SPEC.loader
project_harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = project_harness
SPEC.loader.exec_module(project_harness)


class HarnessCliTests(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git is required")
        self.temporary = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temporary.name).resolve()
        self.root = self.sandbox / "project"
        self.root.mkdir()
        self.junctions: list[Path] = []
        self.git_config = self.sandbox / "gitconfig"
        subprocess.run(
            [shutil.which("git"), "config", "--file", str(self.git_config), "user.name", "Harness Tests"],
            check=True,
        )
        subprocess.run(
            [shutil.which("git"), "config", "--file", str(self.git_config), "user.email", "harness@example.invalid"],
            check=True,
        )
        self.environment = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(self.git_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        for junction in reversed(self.junctions):
            try:
                if junction.exists() or junction.is_symlink() or getattr(junction, "is_junction", lambda: False)():
                    os.rmdir(junction)
            except OSError:
                pass
        self.temporary.cleanup()

    def git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [shutil.which("git"), "-C", str(self.root), *arguments],
            check=check,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def create_junction(self, link: Path, target: Path) -> None:
        if os.name != "nt":
            self.skipTest("NTFS junction test requires Windows")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            self.skipTest(f"Could not create an NTFS junction: {result.stderr or result.stdout}")
        self.junctions.append(link)

    def initialize_existing_repository(self) -> str:
        self.git("init", "-b", "main")
        source = self.root / "src" / "app.txt"
        source.parent.mkdir()
        source.write_text("existing\n", encoding="utf-8")
        self.git("add", "--", "src/app.txt")
        self.git("commit", "--no-gpg-sign", "-m", "initial")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def accept_iteration(self, number: str = "001") -> None:
        bundle = self.root / "harness" / "iterations" / number
        prd = bundle / f"prd-{number}.md"
        spec = bundle / f"spec-{number}.md"
        l1 = bundle / "README.md"
        root_readme = self.root / "harness" / "README.md"
        progress = self.root / "harness" / "progress.md"
        prd.write_text(
            prd.read_text(encoding="utf-8")
            .replace("- 状态：`草案`", "- 状态：`已验收`", 1)
            .replace("- 批准依据：尚无；当前仅建立草案。", f"- 批准依据：用户明确批准 PRD-{number} 产品基线。", 1)
            .replace("- 验收依据：尚无；只有用户明确验收后才更新。", "- 验收依据：用户明确回复验收通过。", 1)
            .replace(
                "说明当前事实、用户/业务问题、触发背景，以及不采取行动的代价。不要在这里预设技术实现。",
                "用户需要一个可观察、可验证的交付结果。",
                1,
            )
            .replace("说明本轮希望获得的可观察结果。", "交付结果在验收测试中可被直接观察。", 1)
            .replace(f"### R-{number}-01：待定义", f"### R-{number}-01：交付已实现行为", 1)
            .replace("用产品行为、约束或必须交付的结果描述需求。", "系统必须提供本轮批准的可观察行为。", 1)
            .replace(
                f"- **AC-{number}-01**：给出可观察、可验证、能映射到需求的完成条件。",
                f"- **AC-{number}-01**：验证命令通过并证明 R-{number}-01 的行为。",
                1,
            )
            .replace("- 明确本轮不解决的相邻问题，防止范围静默扩大。", "- 不改变相邻的未批准能力。", 1)
            .replace(
                "- 记录产品、安全、兼容、时间、成本或合规约束；不要写代码步骤。",
                "- 保持既有兼容性与用户数据安全。",
                1,
            )
            .replace(
                "- 列出会改变范围、验收或重要取舍且仍需用户决定的问题。",
                "- 无开放问题。",
                1,
            ),
            encoding="utf-8",
        )
        spec.write_text(
            spec.read_text(encoding="utf-8")
            .replace("- 状态：`受 PRD 阻塞`", "- 状态：`已完成`", 1)
            .replace(
                f"- 当前批准基线：尚无；等待 PRD-{number} 批准。",
                f"- 当前批准基线：用户已批准的 PRD-{number}（R-{number}-01 / AC-{number}-01）。",
                1,
            )
            .replace("- 实施授权：尚无。", "- 实施授权：用户明确批准当前 PRD/SPEC 并要求实现。", 1)
            .replace(
                "在 PRD 获批后，定义组件职责、边界与关键取舍。不得在 SPEC 中新增 PRD 未授权的产品范围。",
                "实现限定在批准需求的责任边界内，不新增产品范围。",
                1,
            )
            .replace(
                f"| R-{number}-01 / AC-{number}-01 | 待定义 | 待定义 |",
                f"| R-{number}-01 / AC-{number}-01 | src/feature | 验收测试通过 |",
                1,
            )
            .replace(
                "列出会创建或修改的责任路径、公共接口、输入输出、Schema 与不变量。",
                "责任路径为 src/feature；保持现有公共接口和数据不变量。",
                1,
            )
            .replace(
                "把实施拆成可验证切片或工作包；每个切片说明依赖、输出与停止条件。",
                "实现批准行为，然后运行映射到 AC 的验证；失败即停止。",
                1,
            )
            .replace("说明向后兼容、数据迁移、部署顺序和用户资产保护。", "保持向后兼容；本轮不迁移用户数据。", 1)
            .replace("说明失败时如何安全恢复，不改写历史或丢弃用户数据。", "失败时撤销本轮代码差异且不改写历史。", 1)
            .replace(
                "列出主要技术/交付风险。实现前已知会偏离批准基线的变化，先修订并重新批准受影响的 PRD/SPEC；deviation 只在实现完成后记录 as-built 事实差异。",
                "主要风险是行为回归；任何预知范围变化先重批基线。",
                1,
            )
            .replace(
                "按风险定义单元、集成、端到端、静态或人工验证，并逐项映射验收 ID。",
                f"运行验收测试并记录通过结果，映射 AC-{number}-01。",
                1,
            )
            .replace(
                f"- {project_harness.datetime.now().date().isoformat()}：创建受 PRD 阻塞的规格骨架；尚未授权实施。",
                f"- {project_harness.datetime.now().date().isoformat()}：用户批准 PRD-{number}/SPEC-{number} 并授权实施。",
                1,
            ),
            encoding="utf-8",
        )
        l1.write_text(
            l1.read_text(encoding="utf-8")
            .replace("- PRD 状态：`草案`", "- PRD 状态：`已验收`", 1)
            .replace("- SPEC 状态：`受 PRD 阻塞`", "- SPEC 状态：`已完成`", 1),
            encoding="utf-8",
        )
        root_readme.write_text(
            root_readme.read_text(encoding="utf-8").replace(
                "| 草案 | 受 PRD 阻塞 | 0 |",
                "| 已验收 | 已完成 | 0 |",
                1,
            ),
            encoding="utf-8",
        )
        progress.write_text(
            progress.read_text(encoding="utf-8").replace(
                "| 草案 | 受 PRD 阻塞 | 完成并批准 PRD |",
                "| 已验收 | 已完成 | 已完成并验收 |",
                1,
            )
            + f"\n## S-20990101-01 / CLOSE / 2099-01-01T00:00:00+00:00\n\n"
            + f"- 关联：PRD-{number} / SPEC-{number}\n"
            + "- 验证证据：全部验收项通过，用户明确验收。\n",
            encoding="utf-8",
        )

    def complete_deviation_entry(
        self,
        *,
        identity: str = "DEV-001-001",
        status: str = "已修复",
        title: str = "Observed mismatch",
        disposition: str = "Implementation returned to the approved behavior",
        evidence: str = "User-approved baseline and implementation review",
        verification: str = "Regression test passed for the approved behavior",
        closed_at: str = "2099-01-01T00:00:00+00:00",
    ) -> str:
        return (
            f"\n### {identity}：{title}\n\n"
            f"- 状态：{status}\n"
            "- 发现时间：2098-12-31T00:00:00+00:00\n"
            "- 关联需求/验收：R-001-01 / AC-001-01\n"
            "- SPEC 章节：SPEC-001 §3\n"
            "- 原批准内容：Promise A\n"
            "- as-built 事实：Behavior B was observed\n"
            "- 原因：Implementation detail diverged during delivery\n"
            "- 影响：Behavior B affected the documented result\n"
            "- 验收影响：AC-001-01 required reconciliation\n"
            f"- 明确处置：{disposition}\n"
            f"- 处置依据：{evidence}\n"
            f"- 验证：{verification}\n"
            f"- 关闭或转交时间：{closed_at}\n"
        )

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = project_harness.main(list(arguments))
        return result, stdout.getvalue(), stderr.getvalue()

    def shared_iteration_includes(self) -> tuple[str, ...]:
        return (
            "--include",
            "harness/README.md",
            "--include",
            "harness/progress.md",
        )

    def init(self, name: str = "示例项目") -> tuple[int, str, str]:
        arguments = (
            "init",
            "--project-root",
            str(self.root),
            "--project-name",
            name,
        )
        preview_result, preview_stdout, preview_stderr = self.run_cli(*arguments, "--dry-run")
        if preview_result != 0:
            return preview_result, preview_stdout, preview_stderr
        token = next(
            (
                line.removeprefix("BASELINE_PLAN_TOKEN ")
                for line in preview_stdout.splitlines()
                if line.startswith("BASELINE_PLAN_TOKEN ")
            ),
            None,
        )
        if token is None:
            return self.run_cli(*arguments)
        return self.run_cli(*arguments, "--accept-baseline-plan", token)

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def test_fresh_init_creates_only_global_scaffold(self) -> None:
        result, stdout, stderr = self.init()
        self.assertEqual(0, result, stderr)
        self.assertIn("VALID:", stdout)
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertTrue((self.root / "harness" / "README.md").is_file())
        self.assertTrue((self.root / "harness" / "principle.md").is_file())
        self.assertTrue((self.root / "harness" / "progress.md").is_file())
        self.assertTrue((self.root / "harness" / "iterations" / ".gitkeep").is_file())
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())
        self.assertIn("示例项目", (self.root / "harness" / "README.md").read_text(encoding="utf-8"))
        self.assertTrue((self.root / ".git").is_dir())
        self.assertEqual("main", self.git("branch", "--show-current").stdout.strip())
        self.assertEqual("1", self.git("rev-list", "--count", "HEAD").stdout.strip())
        self.assertIn("BASELINE_COMMIT", stdout)
        self.assertEqual("", self.git("status", "--porcelain").stdout)

    def test_new_repository_baseline_includes_existing_nonignored_files_exactly(self) -> None:
        source = self.root / "src" / "应用 file.txt"
        source.parent.mkdir()
        source.write_text("keep\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        (self.root / "ignored.log").write_text("runtime\n", encoding="utf-8")

        result, stdout, stderr = self.init()
        self.assertEqual(0, result, stderr)
        tracked = set(self.git("-c", "core.quotepath=false", "ls-files").stdout.splitlines())
        self.assertIn("src/应用 file.txt", tracked)
        self.assertIn(".gitignore", tracked)
        self.assertNotIn("ignored.log", tracked)
        self.assertIn('"src/应用 file.txt"', stdout)
        self.assertEqual("runtime\n", (self.root / "ignored.log").read_text(encoding="utf-8"))

    def test_sensitive_baseline_aborts_atomically(self) -> None:
        (self.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        result, _, stderr = self.init()
        self.assertEqual(2, result)
        self.assertIn("environment secrets file", stderr)
        self.assertFalse((self.root / ".git").exists(), stderr)
        self.assertFalse((self.root / "harness").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertTrue((self.root / ".env").exists())

    def test_missing_child_below_external_junction_is_rejected(self) -> None:
        external = self.sandbox / "external"
        external.mkdir()
        junction = self.root / "linked"
        self.create_junction(junction, external)
        with self.assertRaises(project_harness.HarnessError):
            project_harness.ensure_inside_root(junction / "missing.md", self.root)

    def test_project_root_junction_is_rejected_before_writing(self) -> None:
        self.root.rmdir()
        actual = self.sandbox / "actual-project"
        actual.mkdir()
        self.create_junction(self.root, actual)

        result, _, stderr = self.init()

        self.assertEqual(2, result)
        self.assertIn("Project root path traverses a symbolic link or junction", stderr)
        self.assertFalse((actual / ".git").exists())
        self.assertFalse((actual / "harness").exists())

    def test_new_repository_baseline_rejects_external_junction_atomically(self) -> None:
        external = self.sandbox / "external"
        external.mkdir()
        sentinel = external / "public.txt"
        sentinel.write_text("outside\n", encoding="utf-8")
        self.create_junction(self.root / "linked", external)

        result, _, stderr = self.init()

        self.assertEqual(2, result)
        self.assertIn("outside project root", stderr)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "harness").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertEqual("outside\n", sentinel.read_text(encoding="utf-8"))

    def test_rollback_never_follows_parent_swapped_to_junction(self) -> None:
        parent = self.root / "managed"
        parent.mkdir()
        target = parent / "entry.md"
        operation = project_harness.Operation(path=target, new_raw=b"managed\n", old_raw=None)
        target.write_bytes(operation.new_raw)
        saved_parent = self.root / "saved-managed"
        parent.rename(saved_parent)
        external = self.sandbox / "external"
        external.mkdir()
        external_target = external / "entry.md"
        external_target.write_bytes(operation.new_raw)
        self.create_junction(parent, external)

        errors = project_harness.rollback_operations([operation], self.root)

        self.assertTrue(errors)
        self.assertEqual(operation.new_raw, external_target.read_bytes())
        self.assertEqual(operation.new_raw, (saved_parent / "entry.md").read_bytes())

    def test_ignored_sensitive_file_is_not_committed(self) -> None:
        (self.root / ".gitignore").write_text(".env\n", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        result, _, stderr = self.init()
        self.assertEqual(0, result, stderr)
        self.assertNotIn(".env", self.git("ls-files").stdout.splitlines())

    def test_oversized_baseline_aborts_atomically(self) -> None:
        (self.root / "large.bin").write_bytes(b"12345678901")
        with mock.patch.object(project_harness, "BASELINE_MAX_FILE_BYTES", 10):
            result, _, stderr = self.init()
        self.assertEqual(2, result)
        self.assertIn("baseline limit", stderr)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "harness").exists())

    def test_staged_blob_is_rescanned_after_git_filters(self) -> None:
        source = self.root / "safe.txt"
        source.write_text("safe working-tree content\n", encoding="utf-8")
        real_stage = project_harness.stage_exact_paths

        def inject_staged_secret(git: str, root: Path, relative_paths: list[str]) -> None:
            real_stage(git, root, relative_paths)
            secret = b"-----BEGIN PRIVATE KEY-----\nfiltered secret\n"
            object_id = project_harness.decode_output(
                project_harness.run_git(git, root, ["hash-object", "-w", "--stdin"], input_bytes=secret).stdout
            )
            project_harness.run_git(
                git,
                root,
                ["update-index", "--cacheinfo", f"100644,{object_id},safe.txt"],
            )

        with mock.patch.object(project_harness, "stage_exact_paths", side_effect=inject_staged_secret):
            result, _, stderr = self.init()

        self.assertEqual(2, result)
        self.assertIn("private key material after Git clean filters", stderr)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "harness").exists())
        self.assertEqual("safe working-tree content\n", source.read_text(encoding="utf-8"))

    def test_clean_filter_byte_change_cannot_diverge_from_reviewed_baseline(self) -> None:
        source = self.root / "safe.txt"
        source.write_text("safe payload\n", encoding="utf-8")
        real_stage = project_harness.stage_exact_paths

        def inject_filtered_bytes(git: str, root: Path, relative_paths: list[str]) -> None:
            real_stage(git, root, relative_paths)
            filtered = b"SAFE PAYLOAD\n"
            object_id = project_harness.decode_output(
                project_harness.run_git(
                    git,
                    root,
                    ["hash-object", "-w", "--stdin"],
                    input_bytes=filtered,
                ).stdout
            )
            project_harness.run_git(
                git,
                root,
                ["update-index", "--cacheinfo", f"100644,{object_id},safe.txt"],
            )

        with mock.patch.object(project_harness, "stage_exact_paths", side_effect=inject_filtered_bytes):
            result, _, stderr = self.init()

        self.assertEqual(2, result)
        self.assertIn("clean filters changed baseline bytes", stderr)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "harness").exists())
        self.assertEqual("safe payload\n", source.read_text(encoding="utf-8"))

    def test_npm_credentials_file_aborts_baseline_atomically(self) -> None:
        (self.root / ".npmrc").write_text(
            "//registry.npmjs.org/:_authToken=npm-secret\n",
            encoding="utf-8",
        )

        result, _, stderr = self.init()

        self.assertEqual(2, result)
        self.assertIn("sensitive credential/key filename", stderr)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "harness").exists())

    def test_missing_git_identity_aborts_without_writes(self) -> None:
        empty_config = self.sandbox / "empty-gitconfig"
        empty_config.write_text("", encoding="utf-8")
        keys = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
        saved = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty_config)}, clear=False):
                result, _, stderr = self.init()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(2, result)
        self.assertIn("Git identity is unavailable", stderr)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "harness").exists())

    def test_failed_baseline_commit_rolls_back_only_created_directories(self) -> None:
        preexisting_harness = self.root / "harness"
        preexisting_harness.mkdir()
        real_run_git = project_harness.run_git

        def fail_commit(git: str, root: Path, arguments: list[str], **kwargs: object):
            if "commit" in arguments:
                raise project_harness.HarnessError("forced commit failure")
            return real_run_git(git, root, arguments, **kwargs)

        with mock.patch.object(project_harness, "run_git", side_effect=fail_commit):
            result, _, stderr = self.init()
        self.assertEqual(2, result)
        self.assertIn("forced commit failure", stderr)
        self.assertTrue(preexisting_harness.is_dir())
        self.assertEqual([], list(preexisting_harness.iterdir()))
        self.assertFalse((self.root / ".git").exists(), stderr)
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_git_metadata_appearing_after_preview_is_never_removed(self) -> None:
        real_apply = project_harness.apply_operations

        def inject_foreign_git(root: Path, operations: list[project_harness.Operation]) -> None:
            real_apply(root, operations)
            marker = root / ".git"
            marker.mkdir()
            (marker / "foreign-sentinel").write_text("owned elsewhere\n", encoding="utf-8")

        with mock.patch.object(project_harness, "apply_operations", side_effect=inject_foreign_git):
            result, _, stderr = self.init()

        self.assertEqual(2, result)
        self.assertIn("Git metadata appeared", stderr)
        self.assertEqual(
            "owned elsewhere\n",
            (self.root / ".git" / "foreign-sentinel").read_text(encoding="utf-8"),
        )
        self.assertFalse((self.root / "harness").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_init_dry_run_writes_nothing(self) -> None:
        result, stdout, stderr = self.run_cli(
            "init",
            "--project-root",
            str(self.root),
            "--project-name",
            "Dry Run",
            "--dry-run",
        )
        self.assertEqual(0, result, stderr)
        self.assertIn("DRY-RUN", stdout)
        self.assertIn("BASELINE_DIGEST ", stdout)
        self.assertIn("BASELINE_PLAN_TOKEN v1:", stdout)
        self.assertEqual({}, self.snapshot())

    def test_no_git_apply_requires_reviewed_baseline_plan_token(self) -> None:
        result, _, stderr = self.run_cli(
            "init",
            "--project-root",
            str(self.root),
            "--project-name",
            "Token Required",
        )

        self.assertEqual(2, result)
        self.assertIn("requires --accept-baseline-plan", stderr)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "harness").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_reviewed_baseline_plan_token_rejects_content_changed_after_dry_run(self) -> None:
        source = self.root / "data.txt"
        source.write_text("reviewed A\n", encoding="utf-8")
        preview_result, preview_stdout, preview_stderr = self.run_cli(
            "init",
            "--project-root",
            str(self.root),
            "--project-name",
            "Bound Preview",
            "--dry-run",
        )
        self.assertEqual(0, preview_result, preview_stderr)
        token = next(
            line.removeprefix("BASELINE_PLAN_TOKEN ")
            for line in preview_stdout.splitlines()
            if line.startswith("BASELINE_PLAN_TOKEN ")
        )

        source.write_text("unreviewed B\n", encoding="utf-8")
        result, _, stderr = self.run_cli(
            "init",
            "--project-root",
            str(self.root),
            "--project-name",
            "Bound Preview",
            "--accept-baseline-plan",
            token,
        )

        self.assertEqual(2, result)
        self.assertIn("accepted baseline plan no longer matches", stderr)
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "harness").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertEqual("unreviewed B\n", source.read_text(encoding="utf-8"))

    def test_init_is_byte_idempotent(self) -> None:
        self.assertEqual(0, self.init()[0])
        before = self.snapshot()
        result, stdout, stderr = self.init()
        self.assertEqual(0, result, stderr)
        self.assertIn("NO-CHANGES", stdout)
        self.assertEqual(before, self.snapshot())
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(1, agents.count(project_harness.AGENTS_START))

    def test_legacy_owner_marker_remains_valid_after_skill_rename(self) -> None:
        self.assertEqual(0, self.init()[0])
        for relative in ("harness/README.md", "harness/principle.md", "harness/progress.md"):
            path = self.root / relative
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    project_harness.OWNER_MARKER,
                    project_harness.LEGACY_OWNER_MARKER,
                    1,
                ),
                encoding="utf-8",
            )

        result, stdout, stderr = self.run_cli("validate", "--project-root", str(self.root))

        self.assertEqual(0, result, stderr)
        self.assertIn("VALID:", stdout)

    def test_init_repairs_missing_global_file_without_overwriting_custom_content(self) -> None:
        self.assertEqual(0, self.init()[0])
        readme = self.root / "harness" / "README.md"
        progress = self.root / "harness" / "progress.md"
        principle = self.root / "harness" / "principle.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\n用户保留备注。\n", encoding="utf-8")
        progress_before = progress.read_bytes()
        principle.unlink()

        result, _, stderr = self.init()
        self.assertEqual(0, result, stderr)
        self.assertTrue(principle.is_file())
        self.assertIn("用户保留备注。", readme.read_text(encoding="utf-8"))
        self.assertEqual(progress_before, progress.read_bytes())

    def test_existing_agents_preserves_bom_crlf_and_prefix(self) -> None:
        original = b"\xef\xbb\xbf# Existing\r\n\r\nKeep this byte-for-byte.\r\n"
        (self.root / "AGENTS.md").write_bytes(original)
        result, _, stderr = self.init()
        self.assertEqual(0, result, stderr)
        updated = (self.root / "AGENTS.md").read_bytes()
        self.assertTrue(updated.startswith(original))
        self.assertTrue(updated.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n<!-- project-harness:start v1 -->\r\n", updated)

    def test_foreign_harness_aborts_without_partial_write(self) -> None:
        harness = self.root / "harness"
        harness.mkdir()
        (harness / "README.md").write_text("# Foreign system\n", encoding="utf-8")
        result, _, stderr = self.init()
        self.assertEqual(2, result)
        self.assertIn("not owned", stderr)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertEqual("# Foreign system\n", (harness / "README.md").read_text(encoding="utf-8"))

    def test_unmarked_harness_rules_in_agents_abort(self) -> None:
        (self.root / "AGENTS.md").write_text(
            "Use harness for every PRD and SPEC.\n",
            encoding="utf-8",
        )
        result, _, stderr = self.init()
        self.assertEqual(2, result)
        self.assertIn("unmarked", stderr)
        self.assertFalse((self.root / "harness").exists())

    @unittest.skipUnless(shutil.which("git"), "git is not available")
    def test_git_repository_initializes_without_no_git_warning(self) -> None:
        subprocess.run(
            [shutil.which("git"), "init", str(self.root)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result, stdout, stderr = self.init()
        self.assertEqual(0, result, stderr)
        self.assertNotIn("no-git", stdout)
        self.assertIn("git-unborn", stdout)

    def test_new_iteration_rejects_existing_unborn_repository(self) -> None:
        self.git("init", "-b", "main")
        self.assertEqual(0, self.init()[0])

        result, _, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Must not start",
        )

        self.assertEqual(2, result)
        self.assertIn("has no baseline commit", stderr)
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())

    def test_existing_repository_init_preserves_head_index_and_dirty_product(self) -> None:
        before_head = self.initialize_existing_repository()
        source = self.root / "src" / "app.txt"
        source.write_text("user dirty change\n", encoding="utf-8")
        before_index = self.git("write-tree").stdout.strip()

        result, stdout, stderr = self.init()
        self.assertEqual(0, result, stderr)
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before_index, self.git("write-tree").stdout.strip())
        self.assertEqual("user dirty change\n", source.read_text(encoding="utf-8"))
        self.assertNotIn("BASELINE_COMMIT", stdout)
        self.assertEqual("1", self.git("rev-list", "--count", "HEAD").stdout.strip())
        status = self.git("status", "--porcelain", "--untracked-files=all").stdout
        self.assertIn("src/app.txt", status)
        self.assertIn("harness/README.md", status)

    def test_existing_repository_validation_failure_rolls_back_repair(self) -> None:
        self.initialize_existing_repository()
        self.assertEqual(0, self.init()[0])
        principle = self.root / "harness" / "principle.md"
        principle.unlink()
        invalid = self.root / "harness" / "iterations" / "junk.txt"
        invalid.write_text("user file\n", encoding="utf-8")
        result, _, stderr = self.init()
        self.assertEqual(2, result)
        self.assertIn("rolled back", stderr)
        self.assertFalse(principle.exists())
        self.assertEqual("user file\n", invalid.read_text(encoding="utf-8"))

    def test_existing_repository_init_never_writes_gitkeep_through_junction(self) -> None:
        before_head = self.initialize_existing_repository()
        self.assertEqual(0, self.init()[0])
        iterations = self.root / "harness" / "iterations"
        (iterations / ".gitkeep").unlink()
        iterations.rmdir()
        external = self.sandbox / "external-iterations"
        external.mkdir()
        self.create_junction(iterations, external)
        before_index = self.git("write-tree").stdout.strip()

        result, _, stderr = self.init()

        self.assertEqual(2, result)
        self.assertIn("outside project root", stderr)
        self.assertFalse((external / ".gitkeep").exists())
        self.assertEqual(before_head, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(before_index, self.git("write-tree").stdout.strip())

    def test_validator_checks_gitkeep_before_skipping_it(self) -> None:
        self.assertEqual(0, self.init()[0])
        gitkeep = self.root / "harness" / "iterations" / ".gitkeep"
        gitkeep.unlink()
        external = self.sandbox / "external-gitkeep"
        external.mkdir()
        self.create_junction(gitkeep, external)

        result, stdout, stderr = self.run_cli("validate", "--project-root", str(self.root))

        self.assertEqual(1, result, stderr)
        self.assertIn("unsafe-path", stdout)

    def test_validator_rejects_required_bundle_junction_without_reading_it(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        prd = self.root / "harness" / "iterations" / "001" / "prd-001.md"
        prd.unlink()
        external = self.sandbox / "external-prd"
        external.mkdir()
        self.create_junction(prd, external)

        result, stdout, stderr = self.run_cli("validate", "--project-root", str(self.root))

        self.assertEqual(1, result, stderr)
        self.assertIn("unsafe-path", stdout)

    def test_target_nested_inside_parent_repository_is_rejected(self) -> None:
        parent = self.sandbox / "parent"
        parent.mkdir()
        nested = parent / "nested"
        nested.mkdir()
        self.git_config.write_text(
            "[user]\n\tname = Harness Tests\n\temail = harness@example.invalid\n",
            encoding="utf-8",
        )
        subprocess.run([shutil.which("git"), "-C", str(parent), "init", "-b", "main"], check=True)
        self.root = nested
        result, _, stderr = self.init()
        self.assertEqual(2, result)
        self.assertIn("inside a different Git repository", stderr)
        self.assertFalse((nested / "harness").exists())
        self.assertFalse((nested / "AGENTS.md").exists())

    @unittest.skipUnless(shutil.which("git"), "git is not available")
    def test_git_ignored_harness_aborts_before_writing(self) -> None:
        subprocess.run(
            [shutil.which("git"), "init", str(self.root)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (self.root / ".gitignore").write_text("harness/\n", encoding="utf-8")
        result, _, stderr = self.init()
        self.assertEqual(2, result)
        self.assertIn("ignored by Git", stderr)
        self.assertFalse((self.root / "harness").exists())

    def test_ignored_iteration_bundle_aborts_before_allocation(self) -> None:
        (self.root / ".gitignore").write_text("", encoding="utf-8")
        self.assertEqual(0, self.init()[0])
        (self.root / ".gitignore").write_text(
            "harness/iterations/*/*.md\n",
            encoding="utf-8",
        )

        result, _, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Ignored bundle",
            "--dry-run",
        )

        self.assertEqual(2, result)
        self.assertIn("Governance paths are ignored by Git", stderr)
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())
        self.assertTrue((self.root / "AGENTS.md").is_file())

    def test_new_iteration_dry_run_then_apply(self) -> None:
        self.assertEqual(0, self.init()[0])
        result, stdout, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "首轮能力",
            "--dry-run",
        )
        self.assertEqual(0, result, stderr)
        self.assertIn("CREATE harness", stdout)
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())

        result, stdout, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "首轮能力",
        )
        self.assertEqual(0, result, stderr)
        bundle = self.root / "harness" / "iterations" / "001"
        self.assertEqual(
            {"README.md", "prd-001.md", "spec-001.md", "deviation-001.md"},
            {path.name for path in bundle.iterdir()},
        )
        prd = (bundle / "prd-001.md").read_text(encoding="utf-8")
        self.assertIn(f"- Git 基线：`{self.git('rev-parse', 'HEAD').stdout.strip()}`", prd)
        self.assertIn("- Git 分支：`refs/heads/main`", prd)
        self.assertEqual(
            self.git("rev-parse", "HEAD").stdout.strip(),
            self.git(
                "rev-parse",
                "refs/project-harness/iterations/001/base/refs/heads/main",
            ).stdout.strip(),
        )
        root_readme = (self.root / "harness" / "README.md").read_text(encoding="utf-8")
        self.assertIn("[001](iterations/001/README.md)", root_readme)
        progress = (self.root / "harness" / "progress.md").read_text(encoding="utf-8")
        self.assertRegex(progress, r"S-\d{8}-02 / OPEN")
        self.assertIn("VALID:", stdout)

    def test_new_iteration_rejects_detached_head(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.git("checkout", "--detach")

        result, _, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Detached",
        )

        self.assertEqual(2, result)
        self.assertIn("Detached HEAD", stderr)
        self.assertFalse((self.root / "harness" / "iterations" / "001").exists())

    def test_base_anchor_does_not_execute_reference_transaction_hook(self) -> None:
        self.assertEqual(0, self.init()[0])
        sentinel = self.root / "base-ref-hook-ran.txt"
        hook = self.root / ".git" / "hooks" / "reference-transaction"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(
            f"#!/bin/sh\nprintf ran > '{sentinel.as_posix()}'\nexit 1\n",
            encoding="utf-8",
            newline="\n",
        )
        hook.chmod(0o755)

        result, _, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "One",
        )

        self.assertEqual(0, result, stderr)
        self.assertFalse(sentinel.exists())
        self.assertTrue((self.root / "harness" / "iterations" / "001").is_dir())

    def test_final_commit_rejects_switch_to_different_branch_at_same_baseline(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        self.git("switch", "-c", "other")

        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--dry-run",
        )

        self.assertEqual(2, result)
        self.assertIn("was created on", stderr)

    def test_second_iteration_waits_for_first_final_commit_then_uses_next_number(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        result, _, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Too early",
        )
        self.assertEqual(2, result)
        self.assertIn("Create iterations serially", stderr)
        self.assertFalse((self.root / "harness" / "iterations" / "002").exists())

        self.accept_iteration()
        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            *self.shared_iteration_includes(),
        )
        self.assertEqual(0, result, stderr)
        result, _, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Two",
        )
        self.assertEqual(0, result, stderr)
        self.assertTrue((self.root / "harness" / "iterations" / "002" / "prd-002.md").is_file())

    def test_acceptance_status_rejects_unresolved_as_built_deviation(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        deviation = self.root / "harness" / "iterations" / "001" / "deviation-001.md"
        text = deviation.read_text(encoding="utf-8")
        text = text.replace("当前开放偏差：`0`", "当前开放偏差：`1`", 1)
        text += (
            "\n### DEV-001-001：Observed mismatch\n\n"
            "- 状态：开放\n"
            "- 原批准内容：Promise A\n"
            "- as-built 事实：Behavior B\n"
        )
        deviation.write_text(text, encoding="utf-8")
        l1 = self.root / "harness" / "iterations" / "001" / "README.md"
        l1.write_text(l1.read_text(encoding="utf-8").replace("- 开放偏差：`0`", "- 开放偏差：`1`"), encoding="utf-8")
        root_readme = self.root / "harness" / "README.md"
        root_readme.write_text(root_readme.read_text(encoding="utf-8").replace("| 已验收 | 已完成 | 0 |", "| 已验收 | 已完成 | 1 |"), encoding="utf-8")

        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("acceptance-open-deviation", stdout)

    def test_accepted_prd_rejects_future_or_uncertain_user_evidence(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        prd = self.root / "harness" / "iterations" / "001" / "prd-001.md"
        accepted = prd.read_text(encoding="utf-8")
        for evidence in (
            "待用户验收。",
            "用户可能通过验收。",
            "用户尚未验收。",
            "用户批准当前 PRD/SPEC 并要求实现。",
        ):
            with self.subTest(evidence=evidence):
                prd.write_text(
                    accepted.replace("用户明确回复验收通过。", evidence, 1),
                    encoding="utf-8",
                )
                result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
                self.assertEqual(1, result)
                self.assertIn("acceptance-evidence", stdout)
        prd.write_text(accepted, encoding="utf-8")

    def test_completed_iteration_rejects_template_placeholders_and_missing_baseline_approval(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        prd = self.root / "harness" / "iterations" / "001" / "prd-001.md"
        spec = self.root / "harness" / "iterations" / "001" / "spec-001.md"
        prd.write_text(
            prd.read_text(encoding="utf-8")
            .replace("- 批准依据：用户明确批准 PRD-001 产品基线。", "- 批准依据：尚无。", 1)
            .replace("### R-001-01：交付已实现行为", "### R-001-01：待定义", 1),
            encoding="utf-8",
        )
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(
                "- 当前批准基线：用户已批准的 PRD-001（R-001-01 / AC-001-01）。",
                "- 当前批准基线：尚无；等待 PRD-001 批准。",
                1,
            ),
            encoding="utf-8",
        )

        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))

        self.assertEqual(1, result)
        self.assertIn("prd-approval-evidence", stdout)
        self.assertIn("prd-template-placeholder", stdout)
        self.assertIn("spec-approved-baseline", stdout)

    def test_close_event_requires_exact_association_and_completed_verification(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        progress = self.root / "harness" / "progress.md"
        accepted = progress.read_text(encoding="utf-8")
        prefix, suffix = accepted.rsplit("- 关联：PRD-001 / SPEC-001\n", 1)
        progress.write_text(
            prefix + "- 关联：SPEC-001\n- 下一步：等待 PRD-001。\n" + suffix,
            encoding="utf-8",
        )
        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--dry-run",
        )
        self.assertEqual(2, result)
        self.assertIn("lacks a CLOSE event", stderr)

        for evidence in (
            "验证计划尚待执行。",
            "测试尚未运行，等待验证。",
            "没有运行任何测试，仅记录验证计划。",
        ):
            with self.subTest(evidence=evidence):
                progress.write_text(
                    accepted.replace("全部验收项通过，用户明确验收。", evidence, 1),
                    encoding="utf-8",
                )
                result, _, stderr = self.run_cli(
                    "commit-iteration",
                    "--project-root",
                    str(self.root),
                    "--number",
                    "001",
                    "--dry-run",
                )
                self.assertEqual(2, result)
                self.assertIn("lacks a CLOSE event", stderr)
        progress.write_text(accepted, encoding="utf-8")

    def test_malformed_deviation_heading_cannot_bypass_validation_or_commit_gate(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        deviation = self.root / "harness" / "iterations" / "001" / "deviation-001.md"
        base = deviation.read_text(encoding="utf-8")
        for heading in ("### DEV-001-001: ASCII colon", "### DEV-001-01：Short serial"):
            with self.subTest(heading=heading):
                deviation.write_text(
                    base
                    + f"\n{heading}\n\n"
                    + "- 状态：开放\n"
                    + "- 原批准内容：Promise A\n"
                    + "- as-built 事实：Behavior B\n",
                    encoding="utf-8",
                )
                result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
                self.assertEqual(1, result)
                self.assertIn("malformed-deviation-heading", stdout)
                result, stdout, _ = self.run_cli(
                    "commit-iteration",
                    "--project-root",
                    str(self.root),
                    "--number",
                    "001",
                    "--dry-run",
                )
                self.assertEqual(2, result)
                self.assertIn("malformed-deviation-heading", stdout)
        deviation.write_text(base, encoding="utf-8")

    def test_resolved_deviation_requires_disposition_evidence(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        deviation = self.root / "harness" / "iterations" / "001" / "deviation-001.md"
        deviation.write_text(
            deviation.read_text(encoding="utf-8")
            + self.complete_deviation_entry(evidence="", verification=""),
            encoding="utf-8",
        )
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("deviation-disposition-evidence", stdout)
        self.assertIn("deviation-verification", stdout)

    def test_as_built_deviation_before_completed_spec_is_rejected(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        deviation = self.root / "harness" / "iterations" / "001" / "deviation-001.md"
        deviation.write_text(
            deviation.read_text(encoding="utf-8")
            + self.complete_deviation_entry(),
            encoding="utf-8",
        )
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("deviation-before-completion", stdout)

    def test_fully_disposed_as_built_deviation_validates_after_completion(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        deviation = self.root / "harness" / "iterations" / "001" / "deviation-001.md"
        deviation.write_text(
            deviation.read_text(encoding="utf-8") + self.complete_deviation_entry(),
            encoding="utf-8",
        )
        result, stdout, stderr = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(0, result, stderr)
        self.assertIn("VALID:", stdout)

    def test_residual_and_transfer_deviations_require_status_specific_authority(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        deviation = self.root / "harness" / "iterations" / "001" / "deviation-001.md"
        base = deviation.read_text(encoding="utf-8")
        deviation.write_text(
            base
            + self.complete_deviation_entry(
                status="已接受残余",
                disposition="Residual behavior retained",
                evidence="Internal review only",
            ),
            encoding="utf-8",
        )
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("deviation-residual-acceptance", stdout)

        deviation.write_text(
            base
            + self.complete_deviation_entry(
                status="已转后续迭代",
                disposition="Transfer recorded without a receiving iteration",
                evidence="Tracking note exists",
            ),
            encoding="utf-8",
        )
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("deviation-transfer-target", stdout)

    def test_deviation_ledger_open_count_drift_is_detected(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        deviation = self.root / "harness" / "iterations" / "001" / "deviation-001.md"
        deviation.write_text(
            deviation.read_text(encoding="utf-8")
            + "\n### DEV-001-001：Open mismatch\n\n"
            + "- 状态：待处置\n"
            + "- 原批准内容：Promise A\n"
            + "- as-built 事实：Behavior B\n",
            encoding="utf-8",
        )
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("deviation-open-count-drift", stdout)

    def test_commit_iteration_requires_acceptance(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--dry-run",
        )
        self.assertEqual(2, result)
        self.assertIn("not explicitly accepted", stderr)
        self.assertEqual("1", self.git("rev-list", "--count", "HEAD").stdout.strip())

    def test_dirty_shared_control_requires_exact_explicit_include(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        principle = self.root / "harness" / "principle.md"
        principle.write_text(
            principle.read_text(encoding="utf-8") + "\nUnrelated concurrent principle proposal.\n",
            encoding="utf-8",
        )

        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            *self.shared_iteration_includes(),
            "--dry-run",
        )
        self.assertEqual(2, result)
        self.assertIn("harness/principle.md", stderr)
        self.assertIn("explicit --include", stderr)

        result, stdout, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            *self.shared_iteration_includes(),
            "--include",
            "harness/principle.md",
            "--dry-run",
        )
        self.assertEqual(0, result, stderr)
        self.assertIn('COMMIT_PATH "harness/principle.md"', stdout)

    def test_commit_iteration_dry_run_then_creates_one_exact_final_commit(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "Export")[0],
        )
        self.accept_iteration()
        feature = self.root / "src" / "feature.txt"
        feature.parent.mkdir(exist_ok=True)
        feature.write_text("implemented\n", encoding="utf-8")
        unrelated = self.root / "notes.tmp"
        unrelated.write_text("user note\n", encoding="utf-8")

        result, stdout, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--include",
            "src/feature.txt",
            *self.shared_iteration_includes(),
            "--dry-run",
        )
        self.assertEqual(0, result, stderr)
        self.assertIn('COMMIT_PATH "src/feature.txt"', stdout)
        self.assertIn('UNRELATED_UNSTAGED "notes.tmp"', stdout)
        self.assertEqual([], self.git("diff", "--cached", "--name-only").stdout.splitlines())

        result, stdout, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--include",
            "src/feature.txt",
            *self.shared_iteration_includes(),
        )
        self.assertEqual(0, result, stderr)
        self.assertIn("ITERATION_COMMIT", stdout)
        self.assertEqual("2", self.git("rev-list", "--count", "HEAD").stdout.strip())
        self.assertEqual(
            self.git("rev-parse", "HEAD").stdout.strip(),
            self.git("rev-parse", "refs/project-harness/iterations/001/final").stdout.strip(),
        )
        subject = self.git("log", "-1", "--format=%s").stdout.strip()
        self.assertIn("PRD-001", subject)
        committed = set(self.git("show", "--format=", "--name-only", "HEAD").stdout.splitlines())
        self.assertIn("src/feature.txt", committed)
        self.assertNotIn("notes.tmp", committed)
        self.assertTrue(unrelated.exists())

        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
        )
        self.assertEqual(2, result)
        self.assertIn("already has a recorded final commit", stderr)

    def test_commit_iteration_rejects_pre_staged_changes_without_altering_index(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        staged = self.root / "staged.txt"
        staged.write_text("keep staged\n", encoding="utf-8")
        self.git("add", "--", "staged.txt")
        before = self.git("diff", "--cached", "--name-only").stdout
        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
        )
        self.assertEqual(2, result)
        self.assertIn("index already contains staged changes", stderr)
        self.assertEqual(before, self.git("diff", "--cached", "--name-only").stdout)

    def test_commit_iteration_rejects_intent_to_add_without_altering_index(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        intent = self.root / "intent.txt"
        intent.write_text("keep intent\n", encoding="utf-8")
        self.git("add", "-N", "--", "intent.txt")
        before = self.git("ls-files", "--stage", "--", "intent.txt").stdout

        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--dry-run",
        )

        self.assertEqual(2, result)
        self.assertIn("intent-to-add", stderr)
        self.assertEqual(before, self.git("ls-files", "--stage", "--", "intent.txt").stdout)

    def test_manual_intermediate_iteration_commit_blocks_final_commit(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.git("add", "--", "harness")
        self.git("commit", "--no-gpg-sign", "-m", "manual intermediate PRD-001")
        self.accept_iteration()
        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
        )
        self.assertEqual(2, result)
        self.assertIn("Git HEAD advanced", stderr)

    def test_editing_prd_baseline_cannot_hide_an_intermediate_commit(self) -> None:
        self.assertEqual(0, self.init()[0])
        original_base = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        product = self.root / "product.txt"
        product.write_text("intermediate\n", encoding="utf-8")
        self.git("add", "--", "product.txt")
        self.git("commit", "--no-gpg-sign", "-m", "intermediate product commit")
        intermediate = self.git("rev-parse", "HEAD").stdout.strip()
        prd = self.root / "harness" / "iterations" / "001" / "prd-001.md"
        prd.write_text(
            prd.read_text(encoding="utf-8").replace(original_base, intermediate, 1),
            encoding="utf-8",
        )
        self.accept_iteration()

        result, stdout, _ = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--dry-run",
        )

        self.assertEqual(2, result)
        self.assertIn("iteration-base-anchor-drift", stdout)
        self.assertEqual(
            original_base,
            self.git(
                "rev-parse",
                "refs/project-harness/iterations/001/base/refs/heads/main",
            ).stdout.strip(),
        )

    def test_product_only_intermediate_commit_then_reset_is_detected_from_reflog(self) -> None:
        self.assertEqual(0, self.init()[0])
        base_commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        feature = self.root / "src" / "feature.txt"
        feature.parent.mkdir(exist_ok=True)
        feature.write_text("intermediate\n", encoding="utf-8")
        self.git("add", "--", "src/feature.txt")
        self.git("commit", "--no-gpg-sign", "-m", "product-only intermediate")
        self.git("reset", "--mixed", base_commit)
        self.accept_iteration()

        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--include",
            "src/feature.txt",
            *self.shared_iteration_includes(),
            "--dry-run",
        )

        self.assertEqual(2, result)
        self.assertIn("candidate paths appear in commits outside", stderr)
        self.assertIn("src/feature.txt", stderr)

    def test_final_marker_survives_reset_and_blocks_second_final_commit(self) -> None:
        self.assertEqual(0, self.init()[0])
        base_commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            *self.shared_iteration_includes(),
        )
        self.assertEqual(0, result, stderr)
        final_commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("reset", "--mixed", base_commit)

        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            "--dry-run",
        )

        self.assertEqual(2, result)
        self.assertIn("already has a recorded final commit", stderr)
        self.assertEqual(
            final_commit,
            self.git("rev-parse", "refs/project-harness/iterations/001/final").stdout.strip(),
        )

    def test_final_commit_does_not_execute_repository_hooks(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        sentinel = self.root / "hook-ran.txt"
        reference_sentinel = self.root / "reference-hook-ran.txt"
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(
            f"#!/bin/sh\nprintf ran > '{sentinel.as_posix()}'\nexit 1\n",
            encoding="utf-8",
            newline="\n",
        )
        hook.chmod(0o755)
        reference_hook = self.root / ".git" / "hooks" / "reference-transaction"
        reference_hook.write_text(
            f"#!/bin/sh\nprintf ran > '{reference_sentinel.as_posix()}'\nexit 1\n",
            encoding="utf-8",
            newline="\n",
        )
        reference_hook.chmod(0o755)

        result, _, stderr = self.run_cli(
            "commit-iteration",
            "--project-root",
            str(self.root),
            "--number",
            "001",
            *self.shared_iteration_includes(),
        )

        self.assertEqual(0, result, stderr)
        self.assertFalse(sentinel.exists())
        self.assertFalse(reference_sentinel.exists())
        self.assertEqual("2", self.git("rev-list", "--count", "HEAD").stdout.strip())

    def test_commit_object_failure_restores_empty_index(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        self.accept_iteration()
        real_run_git = project_harness.run_git

        def fail_commit_tree(git: str, root: Path, arguments: list[str], **kwargs: object):
            if "commit-tree" in arguments:
                raise project_harness.HarnessError("forced commit-tree failure")
            return real_run_git(git, root, arguments, **kwargs)

        with mock.patch.object(project_harness, "run_git", side_effect=fail_commit_tree):
            result, _, stderr = self.run_cli(
                "commit-iteration",
                "--project-root",
                str(self.root),
                "--number",
                "001",
                *self.shared_iteration_includes(),
            )
        self.assertEqual(2, result)
        self.assertIn("forced commit-tree failure", stderr)
        self.assertEqual("", self.git("diff", "--cached", "--name-only").stdout)
        self.assertEqual("1", self.git("rev-list", "--count", "HEAD").stdout.strip())

    def test_incomplete_bundle_blocks_new_number(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        (self.root / "harness" / "iterations" / "001" / "spec-001.md").unlink()
        result, stdout, stderr = self.run_cli(
            "new-iteration",
            "--project-root",
            str(self.root),
            "--title",
            "Must Not Allocate",
        )
        self.assertEqual(2, result)
        self.assertIn("incomplete-bundle", stdout)
        self.assertIn("Repair validation errors", stderr)
        self.assertFalse((self.root / "harness" / "iterations" / "002").exists())

    def test_validator_detects_derived_status_drift(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        prd = self.root / "harness" / "iterations" / "001" / "prd-001.md"
        text = prd.read_text(encoding="utf-8").replace("- 状态：`草案`", "- 状态：`已批准`", 1)
        prd.write_text(text, encoding="utf-8")
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("l1-prd-drift", stdout)
        self.assertIn("l0-prd-drift", stdout)
        self.assertIn("progress-index-drift", stdout)

    def test_validator_detects_broken_relative_link(self) -> None:
        self.assertEqual(0, self.init()[0])
        readme = self.root / "harness" / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\n[broken](missing.md)\n",
            encoding="utf-8",
        )
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("broken-link", stdout)

    def test_validator_detects_duplicate_registry_row(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        readme = self.root / "harness" / "README.md"
        text = readme.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| [001]"))
        text = text.replace(project_harness.ITERATIONS_END, row + "\n" + project_harness.ITERATIONS_END)
        readme.write_text(text, encoding="utf-8")
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("l0-registration-count", stdout)

    def test_validator_detects_cross_iteration_deviation_id(self) -> None:
        self.assertEqual(0, self.init()[0])
        self.assertEqual(
            0,
            self.run_cli("new-iteration", "--project-root", str(self.root), "--title", "One")[0],
        )
        deviation = self.root / "harness" / "iterations" / "001" / "deviation-001.md"
        deviation.write_text(
            deviation.read_text(encoding="utf-8")
            + "\n### DEV-002-001：Wrong owner\n\n- 状态：`开放`\n",
            encoding="utf-8",
        )
        result, stdout, _ = self.run_cli("validate", "--project-root", str(self.root))
        self.assertEqual(1, result)
        self.assertIn("deviation-prefix", stdout)

    def test_validate_json_is_machine_readable(self) -> None:
        self.assertEqual(0, self.init()[0])
        result, stdout, stderr = self.run_cli(
            "validate",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(0, result, stderr)
        payload = json_loads(stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(str(self.root), payload["project_root"])


def json_loads(value: str) -> dict[str, object]:
    import json

    return json.loads(value)


if __name__ == "__main__":
    unittest.main()
