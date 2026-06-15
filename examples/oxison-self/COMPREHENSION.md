I have now read all the essential files. Here is the full structured analysis.

---

# oxison — Repository Comprehension Report

## 1. What the Software Does

**oxison** is an AI-powered documentation engine: point it at any local repository and it produces a canonical set of product documentation without ever touching the target repo's files.

Its core pipeline reads the codebase, drives read-only `claude -p` subprocesses to understand it, and writes the results into its own output directory. It operates across three named stages:

| Stage | CLI command | What it does |
|---|---|---|
| **Oxicome** | `oxison run` | Map → Comprehend → Generate PRODUCT/MANUAL/STACK + branch (roadmap analysis or security scan) |
| **Oxipensa** | `oxison plan` | Consume `comprehension.json` → produce prioritised `roadmap.json` + `ROADMAP.md` |
| **Oxfaz** | `oxison build` | Consume `roadmap.json` → autonomous code-building loop in isolated git worktrees |

The five primary artifacts produced by `run`:

| File | Contents |
|---|---|
| `PRODUCT.md` | What the software is, who it is for, core features, mental model |
| `MANUAL.md` | Prerequisites, install, config, usage, workflows |
| `STACK.md` | Languages, deps+versions grounded in the dependency manifests |
| `ROADMAP-ANALYSIS.md` | Analysis of the repo's own roadmap (if one exists) |
| `SECURITY-NOTES.md` | Lightweight security surface scan (if no roadmap is found) |
| `COMPREHENSION.md` | The intermediate whole-repo understanding |
| `comprehension.json` | Machine-readable schema-1.0 envelope (the Oxipensa contract) |
| `repomap.json` | Deterministic repo map: languages, deps, entry points, services |
| `.oxison-run.json` | Per-step status + cost; enables `--resume` |

---

## 2. Who It Is For

**Developers and engineering teams** who want to:

- Rapidly document an unfamiliar or legacy codebase without manual effort
- Maintain living product docs that are regenerated from the code rather than hand-written
- Run autonomous code-generation tasks against a roadmap (`oxison build`)
- Integrate documentation generation into CI (bare-mode auth via `ANTHROPIC_API_KEY`)
- Use the tool from within Claude Code as a plugin (`/oxison /path/to/repo`)

It is positioned as a developer productivity CLI for solo engineers and teams; the safety model is designed so it is safe to point at repos belonging to others (the target is *structurally* unmodified — not just "told to be careful").

---

## 3. Architecture

### High-Level Data Flow

```
oxison run /target/repo
         │
         ├─ config.build_run_config()       → RunConfig  (target, output_dir, auth, knobs)
         ├─ preflight.preflight()            → checks claude CLI exists
         ├─ manifest.RunManifest             → .oxison-run.json  (resumable state)
         │
         └─ pipeline.run_pipeline()
              │
              ├─ [1] repomap.build_repo_map()       → RepoMap  (deterministic, no AI)
              │        walk files, language histogram, manifest deps, entry points,
              │        service hints, top-level tree   → repomap.json
              │
              ├─ [2] sources.ingest_paths()          → extra_context string (optional)
              │        PDF / pptx / docx / md / audio adapters
              │
              ├─ [3] comprehend.comprehend()          → Comprehension  (AI, read-only)
              │        est. tokens ≤ threshold → single_pass_prompt → invoke()
              │        est. tokens > threshold → map-reduce:
              │            gather(slice_prompt per top-level dir) → synthesis_prompt
              │        → COMPREHENSION.md
              │
              ├─ [4] generate.generate()              → [PRODUCT, MANUAL, STACK].md  (AI, concurrent)
              │        product_prompt / manual_prompt / stack_prompt → invoke()  (3× parallel)
              │
              ├─ [5] comprehension_doc.build_comprehension_doc() → comprehension.json
              │
              └─ [6] branch.run_branch()              → ROADMAP-ANALYSIS.md OR SECURITY-NOTES.md
                       detect_roadmap() found?
                         yes → roadmap_analysis_prompt → invoke()
                         no  → security_prompt → invoke()

dispatch.invoke() — the sole AI entry-point
    builds argv: claude --permission-mode bypassPermissions
                        --allowedTools Read,Glob,Grep   ← READ_ONLY_TOOLS constant
                        --output-format stream-json …
    spawns in new session, drains stdout+stderr concurrently, enforces 1 MB limit,
    env whitelist, wall-clock timeout + SIGTERM/SIGKILL, cost extraction
```

### Oxipensa sub-flow (`oxison plan`)

