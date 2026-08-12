<!-- managed-by: harness-lite v1 -->
# SPEC-001：并行 PRD 编排与全局治理无感化实施规格

## 文档元数据

- SPEC ID：`SPEC-001`
- 状态：`实施中`
- 创建日期：`2026-08-11`
- 对应需求：[`prd-001.md`](prd-001.md)
- 对应偏差：[`deviation-001.md`](deviation-001.md)
- 当前批准基线：用户于 2026-08-11 批准的 PRD-001（R-001-01～R-001-14 / AC-001-01～AC-001-16）及 P-001～P-003。
- 批准依据：用户于 2026-08-11 明确批准 SPEC-001 实施规格，逐项裁决 OQ-001-01～OQ-001-06，并要求按推荐方案和指定 `merge --no-ff` 策略开始迭代。
- 实施授权：用户已明确授权并要求开始实施 PRD-001 / SPEC-001；按其指令先提交既有三路起草改动，再开始本迭代新实现。

## 0. Lifecycle-v2 Bootstrap 过渡

OQ-001-01 与 OQ-001-06 已明确授权一次窄范围 bootstrap 过渡，以避免用尚未实现的 v2 journal/finalizer 反向阻塞 v2 自身：

1. Checkpoint 1 只保存 PRD-001 创建前已存在的三路起草分类改动；已创建为本地 commit `6cc0104075b5394a3ed6c6933b59817832503aeb`。
2. Checkpoint 2 只保存审阅后的 `AGENTS.md` 与 `harness/` 治理基线。
3. 两者都是 non-candidate、non-integrated、non-final 的恢复点，不取得验收权威，不 push。
4. 旧 base anchor `7376803cffb09269bc8a03346901b2e9e224d704` 保留为真实历史起点，不重指、不 amend、不 squash；PRD-001 不再使用 legacy `commit-iteration` finalizer。
5. v2 operation journal、lease 和 main-only-integrated 约束在相应实现切片可用并验证后，强制约束 Harness 编排 mutation。在此之前，新源码实现继续采用单 writer Local 与 exact checkpoint manifest；该 bootstrap 例外不授权 worktree、main integration、push、force 或破坏性操作。

本节只是把用户已批准的 WIP checkpoint 与“先提交后实施”裁决落实为可执行过渡，不改变 R-001-01～R-001-14、AC-001-01～AC-001-16 或最终验收门禁。

## 1. 架构与职责边界

### 1.1 Governance Core

负责 iteration 身份、PRD/SPEC/deviation 状态机、权威顺序、批准/验收门禁、验收 ID 证据和 principle baseline。它不直接执行 Git、运行时环境或产品冲突裁决。

### 1.2 Request Intake 与 Policy Engine

每次请求输出结构化 decision record，至少包含：

- `request_kind`：只读、既有 PRD 延续、新 PRD；
- `clarity`：decision-complete 或阻塞问题；
- `risk_vector`：用户行为、公共契约、schema/data、migration/rollback、security/privacy/permission、外部系统、不可逆性和兼容性；
- `governance_path`：grill、co-draft、PRD-first；
- `execution_topology`：Local、independent worktree、stacked、serialize；
- `authorization_state`：PRD、SPEC、实施、集成、验收；
- `reason_codes` 与被检查的 repo context。

硬风险规则优先于模型置信度。任何会改变范围或验收的 unknown 都升级门禁；分类不得写入批准字段。

### 1.3 Workspace Orchestrator

负责 Local/Parallel/Draining 状态机、worktree 路径规划、任务路由、writer lease、operation journal、资源 namespace、恢复和清理。外层 workspace 仅作本机路由容器，不是 Git 项目或规范事实源；linked worktree 使用 primary checkout 的 sibling/专用目录，不嵌套在项目仓库内部。

### 1.4 Git/Worktree Adapter

只接收显式 operation plan 和 expected refs，负责读取 dirty 分类、创建 branch/worktree、维护 Harness refs、构造 candidate/integration tree、compare-and-swap 更新和 Git 结果摘要。

该 adapter 禁止自动 stash/reset/clean/force，禁止重写已验收历史，禁止强删 dirty worktree，禁止把 commit 与 push 捆绑，禁止在没有精确授权时推进 main 或远端 ref。

