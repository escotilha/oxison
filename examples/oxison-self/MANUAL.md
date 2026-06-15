# oxison User Manual

**oxison** is an AI-powered documentation engine. Point it at any local repository and it comprehends the codebase — without touching a single file in it — and writes a canonical set of product docs into its own output directory.

---

## Prerequisites

### Required

| Requirement | Version | Notes |
|---|---|---|
| **Python** | ≥ 3.11 | `tomllib` (used internally) is stdlib from 3.11 |
| **Claude Code CLI** (`claude`) | any current release | Install from [claude.com/claude-code](https://claude.com/claude-code); run `claude` once to sign in |

The `claude` binary must be on your `PATH` and signed in before any oxison command will work. oxison calls `claude --version` as its very first action (preflight) and exits with code `3` if the check fails — nothing is spent on AI calls before then.

### Optional, by feature

| Feature | What to install |
|---|---|
| Richer terminal output | `pip install 'oxi-son[pretty]'` — adds `rich ≥ 13` |
| PDF text extraction | `pip install 'oxi-son[pdf]'` — adds `pypdf ≥ 4` |
| PowerPoint extraction | `pip install 'oxi-son[pptx]'` — adds `python-pptx ≥ 0.6` |
| Word document extraction | `pip install 'oxi-son[docx]'` — adds `python-docx ≥ 1.1` |
| All three source adapters at once | `pip install 'oxi-son[sources]'` |
| Scanned-PDF OCR (`--ocr`) | The unpublished `document_extraction` package (heavy PaddleOCR); not an oxison dependency — files degrade gracefully to "skipped" if absent |
| Autonomous build sandbox (Layer 1) | `npm i -g @anthropic-ai/sandbox-runtime` — required for `oxison build` unless `--no-sandbox` is passed |
| Autonomous build sandbox (Layer 2) | `podman` (or `docker`) plus the worker image built from `docker/oxfaz-worker/` |

---

## Installation

oxison is not published to PyPI. Install directly from the GitHub repository.

### Zero-install, always-latest (recommended)

```bash
uvx --from git+https://github.com/escotilha/oxison oxison run /path/to/repo
```

`uvx` creates an isolated environment on the fly. No global install; uses the latest commit on `main`.

### Pinned release

```bash
pip install "git+https://github.com/escotilha/oxison.git@v0.1.0"
```

### From a local clone

```bash
# zero-install from the clone directory
uvx --from . oxison run /path/to/repo

# or install into the active environment
pip install -e .
```

### Install as a persistent global command

```bash
uv tool install git+https://github.com/escotilha/oxison
# or
pipx install git+https://github.com/escotilha/oxison
```

After either command, `oxison` is available on your `PATH` directly.

---

## Configuration

oxison does not use a config file. All configuration is passed via command-line flags or environment variables.

### Environment variables

| Variable | Scope | Purpose |
|---|---|---|
| `OXISON_API_KEY` | `run`, `plan`, `build` | API key for bare (CI) auth mode. Takes precedence over `ANTHROPIC_API_KEY`. |
| `ANTHROPIC_API_KEY` | `run`, `plan`, `build` | Fallback API key for bare mode if `OXISON_API_KEY` is not set. |

**Key resolution order:** `--api-key` flag → `OXISON_API_KEY` → `ANTHROPIC_API_KEY`.

### Auth modes

| Mode | When it applies | What it requires |
|---|---|---|
| **OAuth** (default) | Interactive desktop use | The `claude` CLI already signed in — nothing else to configure |
| **Bare** | CI / headless / container builds | `OXISON_API_KEY` or `ANTHROPIC_API_KEY` set, or `--api-key` passed |

Bare mode is activated by passing `--bare`, by passing `--api-key`, or automatically when an API key env var is detected. Bare mode is **required** for `oxison build --sandbox-layer container`, because the macOS Keychain OAuth store is not reachable inside a Linux container.

---

## Usage

### Quick reference

```bash
oxison run    <repo>         # comprehend a repo; write docs
oxison plan   <output-dir>   # turn comprehension.json into a roadmap
oxison build  <output-dir>   # run the autonomous build loop (writes code)
oxison version               # print version and banner
```

---

### `oxison run` — document a repository

Reads a local repository, comprehends it with read-only AI workers, and writes product documentation into an output directory. **The target repository is never modified.**

```
oxison run <target> [options]
```

| Flag | Default | Description |
|---|---|---|
| `--output-dir PATH` | `./oxison-output` | Where to write all artifacts |
| `--model MODEL` | Claude default (Opus) | Override the Claude model for every AI call |
| `--max-budget-usd N` | none | Hard dollar cap passed to each individual `claude` call |
| `--chunk-threshold N` | `100000` | Estimated token count above which the comprehension stage switches to map-reduce (one worker per top-level directory + a synthesis pass) |
| `--max-concurrency N` | `4` | Maximum concurrent `claude` subprocesses |
| `--resume` | off | Skip pipeline steps already marked done in `.oxison-run.json` |
| `--add PATH` | — | Add a non-repo source file (PDF, pptx, docx, md, audio/video); repeatable |
| `--sources DIR` | — | Ingest every supported file in a directory |
| `--ocr` | off | Enable scanned-PDF OCR (requires optional `document_extraction` package) |
| `--stt-key KEY` | — | Cloud STT API key to enable audio/video transcription |
| `--stt-provider NAME` | `openai` | STT provider to use (e.g. `deepgram`) |
| `--bare` | off | Use bare (API key) auth instead of the Claude Code OAuth login |
| `--api-key KEY` | — | Explicit API key (implies `--bare`) |

**Examples:**

```bash
# Minimal: document a repo with defaults
oxison run /path/to/repo

# Write artifacts to a specific directory
oxison run /path/to/repo --output-dir ./docs

# Use Sonnet instead of Opus (much cheaper for routine runs)
oxison run /path/to/repo --model claude-sonnet-4-6

# Cap spend per AI call
oxison run /path/to/repo --max-budget-usd 5

# Both: Sonnet with a $2 per-call cap (good default for CI)
oxison run /path/to/repo --model claude-sonnet-4-6 --max-budget-usd 2

# Resume an interrupted run (skips steps already completed)
oxison run /path/to/repo --resume

# Tune map-reduce cutover (default 100 000 estimated tokens)
oxison run /path/to/repo --chunk-threshold 60000

# Add supplementary sources alongside the repo
oxison run /path/to/repo --add spec.pdf --add deck.pptx --add notes.md

# Ingest a whole directory of source files
oxison run /path/to/repo --sources ./inputs/

# Scanned PDFs (requires optional document_extraction package)
oxison run /path/to/repo --add scanned.pdf --ocr

# Audio/video transcription (sends data to cloud STT — the one off-host path)
oxison run /path/to/repo --add demo.mp4 --stt-key "$STT_KEY" --stt-provider deepgram

# CI bare-mode auth via environment variable
ANTHROPIC_API_KEY=sk-… oxison run /path/to/repo --bare
```

#### Output artifacts

After a successful run, `./oxison-output/` contains:

| File | Contents |
|---|---|
| `PRODUCT.md` | What the software is, who it is for, core features, mental model |
| `MANUAL.md` | Prerequisites, install, configuration, usage, workflows |
| `STACK.md` | Languages, dependencies and versions grounded in the manifests |
| `ROADMAP-ANALYSIS.md` | Analysis of the repo's own roadmap (produced when a roadmap file is detected) |
| `SECURITY-NOTES.md` | Lightweight security surface scan (produced when no roadmap is found) |
| `COMPREHENSION.md` | The intermediate whole-repo understanding the docs are built from |
| `comprehension.json` | Machine-readable schema-1.0 envelope; the contract for `oxison plan` |
| `repomap.json` | Deterministic repo map: languages, deps, entry points, services |
| `.oxison-run.json` | Per-step status and cost; enables `--resume` |

`comprehension.json` is always emitted when any `--add` or `--sources` input is given; it is also the input required by `oxison plan`.

---

### `oxison plan` — generate a prioritised roadmap

Consumes the `comprehension.json` produced by `oxison run` and emits a prioritised, gated `roadmap.json` plus a human-readable `ROADMAP.md`. The planner worker is read-only; oxison owns all writes.

```
oxison plan <comprehension> [options]
```

`<comprehension>` may be a path to `comprehension.json` directly, or to the output directory that contains it (e.g. `./oxison-output`).

| Flag | Default | Description |
|---|---|---|
| `--output-dir PATH` | Same directory as the comprehension | Where to write `roadmap.json` and `ROADMAP.md` |
| `--repo PATH` | none | Optional: ground the planner in the actual repo (read-only) |
| `--answers-file PATH` | — | Text file of user guidance to refine the roadmap |
| `--max-tasks N` | `40` | Reject a proposed roadmap that exceeds this task count |
| `--model MODEL` | Claude default | Override the Claude model |
| `--max-budget-usd N` | none | Hard dollar cap for the planner call |
| `--bare` / `--api-key` | — | Auth (same as `run`) |

**Examples:**

```bash
# Basic: plan from a comprehension
oxison plan ./oxison-output

# Ground the planner in the actual repo for richer context
oxison plan ./oxison-output --repo /path/to/repo

# Inject written guidance (answers to the open questions the planner raised)
oxison plan ./oxison-output --repo /path/to/repo --answers-file notes.txt

# Cap the plan to 20 tasks maximum
oxison plan ./oxison-output --max-tasks 20
```

Every proposed roadmap passes a deterministic plan-gate before it is written: non-empty titles, valid task kinds, observable acceptance criteria, no dependency cycles or dangling references, and no task targeting a protected path (CI config, `.env`, lockfiles, `.git/`). A roadmap that fails the gate is given one self-correcting pass; if it still fails, it is never written and the command exits with an error.

---

### `oxison build` — autonomous build loop

Ingests a `roadmap.json` from `oxison plan` and runs a supervised build loop, dispatching one write-capable worker per task into an isolated git worktree. **This command writes code.** Always start with `--dry-run`.

```
oxison build <roadmap> --repo <path> [options]
```

`<roadmap>` may be a path to `roadmap.json` directly, or to the output directory that contains it.

| Flag | Default | Description |
|---|---|---|
| `--repo PATH` | **required** | The git repository to build in |
| `--dry-run` | off | Show the plan; spawn no workers |
| `--max-ticks N` | none | Hard ceiling on loop iterations (LP1 guardrail) |
| `--budget-ceiling-usd N` | none (inactive) | Total run-level cost ceiling; halts the loop when reached (LP3 guardrail) |
| `--max-workers N` | `1` | Tasks dispatched per tick |
| `--no-progress-ticks N` | `5` | Halt after N consecutive ticks with nothing advancing (LP2 guardrail) |
| `--worker-budget-usd N` | `5.0` | Per-worker hard cost cap; also the minimum charged for a timed-out worker |
| `--sandbox-layer` | `srt` | `srt` (Layer 1: filesystem + egress allowlist via `@anthropic-ai/sandbox-runtime`) or `container` (Layer 2: rootless container) |
| `--no-sandbox` | off | Disable the sandbox entirely — **only on repos you fully trust**; prints a loud stderr warning |
| `--bare` / `--api-key` | — | Auth (same as `run`) |
| `--model MODEL` | Claude default | Override the Claude model |

**Examples:**

```bash
# Always preview first
oxison build ./oxison-output --repo ~/code/myrepo --dry-run

# Bounded real run: 20 ticks, $50 ceiling, default srt sandbox
oxison build ./oxison-output --repo ~/code/myrepo \
  --max-ticks 20 --budget-ceiling-usd 50

# Stronger isolation: rootless container (requires podman/docker + API key)
ANTHROPIC_API_KEY=sk-… oxison build ./oxison-output \
  --repo ~/code/myrepo --sandbox-layer container

# Trusted local repo only — no sandbox
oxison build ./oxison-output --repo ~/code/myrepo --no-sandbox
```

Build state is stored in `<repo>/oxison-build/state.db` (SQLite). Worker branches land under `<repo>/oxison-build/worktrees/`. The main working tree of the target repo is never directly modified.

#### Setting up the container sandbox (Layer 2)

```bash
# Install a container runtime (once)
brew install podman && podman machine init && podman machine start

# Build the worker image (once, from the oxison repo)
podman build -t localhost/oxfaz-worker:latest docker/oxfaz-worker

# Run with Layer 2
ANTHROPIC_API_KEY=sk-… oxison build ./oxison-output \
  --repo ~/code/myrepo --sandbox-layer container
```

> **macOS note:** the repo must live under `$HOME` for the podman VM to mount it. Paths under `/tmp` or external volumes will not mount.

---

### `oxison version`

```bash
oxison version
```

Prints the version number and ASCII banner. `oxison --version` also works.

---

## Common Workflows

### Document an unfamiliar repo quickly

```bash
# Use Sonnet with a per-call cap for cost efficiency
oxison run /path/to/repo --model claude-sonnet-4-6 --max-budget-usd 2
# Read the output
cat ./oxison-output/PRODUCT.md
cat ./oxison-output/MANUAL.md
```

### Re-run with updated sources without starting over

If any pipeline step failed or you want to regenerate just the docs without re-running the (already-complete) comprehension:

```bash
oxison run /path/to/repo --resume
```

The free deterministic map step always re-runs; AI steps with a completed record in `.oxison-run.json` are skipped.

### Document a repo plus a product spec

```bash
oxison run /path/to/repo \
  --add spec.pdf \
  --add architecture-deck.pptx \
  --add decisions.md \
  --model claude-sonnet-4-6
```

All sources are merged into one provenance-tagged comprehension pass. `comprehension.json` records which files were successfully extracted.

### Full three-stage pipeline: document → plan → build

```bash
# Stage 1: comprehend and document
oxison run /path/to/repo --model claude-sonnet-4-6

# Stage 2: produce a roadmap from the comprehension
oxison plan ./oxison-output --repo /path/to/repo

# Stage 3: preview the planned work (safe)
oxison build ./oxison-output --repo /path/to/repo --dry-run

# Stage 3 (live): run the build loop with guardrails
oxison build ./oxison-output --repo /path/to/repo \
  --max-ticks 10 --budget-ceiling-usd 20 --worker-budget-usd 3
```

### Headless / CI use

```bash
# Set the API key in the environment; pass --bare explicitly
export ANTHROPIC_API_KEY="sk-ant-..."
oxison run /path/to/repo \
  --bare \
  --model claude-sonnet-4-6 \
  --max-budget-usd 2 \
  --output-dir ./docs/oxison-output
```

Key resolution in CI: `--api-key` flag → `OXISON_API_KEY` → `ANTHROPIC_API_KEY`.

### Use oxison from inside Claude Code (no terminal needed)

Install the plugin once:

```
/plugin marketplace add escotilha/oxison
/plugin install oxison@oxison
```

Then invoke it by path or by asking:

```
/oxison /path/to/repo
```

The skill resolves the `oxison` CLI automatically (zero-install via `uvx` if not on `PATH`), defaults to Sonnet with a `--max-budget-usd 2` per-call cap, runs the full pipeline, and verifies the target repo was left byte-for-byte unchanged afterward. Pass `--opus` to use Opus, or `--full-budget` to remove the budget cap.

To update the plugin:

```
/plugin marketplace update oxison
```

To install the skill manually without the plugin system:

```bash
mkdir -p ~/.claude/skills
cp -r /path/to/oxison/skills/oxison ~/.claude/skills/oxison
```

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `2` | Configuration error — bad target path, bad flag, or `--bare` without a key |
| `3` | Preflight failed — Claude CLI not found, not on `PATH`, or not authenticated |
| `4` | Comprehension failed |
| `5` | Artifact generation failed |
| `6` | Branch step failed (roadmap analysis or security scan) |

---

## Troubleshooting

### `oxison: preflight failed: 'claude' is not installed or not on PATH`

The Claude Code CLI is missing or not on `PATH`.

1. Install it from [claude.com/claude-code](https://claude.com/claude-code).
2. Run `claude` once interactively to complete sign-in.
3. Confirm: `claude --version`.

### `oxison: config error: bare mode requires an API key`

You passed `--bare` but no key is available. Set one of:

```bash
export OXISON_API_KEY="sk-ant-..."
# or
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or pass it directly: `--api-key sk-ant-...`. Drop `--bare` entirely to use your Claude Code OAuth login instead.

### `oxison: config error: target path does not exist`

The path passed as the target does not exist or is not a directory. Use an absolute path and confirm it with `ls /path/to/repo`.

### `oxison build` fails with `srt runtime is not installed`

The Layer 1 sandbox requires `@anthropic-ai/sandbox-runtime`:

```bash
npm i -g @anthropic-ai/sandbox-runtime
```

If you cannot install it and the repo is fully trusted, add `--no-sandbox` (a loud warning will be printed to stderr). Never use `--no-sandbox` on repos you do not trust.

### `oxison build --sandbox-layer container` fails with `image not found`

Build the worker image first (from the oxison repo):

```bash
podman build -t localhost/oxfaz-worker:latest docker/oxfaz-worker
# or
docker build -t localhost/oxfaz-worker:latest docker/oxfaz-worker
```

### `oxison build --sandbox-layer container` fails with `needs an API key`

The macOS Keychain is not reachable inside a container. Set an API key:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### `oxison build --sandbox-layer container` fails to mount the repo on macOS

The repo must live under `$HOME` for the podman VM to mount it. Move the repo to `~/code/` or another path under your home directory.

### The run is expensive

oxison defaults to Claude's default model (Opus). For routine runs, pass `--model claude-sonnet-4-6` and `--max-budget-usd 2` (per-call cap). Cost per call is recorded in `.oxison-run.json` after each step. For a large repo the comprehension stage spawns one worker per top-level directory plus a synthesis pass — tune `--chunk-threshold` upward to reduce the number of workers, or downward to split more aggressively.

### An AI step failed partway through

Re-run with `--resume`. oxison reads `.oxison-run.json` and skips every step that is already marked done — the free deterministic map step always re-runs regardless.

### Source adapter skips a file (`logged as skipped-with-reason`)

PDF, pptx, and docx adapters require the `sources` extra:

```bash
pip install 'oxi-son[sources]'
```

OCR for scanned PDFs requires the optional `document_extraction` package (not an oxison dependency; install it separately if available). Audio/video transcription requires `--stt-key`.