```
comprehension.json
    └─ oxipensa.plan()
         ├─ roadmap_plan_prompt() → invoke() (read-only worker returns JSON)
         ├─ jsonutil.extract_json_object()
         ├─ oxipensa_gate.gate_roadmap()  ← deterministic validator
         │     pass → build_roadmap_doc() → RoadmapDoc
         │     fail → one corrective pass (prior_errors fed back) → re-gate
         │     still fail → PlanError (never written)
         └─ write roadmap.json + ROADMAP.md
```

### Oxfaz sub-flow (`oxison build`)

```
roadmap.json → engine.roadmap_ingest.ingest_roadmap() → TaskStore (SQLite state.db)
                                                                │
engine.loop.run_build_loop()  ←── ticker
    each tick:
        LP1 check (max_ticks), LP3 check (budget_ceiling)
        locks_expire() sweep  ← L4 stale-lock reaper
        _eligible() → planned tasks with unmet deps filtered out
        for task in eligible:
            locks_claim() on task.files_touched
            mark_dispatched() (crash-safe, before spawning)  ← I1/I2 idempotency
            dispatch.launch_worker()
                → Layer 1 (srt sandbox, default): srt_wrap(claude -p, FULL_WRITE)
                → Layer 2 (container): launch_worker_container()
            grade_diff() → gates.GradeVerdict
            mark_merged() or mark_failed()
        LP2 no-progress counter
```

### Invariants enforced structurally

1. **Read-only workers** (`run`/`plan`): `--allowedTools Read,Glob,Grep` — no `Bash`, no write tools. The constant `READ_ONLY_TOOLS = ("Read", "Glob", "Grep")` is defined in `src/oxison/config.py` and a unit test asserts `Bash` is absent from the built argv.
2. **oxison owns every write**: all file writes go through `_write()` helpers in `pipeline.py`, `generate.py`, `branch.py`; workers only return text.
3. **Build workers are sandboxed**: Layer 1 (srt — OS-level filesystem + egress allowlist) or Layer 2 (rootless container — host filesystem physically absent).
4. **Protected paths** (`EngineConfig.protected_paths`): `.github/workflows/`, `.env`, `.git/`, lockfiles, `oxison-build/` — the grader re-checks the *actual* diff, not just the plan's declared files.

---

## 4. Key Modules and Their Responsibilities

### Top-level package (`src/oxison/`)

| File | Responsibility |
|---|---|
| `cli.py` | `argparse` entry-point; `cmd_run`, `cmd_plan`, `cmd_build`, `cmd_version`; loads `pipeline` dynamically via `importlib` to stay decoupled |
| `config.py` | `RunConfig` (frozen dataclass, single immutable run descriptor); `READ_ONLY_TOOLS` constant; `build_run_config()` validates + resolves all inputs; `AuthMode` (`oauth` / `bare`) |
| `pipeline.py` | `run_pipeline()` — stage sequencer: map → ingest → comprehend → generate → comprehension_json → branch; owns all writes to `output_dir`; `--resume` cache logic via manifest |
| `repomap.py` | Deterministic, zero-AI `build_repo_map()`: walks files (skipping vendor/build dirs), language histogram, parses `pyproject.toml`/`Cargo.toml`/`package.json`/`requirements.txt` for deps, entry-point heuristics, service hints; `estimate_tokens()` for chunker cutover |
| `comprehend.py` | `comprehend()`: single-pass for small repos; map-reduce (concurrent slice workers + synthesis) for large repos; bounded by `max_concurrency` semaphore |
| `dispatch.py` | `invoke()`: the sole `claude -p` subprocess wrapper; handles all 8 hardened concerns (process-group isolation, concurrent drain, 1 MB limit, env whitelist, truncated-JSON tolerance, wall-clock timeout + SIGKILL escalation, cost extraction, argv-form spawn) |
| `generate.py` | `generate()`: runs PRODUCT/MANUAL/STACK workers concurrently via semaphore; `ARTIFACTS` dict maps step name → filename |
| `branch.py` | `run_branch()`: detects roadmap file (`ROADMAP.md`, `BACKLOG.md`, `TODO.md`, …), calls `_analyze_roadmap()` or `_security_scan()`; opportunistic `oxi_core.parse_roadmap` enrichment |
| `preflight.py` | `preflight()`: runs `claude --version` via subprocess; validates API key in bare mode |
| `manifest.py` | `RunManifest`: flat JSON run state (step → `StepRecord`); atomic writes (temp file + `os.replace`); `is_complete()` for `--resume` |
| `prompts.py` | All prompt builders as pure functions: `single_pass_prompt`, `slice_prompt`, `synthesis_prompt`, `product_prompt`, `manual_prompt`, `stack_prompt`, `roadmap_analysis_prompt`, `security_prompt`, `roadmap_plan_prompt`; `IDENTITY` block with hard constraints |
| `oxipensa.py` | `plan()`: self-correcting planner loop (propose → gate → optional one-pass correction); `load_comprehension()` with schema-version pinning |
| `oxipensa_gate.py` | Deterministic `gate_roadmap()`: validates non-empty titles, valid task kinds, observable acceptance criteria, no dependency cycles or dangling refs, no protected-path `files_hint`, task count cap |
| `comprehension_doc.py` | `build_comprehension_doc()` → `ComprehensionDoc`: schema-1.0 JSON envelope with source provenance ledger |
| `roadmap_doc.py` | `RoadmapDoc` and `build_roadmap_doc()`: typed roadmap object with stable content-addressed task identifiers |
| `jsonutil.py` | `extract_json_object()`: tolerantly extracts a JSON object from AI output that may contain surrounding prose |
| `mdutil.py` | `strip_preamble()`: removes leading prose before the first `# ` heading from AI artifact output |

