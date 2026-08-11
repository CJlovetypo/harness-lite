<!-- managed-by: harness-lite v1 -->
# Harness Lite 全局进度与决策记录

## 记录协议

- 本文件是跨迭代的追加式审计档案。当前状态与读取路由先看 [`README.md`](README.md)，再用事件 ID、迭代 ID 或日期定向读取本文件。
- 历史事件只追加；需要纠错时追加新事件，不把旧记录改成仿佛当时已经知道。
- 只记录有助恢复协作上下文的结论、公开依据、执行、验证和下一步；不记录隐藏推理、逐字聊天、token、密钥或工具噪声。
- 会话 ID 使用 `S-YYYYMMDD-NN`；事件类型使用 `OPEN`、`DECISION`、`CHECKPOINT`、`MERGE`、`CLOSE`。

## 当前迭代索引

| 迭代 | PRD 状态 | SPEC 状态 | 下一步 |
|---|---|---|---|
<!-- project-harness:progress-index:start -->
| [001](iterations/001/README.md) | 已批准 | 已批准 | 提交治理 checkpoint 2 后开始实现 |
<!-- project-harness:progress-index:end -->

## 事件

## S-20260811-01 / OPEN / 2026-08-11T23:24:33+08:00

- 关联：Harness bootstrap
- 会话背景：目标项目尚未建立本 Harness。
- 用户目标：初始化轻量、可追溯的项目治理结构。
- 决策与依据：创建全局路由、原则、追加式日志与迭代目录；不虚构产品迭代或项目原则。目标无 Git 时允许初始化并创建一次 bootstrap baseline commit；已有 Git 时初始化不提交。
- 执行与变更：初始化 `AGENTS.md` managed block 与 `harness/` 基础结构。
- 验证证据：由初始化后的结构校验记录。
- 关联偏差：无。
- 未决问题与下一步：确认项目原则；出现明确产品目标时创建首轮迭代。

## S-20260811-01 / CLOSE / 2026-08-11T23:24:33+08:00

- 关联：Harness bootstrap
- 会话背景：完成基础结构初始化。
- 用户目标：同 OPEN。
- 决策与依据：未创建虚假的产品迭代；Git 仅使用适用的 bootstrap 例外，产品迭代只在用户明确验收后创建一个最终提交。
- 执行与变更：完成基础文档与控制入口。
- 验证证据：运行 `project_harness.py validate`。
- 关联偏差：无。
- 未决问题与下一步：定义原则或创建首轮 PRD/SPEC 四件套。

## S-20260811-02 / OPEN / 2026-08-11T23:24:50+08:00

- 关联：PRD-001 / SPEC-001
- 会话背景：出现新的产品目标“并行 PRD 编排与全局治理无感化”。
- 用户目标：建立本轮产品范围、验收与实施基线。
- 决策与依据：分配下一单调编号 001，一次性创建同号四件套；当前仅为草案，不自动授权实施。
- 执行与变更：创建 `harness/iterations/001/` 并更新 L0 与全局索引。
- 验证证据：运行 `project_harness.py validate`。
- 关联偏差：无。
- 未决问题与下一步：完成并批准 PRD-001。

## S-20260811-03 / CHECKPOINT / 2026-08-11T23:29:59+08:00

- 关联：PRD-001 / SPEC-001
- 会话背景：用户要求汇总多 PRD 并行、懒 worktree、无感交互、Git 透明、principle 全局产品控制和 progress 过程控制的完整讨论。
- 用户目标：形成新一轮 Harness Lite PRD/SPEC 供审阅，同时确保自动化不偏离总体原则且迭代过程有据可循。
- 决策与依据：形成 14 项产品需求、16 项验收标准、3 条待批准长期原则和同号 SPEC 草案；治理路径、执行拓扑和授权状态分离；principle 保持单一全局权威，progress 采用不可变事件语义合并，README 从权威源重建。
- 执行与变更：完成 `prd-001.md`、`spec-001.md` 和 L0/L1 路由更新；保留 `principle.md` 现状，未把候选原则伪装成已批准原则。
- Git 透明：本轮未创建 worktree、未 commit、未 push；仅创建本地 iteration base anchor `refs/project-harness/iterations/001/base/refs/heads/main` 指向 `7376803cffb09269bc8a03346901b2e9e224d704`。
- 验证证据：待运行 `project_harness.py validate` 并检查 Git diff、文档链接和模板占位符。
- 关联偏差：无；尚未实施，不创建 as-built deviation。
- 未决问题与下一步：审阅 P-001～P-003、OQ-001-01～OQ-001-06 和 SPEC 技术基线；裁决既有 dirty 改动归属；在原则、PRD、SPEC批准和实施授权前不进入实现。

