# Harness Lite Contract

## Contents

1. Purpose and authority
2. Document and state model
3. Identity, refs, and dual baselines
4. Three-axis routing and lifecycle
5. Workspace and runtime isolation
6. Document contracts
7. Principle, progress, and derived-view reconciliation
8. Candidate, integration, and acceptance evidence
9. Interaction and Git transparency
10. Journals, concurrency, recovery, and cleanup
11. Legacy compatibility and upgrade
12. Completion, validation, and update matrix

## 1. Purpose and authority

Harness Lite preserves this chain for every product iteration, whether it runs in the primary checkout or a linked worktree:

```text
committed global principles
  -> approved PRD
  -> approved SPEC
  -> explicitly authorized implementation
  -> feature candidate + verification evidence
  -> latest-main integrated candidate + re-verification
  -> exact-result acceptance
  -> main/final identity + historical evidence
```

Parallel work changes execution topology only. It never weakens the vertical `PRD → SPEC → implementation → acceptance evidence` chain.

Authority order is:

1. main's explicitly approved, committed `harness/principle.md`;
2. the exact approved same-number PRD baseline;
3. the exact approved same-number SPEC baseline;
4. separately recorded implementation authorization;
5. factual candidate/integrated verification evidence;
6. explicit acceptance of an exact integrated result.

`deviation-NNN.md` describes completed as-built differences. `progress.md` preserves history. READMEs route readers. None of those can approve scope, authorize implementation, or override a principle.

When authorities conflict, stop only the affected scope. Do not silently union principles, expand/shrink a PRD, let a SPEC add product scope, or treat merge order as a decision. A known pre-implementation change returns to the affected PRD/SPEC approval gate. A completed factual difference enters the deviation ledger and needs an explicit disposition before acceptance.

## 2. Document and state model

Normative, Git-backed governance remains:

```text
<project-root>/
├── AGENTS.md
└── harness/
    ├── README.md                 # L0 derived router
    ├── principle.md              # one global normative authority
    ├── progress.md               # immutable event history + derived index
    └── iterations/
        └── NNN/
            ├── README.md         # L1 derived router
            ├── prd-NNN.md
            ├── spec-NNN.md
            └── deviation-NNN.md
```

Operational state belongs under the repository's Git common directory and is not committed as normative governance. It includes operation journals, locks/leases, worktree absolute paths, owner/task identities, runtime namespaces, queue state, and notification receipts. It must be reconstructible from Git refs, worktree inventory, governance bundles, and journals; it is never the sole product authority.

Use progressive disclosure:

| Level | Source | Read when |
|---|---|---|
| L0 | `harness/README.md` | Always first; find relevant iterations, topology, drift, and next gate |
| L1 | `iterations/NNN/README.md` | The request names or routes to the iteration |
| L2 | PRD/SPEC/deviation/principle | Select by intent and authority gate |
| L3 | Target `progress.md` events and evidence receipts | Approval reconstruction, conflict resolution, integration, recovery, or audit |

Do not load the entire history by default. Use iteration, event, operation, candidate, or evidence identity to select the necessary records.

## 3. Identity, refs, and dual baselines

### 3.1 Iteration identity

Use decimal IDs padded to at least three digits. IDs are governance identities, not release numbers, branch names, task IDs, or phases. Allocate atomically with a global coordinator and compare-and-swap; never independently scan `max + 1` in multiple worktrees. Never reuse an abandoned, cancelled, completed, or partially allocated ID.

Create the four-file bundle together. Repair an incomplete reservation/bundle through its operation journal; do not allocate around it.

### 3.2 Lifecycle-v2 refs

Lifecycle-v2 uses an independent namespace:

```text
refs/project-harness/v2/allocations/NNN
refs/project-harness/v2/iterations/NNN/base
refs/project-harness/v2/iterations/NNN/candidates/GGG
refs/project-harness/v2/iterations/NNN/integrated
refs/project-harness/v2/iterations/NNN/final
```

- The allocation ref points to immutable allocation metadata; the base ref points to the immutable allocation commit.
- Candidate content changes create a new generation; never overwrite evidence already referenced by another iteration or integration.
- Integrated and final refs bind exact evidence-bearing identities and move only through compare-and-swap with the corresponding main update.
- Custom refs are local evidence unless a separate transport policy exists. Ordinary push/clone must not be described as synchronizing them.

Legacy refs under `refs/project-harness/iterations/NNN/...` remain read-only compatible. Do not rewrite them or create v2 direct refs under a namespace that conflicts with legacy nested `base/refs/heads/...` paths.

### 3.3 Dual baselines

Lifecycle-v2 separates two identities that serial Harness treated as one:

| Identity | Meaning | Rule |
|---|---|---|
| Allocation base | The committed product/allocation snapshot at ID reservation | Immutable; anchored by the v2 base ref and allocation metadata |
| Implementation start | The exact committed snapshot from which the writer/worktree starts | Must descend from allocation base; may include the approved PRD/SPEC governance commit |