### 1.5 Governance Reconciler

负责：

- principle 三方权威检查和开放 PRD impact audit；
- progress 事件验证、幂等 union 和 resolution gate；
- L0/L1 README、progress index 与状态卡的确定性重建；
- candidate/integrated/final 证据和 PRD/SPEC/deviation 的一致性验证。

### 1.6 Project Runtime Adapter

项目可声明 `setup-worktree`、`verify-candidate`、`verify-integration`、`teardown-worktree` 和资源 claim 适配器。Harness 只传入 PRD、operation 和 namespace，记录命令摘要、exit status、证据 hash 和产物位置；不内置 npm、venv、Docker、数据库或 secret 逻辑，也不自动创建外部资源。

### 1.7 Authorization 与 UX Layer

负责把动作映射为 `Silent / Notify / Confirm`，聚合业务动作通知并绑定精确 PRD、base、path、refs 和 manifest。它不得把“已通知”解释为“已批准”，也不得让 subagent 绕过协调者直接执行 Git 写操作。

## 2. 核心状态机与不变量

### 2.1 治理与授权状态

```text
CLASSIFIED
  -> GRILL_BLOCKED | PRD_DRAFT | PRD_SPEC_CODRAFT
  -> PRD_APPROVED
  -> SPEC_APPROVED
  -> IMPLEMENTATION_AUTHORIZED
  -> IMPLEMENTING
  -> CANDIDATE_VERIFIED
  -> INTEGRATION_PENDING
  -> INTEGRATED_VERIFIED
  -> AWAITING_ACCEPTANCE
  -> ACCEPTED
  -> CLOSED
```

状态可以因新风险、principle drift、main drift、依赖变化或验证失败向前置门禁退回，但不得通过直接编辑状态文本跳过批准证据。

### 2.2 Workspace 状态

```text
IDLE -> SINGLE_LOCAL -> PARALLEL -> DRAINING -> IDLE
                         |    ^
                         +----+  新 PRD 到达
```

- `IDLE`：无可写活跃 PRD，primary checkout 可回到 main。
- `SINGLE_LOCAL`：PRD-A 使用 primary checkout；默认保持当前 main 分支和 dirty tree，不额外创建 worktree/功能分支。
- `PARALLEL`：PRD-B 及之后各自使用 linked worktree；A 保持原地。此时 main branch 可能仍被 A 的 checkout 占用。
- `MAIN_RELEASE_REQUIRED`：只有当非 A 候选需要先集成时进入。验证 A 仍位于记录的 committed base、dirty/index 全部归 A、无 merge/rebase/conflict、目标 branch 不冲突后，先通知用户，再在原目录执行分支绑定；文件、cwd 和运行时不迁移。
- `DRAINING`：并发已下降但仍有 PRD 活跃；所有幸存者原地完成，不回迁。

“活跃可写 PRD”从实施授权生效、持有 writer lease 时开始，直到 integrated/final 结果已处理、lease 释放且工作区可清理为止。仅起草 PRD/SPEC 不触发 worktree。

### 2.3 不变量

1. 每个 PRD 有唯一 iteration ID、immutable base、principle base hash、事件 namespace 和最多一个 writer lease。
2. 每次 mutation 前校验 `(absolute root, worktree path, branch state, iteration, base, lease generation)`。
3. B 的 base 只能来自明确 committed main 或声明依赖的 stable candidate；不得取决于命令发起 cwd 的 dirty HEAD。
4. 第二 PRD 到来只新增，不移动 A；并发减少只等待，不搬迁 survivor。
5. main 只接收经 latest-main 集成验证并对 exact tree 获得授权的结果。
6. principle 是单一全局规范，progress 是不可变历史，README 是派生路由。
7. ignored/untracked/runtime 状态不得被 Git clean 状态掩盖或静默删除。

## 3. 持久化契约

### 3.1 规范事实与本机运行态

- `harness/` 继续是唯一可编辑的规范治理事实源。
- iteration bundle 保持 README、PRD、SPEC、deviation 四件套。
- 分支、base/candidate/integrated/final refs 提供可验证 Git 身份。
- worktree 绝对路径、线程 owner、heartbeat、端口和本机进程属于 Git common dir 下的 operational registry，不提交进规范文档。
- operational registry 必须能由 Git refs、`git worktree list`、iteration bundle 和 journal 重建，不能成为唯一事实源。

