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
4. 批准 PRD、授权 SPEC、完成实施，并核对 as-built 事实。
5. 校验证据并取得用户明确验收。
6. 预览并创建该迭代唯一的最终提交。

操作说明参见 [SKILL.md](SKILL.md)，文档模型和生命周期规则参见 [Harness contract](references/harness-contract.md)。

## 测试

```text
python -m unittest discover -s scripts/tests -v
```

测试套件覆盖初始化安全、Git 状态保留、路径边界、密钥和文件大小门禁、偏差校验、验收证据、确定性最终提交与回滚行为。