The PRD exposes both fields for humans, but refs, allocation metadata, activation journal, and guard receipt are authoritative. Editing PRD text cannot repoint either identity.

For an independent PRD, implementation starts from an exact committed main snapshot. For stacked work, the allowed start is an explicitly declared stable dependency candidate. It must never be inferred from the caller's dirty cwd.

Allocation metadata also binds the committed governance tree and `principle_base_hash`. A principle hash mismatch is drift, not a cosmetic metadata difference.

## 4. Three-axis routing and lifecycle

Every request produces one decision record with three independent axes. No axis grants approval to another.

### 4.1 Governance path

1. **Grill** when a decision-bearing ambiguity can change scope, acceptance, failure behavior, compatibility, schema/data, migration, permissions, security/privacy, external effects, irreversibility, or a meaningful trade-off. Inspect local evidence first and ask only questions whose answers could change the product baseline.
2. **Co-draft** only for a decision-complete, localized, low-risk, reversible change without material coordination. The PRD and same-number SPEC may be drafted together, but remain unapproved and unauthorized.
3. **PRD-first** for decision-complete work that is not small. Size alone is not ambiguity and does not justify generic questioning.

### 4.2 Execution topology

Topology depends on active writers, stable dependencies, dirty ownership, and resource conflicts—not on documentation size:

```text
IDLE -> SINGLE_LOCAL -> PARALLEL -> DRAINING -> IDLE
                         ^    |
                         +----+  new writer arrives
```

- Drafting and read-only work do not consume writer topology.
- The sole active implementation PRD uses `SINGLE_LOCAL` in the primary checkout.
- The second and later active implementation PRDs use sibling linked worktrees unless dependency/resource rules require stacking or serialization.
- When parallel work drains to one writer, the survivor stays in place. The topology does not migrate it back to Local.
- Only after all writers release may the next single writer use Local again.

### 4.3 Authorization state

Derive these gates separately from exact evidence:

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

New ambiguity, principle drift, dependency/candidate drift, main drift, deviation, or failed verification returns the iteration to the relevant earlier gate. Never advance by editing a status label without its evidence.

## 5. Workspace and runtime isolation

### 5.1 Additive topology

- `0 → 1`: assign Local ownership in the primary checkout. Do not create an extra worktree or isolation-only branch, commit, or stash.
- `1 → 2`: create B's branch and sibling linked worktree from B's exact implementation start. A remains in its original cwd with byte-identical worktree/index/untracked state and the same task/runtime.
- `2 → 3+`: create one sibling worktree and writer lease per additional PRD after dependency and resource checks.
- `N → 1`: sticky drain; no survivor migration.
- `1 → 0`: release only after evidence and conservative cleanup gates.

If Local A occupies main and B must integrate first, the main-release operation may bind A's existing checkout in place to an A branch. It must prove exact source HEAD, full index/worktree preservation, A ownership, no Git operation markers, and no branch collision. It performs no commit, stash, reset, copy, or cwd change and requires before/after notification.

### 5.2 Writer guard

Before every mutation, validate at least:

```text
(repository common dir,
 absolute worktree path,
 iteration,
 owner/task,
 branch or detached state,
 allocation base,
 implementation start,
 writer-lease generation,
 expected operation)
```

A mismatch blocks before file or ref mutation. One PRD has at most one primary writer. The coordinator alone owns allocation, global principle, shared reconciliation, and merge-train mutations.

### 5.3 Runtime claims

Git worktrees isolate checkouts, not processes, credentials, ports, databases, caches, containers, logs, or external environments. Each implementation PRD declares resource claims and receives a deterministic local namespace. Project adapters may implement setup, candidate verification, integration verification, and teardown, but Harness records their inputs/results and does not assume a specific stack.

Exclusive or incompatible claims create a serialization barrier. Unknown shared state blocks cleanup and may block implementation/integration.

## 6. Document contracts

### `principle.md`

Store only explicitly approved durable principles, conflict policies, and change history. Main's committed bytes are the one current authority. Feature copies may propose an exact change but cannot become authority by merge.

### `prd-NNN.md`

Include metadata, both baselines, `principle_base_hash`, `depends_on`, `conflicts_with`, integration target, shared contract/schema hints, resource claims, background/problem, goals, stable requirement IDs, acceptance IDs, non-goals, constraints, open questions, and approval/revision evidence.

Describe what and why. Exclude functions/classes, algorithms, shell commands, dependency versions, file-by-file coding steps, and test implementation.

### `spec-NNN.md`

Reference the same-number PRD and the exact approved PRD baseline. Include architecture/responsibility boundaries, requirement traceability, ownership/touched-area plan, interfaces/data contracts, workspace/runtime adapter expectations, execution slices, compatibility/migration, rollback, risks, and verification.

