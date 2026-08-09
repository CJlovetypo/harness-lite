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
4. Approve the PRD, authorize the SPEC, implement, and reconcile as-built facts.
5. Validate evidence and obtain explicit acceptance.
6. Preview and create the single final iteration commit.

See [SKILL.md](SKILL.md) for operating instructions and [the Harness contract](references/harness-contract.md) for the document model and lifecycle rules.

## Test

```text
python -m unittest discover -s scripts/tests -v
```

The suite covers initialization safety, Git preservation, path containment, secret and size gates, deviation validation, acceptance evidence, deterministic finalization, and rollback behavior.
