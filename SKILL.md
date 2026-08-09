---
name: harness-lite
description: Initialize, adopt, or validate a lightweight Git-backed project governance harness in the current project, with durable principles, numbered PRD/SPEC/as-built deviation bundles, progressive README routing, an append-only decision log, and acceptance-gated iteration commits. Use when users ask to initialize or introduce a project harness, PRD-Spec workflow, requirement governance, 项目治理, 需求管理, 上下文管理, Git-linked iterations, or numbered project iterations under harness/.
---

# Harness Lite

Create a small governance control plane beside the project's implementation. Keep product intent, implementation design, actual deviations, decisions, and validation evidence linked without turning every task into paperwork.

## Preserve the core contract

- Keep `harness/` as the only editable source of project-governance truth.
- Use the baseline authority order: approved principles > approved PRD > approved SPEC. A deviation is descriptive evidence of the implemented result versus that baseline, never an approval source, implementation authorization, or exception to it. Treat READMEs as derived routing and `progress.md` as historical evidence.
- Store every numbered iteration in `harness/iterations/NNN/` as one complete bundle: `README.md`, `prd-NNN.md`, `spec-NNN.md`, and `deviation-NNN.md`.
- Use L0 `harness/README.md` first, then the relevant L1 iteration README, then only the L2/L3 documents required by the task.
- Never create a new iteration merely because a new conversation starts. Create one only for a new product goal, scope, user-visible behavior, public contract, workflow, or acceptance target.
- Do not infer approval. Drafting or reviewing a PRD/SPEC does not authorize implementation unless the user's request explicitly does so.

Read [references/harness-contract.md](references/harness-contract.md) before changing the document model, lifecycle, statuses, or authority rules. Read [references/adoption.md](references/adoption.md) only when adopting existing governance documents or repairing a partial Harness.

## Resolve the target project

1. Use a user-supplied project path when present.
2. Otherwise use the current Git repository root from `git rev-parse --show-toplevel`.
3. If no Git root exists, use the current working directory as the project root. Initialization must create a repository there with `git init -b main`; never create a nested repository inside an ancestor repository.
4. Resolve and report the absolute target before writing. Never initialize the Skill's own installation directory unless that directory is explicitly the user's project.
5. Inspect `AGENTS.md`, `harness/`, existing PRD/SPEC-like documents, Git identity/status when Git exists, and the files that Git would include in a new repository. Preserve all user changes.

## Classify the project before applying

- **Fresh**: no `harness/` exists. Initialize normally.
- **Managed**: `harness/README.md` contains a recognized Harness Lite ownership marker. Re-run initialization only to add missing managed files; never overwrite non-empty files. Continue to recognize the legacy marker in projects initialized before the Skill rename.
- **Partial or foreign**: `harness/` contains content without the ownership marker, managed markers are malformed, or legacy governance files may conflict. Stop before writing. Follow the adoption reference and request a choice only when source-of-truth ownership or file movement is genuinely ambiguous.
- **Already valid**: run validation and report the result. Do not manufacture changes.

## Initialize safely

Resolve a Python 3 interpreter, then run a dry run first:

```text
<python> <skill-dir>/scripts/project_harness.py init \
  --project-root <absolute-project-root> \
  --project-name <human-readable-name> \
  --dry-run
```

Review every planned path, size, and SHA-256. When the target has no Git repository, also copy the emitted `BASELINE_PLAN_TOKEN`; it binds the reviewed manifest and rendered bootstrap timestamp. If the plan is in scope, apply it with:

```text
<python> <skill-dir>/scripts/project_harness.py init \
  --project-root <absolute-project-root> \
  --project-name <human-readable-name> \
  --accept-baseline-plan <exact-token-from-dry-run>
```

If Git already exists, apply with the original command after removing only `--dry-run`; no baseline-plan token is emitted or accepted because initialization will not commit.

The initializer may:

- create the three global Harness documents and `harness/iterations/`;
- create or append one bounded managed block in root `AGENTS.md`;
- record the bootstrap in the append-only progress log;
- when the target has no Git repository, require the exact plan token from a preceding dry run, run `git init -b main`, recheck every existing non-ignored file together with the new Harness files against the accepted manifest, stage exactly that set, inspect the staged diff, and create one initial baseline commit.

It must not edit `.gitignore`, force-add ignored files, move legacy documents, overwrite non-empty files, install dependencies, push, create a PR, rewrite history, or modify product code. In an existing repository, initialization does not create a commit or absorb pre-existing changes.

If Git already exists but `HEAD` is unborn, initialization still does not infer authority to commit. Report that state and stop before `new-iteration`; the user must separately authorize a project baseline commit.

For a new repository, treat the baseline commit as part of successful initialization rather than as an iteration commit. Include all and only existing non-ignored project files plus the initialized Harness/`AGENTS.md`; inspect for unexpected, sensitive, or oversized files before committing. Any path, byte, project-root, or rendered-Harness change after dry run invalidates the accepted plan token before writes. If the preview is unsafe or Git cannot create the repository/commit, stop and report the exact blocker instead of leaving a claimed completed baseline.