A co-drafted pre-approval SPEC may be `草案` and trace proposed PRD IDs, but has no approved baseline, approval evidence, or implementation authorization. A material product need absent from the PRD returns to PRD revision and approval.

### `deviation-NNN.md`

Record only material differences discovered after implementation. Each `DEV-NNN-SSS` includes discovery time, exact PRD/acceptance/SPEC references, original promise, observed fact, cause, impact, acceptance impact, disposition authority, verification, and closure/transfer evidence. Keep resolved entries. A cross-iteration transfer remains canonical in the source ledger and is linked, not copied.

### `progress.md`

New events have a globally unique event ID separate from the session ID, iteration/scope, event type, occurred-at timestamp, operation ID, source identity, causal parent, authority/evidence refs, factual summary, and optional `corrects` link. Preserve legacy `S-YYYYMMDD-NN` blocks unchanged.

Do not store hidden reasoning, verbatim chat, tokens, secrets, or raw tool noise.

### READMEs

L0 is the compact cross-PRD registry; L1 is the compact iteration status/result route. Managed sections show topology, governance gates, principle identity/drift, dependencies, candidate/integration status, recent event IDs, open deviations, and next gate. Do not duplicate full requirements, designs, deviation bodies, or history.

## 7. Principle, progress, and derived-view reconciliation

### 7.1 Global principle gate

1. Read base principle, latest committed main principle, and candidate principle.
2. With no feature diff, use latest main and audit drift.
3. With a feature diff, require the global principle lease, stable change ID, exact approved before/after bytes, and impact scope.
4. If main drifted, display the exact combined result and obtain renewed confirmation even when text hunks do not overlap.
5. For every open PRD with an older hash, append either a no-impact audit event or revise/reapprove affected PRD/SPEC and regenerate candidates.

No automatic union, ours/theirs, latest-wins, or merge-order-wins policy is valid for principles.

### 7.2 Immutable progress union

For branch base, branch candidate, and latest main:

1. prove that the branch did not change any base event bytes;
2. extract new events absent from latest main;
3. de-duplicate same ID/same bytes;
4. block same ID/different bytes as tampering;
5. preserve branch causal order while physical main order reflects integration order;
6. retain contradictory facts and block authority-sensitive decisions until a new resolution event;
7. rebuild the progress index rather than merging it as text.

### 7.3 Derived rebuild

Rebuild L0/L1 managed blocks from latest main principles, reconciled progress, PRD/SPEC/deviation states, refs, and bounded operational projections. Worktree copies are previews only. User-authored sections must be physically outside managed blocks and are preserved byte-for-byte unless a real user-content conflict needs resolution.

## 8. Candidate, integration, and acceptance evidence

### 8.1 Feature candidate

Feature candidacy requires:

- exact approved PRD/SPEC and implementation authorization;
- current principle hash or completed drift audit;
- acceptance evidence and project verification;
- disposed material deviations;
- writer/root/path/base/lease guard receipts;
- owned included paths and explicit exclusions;
- stable dependency candidate identities.

Persist `CandidateEvidence` bound to iteration/generation, allocation base, implementation start, candidate ref/commit/tree, principle hash, included paths, dependencies, verification receipts, and authority/guard receipt digests. A raw ref or caller assertion is not evidence.

### 8.2 Latest-main integration candidate

The single merge train prepares from exact latest main, in declared dependency order. It revalidates identities, applies the principle gate, imports progress semantically, merges the iteration bundle and implementation, rebuilds derived views, and runs cross-PRD/full verification. Persist evidence bound to the exact integrated tree/commit and main base.

Implementation conflicts return to the owning PRD and produce a new candidate. A deterministic governance normalizer may resolve only governed files when its exact plan, inputs, staged result tree, receipt, and crash replay are validated.

### 8.3 Integration strategy and main advance

Default to `merge --no-ff`. A project may explicitly declare another strategy. If squash, cherry-pick, rebase, conflict repair, or any other operation changes candidate commit identity, generate a new integrated candidate, rerun verification, and bind new evidence before acceptance.

The user confirms the exact latest-main integrated result; on the normal path this is also final acceptance. Any change to main, candidate, tree, principles, dependencies, or evidence invalidates that confirmation. Main and integrated/final refs advance through one guarded compare-and-swap boundary. A post-advance failure never triggers automatic history rewrite.

## 9. Interaction and Git transparency

Actions use three levels:

| Level | Examples | User-visible contract |
|---|---|---|
| Silent | Read/status, routing, validation, evidence collection, preview, authorized idempotent replay | No mutation hidden inside the action |
| Notify | Worktree create/remove, safe branch create, in-place Local binding, queue/invalidations, manifest-owned local runtime | Before and after summaries |
| Confirm | Approvals, principle change, commit, main advance/merge, cleanup with risk, lease takeover, external/shared mutation, final acceptance, future push | Exact scope shown before mutation |

