# Harness Lite

[English](README.md) | [简体中文](README.zh-CN.md)

Harness Lite 是一个面向 Codex 的轻量级 Git 产品治理 Skill。用户只需用自然语言描述产品工作；Harness 会自动路由需求，只在并发真正需要时隔离各 PRD 的实现，并保留从全局原则到最终进入 main 的精确结果之间的完整证据链。

## 用户实际体验

用户通常始终停留在同一个 Codex 项目里，以需求而不是路径或 Git 命令来工作：

1. Harness 检查仓库，并分别判断治理路径、执行拓扑和当前授权门禁。
2. 只有一个实施中的 PRD 时，直接在主工程 checkout 中以 **Local** 模式工作，不创建额外 linked worktree。
3. 第二个 PRD 成为 active writer 时，Harness 会先告知，再从其精确 committed implementation start 创建 sibling linked worktree。第一个 PRD 的 cwd、文件、index、untracked 状态和运行环境保持不变。
4. 第三个及后续 PRD 各自获得独立任务、worktree、writer lease 和运行时 namespace；依赖或独占资源也可能要求 stacked 或串行。
5. 并发数量下降后，幸存 PRD 原地完成，不会为了恢复单工作区形态而被搬迁。
6. 每个 PRD 独立通过 PRD/SPEC 批准、实施授权、验收证据、偏差处置和 feature candidate 门禁。
7. 单一 merge train 在精确 latest main 上重建治理、执行跨 PRD 验证，并把 exact integrated result 交给用户做最终确认。

只读、路由、验证和恢复尽量低噪声。worktree 创建/移除和 branch 绑定会在执行前后告知。每次 commit 都明确展示 exact scope、message、验证、排除项、结果 hash 和 `pushed=false`。本生命周期版本不实现 push。

## 治理模型

```text
committed 全局 principle
  -> 已批准 PRD
  -> 已批准 SPEC
  -> 已授权实现
  -> feature candidate evidence
  -> latest-main integrated evidence
  -> exact-result acceptance
```

- main 上 committed `harness/principle.md` 是所有 PRD/worktree 唯一的全局原则权威。principle drift 必须先完成影响审计，才能 candidate 或 integration。
- `harness/progress.md` 是不可改写的事件历史。并行分支按事件 ID 和 exact bytes 做 union；纠错与冲突解决通过追加事件完成。
- L0/L1 README 是派生路由，从权威文档、事件、refs 和受限本机事实中重建。
- deviation 只记录实施完成后的 as-built 事实，不批准范围、不授权实施，也不提供原则例外。
- 默认使用 `merge --no-ff` 集成。若项目声明的其他策略改变 candidate commit identity，Harness 必须生成新的 integrated candidate、重新验证并重新绑定证据。

## 生成的结构

```text
AGENTS.md
harness/
├── README.md
├── principle.md
├── progress.md
└── iterations/
    └── NNN/
        ├── README.md
        ├── prd-NNN.md
        ├── spec-NNN.md
        └── deviation-NNN.md
```

Lifecycle-v2 还会在 Git common directory 下保存可重建的本机 journal、lease、workspace 路由和证据 receipt。绝对 worktree 路径与本机运行时细节不会作为规范治理内容提交。

每个迭代绑定两个不同的基线：

- **allocation base**：用于预留 PRD 身份和证明 ancestry 的不可变基线；
- **implementation start**：实现真正开始时的精确 committed snapshot，通常晚于包含已批准治理四件套的提交。

## 核心保证

- 初始化不会创建虚假的产品迭代。无 Git 项目只会创建一次由 `BASELINE_PLAN_TOKEN` 绑定的已审阅 baseline commit；已有仓库初始化不会暂存或提交现有改动。
- 三轴独立判断避免用“需求很小”同时推断 worktree 策略或实施授权。
- 第一个 writer 使用 Local；只有 writer 2+ 才创建 linked worktree。新增 B 不会 commit、stash、复制或移动 dirty A。
- 每个 PRD 只有一个 writer lease，并在写入前校验 exact root/path/branch/base。worktree 是 checkout 隔离，不是权限或运行时沙箱。
- Candidate 与 integrated evidence 绑定精确 commit/tree、原则、依赖、验证和权威 receipt；只有 ref 不构成证据。
- Journal 与 compare-and-swap 让 ID 分配、worktree 创建、治理 reconciliation 和 main advance 可恢复，不重复 ID、事件、worktree 或 commit。
- 清理采取保守策略：dirty、staged、untracked、ignored、链接/junction、活动进程/lease 或未知状态一律保留并进入 reconcile。
- 不自动 stash/reset/clean/force，不隐藏 main 推进，也不提供 push 命令。

## 安装

将本仓库以 `harness-lite` 为目录名放入 Codex Skills 目录：

```text
<CODEX_HOME>/skills/harness-lite
```

然后使用 `$harness-lite` 调用，例如：

```text
使用 $harness-lite 治理这个产品变更。自动隔离并行 PRD，只向我展示产品决策和有意义的 Git 状态变化。
```

## CLI 与兼容性

通常由 Skill 自动编排内置工具。如需检查入口：

```text
python scripts/project_harness.py init --help
python scripts/project_harness.py validate --help
python scripts/harness_lifecycle.py status --help
python scripts/harness_lifecycle.py route --help
python scripts/harness_lifecycle.py plan-start --help
python scripts/harness_lifecycle.py start --help
python scripts/harness_upgrade.py --help
```

已完成的 legacy serial 迭代继续可读、可验证。升级使用 exact dry-run plan，保留旧 principle、事件、deviation 和 refs，只替换 `AGENTS.md` 的 bounded managed block。Legacy `new-iteration` 与 `commit-iteration` 仅作为未升级迭代的兼容工具；其“单 active iteration / 单 final commit”规则不适用于 lifecycle-v2。

操作说明参见 [SKILL.md](SKILL.md)，完整权威、拓扑、证据与恢复契约参见 [Harness contract](references/harness-contract.md)。

## 测试

```text
python -m unittest discover -s scripts/tests -v
```

测试覆盖初始化与 legacy 兼容、三轴路由、原子 ID 分配、Local/worktree 转换（包括 dirty-A 与 B-first）、principle/progress reconciliation、candidate/integrated evidence、merge-train identity、透明交互、并发与崩溃恢复。[`evals/evals.json`](evals/evals.json) 还覆盖相同的用户行为场景。
