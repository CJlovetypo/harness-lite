# Project Instructions

<!-- project-harness:start v1 -->
## Harness Lite governance

Treat `harness/` as the only editable source of project-governance truth.

Before authorized project writes:

1. Read `harness/README.md` (L0) and select the relevant iteration.
2. Read that iteration's `README.md` (L1). For writable product/governance work, also read `harness/principle.md`.
3. Read by intent: scope/acceptance -> PRD; design/implementation/test/migration or a known change -> PRD + SPEC; a completed as-built difference -> deviation + cited clauses; decision history -> targeted events in `harness/progress.md`.
4. Classify the request as a new iteration, continuation of an approved iteration, or read-only work. Read-only work does not create governance logs.
5. Preserve user changes and check the existing diff before editing when Git is present.

Authority and divergence:

- Baseline authority is approved principles > approved PRD > approved SPEC.
- A deviation records a factual difference between completed as-built work and the approved PRD/SPEC. It is never a baseline, approval source, or implementation authorization.
- `harness/progress.md` is historical evidence. Root and iteration READMEs are derived routing, not approval sources.
- Revise and reapprove the affected PRD/SPEC when a change is known before implementation. After implementation, record every material difference and obtain an explicit disposition before acceptance.

Requirement discovery gate:

- Inspect available project context before asking the user. Resolve clarity first, then assess size by product blast radius and risk rather than lines of code.
- If a decision-bearing ambiguity remains, grill the user before treating the PRD as ready. Ask pointed, context-specific questions and challenge assumptions about the problem/users, scope/non-goals, acceptance/failure cases, constraints, compatibility/data/security/migration concerns, and meaningful trade-offs. Do not ask what the repository can answer. Resolve each ambiguity or have the user explicitly remove it from this iteration as a non-goal with its impact and next gate recorded; anything still affecting in-scope acceptance remains blocking, and the SPEC stays `受 PRD 阻塞`.
- Small-and-clear additionally requires localized impact, low risk, straightforward rollback, and no material cross-system coordination. Public API/schema changes, data migration, permissions, security/privacy/compliance, irreversible behavior, and substantial compatibility impact are normally not small. Only this class is eligible for co-drafting; prefer completing and presenting both drafts together unless staged review has a concrete benefit. A filled pre-approval SPEC is `草案`, has no approved baseline, SPEC approval evidence, or implementation authorization, and traces the proposed PRD IDs.
- For clear but non-small work, use the standard PRD-first path without grilling merely because the work is large. Co-drafting changes timing, not authority: do not turn guesses into requirements, put implementation details in the PRD, infer approval, or treat approval as implementation authorization.

Iteration rules:

- Store each iteration at `harness/iterations/NNN/` with exactly `README.md`, `prd-NNN.md`, `spec-NNN.md`, and `deviation-NNN.md`.
- Use a monotonic ID padded to at least three digits. Repair incomplete existing bundles before allocating `max + 1`; never reuse gaps or retired IDs.
- Create a new iteration only for a new goal, scope, user-visible behavior, public contract, workflow, or acceptance target. A new chat alone is not a new iteration.
- Keep implementation details out of PRD. Do not let SPEC add product scope absent from PRD.
- Do not implement before explicit PRD approval, explicit SPEC approval, and separate implementation authorization. A single user response may supply all three, but generic approval of the drafts does not authorize implementation.

Evidence and synchronization:

- Use progress events `OPEN`, `DECISION`, `CHECKPOINT`, `MERGE`, and `CLOSE` with IDs `S-YYYYMMDD-NN`; append corrections rather than rewriting historical events.
- Record completed as-built differences as `DEV-NNN-SSS` in the owning iteration. Preserve resolved/closed entries and link cross-iteration transfers instead of copying them.
- Update L1 when status, decisions, evidence, deviations, results, or next steps change. Update L0 only when global routing, focus, registry, or open-deviation facts change.
- Only the coordinating agent allocates IDs or updates shared progress, deviation, and routing files; subagents report results to it.

Completion and Git boundary:

- Move work to `待验收` only when every acceptance ID has evidence, every material as-built difference is recorded and explicitly disposed, progress is closed, and summaries are current. Unresolved differences keep the iteration blocked. Only explicit user acceptance sets `已验收`.
- Do not initialize nested Git or edit `.gitignore`. If the target has no Git repository, Harness bootstrap may initialize Git and create exactly one initial baseline commit only from the exact `BASELINE_PLAN_TOKEN` returned by a reviewed dry run; if Git already exists, initialization makes no commit.
- Keep Git `HEAD` on the independently anchored PRD branch/baseline throughout the iteration. Work serially: finalize the current iteration before allocating another. Give each iteration exactly one final commit, and only after explicit user acceptance of the completed result; implementation approval is not acceptance, and any intermediate commit blocks automated finalization. Explicitly scope every dirty shared control file, run required checks before the deterministic no-hook finalizer, and preserve its base/final refs under `refs/project-harness/iterations/NNN/`. Do not push, create a PR, rewrite history, or perform destructive Git operations without separate authorization.

PRD-001 lifecycle-v2 bootstrap transition:

- The user explicitly approved OQ-001-01 and OQ-001-06: preserve the pre-existing drafting-path changes and the approved PRD-001 governance baseline as two local checkpoint commits before new implementation. These checkpoints are recovery points only; they are not candidate, integrated, final, or acceptance authority, and they must not be pushed.
- Preserve the legacy PRD-001 base anchor at `7376803cffb09269bc8a03346901b2e9e224d704`. Do not amend, squash, repoint, or hide the transition history, and do not use the legacy `commit-iteration` finalizer for PRD-001.
- Checkpoint 1 is `6cc0104075b5394a3ed6c6933b59817832503aeb` and contains only the pre-PRD drafting-path work. Checkpoint 2 may contain only the reviewed `AGENTS.md` and `harness/` governance paths after validation. Every later checkpoint remains Confirm-level, must show exact scope/message/verification, and must report `pushed=false`.
- The v2 journal/lease requirements govern Harness orchestration mutations once their implementation slice is available and validated. Until then, PRD-001 source implementation remains single-writer Local work under exact checkpoint manifests; this narrow bootstrap allowance does not authorize worktree creation, main integration, remote writes, destructive Git operations, or bypassing product gates.
<!-- project-harness:end -->
