# oxison

**oxison** is an AI-powered documentation engine: point it at any local repository and it comprehends the code, then writes a canonical set of product documentation back — without ever touching the target repo's files. It drives the [Claude Code](https://claude.com/claude-code) CLI as a structured, read-only subprocess and owns every write itself.

---

## Problem It Solves

Keeping documentation accurate requires someone to read the code, understand it holistically, and translate that understanding into prose. That work is time-consuming, quickly goes stale, and is often skipped entirely — especially on legacy or unfamiliar codebases.

oxison automates that loop. Instead of maintaining docs by hand, you regenerate them from the source of truth (the code itself) whenever you need them.

### Who It Is For

- **Developers onboarding to an unfamiliar or legacy codebase** who want a fast, reliable orientation.
- **Engineering teams** who want living product docs generated from the repo rather than maintained separately.
- **Solo engineers** who want publication-quality documentation without the writing overhead.
- **CI pipelines** that need to gate on up-to-date docs or emit a structured comprehension artifact (`comprehension.json`) for downstream tooling.
- **Anyone who wants to go further** — from documentation into an AI-generated, prioritised roadmap (`oxison plan`) or an autonomous code-building loop (`oxison build`).

oxison is safe to point at repos you don't fully control: its read-only safety model is structural, not advisory.

---

## Core Features and Capabilities

### Documentation generation (`oxison run`)

The primary command reads a repository and writes up to six artifacts into `./oxison-output/`:

| Artifact | Description |
|---|---|
| `PRODUCT.md` | What the software is, who it's for, core features, mental model |
| `MANUAL.md` | Prerequisites, install, configuration, usage, workflows |
| `STACK.md` | Languages, dependencies and versions, runtime and infra — grounded in the actual manifests |
| `ROADMAP-ANALYSIS.md` | *(if the repo has a roadmap)* Analysis of planned work, feasibility vs. current code, sequencing |
| `SECURITY-NOTES.md` | *(if no roadmap is found)* Lightweight read-only security surface scan |
| `COMPREHENSION.md` | The intermediate whole-repo understanding the docs are built from |

Two supporting files are always written: `repomap.json` (the deterministic repo map) and `.oxison-run.json` (per-step status and cost, used by `--resume`).

### Multi-source ingestion

Beyond the repository itself, oxison can merge additional non-repo sources into the comprehension pass — PDFs, PowerPoint presentations, Word documents, plain markdown, and audio/video recordings (via a cloud STT API, the one opt-in off-host path). Every adapter is read-only: source files are never modified. A `comprehension.json` envelope is emitted alongside the markdown, carrying a machine-readable provenance ledger for downstream consumers.

### Planning (`oxison plan`)

`oxison plan` consumes `comprehension.json` and produces a prioritised `roadmap.json` plus a human-readable `ROADMAP.md`. Each task carries a stable deterministic identifier, provenance back to the comprehension, dependency sequencing, and at least one observable acceptance criterion (a checkable end-state, not aspirational language). Every proposed roadmap passes a deterministic plan-gate before it is written; a roadmap that fails is fed back to the planner for one self-correcting pass and is never written if it still fails.

### Autonomous building (`oxison build`)

`oxison build` consumes a `roadmap.json` and runs a bounded autonomous build loop, dispatching one write-capable worker per task inside an isolated git worktree. Workers are sandboxed (filesystem + egress allowlist via `srt`, or rootless container isolation at `--sandbox-layer container`). A grader re-checks every worker's actual diff against a protected-path list before accepting it. Three guardrails bound every run: an iteration cap, a no-progress halt, and a spend ceiling. Start with `--dry-run`.

### Claude Code plugin

oxison ships as a Claude Code plugin installable directly from the marketplace (`/plugin marketplace add escotilha/oxison`). Invoking `/oxison /path/to/repo` inside a Claude Code session runs the full pipeline without opening a terminal — the skill resolves the `oxison` CLI via `uvx` if it isn't already on `PATH`.

### Resumable runs and cost controls

`--resume` skips steps already recorded as complete in `.oxison-run.json`. `--max-budget-usd` caps per-call spend. Every AI call's cost is reported and recorded. The default model is Opus; `--model claude-sonnet-4-6` reduces cost for routine runs.

---

## How It Works

From a user's perspective the pipeline is a four-stage cascade:

```
1. Map    (no AI)     →  repomap.json
   Walk the repo: language histogram, dependency manifests, entry points, service hints.

2. Comprehend (AI)   →  COMPREHENSION.md
   Small repos: one pass.
   Large repos: map-reduce — one AI worker per top-level directory, then a synthesis pass.

3. Generate (AI, parallel)  →  PRODUCT.md, MANUAL.md, STACK.md
   Three workers run concurrently, each reading the comprehension and the repo map.

4. Branch (AI)       →  ROADMAP-ANALYSIS.md  or  SECURITY-NOTES.md
   Detects whether a roadmap file exists and routes to the appropriate analysis.
```

Every AI call is a `claude -p` subprocess launched with `--allowedTools Read,Glob,Grep` — no shell, no write tools. Workers return text; oxison writes all files. After a run the target repository's git working tree is byte-for-byte unchanged.

---

## Notable Design Decisions

**Structural read-only enforcement, not policy.** The read-only guarantee for `run` and `plan` is not "the AI is instructed not to write." The worker is launched with only `Read`, `Glob`, and `Grep` as allowed tools. `Bash` is deliberately absent because, under `--permission-mode bypassPermissions`, a shell is a full write and execution primitive. A unit test asserts `Bash` is absent from the built command line so this cannot silently regress.

**oxison owns every write.** Workers return markdown; the pipeline owns all file writes. This means there is a single, auditable write path and workers cannot write to arbitrary locations even if they tried.

**Sandboxed build workers.** `oxison build` workers have full read/write tools by design, but are bounded: each runs in its own git worktree (the main working tree is never edited directly), wrapped in a filesystem + egress sandbox (`srt`, on by default), with a grader that re-checks the actual diff — not just the plan — against protected paths (`.github/workflows/`, `.env`, lockfiles, `.git/`, `oxison-build/`). A stronger container sandbox (`--sandbox-layer container`) physically removes the host filesystem from the worker's mount namespace.

**Crash-safe, idempotent build state.** The task store (`state.db`, SQLite) marks a task as dispatched before its worker spawns, guarded by a `WHERE status='planned'` transition. A crash or double-tick cannot re-dispatch in-flight work.

**Deterministic map, AI-driven comprehension.** The repo map (`repomap.json`) is produced without AI — it is a deterministic walk of the file tree. Only the comprehension and generation steps call Claude, making the map cheap, reproducible, and usable as structured input context for the AI stages.

**Graceful degradation of optional dependencies.** Source adapters for PDF, PowerPoint, and Word are optional extras. If the underlying library is absent, the file is logged as skipped-with-reason rather than raising an error. The OCR adapter (PaddleOCR) and roadmap-parser enrichment (`oxi_core`) are not declared dependencies at all; both are opportunistically used if importable.
