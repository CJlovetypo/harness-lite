# Project Instructions

<!-- project-harness:start v1 -->
## Harness Lite governance

Treat `harness/` as the only editable source of normative project-governance truth. Main's committed `harness/principle.md` is the single global principle authority; `progress.md` is immutable historical evidence; L0/L1 READMEs are derived routing; deviation records completed as-built facts and never grants approval or an exception.

Before authorized writes:

1. Read L0 `harness/README.md`, the relevant L1, committed main principles, and then only the PRD/SPEC/deviation/events needed by task intent.
2. Inspect Git root/status/worktrees/refs, active operation journals/leases, dirty/staged/untracked/ignored state, dependency candidates, and resource claims. Preserve user changes.
3. Route independently on three axes:
   - governance: grill the user on decision-bearing ambiguity; co-draft only small-and-clear work; otherwise PRD-first;
   - topology: read-only/drafting, Local sole writer, sibling worktree for writer 2+, stacked stable dependency, or serialize conflicting resources/principles;
   - authority: PRD approval, SPEC approval, implementation authorization, principle impact audit, integrated-result acceptance, and final closure are separate gates.
4. Report reason codes, current principle identity/drift, topology, blockers, and next gate. Classification or notification never implies approval.

Small-and-clear means decision-complete, localized, low risk, readily reversible, and without material cross-system coordination. For clear but non-small work, use PRD-first without grilling merely because it is large. Co-drafting changes timing only: never treat approval as implementation authorization.

Identity and lifecycle-v2:

- Allocate monotonic iteration/event identities only through the coordinator; create every iteration's README/PRD/SPEC/deviation bundle together. A new task alone is not a new iteration.
- Bind an immutable allocation base and a separate exact implementation start. The implementation start must descend from the allocation base and may include the committed approved-governance bundle. Editable PRD text cannot repoint either identity.
- `0 → 1`: the only active implementation PRD uses the primary checkout as Local; do not create an isolation-only worktree/branch/commit/stash.
- `1 → 2+`: add each later writer as a sibling linked worktree from its exact allowed committed start. Do not commit, stash, copy, move, or change the first Local PRD's cwd/files/index/runtime.
- `N → 1`: sticky drain; survivors stay in place. Return to Local only after all writers release and a later single PRD starts.
- If another PRD must advance main first, bind dirty Local A in place to its own branch only after exact ownership/Git preflight and before/after notification; preserve tree, index, cwd, and runtime and make no commit/stash.
- Every mutation validates repository, absolute path, iteration, owner/task, branch, allocation base, implementation start, lease generation, and operation before writing. Worktrees are not security sandboxes; namespace and claim ports, databases/schema, caches, logs, containers, accounts, and external environments.

Authority and reconciliation:

- Baseline authority is approved principles > approved PRD > approved SPEC. Do not implement without exact PRD approval, SPEC approval, and separate implementation authorization.
- A principle change needs the global lease, exact before/after text, stable change identity, explicit approval, and impact audit of all open PRDs. Hash drift blocks candidacy/integration until no-impact evidence or revised/reapproved baselines and revalidation exist.
- New progress events use globally unique event IDs separate from session IDs. Preserve legacy `S-*` blocks. Union by identity and exact bytes: same/same is idempotent, same/different blocks; append correction/resolution events instead of rewriting history.
- Rebuild README managed blocks and progress indexes from principles, bundles, events, refs, and bounded operational facts. Never use ours/theirs to make derived copies authoritative; preserve user-authored regions outside managed markers.
- Only the coordinating agent mutates shared principles/progress/routing or allocates IDs. Subagents return scoped evidence.

Candidate, integration, and acceptance:

- A feature candidate requires exact authority, current principle/impact audit, writer guard, AC verification, disposed material deviations, owned included paths/exclusions, and stable dependency identities. Persist evidence bound to exact ref/commit/tree, baselines, generation, principle hash, and receipt digests.
- Integrate one candidate at a time from exact latest main: revalidate dependencies, reconcile principles/progress, merge implementation and bundle, rebuild derived views, run cross-PRD/full verification, and persist the exact integrated identity.
- Implementation conflicts return to the owning PRD worktree. The integration lane may only apply deterministic, journaled governance normalization.
- Default to `merge --no-ff`. If any allowed strategy changes candidate commit identity, create a new integrated candidate, rerun verification, and bind fresh evidence.
- Advance main/final refs only after explicit confirmation of the exact latest-main integrated result; that confirmation may be final acceptance. Any main/tree/candidate/principle/evidence change invalidates it. Never auto-reset or rewrite accepted history.

