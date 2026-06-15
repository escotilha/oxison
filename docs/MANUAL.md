# oxison User Manual

oxison comprehends any local git repository and writes four product artifacts back to disk: `PRODUCT.md`, `MANUAL.md`, `STACK.md`, and either `ROADMAP-ANALYSIS.md` or `SECURITY-NOTES.md`. It works in three independent stages — comprehend (`run`), plan (`plan`), build (`build`) — each producing a machine-readable JSON contract that feeds the next.

---

## Prerequisites

### Runtime

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.11 | `tomllib` is stdlib from 3.11; earlier releases will not work |
| Claude Code CLI | any current release | Must be installed and authenticated before using oxison |

### Claude Code authentication

oxison drives the Claude Code CLI as a subprocess. The default auth mode (OAuth) reuses your existing Claude Code login — nothing extra to configure. For CI or headless environments, an API key is required instead (see [Auth modes](#auth-modes)).

Verify the CLI is reachable and signed in before running:

```bash
claude --version
```

If `claude` is not on your `PATH`, or `claude -p "hi" --bare` fails, fix that before proceeding.

### Optional: sandbox runtime (required for `oxison build`)

The build stage sandboxes every worker with Anthropic's `@anthropic-ai/sandbox-runtime` (`srt`). Install it once:

```bash
npm i -g @anthropic-ai/sandbox-runtime
```

If `srt` is missing, `oxison build` fails at preflight with an install hint. You can bypass with `--no-sandbox`, but only on repos you fully trust.

### Optional: container runtime (Layer 2 sandbox)

Stronger isolation for build workers. Requires `podman` (recommended) or `docker`, plus a one-time image build:

```bash
brew install podman && podman machine init && podman machine start
podman build -t localhost/oxfaz-worker:latest docker/oxfaz-worker
```

Layer 2 also requires an explicit API key — the host Keychain is not reachable inside a container.

### Optional: source adapters

To ingest PDFs, Word documents, or PowerPoint files alongside a repo:

```bash
pip install 'oxi-son[sources]'
```

---

## Installation

### Zero-install (recommended for one-off use)

Run the latest version directly from GitHub without a local clone:

```bash
uvx --from git+https://github.com/escotilha/oxison oxison run /path/to/repo
```

Pin to a specific release:

```bash
pip install "git+https://github.com/escotilha/oxison.git@v0.4.0"
```

### From a local clone

```bash
git clone https://github.com/escotilha/oxison
cd oxison

# zero-install from the clone (no pip needed)
uvx --from . oxison run /path/to/repo

# or install into the current environment
pip install -e .
```

### Persistent `oxison` command

To have `oxison` available system-wide:

```bash
uv tool install git+https://github.com/escotilha/oxison
# or
pipx install git+https://github.com/escotilha/oxison
```

### Development install

```bash
uv venv --python 3.12 && . .venv/bin/activate
uv pip install -e ".[dev]"
ruff check src tests && mypy src && pytest -q
```

---

## Configuration

### Auth modes

| Mode | When it applies | How to activate |
|---|---|---|
| **OAuth** (default) | Interactive use on a machine where you are signed into Claude Code | Nothing — this is the default |
| **Bare** | CI, containers, or any headless environment | Pass `--bare`, or set `OXISON_API_KEY` / `ANTHROPIC_API_KEY` |

Key resolution precedence in bare mode: `--api-key` > `OXISON_API_KEY` > `ANTHROPIC_API_KEY`.

Bare mode requires a key to be present. Passing `--bare` without a key produces a config error.

### Environment variables

| Variable | Used for |
|---|---|
| `OXISON_API_KEY` | Primary API key (bare mode) |
| `ANTHROPIC_API_KEY` | Fallback API key (bare mode) |
| `KIMI_API_KEY` / `MOONSHOT_API_KEY` | Kimi provider key (`--provider kimi`) |
| `XAI_API_KEY` / `GROK_API_KEY` | Grok provider key (`--provider grok`) |

No other environment variables are read by oxison itself. The standard shell environment (`PATH`, `HOME`, `USER`, `LANG`, etc.) is passed through to the `claude` subprocess via a whitelist. Inherited `ANTHROPIC_*` vars are deliberately **stripped** (a secrets boundary); the provider overlay below is the only sanctioned way `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` reach the worker, and oxison constructs it from your explicit `--provider` choice.

### Model providers (Anthropic-compatible)

oxison drives `claude -p`, which speaks the Anthropic Messages API, so any model with an Anthropic-compatible endpoint can run the full pipeline. Select one with `--provider` on any command (`run`, `plan`, `ideate`, `build`):

| Provider | Endpoint | Key (precedence: `--api-key` > envs) | Default model |
|---|---|---|---|
| `kimi` | `https://api.moonshot.ai/anthropic` | `KIMI_API_KEY`, `MOONSHOT_API_KEY` | `kimi-k2.7-code` |
| `grok` | `https://api.x.ai` | `XAI_API_KEY`, `GROK_API_KEY` | `grok-4.3` (also `grok-build-0.1`) |

```bash
export KIMI_API_KEY=...
oxison run /path/to/repo --provider kimi

export XAI_API_KEY=...
oxison run /path/to/repo --provider grok --model grok-build-0.1
```

Provider mode forces bare-style token auth (it ignores the host OAuth login) and defaults `--model` to the provider's; override with `--model`. For a sandboxed `oxison build`, the provider's API host is auto-added to the worker egress allowlist so the sandboxed worker can reach it.

#### Saving a provider key (`oxison auth`)

You don't have to set an env var every session. The first time you run a provider with no key, oxison prompts (hidden) and offers to save it; thereafter it's resolved automatically. Keys are stored in your **OS keychain** (macOS Keychain via `security`, Linux libsecret via `secret-tool`) when available, falling back to a `0600` JSON file at `$XDG_CONFIG_HOME/oxison/credentials` (else `~/.config/oxison/credentials`).

| Command | Effect |
|---|---|
| `oxison auth set <provider>` | Prompt (hidden) and save a key; or `--api-key <key>` for non-interactive/scripts |
| `oxison auth status` | Show the active backend and, per provider, whether a key is saved or detected in the env. Never echoes any part of a key |
| `oxison auth rm <provider>` | Delete a saved key from every backend |

Full resolution order per run: `--api-key` > env var > saved key > interactive prompt. The prompt only appears on an interactive terminal — in CI/headless oxison fails fast with a clear "set `XAI_API_KEY`…" message rather than hanging. oxison never prints any part of a saved key.

### Output directory

Artifacts are written to `./oxison-output/` by default. Override with `--output-dir`:

```bash
oxison run /path/to/repo --output-dir ./docs
```

For `oxison plan`, the default is the directory containing `comprehension.json`. For `oxison build`, the build state lives under `<repo>/oxison-build/`.

---

## Usage

### `oxison run` — comprehend a repo and write product docs

```bash
oxison run <target>
```

`<target>` is the path to any local directory. It does not need to be a git repository (though git metadata is used when present).

**What it writes** (all in `./oxison-output/` by default):

| File | Contents |
|---|---|
| `PRODUCT.md` | What the software is, who it's for, core features, mental model |
| `MANUAL.md` | Prerequisites, install, configuration, usage, workflows |
| `STACK.md` | Languages, dependencies, runtime, infra — grounded in the manifests |
| `ROADMAP-ANALYSIS.md` | If the repo contains a roadmap: feasibility analysis, sequencing, next items |
| `SECURITY-NOTES.md` | If no roadmap found: lightweight security surface scan |
| `COMPREHENSION.md` | The intermediate whole-repo understanding the docs are built from |
| `repomap.json` | Deterministic repo map (no AI): languages, deps, entry points, services |
| `.oxison-run.json` | Per-step status and cost; used by `--resume` |

**The target repo is never modified.** AI workers are structurally limited to `Read`, `Glob`, `Grep` — no shell, no write tools. `git status` on the target remains clean after every run.

**Common flags:**

```bash
# Override model (default is Opus — powerful but pricey)
oxison run /path/to/repo --model claude-sonnet-4-6

# Cap spend per AI call
oxison run /path/to/repo --max-budget-usd 5

# Resume an interrupted run (skip completed steps)
oxison run /path/to/repo --resume

# Choose where artifacts are written
oxison run /path/to/repo --output-dir ./docs

# Tune map-reduce cutover (default: 100,000 estimated tokens)
oxison run /path/to/repo --chunk-threshold 60000

# Control parallel slice workers (default: 4)
oxison run /path/to/repo --max-concurrency 2

# Add extra non-repo sources (repeatable)
oxison run /path/to/repo --add spec.pdf --add deck.pptx

# Ingest a whole directory of sources
oxison run /path/to/repo --sources ./inputs/

# Bare-mode for CI
ANTHROPIC_API_KEY=sk-... oxison run /path/to/repo --bare
```

---

### `oxison plan` — generate a roadmap from a comprehension

```bash
oxison plan <comprehension>
```

`<comprehension>` is either a path to a `comprehension.json` file or a directory containing one (e.g., the `oxison-output/` directory from a prior run).

**What it writes** (next to `comprehension.json` by default):

| File | Contents |
|---|---|
| `roadmap.json` | Prioritized task list: identifiers, kinds, dependencies, acceptance criteria |
| `ROADMAP.md` | Human-readable version of the same roadmap |

Every proposed roadmap passes a deterministic plan-gate before being written: non-empty titles, valid task kinds, observable acceptance criteria, no dependency cycles, no tasks targeting protected paths (`.github/workflows`, `.env`, lockfiles, `.git/`). A failing roadmap gets one self-correcting pass; if it still fails, the run errors out rather than writing a bad roadmap.

**Common flags:**

```bash
# Point the planner at the actual repo for additional grounding (read-only)
oxison plan ./oxison-output --repo /path/to/repo

# Provide written guidance to refine the roadmap
oxison plan ./oxison-output --answers-file notes.txt

# Cap the number of tasks (default: 40)
oxison plan ./oxison-output --max-tasks 20

# Control output location
oxison plan ./oxison-output --output-dir ./plan/

# Bare-mode for CI
ANTHROPIC_API_KEY=sk-... oxison plan ./oxison-output --bare
```

---

### `oxison build` — autonomous build loop

```bash
oxison build <roadmap> --repo <path>
```

`<roadmap>` is a path to a `roadmap.json` or a directory containing one. `--repo` is required and must point to a git repository.

> **Build mode writes code.** Workers have full read/write tools (`Bash` included). Each runs in its own git worktree under `<repo>/oxison-build/worktrees/`; the main working tree is never touched directly. Always start with `--dry-run` to review the plan before spawning any workers.

**Build state** is durable: `<repo>/oxison-build/state.db` (SQLite, WAL mode). Tasks survive crashes and restarts. A task is marked dispatched before its worker spawns, so a crash or double-tick cannot re-dispatch in-flight work.

**Common flags:**

```bash
# Review the plan without spawning any workers
oxison build ./oxison-output --repo /path/to/repo --dry-run

# Run with explicit guardrails
oxison build ./oxison-output --repo /path/to/repo \
  --max-ticks 20              \  # LP1: hard ceiling on loop iterations
  --no-progress-ticks 5       \  # LP2: halt if 5 ticks pass with nothing advancing
  --budget-ceiling-usd 50        # LP3: stop when cumulative spend reaches $50

# Per-worker cost cap (default: $5.00)
oxison build ./oxison-output --repo /path/to/repo --worker-budget-usd 3.0

# Run multiple tasks in parallel
oxison build ./oxison-output --repo /path/to/repo --max-workers 2

# Disable sandbox (trusted local repos only — prints a loud warning)
oxison build ./oxison-output --repo /path/to/repo --no-sandbox

# Use stronger container isolation (Layer 2)
ANTHROPIC_API_KEY=sk-... oxison build ./oxison-output --repo /path/to/repo \
  --sandbox-layer container
```

**The three loop guardrails:**

| Flag | What it bounds | When to set it |
|---|---|---|
| `--max-ticks N` | Total loop iterations | Always — prevents runaway loops |
| `--no-progress-ticks N` | Consecutive iterations with no task advancing (default: 5) | Leave at default unless you expect many adapter retries |
| `--budget-ceiling-usd N` | Cumulative spend across all workers | Set to something meaningful before any long run |

A timed-out worker is charged its per-worker cap as a floor, so the spend meter is always honest.

---

### `oxison version`

```bash
oxison version   # or: oxison --version
```

Prints the banner and version string.

---

## Common Workflows

### Workflow 1: Document an unfamiliar repo in one command

```bash
oxison run ~/code/some-project --model claude-sonnet-4-6 --max-budget-usd 3
```

After ~2 minutes you have `./oxison-output/PRODUCT.md`, `MANUAL.md`, and `STACK.md` grounded in the actual code. Open `COMPREHENSION.md` to see the raw intermediate understanding.

### Workflow 2: Large repo — tune for cost and speed

For repositories above ~100,000 estimated tokens, oxison automatically switches to map-reduce (one worker per top-level directory + a synthesis pass). You can tune this:

```bash
oxison run ~/code/big-project \
  --chunk-threshold 60000 \   # start map-reduce earlier
  --max-concurrency 6     \   # more parallel slice workers
  --model claude-sonnet-4-6
```

Watch `.oxison-run.json` during the run to see per-step costs.

### Workflow 3: Resume an interrupted run

If a run is killed partway through, restart it without re-paying for completed steps:

```bash
oxison run /path/to/repo --resume
```

The deterministic map (no AI, no cost) always re-runs. All AI steps already recorded as `done` in `.oxison-run.json` are skipped.

### Workflow 4: Ingest supplementary sources alongside code

Mix PDFs, slide decks, and Word documents into the comprehension pass:

```bash
pip install 'oxi-son[sources]'

oxison run ~/code/myproject \
  --add docs/architecture.pdf \
  --add design/deck.pptx      \
  --add notes.md
```

Or ingest an entire inputs folder:

```bash
oxison run ~/code/myproject --sources ./project-inputs/
```

When any `--add` or `--sources` flag is used, oxison also writes a `comprehension.json` — a structured, versioned artifact suitable for downstream tooling.

### Workflow 5: Full pipeline — comprehend → plan → build

```bash
# Step 1: comprehend the repo
oxison run ~/code/myproject --output-dir ./plan-artifacts

# Step 2: turn the comprehension into a roadmap
oxison plan ./plan-artifacts --repo ~/code/myproject

# Step 3: review what would be built
oxison build ./plan-artifacts --repo ~/code/myproject --dry-run

# Step 4: run the build loop with guardrails
oxison build ./plan-artifacts --repo ~/code/myproject \
  --max-ticks 30 --budget-ceiling-usd 40
```

Each step is independent. You can re-run `plan` with different `--answers-file` guidance and then re-run `build` without re-comprehending.

### Workflow 6: CI — headless doc generation

```bash
# In a CI job (GitHub Actions, etc.)
export ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}

oxison run $GITHUB_WORKSPACE \
  --bare \
  --model claude-sonnet-4-6 \
  --max-budget-usd 2 \
  --output-dir ./docs-output
```

Commit or upload the `docs-output/` directory as a CI artifact.

### Workflow 7: Install the Claude Code skill

If you use Claude Code interactively, install the bundled skill so you can run oxison from inside any session:

```bash
mkdir -p ~/.claude/skills
cp -r skills/oxison ~/.claude/skills/oxison
```

Then inside Claude Code, type `/oxison /path/to/repo` or ask it to "document this repo". The skill uses Sonnet with a per-call budget cap by default. Pass `--opus` for oxison's default model, or `--full-budget` to remove the cap.

---

## Cost

oxison makes **5 AI calls** for a small repo (comprehend + 3 doc generators + branch stage). Large repos add one worker per top-level directory plus a synthesis pass.

The default model is Opus — approximately **$1–2 per call**. For routine or exploratory runs:

- Pass `--model claude-sonnet-4-6` for a much cheaper alternative.
- Pass `--max-budget-usd N` to hard-cap spend per AI call.

Every call's actual cost is printed when it completes and recorded in `.oxison-run.json`:

```bash
cat ./oxison-output/.oxison-run.json   # per-step status + cost
```

---

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Config error — bad target path, invalid flag, or bare mode without a key |
| 3 | Preflight failed — `claude` CLI missing, not authenticated, or sandbox prerequisite absent |
| 4 | Comprehension failed |
| 5 | Artifact generation or planning failed |
| 6 | Branch stage (roadmap analysis / security notes) failed |

---

## Troubleshooting

### `preflight failed: claude CLI not found`

The `claude` binary is not on your `PATH`. Install Claude Code from [claude.com/claude-code](https://claude.com/claude-code) and ensure it is reachable in your shell before running oxison.

### `preflight failed` with an auth error

In OAuth mode, sign into Claude Code first: `claude login`. In bare mode, ensure `OXISON_API_KEY` or `ANTHROPIC_API_KEY` is set and the key is valid.

### `config error: bare mode requires an API key`

`--bare` was passed but no key is available. Set `OXISON_API_KEY` or `ANTHROPIC_API_KEY`, or drop `--bare` to use your Claude Code login.

### `build sandbox enabled but the srt runtime is not installed`

Run `npm i -g @anthropic-ai/sandbox-runtime` to install the Layer 1 sandbox runtime. If you cannot install it, pass `--no-sandbox` only on repos you fully trust (a loud warning is printed to stderr).

### Container sandbox: image not found

Build the worker image before using `--sandbox-layer container`:

```bash
# With podman
podman build -t localhost/oxfaz-worker:latest docker/oxfaz-worker

# With docker
docker build -t localhost/oxfaz-worker:latest docker/oxfaz-worker
```

### Container sandbox: `api key required`

Set `ANTHROPIC_API_KEY` or pass `--api-key`. The host Keychain is not reachable from inside a container, so bare-mode auth is mandatory for Layer 2.

### Container sandbox: repo path fails to mount (macOS)

The repo must be under `$HOME` (e.g. `~/code/`) for the podman VM to share it. Paths under `/tmp` or on external volumes are not shared by the podman VM and will not mount.

### Run interrupted mid-way

Restart with `--resume`. oxison reads `.oxison-run.json` and skips every step already recorded as completed:

```bash
oxison run /path/to/repo --resume
```

The deterministic repo map always re-runs (it costs nothing).

### Plan-gate error: `roadmap failed validation after 2 attempts`

The planner produced a roadmap that failed the gate twice. This is rare and usually means the comprehension was ambiguous. Try:

1. Re-running `oxison run` (a fresh comprehension may produce better input).
2. Writing concise `--answers-file` guidance to steer the planner away from the area that failed.
3. Reducing `--max-tasks` to give the planner a simpler scope.

### Build loop stopped with `no_progress`

Five consecutive ticks passed with no task advancing (`LP2` guardrail). Common causes:

- Every eligible task has already hit its redispatch cap (repeated adapter failures).
- The sandbox is blocking a tool the worker needs.
- The worker budget (`--worker-budget-usd`) is too low to complete any single task.

Inspect `<repo>/oxison-build/logs/<task-id>.log` for the failed worker's output. Use `--dry-run` to check the current state of the taskstore without spawning new workers.

### Source file skipped — adapter not installed

A `--add` or `--sources` file was skipped because the matching adapter package is absent. Install the extras:

```bash
pip install 'oxi-son[sources]'   # PDF + pptx + docx
pip install 'oxi-son[pdf]'       # PDF only
pip install 'oxi-son[pptx]'      # PowerPoint only
pip install 'oxi-son[docx]'      # Word only
```

OCR for scanned PDFs (`--ocr`) requires an optional, unpublished `document_extraction` package, which is not an oxison dependency. Without it, scanned PDFs are logged as skipped.
