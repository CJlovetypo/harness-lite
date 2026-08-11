# Harness Lite

[English](README.md) | [简体中文](README.zh-CN.md)

Harness Lite 是一个面向 Codex 的轻量级 Git 项目治理 Skill。它把产品意图、实施设计、实际偏差、决策和验收证据连接起来，同时避免把每项工作都变成繁重流程。

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

每个编号迭代都把 PRD、SPEC、如实记录实施结果的偏差台账和路由摘要集中在同一目录中。

## 核心保证

- 初始化基础结构时不会创建虚假的产品迭代。
- 无 Git 的项目会在 `main` 上得到一个经过审阅的基线提交；dry-run 清单通过 `BASELINE_PLAN_TOKEN` 与实际执行绑定。
- 已有 Git 仓库初始化时不会暂存或提交当前改动。
- 起草路径会随需求调整但不降低治理门槛：存在实质性歧义时定向 grill 用户，只有小且明确的改动可联合起草 PRD/SPEC，明确但较大的改动仍由 PRD 先行。
- deviation 只记录实施完成后的 as-built 事实与已批准 PRD/SPEC 之间的差异，不提供批准或实施授权。
- 只有用户明确验收已完成结果后，迭代才能获得唯一的最终提交。
- 无关改动、被忽略的治理文件、密钥、超大文件、畸形四件套、含糊证据和中间提交都会被阻断。
- Harness Lite 永远不会自动推送。

## 安装

将本仓库以 `harness-lite` 为目录名放入 Codex Skills 目录：

```text
<CODEX_HOME>/skills/harness-lite
```

然后使用以下方式调用：

```text
$harness-lite
```

示例提示词：

```text
使用 $harness-lite 在当前项目中初始化轻量级 PRD/SPEC 治理结构。
```

### 升级已有受管项目

安装新版 Skill 后，新的 `$harness-lite` 调用会立即遵循新版行为；但 `init` 会刻意保留已有且非空的 `AGENTS.md` 受管区块。若要把三路起草策略等新版控制规则持久化到旧项目，应先审阅差异，只替换带边界标记的 Harness Lite 区块，并完整保留区块外的项目指令。

## CLI

通常由 Skill 自动调用内置 CLI。如需直接查看命令：

```text
python scripts/project_harness.py --help
python scripts/project_harness.py init --help
python scripts/project_harness.py new-iteration --help
python scripts/project_harness.py validate --help
python scripts/project_harness.py commit-iteration --help
```

主要工作流：

1. 运行 `init --dry-run`，审阅每个计划路径和哈希。
2. 初始化全局 Harness，不虚构产品迭代。
3. 只有出现具体产品目标时才创建编号迭代。
4. 检查项目上下文并选择起草路径：有未决产品问题则 grill，只有小且明确时联合起草，明确但不小则保持 PRD 先行。
5. 明确批准指定的 PRD/SPEC 基线，并单独授权实施；随后完成实施并核对 as-built 事实。
6. 校验证据并取得用户明确验收。
7. 预览并创建该迭代唯一的最终提交。

操作说明参见 [SKILL.md](SKILL.md)，文档模型和生命周期规则参见 [Harness contract](references/harness-contract.md)。

## 测试

```text
python -m unittest discover -s scripts/tests -v
```

测试套件覆盖初始化安全、Git 状态保留、路径边界、密钥和文件大小门禁、偏差校验、验收证据、确定性最终提交与回滚行为。

[`evals/evals.json`](evals/evals.json) 中的 agent 行为场景覆盖小且明确的联合起草路径、存在歧义的 grill 路径，以及明确但不小的 PRD 先行路径。