Git transparency and recovery:

- Silent: reads, routing, validation, previews, evidence collection, and matching authorized replay.
- Notify before and after: worktree create/remove, safe branch create, Local in-place binding, candidate queue changes, and manifest-owned local runtime lifecycle. Include PRD, reason, both baselines, branch, absolute path, namespace, effects on other PRDs, remote involvement, and actual result.
- Confirm exact scope before approvals, commits, main advance/merge, risky cleanup, lease takeover, external/shared mutations, and final acceptance. A bounded standing authorization may cover WIP checkpoints, but each remains transparent and is recovery-only.
- Before every commit show exact branch, paths/tree, message, verification/evidence, exclusions, and `pushed=false`; afterward report hash, actual HEAD, and still-unpushed state.
- Push is not implemented in lifecycle-v2. Never push or imply push. A future push must be separate and explicitly confirmed with remote, source/target refs, exact range, and `force=false`.
- Never auto-stash/reset/clean/force, force-delete a dirty worktree, or use TTL alone for lease takeover.
- Every mutation uses an operation ID, exact accepted plan digest, journal, locks/leases, and ref/file CAS. Matching retries resume without duplicate IDs/events/worktrees/commits/notifications; drift stops for reconcile.
- Cleanup is last. Preserve objects with active claims, Git markers, staged/dirty/untracked/ignored assets, path links/junctions, or manifest uncertainty as `FAILED_NEEDS_RECONCILE`.

Compatibility and completion:

- Do not initialize nested Git or edit `.gitignore`. A no-Git bootstrap may create `main` and one exact reviewed baseline commit only from its matching `BASELINE_PLAN_TOKEN`; initialization of an existing repository creates no commit and absorbs no existing changes.
- Preserve completed legacy serial iterations, principle/history bytes, deviation entries, and legacy refs. Upgrade only from an exact dry run and replace only this bounded block; dirty active legacy work remains legacy unless an explicitly approved recoverable transition exists.
- Legacy one-active-iteration/one-final-commit rules apply only to unupgraded legacy iterations, not lifecycle-v2 work.
- Move to candidate/integrated/accepted/closed only with the corresponding exact evidence. Validate structure, authority, leases, baselines, reconciliation, evidence, project checks, and recovery state; never claim acceptance, integration, cleanup, or remote state that is not proven.

PRD-001 lifecycle-v2 bootstrap transition:

- The user explicitly approved OQ-001-01 and OQ-001-06: preserve the pre-existing drafting-path changes and the approved PRD-001 governance baseline as two local checkpoint commits before new implementation. These checkpoints are recovery points only; they are not candidate, integrated, final, or acceptance authority, and they must not be pushed.
- Preserve the legacy PRD-001 base anchor at `7376803cffb09269bc8a03346901b2e9e224d704`. Do not amend, squash, repoint, or hide the transition history, and do not use the legacy `commit-iteration` finalizer for PRD-001.
- Approved local recovery checkpoints are `6cc0104075b5394a3ed6c6933b59817832503aeb`, `2d1be71c835ea5bc7ff784f09282af1837ffce41`, `ca8223bbe9d214b8c05d294b70d852e3dc57ddec`, `721c2913e0f21f7102b7825e85b49e94d9bf6552`, `91c92a491427483e5d2c0624b4fa7183e5df568a`, and `5dcec6947c3e25c45647f01871c75e5c25b97f17`. On 2026-08-12 the user authorized the coordinator to create later non-final WIP checkpoints autonomously after exact-scope and verification review. Report each resulting hash, scope, and `pushed=false`; this standing authorization does not cover push, main integration, history rewrite, destructive Git, candidate/final authority, or final acceptance.
- The v2 journal/lease requirements govern Harness orchestration mutations once their implementation slice is available and validated. Until the complete lifecycle is accepted, PRD-001 source implementation remains single-writer Local work under exact checkpoint manifests; this narrow bootstrap allowance does not authorize actual worktree creation in this repository, main integration, remote writes, destructive Git operations, or bypassing product gates.
<!-- project-harness:end -->
