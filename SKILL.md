---
name: harness-lite
description: Initialize, adopt, operate, upgrade, or validate lightweight Git-backed PRD/SPEC governance with global principles, append-only progress, adaptive Local/worktree isolation, and acceptance-bound integration evidence. Use for project governance, requirement management, parallel PRDs, worktree orchestration, or numbered iterations under harness/.
---

# Harness Lite

Keep product intent, implementation, deviations, decisions, and acceptance evidence connected while hiding routine workspace mechanics. Parallel execution changes topology, never the authority chain.

## Preserve the authority model

- Keep `harness/` as the only editable source of normative project-governance truth.
- Apply this order: approved principles > approved PRD > approved SPEC. A deviation is factual as-built evidence, never approval, implementation authority, or an exception.
- Treat main's committed `harness/principle.md` and its exact SHA-256 as the single global principle authority for every Local workspace, linked worktree, candidate, and integration result.
- Treat `harness/progress.md` as immutable event history. Append corrections and resolutions; never rewrite an earlier event.
- Treat L0/L1 READMEs as derived routing. Rebuild managed sections from principles, bundles, progress events, refs, and operational status; do not merge README copies as authority.
- Keep every numbered iteration in `harness/iterations/NNN/` with `README.md`, `prd-NNN.md`, `spec-NNN.md`, and `deviation-NNN.md`.
- Do not infer approval. PRD approval, SPEC approval, implementation authorization, integrated-result confirmation/final acceptance, and any principle change remain distinct gates even when one explicit user response supplies several of them.

Read [references/harness-contract.md](references/harness-contract.md) completely before changing the document model, lifecycle, statuses, authority, worktree topology, or evidence rules. Read [references/adoption.md](references/adoption.md) only for partial/foreign Harness adoption or ambiguous legacy ownership.

## Resolve and inspect the project

1. Use the user-supplied project path, otherwise the current Git root, otherwise the current directory.
2. Report the absolute root before writes. Never initialize a nested repository or the Skill installation directory unless explicitly targeted.
3. Inspect `AGENTS.md`, L0/L1, relevant PRD/SPEC/deviation, committed main principles, Git status/worktrees/refs, active operation journals and leases, and discoverable project context.
4. Preserve user changes. Dirty, staged, untracked, ignored, runtime, and shared-resource state are separate safety inputs; Git-clean alone is not enough.
5. Read only the necessary governance levels: L0 first, relevant L1 next, then targeted L2 documents and L3 events.

Classify the project as fresh, managed lifecycle-v2, managed legacy serial, partial/foreign, or already valid. A recognized legacy marker remains readable. Upgrade only through a reviewed, exact dry-run plan; preserve historical documents, events, refs, and user-authored text outside bounded managed blocks.

## Initialize safely

Run a dry run first:

```text
<python> <skill-dir>/scripts/project_harness.py init \
  --project-root <absolute-project-root> \
  --project-name <project-name> \
  --dry-run
```

For a project without Git, apply only with the exact emitted `BASELINE_PLAN_TOKEN`; the initializer may create `main` and one reviewed bootstrap baseline commit containing exactly the accepted non-ignored manifest. In an existing repository it creates no commit and does not absorb existing changes. It must not edit `.gitignore`, force-add ignored files, install dependencies, move legacy documents, push, create a PR, rewrite history, or modify product code.

## Route every request on three independent axes

Resolve material ambiguity before size. Classification chooses the next process; it never grants authority.

1. **Governance path**
   - `grill`: a decision-bearing ambiguity affects scope, acceptance, compatibility, data/schema, migration, permissions, security/privacy, external systems, irreversibility, or a meaningful trade-off.
   - `co-draft`: the request is decision-complete, localized, low-risk, easy to roll back, and has no material coordination burden. Draft PRD and same-number SPEC together; keep both unapproved.
   - `PRD-first`: the request is decision-complete but not small. Approve the PRD before completing/approving the SPEC.
2. **Execution topology**
   - read-only/drafting: no writer workspace;
   - `Local`: the only active implementation PRD uses the primary checkout;
   - independent/stacked linked worktree: the second and later active writers, or an explicit stable-candidate dependency;
   - serialize: principle barriers, incompatible schemas, exclusive environments, or unresolved resource conflicts.