### 3.2 Git refs

新生命周期至少使用：

```text
refs/project-harness/v2/allocations/NNN
refs/project-harness/v2/iterations/NNN/base
refs/project-harness/v2/iterations/NNN/candidates/GGG
refs/project-harness/v2/iterations/NNN/integrated
refs/project-harness/v2/iterations/NNN/final
```

- allocation 与 ref 创建使用 `git update-ref` compare-and-swap 或原子 transaction，不再由各 worktree 独立扫描 `max+1`。
- `base` 创建后不可变。
- 每次 candidate 内容改变创建新 generation，不覆盖已被证据引用的 generation。
- `integrated` 绑定 latest-main 组合后的 exact tree/commit；`final` 绑定最终验收关闭身份。
- v2 使用独立顶层 namespace，避免与现有 `refs/project-harness/iterations/NNN/base/refs/heads/...` 的 file/directory ref namespace 冲突。legacy base/final refs 继续只读兼容，迁移不得改写，也不得尝试在 legacy `.../base` 前缀创建 direct ref。

### 3.3 Leases

本机状态至少包含：global allocation lease、global principle lease、main integration lease 和 per-iteration writer lease。lease 字段包括 scope、operation ID、owner/task、generation、expected root/branch/base、acquired_at 和 heartbeat。

lease takeover 不能仅凭 TTL 静默执行；必须结合 journal、进程和 worktree 状态验证。状态不明时保持旧对象并请求用户确认。

### 3.4 Operation Journal

每个 mutation workflow 使用唯一 operation ID，并原子记录：

```text
PLANNED
RESERVED
BRANCH_READY
WORKTREE_READY
GOVERNANCE_READY
RUNTIME_READY
VALIDATED
READY | FAILED_NEEDS_RECONCILE
```

journal 保存 dry-run manifest、expected refs、已创建对象、文件 hash、通知/授权 identity 和回滚资格。重试遇到匹配状态则继续，遇到同名异状态则停止；不得重复分配 ID、事件、branch、worktree 或 commit。

## 4. Principle 全局控制

1. main 上的 `principle.md` 内容与 snapshot hash 是唯一生效原则集。
2. PRD 创建时记录 principle base hash；feature worktree 中的 principle diff 仅为 proposal。
3. 只有持有 global principle lease、绑定稳定 change ID、精确 before/after 文本、用户批准证据和影响范围的操作可以推进原则变化。
4. 集成时执行 `branch base / latest main / branch candidate` 三方检查：
   - branch 无原则 diff：采用 latest main；
   - 有 diff但无精确批准或 lease：硬阻塞；
   - latest main 等于 branch base：应用精确批准 patch 后做语义审查；
   - latest main 已漂移：即使文本 hunks 不重叠，也展示最终组合文本并重新确认。
5. 原则生效后，所有 baseline 较旧的开放 PRD 标记 `principle-drift`。无影响追加 no-impact CHECKPOINT；有影响则修订/重批 PRD/SPEC并重新验证 candidate。
6. 原则不允许自动 union、latest-wins、merge-order-wins，也不能由 deviation 提供例外。

本轮若批准 PRD 中的 P-001/P-002/P-003，应先以单独、精确的 principle 变更写入 main，再作为本轮和后续 PRD 的 principle baseline。

## 5. Progress 过程控制

### 5.1 事件模型

session ID 与 event ID 分离。旧 `S-YYYYMMDD-NN` 原样保留；新事件使用全局唯一 `EV-<scope>-<ULID>` 或等价原子 ID，并至少包含：

- event ID、session ID、iteration/scope、type；
- `occurred_at`、source branch/base、operation ID；
- causal parent、批准/证据 refs、事实摘要；
- 可选 `corrects: EV-...`，用于追加纠错。

事件创建后不可修改或重编号。时间戳只用于展示，不决定权威或冲突胜负。

### 5.2 并行合并算法

