# Changelog

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
