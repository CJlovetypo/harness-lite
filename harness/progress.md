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
| [001](iterations/001/README.md) | 实施中 | 实施中 | 收束 validator/anchor compatibility 与并行编排切片 |
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

## S-20260812-03 / CHECKPOINT / 2026-08-12T00:13:14+08:00

- 关联：PRD-001 / SPEC-001 / checkpoint 2
- 会话背景：两次获准 lifecycle-v2 bootstrap checkpoint 已完成，实施前置门禁关闭。
- 用户目标：基于已批准原则、PRD、SPEC 和开放问题裁决开始迭代。
- 决策与依据：先实现身份与可恢复性底座，避免在没有 refs/journal/status 的情况下直接扩展 worktree 和共享治理合并。
- 执行与变更：治理 checkpoint 2 已创建为本地 commit `2d1be71`，范围为 `AGENTS.md` 与 8 个 `harness/` 文件；PRD/SPEC 状态切换为实施中。
- Git 透明：checkpoint 2 message 为 `checkpoint: establish PRD-001 governance baseline`，9 条路径、950 additions；未 push。当前继续使用 Local，不创建 worktree。
- 验证证据：70 tests passed；两套 validator 0 errors；checkpoint 2 staged diff check 通过；旧 base anchor 保持 `7376803cffb09269bc8a03346901b2e9e224d704`。
- 关联偏差：无。
- 未决问题与下一步：实现 v2 status、兼容 refs、operation journal 和原子 reservation；完成后执行结构、并发、恢复与 legacy 回归测试。

## S-20260812-04 / CHECKPOINT / 2026-08-12T08:16:05+08:00

- 关联：PRD-001 / SPEC-001 / v2 identity-reservation first slice
- 会话背景：在不提前创建 worktree、branch、governance bundle 或 candidate 的前提下，先建立多 PRD 编排所需的身份、CAS、journal、状态与治理基线底座。
- 用户目标：Harness 自动判断和编排，但任何新 worktree、commit、push 都必须明确告知；全局 principle 与过程 progress 始终保持权威。
- 决策与依据：实现基线必须来自显式允许 ref；全局治理基线固定读取 committed `refs/heads/main`，而不是调用者 cwd/worktree。plan 必须零写并绑定 digest，reserve 才能创建 common-dir journal 与 v2 allocation/base refs；Git transaction 同时验证 accepted base/governance refs。
- 执行与变更：新增版本化 JSON `status`、`plan reserve-iteration`、`reserve-iteration`；实现 v2/legacy ref inventory、原子 allocation/base reservation、operation lock/journal 幂等恢复、坏 journal/orphan/ref mismatch 阻断、旧写入口保护，以及 main commit tree 的 bounded object validation。plan/journal/allocation/status 共同绑定 governance commit/tree 与 principle SHA-256。
- Git 透明：本切片尚未创建 worktree、branch 或真实 v2 ref；尚未 commit、未 push。真实仓库仍只有旧 `PRD-001` base anchor，且只有当前 main worktree。
- 验证证据：新增并发/恢复黑盒 18/18 通过（106.864 秒，反方复跑 112.2 秒）；legacy 回归 70/70 通过（367.668 秒）；本地与安装版 validator 均为 0 errors / 0 warnings；无落盘语法编译与 `git diff --check` 通过。marker-only 损坏 main、caller-only 实现差异、source-ref 漂移均有固定回归。
- 关联偏差：无；当前是非 candidate 的实现 checkpoint，不改变已批准产品范围或验收标准。
- 未决问题与下一步：提交前需用户确认 exact scope/message；下一切片统一 worktree/commit-tree validator 纯语义核心，补齐 v2/legacy anchor compatibility，并让 status 重算 governance object 一致性后再进入 Local/worktree 编排。

## S-20260812-05 / DECISION / 2026-08-12T10:44:32+08:00

