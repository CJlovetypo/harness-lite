<!-- managed-by: harness-lite v1 -->
# 迭代 001：并行 PRD 编排与全局治理无感化

> 本页是 L1 派生摘要，只用于路由与状态恢复；PRD、SPEC、deviation 和 progress 事件才承载相应事实。

## 状态卡

- 迭代：`001`
- PRD 状态：`已批准`
- SPEC 状态：`已批准`
- 开放偏差：`0`
- 摘要更新时间：`2026-08-11`

## 本轮目标

在全局 principle 与 progress 控制下，为 Harness Lite 建立自动需求路由、懒 worktree、多 PRD 并行、透明 Git 操作、merge train 和可恢复编排的产品与技术基线。

## 当前结果

已批准 14 项需求、16 项验收标准、P-001～P-003 全局原则和 SPEC 技术基线；6 个开放问题均已裁决，用户已明确授权开始实施。Checkpoint 1 已保存前序三路起草改动，hash 为 `6cc0104075b5394a3ed6c6933b59817832503aeb`，未 push。

仓库在本轮创建前已有 11 个已跟踪修改文件和未跟踪 `evals/`。用户裁决这些三路起草分类改动先形成独立本地提交；提交完成后才开始 PRD-001 新实现。

## 最近进展

| 日期 | 事件 | 摘要 |
|---|---|---|
| 2026-08-11 | `S-20260811-02 / OPEN` | 创建迭代 001 四件套，进入 PRD 起草 |
| 2026-08-11 | `S-20260811-03 / CHECKPOINT` | 完成 PRD/SPEC 草案；等待原则、开放问题与基线审阅 |
| 2026-08-12 | `S-20260812-01 / DECISION` | 批准原则与 PRD/SPEC，裁决开放问题并授权实施 |
| 2026-08-12 | `S-20260812-02 / CHECKPOINT` | 前序 drafting-path 改动已提交；记录 lifecycle-v2 bootstrap 过渡 |

## 开放事项与下一步

- 开放偏差：无。
- 提交 PRD-001 治理基线，范围仅限 `AGENTS.md` 与 `harness/`；完成后报告 hash 与未 push 状态。
- 随后把 PRD/SPEC 状态切换为实施中，先实现 v2 identity/ref/journal/status 最小闭环。
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