1. 解析 branch base progress、branch candidate progress 和 latest-main progress。
2. 验证 branch 未修改其 base 已存在的事件 block。
3. 提取 main 尚不存在的新事件。
4. 同 ID 同 bytes 幂等去重；同 ID 不同 bytes 视为篡改/冲突并停止。
5. 按 branch 内因果顺序追加；main 文件物理顺序表示 integration order，事件字段保留真实 occurred_at。
6. 不同事件结论冲突时全部保留；涉及批准、验收或政策时保持 blocked，等待新的 DECISION/resolution 事件。
7. `progress-index` 不参与文本 merge，由 reconciler 从权威状态重建。

## 6. README 与状态视图

- L0/L1 branch 副本仅作本地 preview；integration 时从 latest main、已导入 progress、PRD/SPEC/deviation、principle 和 refs 重建 managed blocks。
- README 冲突不使用 ours/theirs，也不能覆盖权威状态。
- 用户手写区域必须与 managed blocks 物理分隔，只在手写区发生真实冲突时请求处理。
- 未合入 main 的 worktree 状态通过本机 `status --all-worktrees` 视图展示；不得为了让 main 看见活动任务而提交虚假的未来状态。

## 7. Git/Worktree 动作与用户交互

### 7.1 Silent

- 读取 L0/L1、repo/worktree/dirty/依赖扫描；
- 需求三轴分类及 reason codes；
- 验证、证据收集、README preview；
- 已授权 operation 的幂等重试；
- 只计算不创建的本地资源 namespace。

### 7.2 Notify：执行前与完成后

- 新建/移除 worktree；
- 安全创建 branch 或为 A 原地绑定 branch；
- candidate 失效、进入/退出 merge queue；
- 无歧义且完全由 manifest 管理的本地临时资源生命周期。

worktree 创建前通知至少展示 PRD、原因、base、branch、绝对路径、对 A 的影响和远端状态；完成后展示实际 path/ref/namespace。若 preflight 发现任何差异，动作升级为 Confirm 或 blocked。

### 7.3 Confirm

- PRD/SPEC 批准、实施授权、原则变化、偏差残余接受和最终验收；
- 所有 commit：先展示 exact paths/tree、message、branch、验证和排除项；完成后报告 hash 与未 push 状态；
- push：与 commit 分离，展示 remote、source/target ref、commit range 和 force=false，单独确认；
- 推进 main、merge/rebase/cherry-pick、删除 branch、强制/破坏性清理、lease takeover、外部/付费资源和共享数据迁移。

任何 force push、自动 stash/reset/clean 或验收后历史改写均不提供无感路径。

经用户批准，本轮允许显式确认后的 WIP checkpoint commit。checkpoint 仅提供恢复点，不改变 PRD/SPEC 状态，也不取得 candidate/integrated/final 权威；每次仍须展示 exact scope、message、验证和未 push 状态。

## 8. 依赖、Candidate 与 Merge Train

### 8.1 依赖模型

iteration 元数据增加 `depends_on`、`conflicts_with`、integration target、shared contract/schema、resource claims 和 touched-area hints。

- `independent`：从 latest committed main 创建。
- `stacked`：从依赖 PRD 的 stable candidate generation 创建，不得先于依赖集成。
- `must-serialize`：principle、不可兼容 schema、独占外部环境等设置全局 barrier。

### 8.2 Feature Candidate

形成 candidate 前校验 PRD/SPEC批准、实施授权、AC 证据、deviation disposition、dirty path ownership 和项目验证。candidate evidence 绑定 base、principle hash、tree/commit、included paths、测试结果与 generation。

### 8.3 Integration Candidate

从 latest main 创建临时 integration worktree，按依赖顺序：

1. 校验 candidate、base 和 main CAS；
2. 通过 principle gate；
3. 语义导入 progress；
4. 合并实现与 iteration bundle；
5. 重建 progress index、L1 和 L0；
6. 运行 cross-PRD/full verification；
7. 记录 exact integrated tree 和证据。

若需要修改产品代码，退出 integration lane，回到 owning PRD worktree 生成新 candidate；integration worktree 不承载临时修复。

### 8.4 Main Advance