## Create an iteration only when needed

If the invoking request contains a concrete new product change, initialize the base first and then create a complete bundle:

```text
<python> <skill-dir>/scripts/project_harness.py new-iteration \
  --project-root <absolute-project-root> \
  --title <iteration-title> \
  --dry-run
```

After reviewing the plan, apply without `--dry-run`. The command requires an attached Git branch with an existing baseline commit, records both the baseline commit and branch in the PRD, and independently anchors them at `refs/project-harness/iterations/NNN/base/refs/heads/...`. It allocates the next monotonic ID, creates all four files together, verifies that none is ignored, updates the L0/index routing, and appends an OPEN event. This version is serial: do not allocate another iteration until every existing iteration has a reachable final marker and governance is clean.

Then:

1. Replace template prompts in the PRD with the actual background, goals, stable requirement IDs, acceptance IDs, non-goals, constraints, and open questions.
2. Keep implementation details out of the PRD.
3. Obtain the required product approval before implementation unless the user's current request already explicitly authorizes it.
4. Complete the same-number SPEC with requirement traceability, architecture, contracts, execution, migration/rollback, risks, and verification.
5. When a different requirement or implementation approach is known before it is built, revise and re-approve the affected PRD/SPEC; do not pre-authorize it through deviation.
6. Only after the SPEC implementation is complete, reconcile the actual result against the exact approved PRD/SPEC baseline. Record every material factual difference in the same-number deviation ledger with discovery time, exact requirement/acceptance and SPEC references, cause, impact, acceptance impact, explicit disposition, verification, and closure evidence. A deviation is factual evidence, not an approval source or implementation authorization. Preserve the compared baseline and never rewrite it to erase that history.
7. Append CHECKPOINT/DECISION/CLOSE evidence and keep L1/L0 reconciliation status and unresolved-actual-deviation counts synchronized when their routing facts change.

Only the coordinating agent allocates iteration, session, or deviation IDs and updates shared routing/log files. Subagents return findings and implementation results without independently editing those shared governance files.

## Validate and hand off

Run validation after initialization and after structural changes:

```text
<python> <skill-dir>/scripts/project_harness.py validate \
  --project-root <absolute-project-root>
```

Treat structural errors as incomplete work. Fix only managed, unambiguous issues; do not overwrite user-authored content to make validation pass.

Report:

- the absolute project root;
- whether this was fresh initialization, repair, validation-only, or iteration creation;
- created and updated paths;
- validation outcome and any warnings;
- whether Git already existed or was initialized, and the initial baseline commit when one was created;
- the next governance gate, usually “define principles” or “complete/approve PRD-NNN.”

Do not create intermediate commits during an iteration. After implementation and as-built reconciliation are complete, present the acceptance evidence and wait for the user to explicitly accept the completed result. Approval to begin implementation is not final acceptance. Only that affirmative acceptance changes the PRD to `已验收` and authorizes one final iteration commit containing all and only that iteration's implementation, tests, Harness records, and explicitly in-scope control-file changes. Do not mark an iteration complete while PRD/SPEC template prompts, missing PRD approval evidence, or an unidentified approved SPEC baseline remain.

After recording the acceptance evidence and synchronizing PRD/SPEC/L1/L0/progress, preview the exact commit:

```text
<python> <skill-dir>/scripts/project_harness.py commit-iteration \
  --project-root <absolute-project-root> \
  --number <NNN> \
  --include <implementation-or-test-path> \
  --include harness/README.md \
  --include harness/progress.md \
  --dry-run
```

Repeat `--include` for each changed product/test path and for every changed shared control file that the user has confirmed belongs to this iteration. The same-number four-file bundle is the only automatic scope; shared files such as `AGENTS.md`, `harness/README.md`, `harness/principle.md`, `harness/progress.md`, and `harness/iterations/.gitkeep` require exact explicit inclusion when dirty. Then run the same command without `--dry-run`.

The finalizer leaves unrelated paths unstaged and refuses any staged/intent-to-add index state, detached or different branch, baseline text that differs from its independent base anchor, unresolved/malformed deviation, missing affirmative user acceptance of the completed result, missing CLOSE verification evidence, ignored/incomplete bundle files, or any intermediate/prior final commit visible through refs or reflogs. It rescans staged blobs after Git filters, binds the reviewed tree to the anchored baseline with an atomic ref transaction, and records `refs/project-harness/iterations/NNN/final`. Repository commit and reference-transaction hooks are deliberately not run; execute and record all required project verification before finalization. Review the exact paths, blob hashes, and staged stat. If unrelated changes share an included file and cannot be separated safely, stop and ask. Require the commit message to contain `PRD-NNN`, report the resulting hash, and never write that hash back into the same commit.

Never push as part of this workflow. A Harness initialization or accepted iteration ends at the local commit.