### `sources/` — multi-source ingest (Oxicome)

| File | Responsibility |
|---|---|
| `base.py` | `SourceAdapter` ABC, `SourceResult`, `SourceUnit` (provenance-tagged text unit) |
| `ingest.py` | `ingest_paths()` orchestrator: routes each file to the right adapter, OCR fallback, accumulates `IngestOutput`; `render_extra_context()` renders units into prompt block |
| `pdf.py` | `PdfAdapter`: extracts text from PDFs using `pypdf` (optional dep); emits `needs_ocr` skip for image-only PDFs |
| `pptx.py` | `PptxAdapter`: slide text from `python-pptx` |
| `docx.py` | `DocxAdapter`: paragraph text from `python-docx` |
| `docs.py` | `DocsAdapter`: plain text/markdown files |
| `ocr.py` | `OcrAdapter`: delegates to optional `document_extraction` package (heavy PaddleOCR) |
| `recording.py` | `RecordingAdapter`: uploads audio/video to cloud STT API (Deepgram etc.) — the one off-host path, opt-in only via `--stt-key` |

### `engine/` — Oxfaz autonomous builder

| File | Responsibility |
|---|---|
| `engconfig.py` | `EngineConfig` frozen dataclass: all constants (protected paths, sandbox config, loop guardrails, worker budget, branch prefix, etc.) |
| `taskstore.py` | `TaskStore`: SQLite `state.db` (2-table schema: `task` + `lock`); task lifecycle (`planned → planning → dispatched → merged/failed`); crash-safe `mark_dispatched` (I1/I2); lock claim/release/expire |
| `loop.py` | `run_build_loop()`: tick coordinator; three guardrails LP1 (max_ticks), LP2 (no_progress), LP3 (budget ceiling); crash-safe ordering (mark dispatched before spawn); dependency gating |
| `dispatch.py` (engine) | `launch_worker()`: creates git worktree on task branch, builds `build_worker_prompt`, routes to srt sandbox or container; streams worker stdout+stderr to log file (never a pipe, D2); `_changed_files()` unions uncommitted + committed diff |
| `invoke.py` (engine) | `build_argv()` with `ToolSet.FULL_WRITE` (the only place write tools are granted in engine code — the "C2 chokepoint") |
| `sandbox.py` | `build_srt_settings()`: srt (sandbox-runtime) settings for Layer 1: `allowWrite` = worktree + scoped `.git` + `~/.claude`; `denyWrite` = `.git/config`, `.git/hooks`; `denyRead` = credential dirs |
| `container.py` | Layer 2: `launch_worker_container()` runs worker inside rootless container; `prepare_clone()` for self-contained clone; `build_run_argv()` with `--cap-drop ALL`, single workspace mount |
| `gates.py` | `grade_diff()`: empty-diff check + protected-path fence on the actual diff |
| `protected.py` | `is_protected_path()`: segment-anchored matching |
| `roadmap_ingest.py` | `ingest_roadmap()`: upserts `roadmap.json` tasks into `TaskStore`; dedup by identifier |

### `memory/` — cross-run memory (experimental)

Implements a durable `MemoryStore` backed by `oxison-build/memory.db` (SQLite). Stores compiled-truth memories (procedural recipes, repo heuristics, episodic mistakes) indexed by content-addressed keys. Supports BM25 full-text (FTS5 with pure-Python fallback), optional cosine vector retrieval (no numpy, no `enable_load_extension`), graph expansion via edge table, and a salience score for eviction. Supervised by `capture.py`, served by `retrieve.py`, injected into prompts by `inject.py`.

