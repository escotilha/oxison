# Changelog

## [Unreleased]

## [0.6.0] — 2026-06-18

Folds in cross-run build memory, safe roadmap integration onto a dedicated branch,
and a full external security-audit hardening pass.

### Added
- **`oxison build --integrate` never advances `main`/`master` in place.** When the
  repo is on a protected branch, the loop now composes the graded roadmap onto a
  dedicated `oxison/integration` branch and restores your original branch at the
  end — leaving `main` untouched for you to review and `git merge` yourself. On a
  non-protected branch it integrates onto that branch as before. Backed by a
  defense-in-depth backstop in `integrate_branch` (`protected_branches`) that
  refuses to fast-forward a protected branch even if the redirect is bypassed.
- **Cross-run memory is now wired into the build loop (#37).** `oxison build`
  captures grader-verified outcomes to `oxison-build/memory.db` and front-loads
  relevant priors from past runs *in the same repo* into each worker's prompt.
  Default-on; `--no-memory` disables it. Capture is gated by the grader (a
  verified+merged task → a `procedural` recipe; a grader-rejected one → an
  `episodic` anti-pattern; engine outages store nothing), and retrieval is
  repo-scoped and **abstaining** — a weak match injects nothing rather than a
  plausible-but-wrong prior. Injected memory is framed as advisory (the task's
  own acceptance criteria win) and sanitized against the `<task_data>` fence so a
  stored field can't break out of the worker's data block.

### Security
- **`.gnupg` is now read-denied to the sandboxed build worker** (v0.5.0 CTO swarm).
  It was write-protected but readable, so a prompt-injected worker could read GPG
  private keys; now in `DEFAULT_CRED_DENY` alongside `.ssh`.
- **Worker-log secret redaction now runs in `finally`** (C-N2) — on every exit
  path (including an unexpected exception), so a credential can't be left in the
  persisted log. Applied to both Layer-1 and Layer-2 workers.
- **`repo_name` newlines are collapsed in the worker-prompt preamble** (N1) — it's
  interpolated before the `<task_data>` fence opens, so the fence can't protect it;
  collapsing newlines stops a crafted repo name injecting into the role framing.

#### External security-audit hardening
- **OCR dynamic-import RCE closed (F3, High).** `sources/ocr.py` imported
  `document_extraction` through a `sys.path` that included the CWD, so a file
  planted under an untrusted target repo could execute in the unsandboxed host
  process via `--ocr`. The loader now strips CWD-relative `sys.path` entries for
  the import and rejects any module resolving under the CWD; `--ocr` also emits a
  one-time privilege warning (F3 mitigation).
- **Build-worker base image pinned (F1, High).** `@anthropic-ai/claude-code` and
  `@anthropic-ai/sandbox-runtime` are pinned (via `ARG`) in the worker Dockerfile
  so the single untrusted-content execution surface is deliberate and auditable.
- **`--api-key` argv exposure flagged (F2, High).** Help text on all four
  subcommands now warns that a key passed as argv is visible in `ps`/shell
  history/CI logs and steers to the `OXISON_API_KEY`/`ANTHROPIC_API_KEY` env var.
- **Lockfiles + CI configs are now protected paths (F4).** `DEFAULT_PROTECTED_PATHS`
  gained `uv.lock`, `go.sum`, `Gemfile.lock`, `Pipfile.lock`, `composer.lock`, and
  CI config dirs — a build worker can no longer tamper with dependency pins or the
  pipeline (enforced identically by the plan-gate and the post-diff grader).
- **`oxison build` gates direct roadmaps on protected paths (F5, Med).** A
  hand-crafted/tampered roadmap fed straight to `build` now fails at ingest if any
  task's `files_hint` targets a protected path, using the same matcher as the
  grader — failing before any worker budget is spent.
- **Worker container resource ceilings (F6).** Layer-2 workers run with
  `--memory 4g` + `--pids-limit 2048` by default (config-overridable) to bound a
  runaway worker (memory exhaustion / fork bomb).
- **Document-parser DoS surface bounded (F7).** Optional parser floors raised to
  patched releases (`pypdf>=6.13.3`, `python-pptx>=1.0.2`, `python-docx>=1.2`),
  `pypdf` bumped 6.13.2 → 6.13.3 (GHSA-jm82-fx9c-mx94), and a 64 MiB size cap skips
  oversized files before they reach an in-process parser.
- **Worker log size capped (F8, Low).** Logs are truncated at 8 MiB
  (`MAX_WORKER_LOG_BYTES`) in the worker-exit `finally` block so a verbose/looping
  worker can't fill the disk.
- **Unused PyYAML dropped (F10, Low).** It was the only base dependency and never
  imported — base deps are now `[]`.

### Internal
- Added isolation tests for `extract_cost_from_log` (the C3 budget-floor path) —
  result-event, no-result, missing-file, and truncated-trailing-line cases.

## [0.5.0] — 2026-06-16

### Security
- **Build-worker prompt fence is now break-out-proof (CTO re-audit HIGH-1).** The
  `<task_data>` fields are sanitized so a roadmap field containing `</task_data>`
  can't close the fence early and inject into the worker's Rules section.
- **oxison's own saved provider keys are denied to the sandboxed worker (M1).**
  `~/.config/oxison` is added to the sandbox `denyRead` list — closing (with the
  fence fix) the prompt-injection → read-keys → exfiltrate chain the re-audit found.
- **SSRF guard handles IPv4-mapped IPv6 (CAND-2).** `::ffff:127.0.0.1`-style
  addresses are re-evaluated as their embedded IPv4, so the mapped form can't slip
  past the private/loopback block.

### Changed
- **`locks_expire()` no longer scans the lock table every no-progress spin (M3).**
  Moved inside the per-tick cache refresh (the unaddressed half of #17), and its
  deletes are batched into one `executemany` (L2).
- **README documents the platform-dependent container egress** (Linux narrows via
  in-container srt; macOS keeps default egress with a warning) (M2).

### Internal
- **Code-health pass (#18).** Lifted the shared git/log helpers
  (`git_cmd`/`changed_files`/`extract_cost_from_log`/`parse_changed_files`) out of
  `engine/dispatch.py` into a public `engine/gitutil.py` (no more cross-module
  private imports from `integrate.py`/`container.py`); added a compound
  `(status, priority, id)` index serving `find_next_planned`; dropped a dead
  `AND merged_at IS NULL` predicate in `inflight_tasks`; removed an unused
  `urllib.error` import.
- **Code-health pass #2 (#18 closeout).** `memory.put()` is now a single
  transaction (~5 commits → 1; L4/M5); `memory.prune()` drops the per-key `get()`
  SELECT via a subquery (CAND-3); `DispatchOutcome` moved to `engine/types.py` so
  `integrate.py` no longer pulls the dispatch module (L3); removed 7 dead
  `EngineConfig` fields (grader/CI/auto-merge/heartbeat, all unwired) (L4); added
  direct `gitutil` tests (CAND-4). (Deliberately left in #18: `vector_rank`'s
  static-SQL full-scan — a conscious no-dynamic-SQL tradeoff, fine at scale (M4);
  and worktree/clone disk retention — kept as the audit trail / needed by
  `--integrate`, needs a retention-policy decision (L1).)

### Fixed
- **Loop reconciles a stranded `planning` task on startup (#15).** A task left in
  `planning` after a crash was caught by neither the inflight sweep nor the
  completion check and could wedge the loop; it's now reset to `planned` and
  re-driven.

### Changed
- **`--max-workers>1` now dispatches concurrently (#16).** The build loop ran the
  eligible batch serially, so `--max-workers` was wall-clock-identical to 1; it
  now `asyncio.gather`s the batch when there's no integrator (integration stays
  serial for the `--ff-only` invariant). File-locks still serialize tasks that
  declare overlapping files.
- **Per-tick query cache cuts no-progress churn (#17).** `merged_identifiers` /
  `status_counts` / `inflight_tasks` / eligibility are cached per tick and
  re-queried only when a task changes state, so a blocked LP2 spin no longer
  re-runs them every 20 ms (~250 q/s → ~0 while blocked).

### Security
- **Layer-2 container egress narrowed to the allowlist (#14).** The rootless-
  container sandbox now runs the worker under srt *inside* the container (image
  gains `bubblewrap`/`socat`/srt), confining its network egress to the same
  domain allowlist as Layer-1 — it previously kept the podman default (open)
  egress. srt settings are bind-mounted read-only. On the macOS podman VM (which
  can't nest bwrap's bind-mount of the volume) the wrap is skipped with a warning
  — no regression; egress narrowing applies on Linux, where Layer-2 deploys.
- **Build-worker prompt-injection hardening (#13).** Untrusted task fields
  (roadmap/web-derived) are now enclosed in a `<task_data>` fence labelled
  data-not-instructions, with the oxison Rules as the worker's only authority;
  and worker logs are redacted of any literal credential (`api_key` / provider
  token) before they persist (CWE-532), so a prompt-injected worker can't surface
  a key via `env` into its log. Defence in depth on top of the sandbox.

## [0.4.0] — 2026-06-15

### Added
- **Seamless provider keys — prompt-and-save + `oxison auth`.** The first time you
  run a provider (`--provider kimi`/`grok`) with no key, oxison prompts for it
  (hidden) and offers to save it; every run after is zero-touch. Keys are stored in
  the **OS keychain** (macOS `security`, Linux `secret-tool`) with a `0600`-file
  fallback (`~/.config/oxison/credentials`). New `oxison auth set/status/rm`
  manages saved keys (status shows backend + last-4 only, never the key). Full
  resolution order: `--api-key` > env var > saved key > interactive prompt; the
  prompt is TTY-gated so CI/headless fails fast instead of hanging.

## [0.3.0] — 2026-06-15

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
