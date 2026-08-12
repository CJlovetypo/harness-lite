<!-- managed-by: harness-lite v1 -->
# Harness Lite Harness 导航

## 这是什么

这是项目 Harness 的 L0 轻量路由页。先用本页定位当前任务，再读取相关迭代的 `README.md`；不要为了了解状态默认加载所有 PRD、SPEC、偏差和完整历史。

本页是派生摘要，不批准需求、技术方案或例外。基线权威为：已批准原则 > 已批准 PRD > 已批准 SPEC。deviation 只记录实现完成后的 as-built 事实差异，不是基线、批准源或实施授权；`progress.md` 只保存历史证据。

## 当前焦点

<!-- project-harness:focus:start -->
- 当前迭代：[001](iterations/001/README.md) — 并行 PRD 编排与全局治理无感化。
- 当前门禁：PRD-001 / SPEC-001 实施中；三轴决策 checkpoint `721c291` 已提交且未 push。治理/candidate/upgrade/UX 纯门禁切片已通过 81 项回归，正在形成自主非最终 checkpoint。
- 下一步：提交纯门禁切片；随后单独收束 validator/anchor compatibility 与 Local/worktree 编排，补齐 B-first 原地 branch binding。
<!-- project-harness:focus:end -->

## 迭代索引

| 迭代 | 标题 | PRD | SPEC | 开放偏差 | 一句话结果 | 下一步 | 入口 |
|---|---|---|---|---:|---|---|---|
<!-- project-harness:iterations:start -->
| [001](iterations/001/README.md) | 并行 PRD 编排与全局治理无感化 | 实施中 | 实施中 | 0 | 三轴决策已提交；治理/candidate/upgrade/UX 纯门禁已验证 | 提交纯门禁 checkpoint，收束 validator/workspace | [进入](iterations/001/README.md) |
<!-- project-harness:iterations:end -->

## 渐进阅读路由

| 当前任务 | 下一步读取 |
|---|---|
| 只想知道当前状态 | 本页；足够就停止 |
| 了解某轮结果或下一步 | 对应 `iterations/NNN/README.md` |
| 判断目标、范围、验收或是否新建迭代 | 迭代 README → PRD；涉及长期取舍再读 `principle.md` |
| 设计、实现、测试、迁移或代码评审 | 迭代 README → PRD + SPEC |
| 评估风险、批准或实现前已知变化 | 迭代 README → PRD + SPEC；变化先修订并重新批准基线 |
| 判断实现后的事实偏差或验收阻塞 | 迭代 README → deviation + 被引用的 PRD/SPEC 条款 |
| 追溯某次决策 | 用迭代 README 的事件 ID 定向读取 `progress.md` |
| 验收、关闭或合并治理历史 | 完整读取相关 PRD、SPEC、deviation 与目标 progress 事件 |

## 层级与更新纪律

```text
L0  harness/README.md
 └─ L1  harness/iterations/NNN/README.md
     ├─ L2  prd-NNN.md / spec-NNN.md / deviation-NNN.md
     └─ L3  harness/progress.md 中的目标事件块
```

- README 只做导航与摘要；发现冲突时从权威正文和证据重建摘要。
- `progress.md` 事件只追加；README 只引用事件 ID，不复制完整日志。
- 新迭代一次性创建同号目录四件套。
- 状态、开放偏差、结果或下一步改变时更新 L1；影响全局路由时再更新 L0。

最后初始化：2026-08-11