### `skills/oxison/SKILL.md`

The Claude Code plugin definition. Declares the skill as `user-invocable`, `context: fork`, Bash-capable (to call `oxison run`), with a 7-step procedure: resolve target → locate/install CLI → pick model+budget → snapshot git state → run oxison → verify read-only guarantee → report. The post-run git check (`rev-parse HEAD` + `status --porcelain`) is explicitly mandatory.

### `docker/oxfaz-worker/Dockerfile`

Node 22 slim image: installs `git`, `ca-certificates`, `ripgrep`, `tini`; installs `@anthropic-ai/claude-code` globally; runs as unprivileged user `worker`. ENTRYPOINT is `tini --` for clean PID-1 reaping. Auth is bare-mode `ANTHROPIC_API_KEY` forwarded at run time.

---

## 5. External Dependencies and Services

### Required

| Dependency | Source | Purpose |
|---|---|---|
| **Claude Code CLI** (`claude` binary) | `https://claude.com/claude-code` | The sole AI backend; every AI call is a `claude -p` subprocess |
| **Python ≥ 3.11** | Runtime | `tomllib` (stdlib), `asyncio`, `sqlite3` |
| **PyYAML ≥ 6.0** | `pyproject.toml` required dep | Currently declared but not heavily used in the comprehension path |

### Optional

| Extra / binary | Purpose |
|---|---|
| `rich>=13` (`pretty` extra) | Richer terminal output |
| `pypdf>=4` (`pdf`/`sources` extra) | PDF text extraction |
| `python-pptx>=0.6` (`pptx`/`sources` extra) | PowerPoint text extraction |
| `python-docx>=1.1` (`docx`/`sources` extra) | Word document text extraction |
| `document_extraction` (unpublished) | Scanned-PDF OCR (PaddleOCR) — opt-in via `--ocr` |
| `oxi_core` (private) | Roadmap parser enrichment for `ROADMAP-ANALYSIS.md` |
| `srt` (`@anthropic-ai/sandbox-runtime`) | Layer-1 build sandbox (npm global install) |
| `podman` or `docker` | Layer-2 build sandbox container runtime |
| Cloud STT API (Deepgram, OpenAI, etc.) | Audio/video transcription via `--stt-key` (the one off-host path) |
| `ANTHROPIC_API_KEY` / `OXISON_API_KEY` | Bare-mode auth (CI; Layer-2 container workers require this) |

### CI / infra

A `.github/workflows/` directory is detected by `repomap._detect_services()` and reported as a service hint. The project itself uses GitHub Actions CI (badge in README).

---

## 6. How It Is Run / Entry Points

### CLI entry point

Defined in `pyproject.toml`:
```toml
[project.scripts]
oxison = "oxison.cli:main"
```

`src/oxison/cli.py:main()` is the sole entry point. `argparse` routes to one of four commands.

### Commands

```bash
# Document a repo (read-only)
oxison run /path/to/repo
oxison run /path/to/repo --output-dir ./docs
oxison run /path/to/repo --model claude-sonnet-4-6 --max-budget-usd 5
oxison run /path/to/repo --resume               # skip completed steps
oxison run /path/to/repo --add spec.pdf --add deck.pptx   # extra sources
oxison run /path/to/repo --sources ./inputs/               # whole directory

# Plan: comprehension.json → roadmap.json + ROADMAP.md
oxison plan ./oxison-output
oxison plan ./oxison-output --repo /path/to/repo --answers-file notes.txt

# Build: roadmap.json → autonomous code writing (writes code)
oxison build ./oxison-output --repo ~/code/myrepo --dry-run
oxison build ./oxison-output --repo ~/code/myrepo \
    --max-ticks 20 --budget-ceiling-usd 50 --no-progress-ticks 5
oxison build ./oxison-output --repo ~/code/myrepo --sandbox-layer container

# Version
oxison version
```

### Zero-install / uvx

```bash
uvx --from git+https://github.com/escotilha/oxison oxison run /path/to/repo
```

### Claude Code plugin

```text
/plugin marketplace add escotilha/oxison
/plugin install oxison@oxison
/oxison /path/to/repo
```

The skill (`skills/oxison/SKILL.md`) drives the `oxison run` CLI as a subprocess, defaults to Sonnet + a `--max-budget-usd 2` cap, snapshots and verifies target git state before and after.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | config error (bad path, bad flag) |
| 3 | preflight failed (Claude CLI missing or not authed) |
| 4 | comprehension failed |
| 5 | artifact generation failed |
| 6 | branch (roadmap/security) failed |