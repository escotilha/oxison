# oxison

**Point it at any local repo — it comprehends the code and writes the product docs back.**

`oxison` reads a repository, understands it by driving the
[Claude Code](https://claude.com/claude-code) CLI as a **read-only**
subprocess, and writes product artifacts into its own output directory.
It never modifies the repo it analyzes.

```bash
oxison run /path/to/repo
# → ./oxison-output/{PRODUCT,MANUAL,STACK}.md
#   + ROADMAP-ANALYSIS.md (if the repo has a roadmap) or SECURITY-NOTES.md (if not)
#   + COMPREHENSION.md + repomap.json + .oxison-run.json
```

## Requirements

- **Python ≥ 3.11**
- The **[Claude Code](https://claude.com/claude-code) CLI**, installed and signed in
  (oxison drives it as a subprocess; by default it uses your existing Claude Code login —
  see [Auth](#auth))
- Read access to the oxison repo to install it (`gh auth login`, or a token/SSH key) — see [Install](#install)

## Safety model

`oxison run` and `oxison plan` **never modify the target repo.** Two invariants
enforce it:

1. **The AI worker is *structurally* read-only** — launched with
   `--allowedTools Read,Glob,Grep`: no shell, no write tools. It physically
   cannot modify, create, delete, or execute anything — not just "told not to."
   (`Bash` is deliberately **excluded**: under `--permission-mode
   bypassPermissions` a shell is a full write/exec primitive, so it belongs to
   the build tier, not here.) A unit test asserts the exclusion against the
   built command line, so it can't silently regress.
2. **oxison owns every write**, exclusively into `./oxison-output/`.
   Workers return markdown; oxison writes the files.

After a `run`/`plan`, the target repo's git working tree is byte-for-byte
unchanged (`git status` clean, `HEAD` unmoved).

### `oxison build` is different — it writes code, by design

The Oxfaz build worker (`oxison build`) is the one stage that **writes**: it has
full read/write tools (`Bash` included) so it can implement a task and run the
project's tests. It is contained by three layers:

1. **A filesystem + network sandbox (`srt`), on by default.** Each worker is
   wrapped in Anthropic's [`@anthropic-ai/sandbox-runtime`](https://github.com/anthropic-experimental/sandbox-runtime)
   (`sandbox-exec` on macOS, `bubblewrap` on Linux). Writes are confined to the
   worker's **worktree** + the scoped parts of `.git` it needs to commit (NOT
   `.git/config` or `.git/hooks`) + Claude's own state; egress is limited to an
   allowlist (the Anthropic API + package registries + your git host); and
   credentials (`~/.ssh`, `~/.aws`, …) are unreadable. So even a
   prompt-injected worker cannot escape the worktree, install a git hook, or
   exfiltrate. **Requires Node + `npm i -g @anthropic-ai/sandbox-runtime`**
   (verified against `srt` 1.0.0); if it's missing, `oxison build` fails at
   preflight with an install hint. `--no-sandbox` disables it (loud stderr
   warning) for trusted local runs.
2. **Worktree isolation** — each worker runs in its own git worktree under
   `oxison-build/worktrees/`, so the repo's main working tree is never edited.
3. **A grader** — rejects any diff that touches a protected path
   (`.github/workflows`, `.env`, lockfiles, `.git/`, `oxison-build/`).

With the srt sandbox on, build mode is safe to point at repos you don't fully
trust.

#### Stronger filesystem isolation: the container sandbox (`--sandbox-layer container`)

For CI, or to isolate a worker more strongly than srt's allowlist, run each
worker **inside a rootless container**. This is the stronger *filesystem*
boundary; note the container currently keeps **default network egress** (see the
caveat below), so for a fully-hostile repo also narrow egress — that tightening
is tracked, not yet shipped.

```bash
# one-time: a container runtime + the worker image
brew install podman && podman machine init && podman machine start   # or docker
podman build -t localhost/oxfaz-worker:latest docker/oxfaz-worker

# then build with Layer 2 (needs an API key — see auth note)
ANTHROPIC_API_KEY=… oxison build ./oxison-output --repo ~/code/myrepo --sandbox-layer container
```

How it's stronger than srt: the worker runs in a container whose **only
bind-mount is its workspace**, so the host filesystem — `~/.ssh`, the main repo,
every credential — is **physically absent**, not merely denied (mount-namespace
isolation, `--cap-drop ALL`, `--security-opt no-new-privileges`). The worker
builds + commits in a self-contained **clone** mounted at `/work` (a linked
worktree's `.git` would point outside the mount), and oxison reads the diff from
that clone afterwards.

Two requirements:
- **Auth is bare-mode.** The macOS Keychain / OAuth store isn't reachable inside
  a Linux container, so the worker authenticates with an `ANTHROPIC_API_KEY`
  (forwarded by name into the container, never baked into the image). `oxison
  build --sandbox-layer container` fails at preflight if no key is set.
- **macOS: the repo must live under `$HOME`.** A path only mounts into the
  podman VM if it's on a shared host dir; `$HOME` is shared by default, `/tmp`
  is not. Repos under `~/code` work; repos under `/tmp` or external volumes
  won't mount.

Verified end-to-end (macOS, podman): a real worker clones the target, builds +
commits its task inside the container, and the host grades + records it — with
`/Users`, `~/.ssh`, and out-of-`/work` writes all confirmed inaccessible from
inside. Egress narrowing (the container currently keeps default egress; srt's
domain proxy can run inside it as a follow-up) is the remaining tightening,
tracked in `docs/superpowers/specs/2026-06-15-oxfaz-worker-sandbox-design.md`.

## What it produces

| File | Contents |
|---|---|
| `PRODUCT.md` | What the software is, who it's for, core features, mental model |
| `MANUAL.md` | Prerequisites, install, configuration, usage, workflows |
| `STACK.md` | Languages, dependencies + versions, runtime, infra/services (grounded in the manifests) |
| `ROADMAP-ANALYSIS.md` | *(if a roadmap exists)* Analysis of planned work, feasibility vs. current code, sequencing, recommended next items |
| `SECURITY-NOTES.md` | *(if no roadmap)* Lightweight read-only security surface scan + a nudge to add a roadmap |
| `COMPREHENSION.md` | The intermediate whole-repo understanding the docs are built from |
| `repomap.json` | The deterministic repo map (languages, deps, entry points, services) |
| `.oxison-run.json` | Per-step status + cost; enables `--resume` |

## How it works

```
map (deterministic, no AI)          → repomap.json
  └─ language histogram, dependency manifests, entry points, services
comprehend (read-only AI)           → COMPREHENSION.md
  └─ single-pass for small repos; map-reduce (slice by top-level dir
     + synthesis) when the estimated token surface exceeds the threshold
generate (read-only AI, parallel)   → PRODUCT.md, MANUAL.md, STACK.md
branch (read-only AI)               → ROADMAP-ANALYSIS.md or SECURITY-NOTES.md
```

The risky part — the `claude -p` subprocess wrapper — is hardened:
process-group isolation, concurrent stdout/stderr drain, a 1 MB stream
limit, an env whitelist, a wall-clock timeout, and cost extraction, all
using argv-form spawning (a prompt can never be shell-interpreted).

## Multi-source ingestion (Oxicome)

oxison can comprehend a repo **plus additional non-repo sources** — PDFs,
presentations, Word documents, plain markdown, and audio/video recordings —
merging them into one provenance-tagged comprehension pass.

```bash
# Feed individual files alongside the repo
oxison run /path/to/repo --add spec.pdf --add deck.pptx --add notes.md

# Or ingest a whole folder at once
oxison run /path/to/repo --sources ./inputs/

# Opt-in OCR for scanned/image-heavy PDFs
oxison run /path/to/repo --add scanned.pdf --ocr

# Transcribe an audio/video recording via a cloud STT API
oxison run /path/to/repo --add demo.mp4 --stt-key $KEY --stt-provider deepgram
```

`--add PATH` is repeatable; `--sources DIR` ingests every supported file in the
directory. Source types are detected by extension (`.pdf`, `.pptx`, `.ppt`,
`.docx`, `.doc`, `.md`, `.txt`, `.mp3`, `.mp4`, `.wav`, …).

### The `comprehension.json` artifact

When any source is added, oxison emits a `comprehension.json` alongside the
usual markdown outputs. It is a structured, provenance-tagged envelope — schema
version `1.0` — containing:

- the human-readable PRODUCT / MANUAL / STACK comprehension;
- a machine-readable ledger of every source ingested (path, type, byte size,
  whether it was extracted successfully).

`comprehension.json` is the stable contract for downstream tooling (CI
pipelines, dashboards, other agents) that need to consume oxison's output
programmatically.

### Installing the source adapters

```bash
pip install 'oxi-son[sources]'   # adds PDF, pptx, and docx support
```

The `sources` extra bundles `pypdf`, `python-pptx`, and `python-docx`. Without
it, those adapters degrade gracefully (the file is logged as skipped-with-reason
rather than raising an error).

### Caveats

- **OCR** (`--ocr`) requires an optional, unpublished `document_extraction`
  package to be importable — it brings the heavy PaddleOCR stack. It is **not**
  an oxison dependency; scanned PDFs fall back to skip-with-reason if the
  package is absent.
- **Recordings** (`--stt-key`) upload audio/video to a third-party cloud STT
  API (e.g. Deepgram). This is the **one path that sends data off-host** — it
  is entirely opt-in and requires an explicit key. All other adapters process
  files locally.

### Safety invariant

oxison's read-only guarantee extends to every source adapter: oxison reads the
files you point it at and never modifies them. No adapter writes back to any
input path.

## Planning (Oxipensa)

`comprehension.json` answers *"what is this and where is it at?"*. **Oxipensa**
turns that into *"what should we build next?"* — it reads a `comprehension.json`
and emits a prioritized, gated **`roadmap.json`** plus a human-readable
**`ROADMAP.md`**.

```bash
# plan from a comprehension produced by `oxison run`
oxison plan ./oxison-output

# ground the planner in the actual repo (read-only), and refine with guidance
oxison plan ./oxison-output --repo /path/to/repo --answers-file notes.txt
```

What you get (`roadmap.json`, schema `1.0` — the Oxipensa→Oxfaz contract):

- a prioritized task list, each task carrying a **deterministic identifier**
  (stable across re-plans, so a builder can dedup), **provenance** (the
  comprehension locators it traces to), **dependency sequencing**, and at least
  one **observable acceptance criterion** (a checkable end-state, not "works
  well");
- the planner's `summary` and any `open_questions` (merged with the
  comprehension's, the hook for refining the plan with `--answers-file`).

How it stays trustworthy: every proposed roadmap passes a deterministic
**plan-gate** before it is written — non-empty titles, valid kinds, real
acceptance criteria, no dependency cycles or dangling links, and **no task may
target a protected path** (CI config, `.env`, lockfiles, `.git/`). A roadmap
that fails the gate is fed back to the planner for one self-correcting pass; a
roadmap that still fails is never written. The planner worker is **read-only**
like every other oxison AI call — it reasons and returns JSON; oxison owns the
writes.

## Building (Oxfaz)

**Oxfaz** is the third stage: it consumes an Oxipensa `roadmap.json` and runs an
autonomous build loop, dispatching one write-worker per task **in an isolated
git worktree** and recording every outcome in a durable taskstore.

```bash
# see what would be built — ingest the roadmap, spawn NO workers
oxison build ./oxison-output --repo /path/to/repo --dry-run

# run the build loop with explicit guardrails
oxison build ./oxison-output --repo /path/to/repo \
  --max-ticks 20 --budget-ceiling-usd 50 --no-progress-ticks 5
```

> ⚠️ **Build mode writes code.** Unlike `run`/`plan` (read-only), Oxfaz workers
> have full read/write tools. Each runs in its own worktree under
> `oxison-build/worktrees/`; the repo's main working tree is never touched
> directly. Start with `--dry-run`.

How it stays safe and bounded:

- **The spine** (`oxison-build/state.db`) is the durable source of truth — a
  2-table SQLite store (task + lock) with crash-safe, idempotent writes. A task
  is marked dispatched **before** its worker spawns and the transition is guarded
  (`WHERE status='planned'`), so a crash or a double-tick can never re-dispatch
  in-flight work.
- **The grader** re-runs the protected-path matcher on each worker's *actual
  diff* — a worker that touches CI config, `.env`, lockfiles, `.git/`, or
  `oxison-build/` fails the grade even if the plan looked clean.
- **Three guardrails** bound every run on a different axis: an **iteration cap**
  (`--max-ticks`), a **no-progress halt** (`--no-progress-ticks` consecutive
  ticks with nothing advancing), and a **budget ceiling** (`--budget-ceiling-usd`;
  a timed-out worker is charged its per-worker cap as a floor, so the meter is
  honest). An unset ceiling is simply inactive — it never reads as infinite.

The full production-grade build stack (AI critics, GitHub PR + CI integration,
auto-merge, deploy-green gating, the three-layer dead-worker reaper) is the
documented follow-on; this is the contract-driven core that takes a roadmap from
`planned` to a graded build.

## Install

Install straight from this repo — no PyPI needed:

```bash
# zero-install, always-latest (recommended)
uvx --from git+https://github.com/escotilha/oxison oxison run /path/to/repo
# pin to a release
pip install "git+https://github.com/escotilha/oxison.git@v0.1.0"
```

Or from a local clone:

```bash
uvx --from . oxison run /path/to/repo      # zero-install, from a clone
# or
pip install -e . && oxison run /path/to/repo
```

While the repo is private, the Git install methods need read access to it
(`gh auth login`, or a token/SSH key with access). Requires **Python ≥
3.11** and the **Claude Code CLI** installed and signed in.

## Auth

By default oxison uses your existing Claude Code login (OAuth) — nothing
to configure. For CI, use `--bare` with `OXISON_API_KEY` or
`ANTHROPIC_API_KEY`.

## Usage

```bash
oxison run /path/to/repo
oxison run /path/to/repo --output-dir ./docs
oxison run /path/to/repo --model claude-sonnet-4-6   # cheaper than the Opus default
oxison run /path/to/repo --max-budget-usd 5          # cap spend per AI call
oxison run /path/to/repo --resume                    # skip steps already completed
oxison run /path/to/repo --chunk-threshold 60000     # tune map-reduce cutover
```

Run `oxison run --help` for the full flag list.

## Cost

oxison makes 5 AI calls for a small repo (comprehend + 3 docs + branch),
more for large repos (one comprehension worker per top-level directory +
a synthesis pass). It uses your Claude Code default model, which is
**Opus** — powerful but pricey (~$1–2 per call). For routine runs, pass
`--model claude-sonnet-4-6` and/or `--max-budget-usd` to cap spend. Every
call's cost is reported and recorded in `.oxison-run.json`.

## Resume

If a run is interrupted (or you want to regenerate only part of it),
`--resume` reads `.oxison-run.json` and skips steps already marked done.
The deterministic map always re-runs (it's free); cached AI steps are
skipped.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | config error (bad target path, bad flag) |
| 3 | preflight failed (Claude CLI missing / not authed) |
| 4 | comprehension failed |
| 5 | artifact generation failed |
| 6 | branch (roadmap/security) failed |

## Optional roadmap-parser enrichment

If a private `oxi_core` roadmap-parser package happens to be importable,
oxison opportunistically uses it to add structure to `ROADMAP-ANALYSIS.md`.
It is **not** a dependency and is not published; the roadmap analysis works
on any roadmap format via the AI pass regardless of whether it's present.

## Claude Code skill

This repo ships a [Claude Code](https://claude.com/claude-code) skill at
[`skills/oxison/SKILL.md`](skills/oxison/SKILL.md). Install it once and you can
run oxison from inside any Claude Code session by typing `/oxison <repo>` (or
just asking it to "document this repo"):

```bash
# install the skill into your personal Claude Code skills
mkdir -p ~/.claude/skills
cp -r skills/oxison ~/.claude/skills/oxison
```

The skill assumes the `oxison` CLI is on your `PATH` (see [Install](#install) —
`uv tool install` or `pipx install` give you a persistent `oxison` command). It
defaults to Sonnet with a per-call budget cap for cost safety, runs the CLI,
and verifies the read-only guarantee held after every run. Pass `--opus` to use
oxison's Opus default or `--full-budget` to drop the budget cap.

## Development

```bash
uv venv --python 3.12 && . .venv/bin/activate
uv pip install -e ".[dev]"
ruff check src tests && mypy src && pytest -q
```

## License

MIT