## S-20260812-01 / DECISION / 2026-08-12T00:00:09+08:00

- 关联：P-001～P-003 / PRD-001 / SPEC-001
- 会话背景：用户完成 OQ-001-01～OQ-001-06 的逐项裁决，并要求基于澄清开始迭代。
- 用户目标：允许恢复性 checkpoint commit，采用推荐的 integrated candidate 验收和首版不 push 策略，固定默认 `merge --no-ff`，批准全局原则，并先提交既有改动后实施。
- 决策与依据：允许明确通知/确认后的 WIP checkpoint，但其不取得治理权威；机器完成 feature gate，用户确认 exact latest-main integrated candidate；默认 `merge --no-ff`，其他策略改变 candidate identity 时必须重新生成 integrated candidate、重验并重绑证据；本轮仍不实现 push；P-001～P-003、PRD-001、SPEC-001 获批。
- 实施授权：用户明确要求“基于上面的澄清，开始进行迭代”。
- 执行与变更：已把 P-001～P-003 写入全局 `principle.md`，同步 PRD/SPEC 批准依据与本页路由；尚未开始产品代码实现。
- Git 透明：当前只有一个活跃 PRD，继续使用 Local，不创建 worktree；按 OQ-001-06，下一步先形成既有改动 checkpoint commit，每次 commit 均须展示范围和验证，且不 push。
- 验证证据：待在提交前运行现有测试、Harness validator、diff/secret/路径检查。
- 关联偏差：无；当前是实施前基线裁决。
- 未决问题与下一步：无产品开放问题。先完成两类内容的明确本地提交，再进入 PRD-001 实现。

## S-20260812-02 / CHECKPOINT / 2026-08-12T00:10:00+08:00

- 关联：PRD-001 / SPEC-001 / checkpoint 1
- 会话背景：按 OQ-001-06，在 PRD-001 新实现前先保存提交前已存在的三路起草分类改动；同时复核现行 legacy finalizer 与获批 WIP checkpoint 策略的过渡边界。
- 用户目标：先提交既有改动，再开始迭代；所有 commit 透明且不 push。
- 决策与依据：checkpoint 1 与治理 checkpoint 2 是 OQ-001-01/OQ-001-06 已授权的一次性 lifecycle-v2 bootstrap 恢复点，不是 candidate/integrated/final 权威；保留旧 base anchor，PRD-001 不使用 legacy finalizer，后续 checkpoint 继续逐次确认。
- 执行与变更：完整运行 70 项单元测试并通过；精确暂存 12 条 pre-PRD 路径，创建本地 commit `6cc0104075b5394a3ed6c6933b59817832503aeb`；同步 `AGENTS.md` 与 SPEC bootstrap 过渡说明。
- Git 透明：checkpoint 1 message 为 `checkpoint: preserve pre-PRD drafting-path changes`，294 additions / 37 deletions；明确排除 `AGENTS.md`、`harness/` 与两个 pycache；未 push，旧 base anchor 仍为 `7376803cffb09269bc8a03346901b2e9e224d704`。
- 验证证据：70 tests passed；本地与安装版 Harness validator 均为 0 errors；staged `git diff --check` 通过。
- 关联偏差：无；这是实施前过渡记录，不是 as-built deviation。
- 未决问题与下一步：验证并提交 checkpoint 2（仅 `AGENTS.md` 与 `harness/`），排除 pycache；然后进入实施中。