A worktree notice includes affected PRD(s), reason, allocation base and implementation start, branch, absolute path, runtime namespace, effect on existing PRDs, remote involvement, and before/after actual state.

Every commit is transparent. Before it, show exact branch, paths/tree, message, verification/evidence IDs, exclusions, and `pushed=false`; after it, show the commit hash, actual HEAD, and still-unpushed state. A user may grant bounded standing authority for WIP checkpoint commits, but they remain recovery points only and still receive before/after disclosure.

Push is not implemented in lifecycle-v2. The reserved interface is always separate from commit and would require remote, source/target refs, exact range, `force=false`, and explicit confirmation. Never imply that a local candidate, integration, or commit was pushed.

Never silently stash, reset, clean, force, rewrite accepted history, delete branches, or force-remove worktrees.

## 10. Journals, concurrency, recovery, and cleanup

Each mutating workflow uses a unique operation ID, versioned canonical plan, accepted plan digest, expected refs, scoped locks/leases, and a durable phase journal. Plans bind exact roots, identities, bytes/hashes, notifications/confirmations, and rollback eligibility.

A matching retry resumes idempotently. It must not allocate another ID, add a duplicate event/worktree/commit, repeat an already-recorded notification, or silently accept drift. Same name/different identity blocks.

Allocation, principle change, writer ownership, and main integration are serialized with locks plus compare-and-swap, not time-of-check assumptions. Lease takeover needs journal/process/worktree proof; TTL alone is insufficient.

Cleanup is last and conservative. Before and immediately under lock, reject active writer/runtime claims, Git operation markers, staged/dirty/untracked/ignored state, path links/junctions, nested worktree overlap, or manifest mismatch. Remove only exact operation-owned objects. If removal happened before a crash, replay completes the journal and missing after-notification without deleting again. Unknown state becomes `FAILED_NEEDS_RECONCILE` and is preserved.

## 11. Legacy compatibility and upgrade

Projects may contain legacy serial and lifecycle-v2 iterations together. Compatibility rules are per iteration:

- completed legacy bundles, principle bytes, deviation ledgers, `S-*` events, and base/final refs remain unchanged and verifiable;
- legacy readers/finalizer remain available only for iterations that have not adopted v2;
- an exact upgrade dry run reports paths, refs, hashes, active/dirty state, and disposition before writes;
- a clean active legacy iteration may adopt v2 through the accepted operation; a dirty one remains legacy unless the user explicitly approves a recoverable transition;
- upgrade replaces only the bounded `AGENTS.md` block and preserves all surrounding user content;
- never create a new v2 ref that collides with the legacy ref namespace.

Legacy serial constraints—one active iteration, fixed branch at base, no intermediate commit, one acceptance-gated final commit—must not be applied to already-adopted lifecycle-v2 iterations. Conversely, v2 behavior must not be claimed for an unupgraded legacy iteration.

## 12. Completion, validation, and update matrix

Before feature candidacy, require every acceptance ID to have evidence, every completed material difference to be registered/disposed, current authority and writer receipts, and no unrelated included changes. Before integrated candidacy, also require latest-main governance reconciliation and full/cross-PRD verification. Only explicit confirmation of the exact integrated result sets `已验收`; only evidence-bound main/ref advancement permits `CLOSED`.

The validator checks structure, IDs, statuses, markers, routing registration, duplicate event/deviation identities, forbidden flat governance files, links, and compatible refs. Lifecycle/workspace/train checks additionally prove authority, leases, baselines, candidate/integrated evidence, drift, and replay state. No validator can decide whether product requirements are good or fabricate user approval.

| Change | Iteration README | Root README | Progress | Deviation/evidence |
|---|---:|---:|---:|---:|
| Reserve/create iteration | Rebuild | Rebuild registry/focus | Append OPEN | Create ledger + allocation evidence |
| PRD/SPEC/authorization gate | Rebuild | Rebuild if routing changes | Append DECISION | Bind exact authority receipt |
| Worktree/topology/candidate state | Rebuild local/status projection | Rebuild operational projection | Append event when governance-significant | Persist lease/candidate receipt |
| Principle change/drift audit | Rebuild all affected | Rebuild global view | Append decision/audit | Bind principle change/impact receipt |
| Integration candidate/main advance | Rebuild from latest main | Rebuild from latest main | Append MERGE/CLOSE | Persist integrated/final evidence |
| Completed as-built difference | Rebuild | Rebuild if open-count/routing changes | Append checkpoint/decision | Update canonical deviation entry |
| Read-only status question | No | No | No | No |

README drift is repaired from authoritative inputs. Historical progress or accepted evidence is never rewritten merely to match today's derived view.