用户确认 exact integrated result 后，使用 CAS 推进 main 和 integrated ref。若 main、principle、candidate 或验证证据已变化，确认失效并重新构造。main 推进后若 post-check 失败，不自动重写历史，走显式 revert 或 forward-fix。

默认集成策略为 `merge --no-ff`，以保留获验 candidate 的 ancestry。项目可以通过明确策略声明使用 squash、cherry-pick 或 rebase 类路径；凡 candidate commit identity 发生改变，原证据不得沿用，必须生成新的 integrated candidate、重新验证并重新绑定 evidence。

正常路径不要求用户单独批准 feature candidate；机器完成 feature gate 后进入 merge train，用户对 exact latest-main integrated candidate 的确认同时承担最终验收。任何 tree、main 或原则变化都会使该确认失效。

## 9. 文件、接口与数据契约

预计修改范围：

- `SKILL.md`：并行生命周期、三轴分类、通知/授权和 recovery 规则。
- `references/harness-contract.md`：authority、并行 iteration、refs、progress/principle reconciliation 和候选/集成验收契约。
- `assets/templates/*.tmpl`：PRD 依赖/principle base、并发事件、L0/L1 状态和 AGENTS 规则。
- `scripts/project_harness.py` 或拆分后的 core/git/workspace/reconcile 模块：结构化计划、原子 refs、leases、journal、worktree、status、candidate/integration 和 migration 命令。
- `scripts/tests/test_project_harness.py`：结构、状态机、并发、恢复、Git 通知与兼容测试。
- `evals/evals.json`：需求分类、1→N、B-first、principle drift、progress merge 和异常恢复的 agent 行为场景。
- `README.md` / `README.zh-CN.md`：用户体验、透明 Git 边界和升级说明。

本轮不实现 push 命令。Harness 继续在本地 commit/integration 边界结束；SPEC 只保留未来 push 接口必须单独展示 remote、source/target ref、commit range 和 force 状态并取得确认的契约。

所有 machine-readable 输出使用版本化 JSON schema，包含 operation ID、project root、iteration、action level、expected/actual refs、planned paths、warnings、blocking reasons 和 next gate。绝对 worktree path 只进入本机输出/registry，不进入规范性可提交文档。

## 10. 需求追踪

| PRD 需求 | 设计位置 | 主要验收 |
|---|---|---|
| R-001-01 | §4 Principle 全局控制 | AC-001-01 |
| R-001-02 | §1.1、§2.1、§8.2 | AC-001-02 |
| R-001-03 | §5 Progress 过程控制 | AC-001-03 |
| R-001-04 | §6 README 与状态视图 | AC-001-04 |
| R-001-05 | §1.2、§2.1 | AC-001-05 |
| R-001-06 | §2.2 Workspace 状态 | AC-001-06 |
| R-001-07 | §2.2、§7 Git/Worktree 交互 | AC-001-07～AC-001-10 |
| R-001-08 | §1.3、§1.6、§3.3 | AC-001-08、AC-001-11 |
| R-001-09 | §8.1 依赖模型 | AC-001-12 |
| R-001-10 | §8.2～§8.4 | AC-001-13 |
| R-001-11 | §7 动作分级 | AC-001-14 |
| R-001-12 | §3.2～§3.4、§12 | AC-001-15 |
| R-001-13 | §11 兼容与迁移 | AC-001-16 |
| R-001-14 | §1.7、§6、§7 | AC-001-04、AC-001-14 |

## 11. 兼容与迁移

1. 提供 `upgrade --dry-run`，识别 legacy serial Harness、existing base/final refs、活动 iteration、dirty/governance 状态，并输出精确 path/ref/hash 计划。
2. 已完成 legacy iteration 保持不变，只增加兼容读取，不重写历史事件或 refs。
3. 旧 progress `S-*` 事件原样保留；新事件启用新 ID。已存在撞号时生成显式 correction/alias 计划，不静默改号。
4. principle 内容不重写；计算初始 snapshot hash，后续变化使用 change ID。
5. 活跃 clean iteration 可以在用户确认后 adopt 新 refs/lease；活跃 dirty iteration 默认继续 legacy 到完成，或先形成可恢复 snapshot 再迁移。
6. 只替换 bounded AGENTS managed block，保留外部用户指令。
7. 迁移完成后必须通过结构、Git、事件、README 重建和历史兼容验证，才写入升级版本标记。

