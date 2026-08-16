# Harness Lite

[English](README.md) | [简体中文](README.zh-CN.md)

Harness Lite is a Codex Skill for lightweight, Git-backed product governance. You describe product work in normal language; Harness routes the requirement, keeps each PRD's implementation isolated when concurrency actually requires it, and preserves the chain from global principles to the exact result accepted into main.

## What the user experiences

You normally stay in one Codex project and talk in terms of requirements, not paths or Git commands:

1. Harness inspects the repository and independently decides the governance path, execution topology, and current authorization gate.
2. With one active implementation PRD, work stays in the primary checkout as **Local**—no extra linked worktree is created.
3. When a second PRD becomes an active writer, Harness announces and creates a sibling linked worktree from its exact committed implementation start. The first PRD's cwd, files, index, untracked state, and runtime remain untouched.
4. A third or later PRD gets another isolated task/worktree/lease/runtime namespace. Dependencies and exclusive resources can instead force stacking or serialization.
5. When concurrency drops, survivors stay where they are until completion; Harness does not move them just to return to a one-worktree shape.
6. Each PRD independently passes approved PRD/SPEC, implementation authorization, acceptance evidence, deviation disposition, and feature-candidate gates.
7. A single merge train rebuilds governance on exact latest main, runs cross-PRD verification, and presents the exact integrated result for final confirmation.

Routine reads, routing, validation, and recovery are low-noise. Worktree creation/removal and branch binding are announced before and after. Commits always disclose exact scope, message, verification, exclusions, resulting hash, and `pushed=false`. Push is not implemented in this lifecycle version.

## Governance model

```text
committed global principle
  -> approved PRD
  -> approved SPEC
  -> authorized implementation
  -> feature candidate evidence
  -> latest-main integrated evidence
  -> exact-result acceptance
```

- Main's committed `harness/principle.md` is the one global principle authority for all PRDs and worktrees. Principle drift requires an impact audit before candidacy or integration.
- `harness/progress.md` is immutable event history. Parallel branches union events by ID and exact bytes; corrections and resolutions are appended.
- L0/L1 READMEs are derived routers rebuilt from authoritative documents, events, refs, and bounded operational facts.
- A deviation records a completed as-built fact. It never approves scope, authorizes implementation, or creates a principle exception.
- Default integration is `merge --no-ff`. If another declared strategy changes candidate commit identity, Harness creates a new integrated candidate, reruns verification, and rebinds evidence.

## What it creates

```text
AGENTS.md
harness/
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

Lifecycle-v2 also stores reconstructible local journals, leases, workspace routing, and evidence receipts under Git's common directory. Absolute worktree paths and local runtime details are not committed as normative governance.

Each iteration binds two different baselines:

- **allocation base**: the immutable identity used to reserve the PRD and prove ancestry;
- **implementation start**: the exact committed snapshot from which implementation begins, often after the approved governance bundle commit.

## Core guarantees

- Bootstrap creates no fake product iteration. A project without Git receives one reviewed baseline commit bound to `BASELINE_PLAN_TOKEN`; initialization of an existing repository does not stage or commit current changes.
- Three independent axes prevent a “small request” shortcut from also deciding worktree use or implementation authority.
- The first writer is Local; only writer 2+ creates linked worktrees. Adding B never commits, stashes, copies, or moves dirty A.
- One writer lease and exact root/path/branch/base guard protect every PRD. Worktrees are checkout isolation, not security or runtime sandboxes.
- Candidate and integrated evidence bind exact commits/trees, principles, dependencies, verification, and authority receipts. A raw ref is not enough.
- Journals and compare-and-swap make allocation, worktree creation, reconciliation, and main advance replayable without duplicate IDs, events, worktrees, or commits.
- Cleanup is conservative: dirty, staged, untracked, ignored, linked/junction, active process/lease, or unknown state is preserved for reconcile.
- No automatic stash/reset/clean/force, no hidden main advance, and no push command.

## Install

Place this repository at `harness-lite` under your Codex skills directory:

```text
<CODEX_HOME>/skills/harness-lite
```

Then invoke it with `$harness-lite`, for example:

```text
Use $harness-lite to govern this product change. Keep parallel PRDs isolated and show me only decisions and meaningful Git state changes.
```

## CLI and compatibility

The Skill normally orchestrates the tools for you. Useful inspection entry points are:

```text
python scripts/project_harness.py init --help
python scripts/project_harness.py validate --help
python scripts/harness_lifecycle.py status --help
python scripts/harness_lifecycle.py route --help
python scripts/harness_lifecycle.py plan-start --help
python scripts/harness_lifecycle.py start --help
python scripts/harness_upgrade.py --help
```

Completed legacy serial iterations remain readable and verifiable. Upgrade uses an exact dry-run plan, preserves old principles/events/deviations/refs, and replaces only the bounded `AGENTS.md` block. Legacy `new-iteration` and `commit-iteration` remain compatibility tools for unupgraded iterations; their one-active-iteration/one-final-commit rules do not govern lifecycle-v2 iterations.

See [SKILL.md](SKILL.md) for operating instructions and [the Harness contract](references/harness-contract.md) for the full authority, topology, evidence, and recovery contract.

## Test

```text
python -m unittest discover -s scripts/tests -v
```

The suite covers initialization and legacy compatibility, three-axis routing, atomic allocation, Local/worktree transitions including dirty-A/B-first behavior, principle/progress reconciliation, candidate/integrated evidence, merge-train identity, transparent interactions, concurrency, and crash recovery. Agent-behavior scenarios in [`evals/evals.json`](evals/evals.json) exercise the same user-facing decisions.
