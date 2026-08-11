# Harness Lite

[English](README.md) | [简体中文](README.zh-CN.md)

Harness Lite is a Codex Skill for lightweight, Git-backed project governance. It keeps product intent, implementation design, as-built deviations, decisions, and acceptance evidence connected without turning every task into heavy process.

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

Each numbered iteration keeps its PRD, SPEC, factual as-built deviation ledger, and routing summary together.

## Core guarantees

- No product iteration is created during bootstrap.
- A project without Git receives one reviewed baseline commit on `main`; the dry-run manifest is bound to apply with `BASELINE_PLAN_TOKEN`.
- An existing Git repository is initialized without staging or committing its current changes.
- Drafting adapts to the request without weakening governance: material ambiguity triggers a targeted user grill, only small-and-clear changes may co-draft PRD and SPEC, and clear but larger changes remain PRD-first.
- A deviation records completed as-built facts versus an approved PRD/SPEC. It never grants approval or implementation authority.
- An iteration receives one final commit only after explicit acceptance of the completed result.
- Unrelated changes, ignored governance files, secrets, oversized files, malformed bundles, ambiguous evidence, and intermediate commits are blocked.
- Harness Lite never pushes automatically.

## Install

Place this repository at `harness-lite` under your Codex skills directory:

```text
<CODEX_HOME>/skills/harness-lite
```

Then invoke it as:

```text
$harness-lite
```

Example prompt:

```text
Use $harness-lite to initialize lightweight PRD/SPEC governance in the current project.
```

### Upgrading an existing managed project

Installing a newer Skill version changes `$harness-lite` behavior immediately, but `init` deliberately preserves an existing non-empty managed `AGENTS.md` block. To persist newer control rules such as the three drafting paths into an older project, request a reviewed replacement of only the bounded Harness Lite block; surrounding project instructions must remain untouched.

## CLI

The Skill normally drives the bundled CLI for you. To inspect it directly:

```text
python scripts/project_harness.py --help
python scripts/project_harness.py init --help
python scripts/project_harness.py new-iteration --help
python scripts/project_harness.py validate --help
python scripts/project_harness.py commit-iteration --help
```

The main workflow is:

1. Run `init --dry-run` and review every planned path and hash.
2. Initialize the global Harness without inventing a product iteration.
3. Create a numbered iteration only for a concrete product goal.
4. Inspect project context and choose the drafting path: grill unresolved product decisions, co-draft only when the change is small and clear, or keep clear but non-small work PRD-first.
5. Explicitly approve the identified PRD/SPEC baselines and separately authorize implementation, then implement and reconcile as-built facts.
6. Validate evidence and obtain explicit acceptance.
7. Preview and create the single final iteration commit.

See [SKILL.md](SKILL.md) for operating instructions and [the Harness contract](references/harness-contract.md) for the document model and lifecycle rules.

## Test

```text
python -m unittest discover -s scripts/tests -v
```

The suite covers initialization safety, Git preservation, path containment, secret and size gates, deviation validation, acceptance evidence, deterministic finalization, and rollback behavior.

Agent-behavior scenarios in [`evals/evals.json`](evals/evals.json) cover the small-and-clear co-draft path, the ambiguity grill path, and the clear-but-non-small PRD-first path.