## 12. 回滚与故障恢复

- 所有 mutation 先 journal expected refs、planned objects 和 file hashes；文件写入使用临时文件与原子替换，refs 使用 CAS transaction。
- 回滚只删除本 operation 创建、仍与 manifest 匹配且 clean/empty 的对象；出现 dirty、untracked、ignored、活动 lease 或进程时保留并标记 `FAILED_NEEDS_RECONCILE`。
- source worktree 保留到 destination/integration tree 验证完成；cleanup 永远最后。
- runtime teardown 只处理带 operation/resource tag 的对象；外部资源需要独立授权。
- main 已推进后不自动 reset/rebase；失败使用显式 revert 或 forward-fix，并追加 progress 证据。
- principle 使用 supersede/change history，progress 使用 correction event；二者均不通过回滚改写历史。
- 可用 feature flag/兼容模式停止创建新并行工作区，但不得自动搬迁或删除已存在的活跃 worktree。

## 13. 风险与变更门禁

| 风险 | 影响 | 门禁/缓解 |
|---|---|---|
| false-small 或遗漏歧义 | 越权实现、遗漏迁移/权限风险 | 硬风险向量、unknown 升级、分类与授权分离 |
| Local dirty main 被占用 | B-first 无法安全集成 | 延迟 branch 绑定、严格 ownership preflight、integration worktree |
| ID/ref/lease 竞态 | 重复 PRD、双 writer、证据错绑 | coordinator lease、CAS refs、generation、journal |
| 崩溃造成半完成状态 | orphan、重复事件或误清理 | 幂等 reconcile、保守回滚、不自动 force |
| ignored/runtime 串扰 | 测试假阳性或数据丢失 | 完整 manifest、namespace、project adapter、清理门禁 |
| principle 并发变化 | 全局规范分裂 | global lease、exact approval、impact audit |
| progress 文本冲突 | 历史丢失或批准失真 | immutable event union、unique ID、resolution event |
| stale candidate | 组合回归 | latest-main integration candidate 与 full revalidation |
| merge/rebase 改变 identity | 验收证据失效 | generation/tree hash；变化后重绑并重验 |
| sandbox 覆盖整个 workspace | 误写其他 worktree | 每任务 writable root 优先；mutation preflight 兜底 |
| custom refs 不随普通 clone/push | 跨机器状态缺失 | 明确 refs 为本地证据；首版不声称跨机器同步 |
| 当前脏改动与新实现重叠 | 无法证明本轮范围 | 实施前先裁决 OQ-001-06并形成可恢复基线 |

任何实现前发现的新产品范围、授权模型、原则语义或不可逆行为必须修订并重新批准 PRD/SPEC，不得预填 deviation。

## 14. 验证计划

### 14.1 结构与权威

- iteration/ref/event IDs 唯一；base immutable；principle hash 与批准证据一致。
- PRD/SPEC/实施授权/验收组合状态合法；缺任一门禁不能候选化。
- progress 不可改写、README 可确定性重建、governance 文件不被 ignore。

### 14.2 分类与 UX

- 覆盖 small-clear、ambiguous-small、clear-high-impact、continuation 和 read-only 场景。
- 覆盖每类动作的 Silent/Notify/Confirm 映射；扫描全部 Git 写入口，拒绝未分类动作。
- 快照测试 worktree、commit、push、principle 和验收卡片的必要字段与低噪声摘要。

### 14.3 Worktree 与依赖

- 覆盖 0→1、1→2、2→3、3→1、1→0，以及 A/B/C 任意完成顺序。
- 验证 A dirty 时 B 内容等于 committed main，B-first 延迟 branch 绑定保持 A tree/index/cwd 不变。
- 验证错误 cwd、branch、base、lease generation 在写前停止。
- 覆盖 independent、stacked、must-serialize 与依赖 candidate stale。

### 14.4 Principle/Progress/Reconcile

- 覆盖 principle 无 diff、已批准 diff、main drift、非重叠语义冲突、开放 PRD no-impact/impact audit。
- 覆盖两个 worktree 同时追加事件、幂等重试、同 ID 异文、correction 和互相冲突的批准陈述。
- 删除 README managed blocks 后重建并比较规范化输出。

