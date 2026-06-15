# Changelog

## [Unreleased]

### Added
- **Model providers — `--provider kimi` / `--provider grok`.** Run the whole
  pipeline (`run` / `plan` / `ideate` / `build`) on any Anthropic-compatible
  endpoint. oxison constructs the `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`
  overlay from an explicit provider choice (never from the ambient env, which
  stays stripped), defaults the model to the provider's, and — for sandboxed
  builds — auto-allows the provider's API host in the worker egress allowlist.
  Kimi K2 (`api.moonshot.ai`, key `KIMI_API_KEY`/`MOONSHOT_API_KEY`, default
  `kimi-k2.7-code`) and xAI Grok (`api.x.ai`, key `XAI_API_KEY`/`GROK_API_KEY`,
  default `grok-4.3`). Adding another provider is one registry entry.

## [0.2.0] — 2026-06-15

oxison grows from "comprehend a repo and write docs" into a full
**idea → comprehension → plan → built product** pipeline, and goes public.

### Added
- **Oxicome — multi-source ingestion.** Comprehend a repo *plus* non-repo
  sources (PDF, pptx, docx, markdown, audio/video transcripts) merged into one
  provenance-tagged `comprehension.json` contract.
- **Oxipensa — the planner.** `oxison plan`: turn a `comprehension.json` into a
  prioritized, dependency-sequenced `roadmap.json` + `ROADMAP.md`, behind a
  deterministic self-correcting plan-gate (observable acceptance per task, no
  protected-path targets).
- **Oxfaz — the autonomous build engine.** `oxison build`: consume a roadmap and
  run a graded build loop — one write-worker per task in an isolated git
  worktree, a crash-safe SQLite taskstore, a protected-path grader on the actual
  diff, and three guardrails (iteration cap, no-progress halt, budget ceiling).
- **Two-layer build sandbox** — srt host-allowlist (Layer 1, default) or a
  rootless container (Layer 2); `--no-sandbox` opt-out for trusted repos.
- **`oxison build --integrate` — sequential task integration.** Merge each
  graded branch into the repo's current branch in dependency order, composing a
  multi-task roadmap into one product on `main`.
- **Oxideia — greenfield mode (`oxison ideate`).** Start from **zero** — a
  plain-text idea plus non-repo inputs incl. **website links** — and get a
  comprehension + `PRODUCT.md` + initial `ROADMAP`, no repo required.
- **Portable cross-run memory store** for oxison.
- **Claude Code plugin + marketplace** — install/run from inside Claude Code
  (`/plugin marketplace add escotilha/oxison` → `/oxison`).
- **CI** — automated per-PR Opus code review (Claude Code GitHub Action), plus
  ruff/mypy/pytest, gitleaks, pip-audit, bandit, and CodeQL gates.

### Security
- **SSRF guard on the URL adapter** — scheme + private/loopback/link-local IP
  block on the initial URL and every redirect hop (fail-closed).
- Bounded worker SIGKILL teardown (no event-loop hang); git-failure routing
  through adapter-failure; direct test corpus for the protected-path gate.

### Changed
- First **public** release; history scrubbed of internal references.

## [0.1.0] — 2026-06-06

First working release. Point oxison at a local repo; it comprehends the
code and writes product docs back, without ever modifying the target.

### Added
- **Scaffold** — `oxison` CLI (argparse, zero CLI deps), `RunConfig` +
  target resolution, resumable JSON run manifest, preflight checks.
- **Comprehension engine** — deterministic read-only repo MAP; trimmed
  async `claude -p` wrapper (process-group isolation, concurrent
  stdout/stderr drain, 1 MB stream limit, env whitelist, wall-clock
  timeout, cost extraction, argv-form); map-reduce chunker for large
  repos.
- **Artifact generators** — `PRODUCT.md`, `MANUAL.md`, `STACK.md`
  (STACK grounded in the deterministic dependency manifests).
- **Roadmap-or-security branch** — `ROADMAP-ANALYSIS.md` when a roadmap
  exists, else a lightweight `SECURITY-NOTES.md`. Opportunistic
  `oxi_core` enrichment when importable.
- **Safety invariant** — every AI worker is read-only; oxison owns all
  writes into `./oxison-output/`. Unit-tested and verified on live runs
  (target repo byte-for-byte unchanged).

### Notes
- `--max-turns` is intentionally absent — it was removed from the Claude
  CLI in 2.1.161; spend is bounded by `--max-budget-usd`.
- Defaults to your Claude Code OAuth login; `--bare` for CI.
