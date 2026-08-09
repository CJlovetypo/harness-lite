# Harness Lite Contract

## Contents

1. Purpose and boundary
2. Document model and authority
3. Progressive disclosure
4. Iteration identity and lifecycle
5. Document contracts
6. Update matrix
7. Completion and validation
8. What not to generalize

## 1. Purpose and boundary

Use the Harness to preserve the chain from intent to evidence:

```text
durable principles
  -> product promise (PRD)
  -> approved implementation baseline (SPEC)
  -> implementation and verification
  -> as-built comparison and deviation disposition
  -> acceptance and historical evidence
```

The Harness governs project work; it is not a second implementation, issue tracker, chat archive, or generated-artifact store. Keep it small enough that the root router can answer “what is active and what should I read next?” without loading the full project history.

## 2. Document model and authority

Use this structure:

```text
<project-root>/
├── AGENTS.md
└── harness/
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

Assign authority and document roles as follows:

1. `principle.md`: explicitly approved, durable project/product trade-offs.
2. Approved `prd-NNN.md`: what outcome, scope, and acceptance promise the iteration makes.
3. Approved `spec-NNN.md`: how the approved PRD will be implemented and verified.
4. `deviation-NNN.md`: factual evidence of completed implementation that differs from the approved PRD/SPEC; never a baseline, approval source, or implementation authorization.
5. `progress.md`: append-only decisions, actions, evidence, and next steps; historical, not normative.
6. Root and iteration READMEs: derived routing and status summaries; never approval sources.

When documents conflict, stop within the conflicting scope. Do not silently expand or shrink product intent. Revise and reapprove the affected PRD/SPEC when a change is known before implementation. After implementation, register every material as-built difference and obtain an explicit disposition before acceptance.

## 3. Progressive disclosure

Use four reading levels:

| Level | Source | Read when |
|---|---|---|
| L0 | `harness/README.md` | Always first; locate active/relevant iterations and next reading step |
| L1 | `iterations/NNN/README.md` | The user names an iteration or L0 routes the task there |
| L2 | PRD, SPEC, deviation | Select by task intent |
| L3 | Target events in `progress.md` | Approval evidence, decision reconstruction, conflict repair, audit, or merge |

Select L2 documents by intent:

- Scope, goals, acceptance, or whether work is new: read the PRD.
- Design, implementation, testing, migration, or code review: read PRD + SPEC.
- Risk or a known pre-implementation change: read and revise the affected PRD/SPEC.
- An as-built difference or acceptance blockage after implementation: read the deviation ledger and the cited PRD/SPEC clauses.
- Long-term trade-off: read principles + the relevant PRD.
- Status-only question: stop at L0/L1 when sufficient.

Do not load the whole append-only history by default. Use the session/event ID, iteration ID, or date supplied by L0/L1 to find the needed event block.

## 4. Iteration identity and lifecycle

Use decimal IDs padded to at least three digits: `001`, `002`, and so on. Treat them as governance identities, independent of release versions, sprint numbers, branch names, ticket IDs, or implementation phases.

Before allocating an ID:

1. Scan all iteration directories and any legacy numbered PRD/SPEC documents being adopted.
2. Repair an incomplete existing bundle before allocating another number.
3. Allocate `max(existing IDs) + 1`.
4. Never reuse cancelled, superseded, abandoned, or empty IDs.
5. Create README, PRD, SPEC, and deviation together, even when there are no deviations.

Record the current full Git `HEAD` and attached `refs/heads/...` branch in the PRD when the bundle is created, and anchor both independently at `refs/project-harness/iterations/NNN/base/refs/heads/...`. The anchor, not editable PRD text, is the immutable authority. That commit must remain `HEAD` on the same branch until explicit acceptance; any intervening commit or branch switch breaks the one-final-commit invariant and blocks automated finalization. This lifecycle is serial: an existing iteration must have a reachable final marker and clean governance before another number is allocated.

Create a new iteration for a new product goal, scope, user-visible behavior, public contract, workflow, or acceptance target. Continue the current iteration for approved implementation, a defect within approved scope, verification, or documentation repair. A new chat alone creates neither a session record nor an iteration unless work is authorized.

Use these PRD statuses:

`草案`, `待批准`, `已批准`, `实施中`, `待验收`, `已验收`, `已取代`, `已取消`.

Use these SPEC statuses:

`受 PRD 阻塞`, `草案`, `待批准`, `已批准`, `实施中`, `已完成`, `已取代`, `已取消`.

Use these deviation statuses:

`开放`, `待处置`, `已修复`, `基线已重批`, `已接受残余`, `已转后续迭代`, `已关闭`.

An agent may mark implemented work `待验收` after evidence is complete. Only explicit user acceptance changes it to `已验收`.

## 5. Document contracts

### principle.md

Store only user-approved durable principles, conflict policies, approval control, and a change record. Do not invent project-specific principles during bootstrap. “No approved project-specific principles yet” is valid.

### prd-NNN.md

Include metadata, background/problem, goals, in-scope requirements, acceptance criteria, non-goals, constraints, open questions, and approval/revision evidence. Give requirements and acceptance criteria stable IDs such as `R-003-01` and `AC-003-01`.

Describe what and why. Exclude functions, classes, algorithms, shell commands, dependency versions, file-by-file coding steps, and test implementation.

### spec-NNN.md

Reference the same-number PRD and its approved baseline. Include architecture/responsibility boundaries, requirement traceability, file/interface/data contracts, execution plan, compatibility/migration, rollback, risks, and verification.

Do not introduce product scope or acceptance requirements that the PRD did not authorize. Return new product needs to the PRD approval gate.

### deviation-NNN.md

Record only factual differences between completed as-built implementation and the same-number approved PRD/SPEC. Use `DEV-NNN-SSS` IDs. Each entry includes status, discovery time, exact baseline references, original promise, observed as-built behavior, cause, impact, acceptance impact, explicit disposition and evidence, verification, and closure.

A deviation is not a change proposal, approval source, or implementation authorization. If a difference is known before implementation, revise and reapprove the affected PRD/SPEC instead. After implementation, register every material difference and explicitly dispose it before acceptance by correcting the implementation, revising and reapproving the baseline, accepting the residual fact, or transferring it as an acknowledged blocker. Keep resolved and closed entries; never erase them by rewriting the baseline. A cross-iteration transfer remains canonical in its original ledger and is linked from the receiving iteration rather than copied.

### progress.md

Append conclusion-level events using `S-YYYYMMDD-NN` session IDs and event types `OPEN`, `DECISION`, `CHECKPOINT`, `MERGE`, and `CLOSE`. Record context, user goal, decisions and stated basis, execution, verification evidence, related deviations, and next steps.

Do not record hidden reasoning, verbatim chat, tokens, secrets, or raw tool noise. Correct an old event with a new event instead of rewriting history.

### READMEs

Keep the root README as a compact registry and routing page. Keep each iteration README as a compact result/status page with recent event IDs, open items, next step, and a task-to-document map.

Do not duplicate full requirements, technical designs, deviation bodies, or event logs into READMEs.

## 6. Update matrix

| Change | Iteration README | Root README | Progress | Deviation |
|---|---:|---:|---:|---:|
| New iteration or renumbering | Yes | Yes | Yes | Create ledger |
| PRD/SPEC status changes | Yes | Yes when routing/focus changes | Yes | Only when a reapproved baseline disposes a recorded as-built difference |
| Decision, checkpoint, or close | Yes | Only if global routing changes | Append | If completed as-built work differs |
| Open deviation count/status changes | Yes | Yes | Append | Update canonical entry |
| Implementation detail within approved SPEC | When result/next step changes | No | At checkpoint/close | No |
| Read-only status question | No | No | No | No |

README drift is repaired from authoritative documents and evidence. Do not rewrite old progress events to make them appear consistent with current knowledge.

## 7. Completion and validation

Before moving an iteration to `待验收`, require:

1. Every acceptance ID has test, static-check, artifact, or manual-review evidence.
2. Implementation matches the approved PRD/SPEC or every material as-built difference is recorded.
3. Every recorded deviation identifies its discovery time, exact requirement/acceptance and SPEC references, cause, impact, and acceptance impact. Every resolved entry also has an explicit disposition, evidence, completed verification, and closure/transfer time: implementation fixed, baseline revised and reapproved, residual fact accepted, or transfer/blocker acknowledged.
4. Progress contains verification, residual risks, and a CLOSE event.
5. L1 is current; L0 is current when routing facts changed.
6. The working diff contains no unrelated user changes in the proposed iteration scope.

The bundled validator checks structure, IDs, statuses, ownership markers, routing registration, duplicate event/deviation identities, forbidden flat governance files, and local Markdown links. It cannot determine whether requirements are good, approvals are genuine, summaries are semantically accurate, or tests actually prove acceptance.

## 8. What not to generalize

Do not carry domain-specific implementation paths, artifact folders, pipeline phases, dependency policies, or ignore rules from a reference project into a new project.

Do not generalize project-specific authorization for automatic Git operations. If the target has no Git repository, Harness bootstrap may initialize Git and create exactly one initial baseline commit; this is the bootstrap exception. The preceding dry-run manifest is bound by an exact `BASELINE_PLAN_TOKEN`, and apply must reject any changed path, byte, target root, or rendered Harness before writes. If Git already exists, Harness initialization does not commit. Each product iteration receives exactly one final commit, and only after explicit user acceptance of the completed result. Its same-number bundle is automatic scope; every dirty shared control file requires exact explicit inclusion. Finalization binds the reviewed staged tree to the independent base anchor and writes `refs/project-harness/iterations/NNN/final` atomically with the branch update. Commit and reference-transaction hooks are not run, so their required checks must be executed and recorded beforehand. Pushing, creating PRs, rewriting history, editing `.gitignore`, or installing dependencies remains separately authorized and repository-specific.