### 14.5 Candidate 与 Integration

- 覆盖 candidate evidence 绑定、main drift、依赖顺序、文本/semantic/schema 冲突、全量测试失败和重新候选。
- 验证 integration worktree 不承载临时实现修复，main 只通过 exact tree CAS 前进。
- 验证 commit 与 push 是分离授权；push/force 默认不执行。

### 14.6 并发与恢复

- 两线程同时分配 ID、同时判断 1→2、同时 candidate、同时请求 integration。
- 在 journal 每个阶段注入 crash：branch 已建/worktree 未建、worktree 已建/governance 未写、事件已导入/README 未重建、验证通过/main 未推进。
- 测试 stale lease、orphan branch/worktree、dirty/untracked/ignored 资产和进程残留。
- 覆盖 Windows 空格路径、文件锁、长路径和 Codex task 重启。

## 15. 执行计划

1. **现状归属与契约批准**：已完成。先把提交前已存在的三路起草分类改动形成独立本地 checkpoint commit，再提交 PRD-001 治理基线；两次提交均明确通知且不 push。
2. **Schema 与兼容层**：定义 decision record、event、refs、lease、journal 和 machine-readable plan schema，先实现 legacy 只读兼容。
3. **Core/Validator**：实现并行 iteration 状态矩阵、授权门禁、principle/progress/README 校验。
4. **Coordinator 与 Recovery**：实现原子 allocation、writer/integration leases、operation journal、status 和 reconcile。
5. **Local/Worktree Orchestrator**：实现懒创建、延迟 main 释放、sticky draining、路径 guard 和 runtime adapter。
6. **Governance Reconciler**：实现 principle 三方 gate、progress semantic union 和 README deterministic rebuild。
7. **Candidate/Merge Train**：实现 candidate generations、integration worktree、latest-main verification 和 main CAS。
8. **UX/Notifications**：实现 Silent/Notify/Confirm 卡片，确保 worktree 透明、commit/push 分离确认。
9. **迁移、文档与验证**：完成 upgrade dry-run、历史兼容、单元/集成/并发/崩溃测试及 README/eval 更新。

每个切片先通过其结构和故障注入测试，再进入下一切片。任何步骤不得在本 PRD/SPEC 批准、实施授权和现有 dirty 归属裁决前开始。

## 16. 批准与修订记录

- 2026-08-11：根据多 PRD、懒 worktree、无感交互、Git 透明、principle/progress 和 merge train 讨论形成 SPEC 草案；尚未批准，尚未授权实施。
- 2026-08-11：用户裁决全部开放问题、批准 P-001～P-003 和本 SPEC，并明确授权按“先提交既有改动，再开始实现”执行。
- 2026-08-12：checkpoint `6cc0104` 与 `2d1be71` 完成且未 push；开始执行 identity/ref/journal/status 最小闭环。
- 2026-08-12：将新 refs 修正到 `refs/project-harness/v2/...` 独立 namespace，避免与 legacy nested base ref 冲突；不改变产品范围或授权门禁。
- 2026-08-12：完成首个 v2 实现切片：只读 `status` / `plan reserve-iteration`、显式 plan digest 接受、Git common-dir operation journal、allocation/base 原子 CAS、legacy/v2 状态识别与旧写入口阻断；该切片不创建 branch/worktree/governance bundle，不 commit、不 push。
- 2026-08-12：独立实现基线与全局治理基线；任意 worktree 发起时，base 只能来自显式允许 ref，governance 固定读取并校验 committed `refs/heads/main` tree，同时把 governance commit/tree 与 principle 内容哈希绑定到 plan、journal、allocation metadata 和 status。提交树校验与 worktree 校验的共享纯语义核心将在下一切片收束，当前 checkpoint 不构成 candidate/final。
- 2026-08-12：用户授权协调器在后续非最终 WIP 切片完成精确范围与验证复核后自主创建本地 checkpoint，并在完成后报告 hash、范围与 `pushed=false`；最终 candidate/integrated 产物仍由用户校核。该 standing authorization 不包含 push、main integration、历史改写、破坏性 Git、candidate/final 权威或最终验收。