3. **Authorization state**
   - independently derive PRD approval, SPEC approval, implementation authorization, principle approval/impact audit, integration readiness, and final acceptance from exact committed evidence.

Use reason codes and surface the chosen path, topology, current principle identity, blocking reasons, and next gate. A new task alone is not a new iteration; a concrete new product goal, scope, public contract, workflow, or acceptance target is.

## Start and isolate implementation adaptively

Use the lifecycle-v2 orchestrator for new parallel-capable work. Reserve identity and the immutable **allocation base** first, create the complete governance bundle, obtain and commit the required approved governance baseline, then bind the exact **implementation start**. These are deliberately separate:

- allocation base: immutable product/allocation identity used for ancestry, audit, and refs;
- implementation start: the exact committed main snapshot from which implementation actually begins, which may include the approved governance bundle commit and must descend from the allocation base.

Topology rules:

- `0 → 1`: activate the first writer in the primary checkout as Local. Do not create a linked worktree, implicit feature branch, commit, stash, or migration merely for isolation.
- `1 → 2`: add PRD-B as a sibling linked worktree from its exact allowed implementation start. Keep PRD-A's cwd, files, index, untracked state, runtime, and task unchanged. Never commit, stash, copy, or move A to create B.
- `2 → 3+`: give each additional writer its own sibling worktree, branch, writer lease, writable root, and runtime namespace after dependency/resource checks.
- `N → 1`: enter sticky draining. The survivor stays where it is until completion; do not migrate it merely because concurrency dropped.
- `1 → 0`: release ownership only after evidence and cleanup gates. The next independent single writer may use Local again.

If B must advance main before dirty Local A, perform the B-first main-release transition only after strict ownership and Git-operation checks. Bind A in place to its own branch without commit, stash, file movement, or cwd change, and notify before and after. Unknown ownership, conflicts, operation markers, stale leases, or path mismatch block before mutation.

Every writer is bound to iteration, task/owner, absolute root, branch state, allocation base, implementation start, lease generation, path ownership, and runtime namespace. Worktrees isolate Git checkouts; they are not security sandboxes. Project adapters must claim mutable ports, databases/schemas, caches, logs, containers, accounts, and shared environments explicitly.

## Govern principles, progress, and derived views

- A feature-worktree principle edit is only a proposal. Principle mutation requires the global principle lease, exact before/after text, stable change identity, explicit approval, and an impact audit across every open PRD.
- When main's principle hash differs from an iteration's `principle_base_hash`, mark principle drift. Record a no-impact checkpoint or revise/reapprove affected PRD/SPEC and regenerate evidence before candidate/integration.
- New progress events use globally unique event IDs independent of session IDs. Preserve legacy `S-*` events byte-for-byte. Union events by identity: same ID/same bytes is idempotent; same ID/different bytes is tampering and blocks; conflicting conclusions remain until a new resolution event.
- Rebuild README managed sections and progress indexes from authoritative inputs after governance import. Preserve physically separate user-authored sections.

Only the coordinating agent allocates iteration/event/deviation IDs and mutates shared routing, principle, or progress state. Subagents return scoped findings and artifacts.

## Build candidates and integrate through the merge train

Before feature candidacy, require exact committed PRD/SPEC approvals, implementation authorization, acceptance evidence, deviation disposition, workspace/ownership guard receipts, current principle identity, and project verification. Candidate evidence binds iteration, generation, allocation base, implementation start, candidate ref/commit/tree, included paths, dependency identities, principle hash, verification receipts, and authority/guard receipts.

Prepare one integration candidate at a time from exact latest main:

1. revalidate candidate and dependency identities;
2. pass the principle gate and drift audit;
3. import progress by immutable event union;
4. merge implementation and the iteration bundle;
5. rebuild progress index and L0/L1 managed views;
6. run cross-PRD/full verification;
7. persist evidence bound to the exact integrated tree/commit and main baseline.

Implementation conflicts return to the owning PRD workspace; the integration lane is not a repair workspace. Governance-only conflicts may be deterministically normalized by the reconciler when its exact plan and result tree are journaled and replayable.