- 关联：PRD-001 / SPEC-001 / Git checkpoint authorization
- 会话背景：v2 identity/reservation 首切片已由用户确认并提交为本地 checkpoint `ca8223bbe9d214b8c05d294b70d852e3dc57ddec`，后续仍有多个可独立验证的实现切片。
- 用户目标：继续实施；checkpoint 由 Harness 自行提交，用户只校核最终完成产物。
- 决策与依据：协调器获得本轮后续非最终 WIP checkpoint 的 standing authorization。每次 checkpoint 仍必须先完成精确路径、staged tree、验证与排除项复核，提交后报告 hash、范围和 `pushed=false`；checkpoint 只提供恢复点，不成为 candidate/integrated/final 或验收权威。
- 执行与变更：更新项目内 bounded AGENTS transition、L0/L1、SPEC 修订记录与 progress 控制面；并行启动 validator/anchor compatibility、Local/worktree orchestrator、principle/progress/README reconciler 三个隔离切片。
- Git 透明：standing authorization 不包含 push、main integration、merge/rebase/cherry-pick、历史改写、破坏性 Git、candidate/final ref 或最终验收。任何新 worktree 仍须在执行前后明确通知。
- 验证证据：上一 checkpoint `ca8223b` 为 7 条路径、4069 additions / 127 deletions；新增 18/18 与 legacy 70/70 通过，两套 validator 0 errors / 0 warnings；该 commit 未 push，旧 base anchor 未改变。
- 关联偏差：无；仅调整本轮 checkpoint 交互门禁，不改变 PRD 产品范围、验收标准或全局 principle。
- 未决问题与下一步：各隔离切片完成后统一集成测试并自主创建非最终 checkpoint；最终 candidate/integrated 证据包提交用户校核。

## S-20260812-06 / CHECKPOINT / 2026-08-12T12:35:00+08:00

- 关联：PRD-001 / SPEC-001 / governance-candidate-upgrade-UX slice
- 会话背景：按三轴决策 checkpoint 之后的执行计划，先收束不直接执行 Git mutation 的治理合并、候选证据、升级预览和用户交互契约，避免与仍在修改的 validator/workspace adapter 混合提交。
- 用户目标：多 PRD 编排尽可能无感且鲁棒；principle 维持全局产品控制，progress 保留过程证据；worktree、commit、push 保持透明，最终只校核完整产物。
- 决策与依据：principle 使用三方 exact-approval gate，progress 使用不可变事件 union，README 只重建 managed 区；candidate/integrated/main-advance 分层绑定 evidence digest；默认 `merge --no-ff`，其他策略改变 identity 时必须生成新 integrated generation、显式重验和新证据；升级首步只提供零写 dry-run，不静默迁移 legacy；所有动作由版本化 Silent/Notify/Confirm envelope fail-closed。
- 执行与变更：新增 `harness_governance.py`、`harness_candidate.py`、`harness_upgrade.py`、`harness_ux.py` 及其隔离测试。本切片均为纯 plan/gate/preview，不创建 branch/worktree/ref，不 merge，不推进 main，不 commit implementation，不 push。
- Git 透明：本事件对应的非最终 checkpoint 由用户 standing authorization 覆盖；精确范围为上述 4 个模块、4 个测试和本轮治理路由记录。提交完成后报告 hash 与 `pushed=false`；pycache、validator/workspace 中间态均排除。
- 验证证据：decision/governance/candidate/upgrade/UX 合并回归 81/81 通过；governance 真实 `progress.md` 解析 9 events / 0 blockers；所有目标路径 `git diff --check` 通过；candidate identity-rebind 正反例、legacy upgrade 零写、worktree/commit/push 通知字段均有固定测试。
- 关联偏差：无；实现与已批准 R-001-01～R-001-05、R-001-10～R-001-14 / SPEC §4～§8、§11 一致。
- 未决问题与下一步：提交本 checkpoint 后，单独 checkpoint validator/anchor compatibility 与 Local/worktree 编排；补齐 B-first 原地 branch binding，再进入实际 candidate refs/merge-train adapter 和最终文档/eval 收束。
