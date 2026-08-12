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

已批准 14 项需求、16 项验收标准、P-001～P-003 全局原则和 SPEC 技术基线；6 个开放问题均已裁决。Checkpoint 1 `6cc0104`、治理 checkpoint 2 `2d1be71` 与 v2 identity/reservation checkpoint `ca8223b` 均已本地创建且未 push。首切片已完成只读 plan/status、显式 digest 接受、原子 reservation、崩溃恢复、legacy 阻断和 committed-main 治理树校验，尚未形成 candidate/final。

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

## 开放事项与下一步

- 开放偏差：无。
- 下一切片统一 worktree/commit-tree validator 语义核心，补齐 v2/legacy anchor compatibility 与 governance status 重算。
- 并行实现 Local/worktree orchestrator 与 principle/progress/README reconciler 的隔离模块；各切片验证后自主 checkpoint 并报告，均不 push。
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