Default integration is `merge --no-ff` so candidate ancestry remains traceable. A project may explicitly declare another strategy, but whenever candidate commit identity changes, create a new integrated candidate, rerun verification, and bind fresh evidence. Never reuse evidence across a changed identity.

Advance main only after the user confirms the exact latest-main integrated result; that confirmation may also be final acceptance on the normal path. Any main, tree, candidate, principle, or evidence change invalidates the confirmation and requires reconstruction. Use compare-and-swap for main and integrated refs. After main moves, failures use an explicit revert or forward-fix; never silently reset or rewrite accepted history.

## Keep automation low-noise and Git-transparent

- **Silent**: read-only discovery, three-axis classification, validation, evidence collection, deterministic previews, and idempotent replay of an already authorized operation.
- **Notify before and after**: worktree create/remove, safe branch creation, Local A in-place branch binding, candidate queue/invalidations, and manifest-owned local runtime setup/teardown. A worktree notice includes PRD, reason, both baselines, branch, absolute path, runtime namespace, effects on existing PRDs, and remote involvement.
- **Confirm**: PRD/SPEC approval, implementation authorization, principle changes, residual deviation acceptance, exact integrated-result/final acceptance, main advance, merge/rebase/cherry-pick, destructive cleanup, lease takeover, external/shared mutations, every commit, and any future push.

Before a commit, show branch, exact paths/tree, message, verification evidence, exclusions, and `pushed=false`; afterward report the resulting hash and still-unpushed state. An explicit standing authorization may cover bounded WIP checkpoint commits, but each checkpoint remains transparent and is only a recovery point—not candidate, integrated, final, approval, or acceptance authority.

Push is not implemented in this lifecycle version. Never infer or execute it. The reserved future interface must be separate from commit and show remote, source/target refs, commit range, and `force=false` before explicit confirmation.

Never use automatic stash/reset/clean/force, force-delete a dirty worktree, or hide raw Git state changes behind “automatic” wording.

## Recover conservatively

Every mutating workflow uses an operation ID, versioned accepted plan digest, expected refs, locks/leases, atomic file/ref updates, and a durable journal. A retry with matching identity resumes without reallocating IDs, duplicating events/worktrees/commits, or repeating notifications; mismatched state stops for reconcile.

Cleanup is always last. Remove only objects created by that operation that still match its manifest and have no writer/process claim, Git operation marker, staged/dirty/untracked/ignored asset, or link/junction ambiguity. Preserve unknown/orphaned state and report `FAILED_NEEDS_RECONCILE`. Never use TTL alone to take over a lease.

## Upgrade legacy projects without rewriting history

Use the upgrade dry run for an existing managed serial project:

```text
<python> <skill-dir>/scripts/harness_upgrade.py \
  --project-root <absolute-project-root> \
  --json
```

Apply only with the exact reviewed plan digest. Keep completed legacy iterations, `S-*` progress events, principle text, deviation history, and legacy base/final refs unchanged and readable. A clean active iteration may be adopted under an exact plan; a dirty active iteration stays legacy unless the user explicitly approves a recoverable transition. Replace only the bounded `AGENTS.md` managed block and preserve all surrounding bytes/content.

The legacy `new-iteration` / `commit-iteration` commands remain compatibility tools for projects not yet upgraded. Do not apply their serial “one active iteration / one final commit” assumptions to lifecycle-v2 work.

## Validate and hand off

Run structural validation after initialization, upgrade, bundle creation, reconciliation, and material governance changes:

```text
<python> <skill-dir>/scripts/project_harness.py validate \
  --project-root <absolute-project-root>
```

Also verify lifecycle status, workspace leases/guards, candidate/integrated evidence, journal recovery, project tests, and deterministic README rebuild as applicable. Treat structural or identity errors as incomplete work; repair only managed, unambiguous content.

Report the absolute root, request route and reason codes, Local/worktree topology, current principle identity/drift, changed paths, notifications/confirmations, verification evidence, candidate/integrated identities, next gate, local commit hashes, and `pushed=false`. Do not claim acceptance, finality, integration, or cleanup that the exact evidence does not prove.
