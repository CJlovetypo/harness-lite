<!-- managed-by: harness-lite v1 -->
# 迭代 001：并行 PRD 编排与全局治理无感化

> 本页是 L1 派生摘要，只用于路由与状态恢复；PRD、SPEC、deviation 和 progress 事件才承载相应事实。

## 状态卡

- 迭代：`001`
- PRD 状态：`实施中`
- SPEC 状态：`实施中`
- 开放偏差：`0`
- 摘要更新时间：`2026-08-12`

## 本轮目标

在全局 principle 与 progress 控制下，为 Harness Lite 建立自动需求路由、懒 worktree、多 PRD 并行、透明 Git 操作、merge train 和可恢复编排的产品与技术基线。

## 当前结果

已批准 14 项需求、16 项验收标准、P-001～P-003 全局原则和 SPEC 技术基线；6 个开放问题均已裁决。Checkpoint `6cc0104`、`2d1be71`、`ca8223b`、`721c291` 与 `91c92a4` 均已本地创建且未 push。现已完成 validator 单一语义核心、authority coordinator、v2 bundle、Local/Worktree/B-first 编排、governance durable apply，以及候选/集成纯 evidence gate；尚未完成真实 merge train 与最终 candidate/final。

仓库在本轮创建前已有 11 个已跟踪修改文件和未跟踪 `evals/`。用户裁决这些三路起草分类改动先形成独立本地提交；提交完成后才开始 PRD-001 新实现。

## 最近进展

| 日期 | 事件 | 摘要 |
|---|---|---|
| 2026-08-11 | `S-20260811-02 / OPEN` | 创建迭代 001 四件套，进入 PRD 起草 |
| 2026-08-11 | `S-20260811-03 / CHECKPOINT` | 完成 PRD/SPEC 草案；等待原则、开放问题与基线审阅 |
| 2026-08-12 | `S-20260812-01 / DECISION` | 批准原则与 PRD/SPEC，裁决开放问题并授权实施 |
| 2026-08-12 | `S-20260812-02 / CHECKPOINT` | 前序 drafting-path 改动已提交；记录 lifecycle-v2 bootstrap 过渡 |
| 2026-08-12 | `S-20260812-03 / CHECKPOINT` | 治理 checkpoint 已发布；开始 v2 identity/ref/journal/status 实现 |
| 2026-08-12 | `S-20260812-04 / CHECKPOINT` | v2 identity/reservation 首切片已提交；后续 checkpoint 获 standing authorization |
| 2026-08-12 | `S-20260812-05 / DECISION` | 非最终 WIP checkpoint 可在精确复核后自主提交并报告；最终产物仍由用户校核 |
| 2026-08-12 | `S-20260812-06 / CHECKPOINT` | 治理/candidate/upgrade/UX 纯门禁 81 项回归通过，形成自主非最终 checkpoint |
| 2026-08-12 | `S-20260812-07 / CHECKPOINT` | validator/workspace/authority/v2 bundle/reconcile apply 交叉回归通过，形成执行底座 checkpoint |

## 开放事项与下一步

- 开放偏差：无。
- 提交已验证的 validator/workspace/authority/v2 bundle/governance apply 执行底座；排除 train 中间态和 pycache。
- 完成真实 candidate ref、integration worktree/default no-ff、显式 commit/main CAS 的 train adapter，并接入主 CLI。
- 当前只有一个活跃 PRD，保持 Local；不创建 worktree，不 push。

## 文档地图与按需阅读

| 任务 | 必读文档 |
|---|---|
| 目标、范围、验收 | [`prd-001.md`](prd-001.md) |
| 架构、实现、测试、迁移 | PRD + [`spec-001.md`](spec-001.md) |
| 风险、批准或实现前已知变化 | PRD + SPEC；变化先修订并重新批准基线 |
| 实现后的事实偏差或验收阻塞 | [`deviation-001.md`](deviation-001.md) + 被引用条款 |
| 长期取舍 | [`../../principle.md`](../../principle.md) + PRD |
| 决策证据 | 在 [`../../progress.md`](../../progress.md) 中搜索 `S-20260811-02` 或 `001` |
