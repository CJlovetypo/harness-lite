# Adoption and Repair

## Contents

1. When to use this reference
2. Adopt existing documents safely
3. Repair a partial managed Harness
4. Git safety boundary

## 1. When to use this reference

Use this workflow only when the target already contains PRD/SPEC/decision documents or a foreign or partial `harness/`. Fresh initialization does not need it.

## 2. Adopt existing documents safely

Inventory before writing:

- resolve the project root and Git root, if present;
- list existing root instructions and governance-like files by path;
- identify which files are actively maintained versus historical;
- map each existing PRD to its SPEC, decisions, status, and completed as-built differences;
- note duplicate sources, missing pairs, unstable IDs, absolute links, and user changes;
- inspect Git history or blame only when needed to distinguish active and historical sources.

Choose one canonical target for every fact. Do not keep two editable PRDs or SPECs for the same iteration.

Prepare a proposed mapping before moving anything:

```text
old/path/goal.md -> harness/iterations/001/prd-001.md
old/path/spec.md -> harness/iterations/001/spec-001.md
old decisions    -> targeted events in harness/progress.md
old as-built gaps -> harness/iterations/001/deviation-001.md
```

Preserve original meaning, approvals, status, dates, and historical IDs. Migrate only completed as-built differences into the deviation ledger; route future change proposals through PRD/SPEC revision and reapproval. Add migration notes and repair active links, but do not rewrite history as though the final structure always existed.

File moves, deletions, or replacement of an existing source of truth can be destructive or materially change ownership. Obtain user approval for the mapping when the intended canonical source is not already explicit. Prefer Git-aware moves when authorized and available. Do not leave active redirect stubs that become a second editable source.

After adoption:

1. Ensure each migrated ID has exactly one complete iteration bundle.
2. Keep principles and approved PRD/SPEC as normative sources; keep deviations as factual ledgers and progress as historical evidence.
3. Rebuild L1 summaries from the authoritative bundle and targeted progress evidence.
4. Rebuild L0 from all L1 and authoritative statuses.
5. Run the validator and inspect Git diff/rename detection when Git is present.

The bundled initializer deliberately refuses to automate foreign-harness migration.

## 3. Repair a partial managed Harness

Run `validate` first. Classify findings:

- Missing empty scaffolding or a missing managed global file: re-run `init`; it may create only missing managed files.
- Incomplete iteration bundle: reconstruct only when the correct iteration identity and content source are unambiguous. Otherwise request direction.
- ID/path mismatch: treat the bundle as one atomic identity; rename directory, filenames, metadata, links, and registry entries together.
- README drift: rebuild the summary from PRD/SPEC/deviation and targeted progress events.
- Malformed ownership markers or foreign `harness/`: do not force or overwrite. Establish ownership and migration intent first.

Never allocate a new iteration while an existing numbered bundle is incomplete.

## 4. Git safety boundary

- Keep governance and implementation in the same repository when Git is used; do not create a nested repository for `harness/`.
- Preserve unrelated user changes and inspect diffs before and after adoption or repair.
- Do not stage with a broad pattern when exact paths are available.
- If the target has no Git repository, fresh Harness bootstrap may initialize Git and create one initial baseline commit only after an `init --dry-run` manifest has been reviewed and its exact `BASELINE_PLAN_TOKEN` is returned with `--accept-baseline-plan`. A path, byte, target-root, or rendered-Harness change invalidates that token before writes. This exception does not apply when Git already exists; initialization of an existing repository makes no commit and neither emits nor accepts the token.
- Create exactly one final commit for an iteration, and only after explicit user acceptance.
- Include dirty shared governance/control files only when each exact path is explicitly confirmed as part of the accepted iteration; finalization records `refs/project-harness/iterations/NNN/final`.
- Run and record repository-required checks before finalization because the deterministic finalizer does not execute commit hooks.
- Do not push, create a PR, rewrite history, or perform other destructive Git operations without explicit authorization.
- Do not commit generated artifacts, caches, local environments, secrets, or large runtime data merely because they exist beside governed source.
