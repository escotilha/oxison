---
name: oxison
description: "Generate product docs (PRODUCT/MANUAL/STACK + a roadmap or security branch doc) for any local repo by running the read-only oxison CLI. Use whenever the user wants to document a repo, comprehend an unfamiliar codebase, auto-generate or refresh product docs, or asks to 'run oxison' / 'document this repo' / 'what does this codebase do' as a written deliverable. The target repo is NEVER modified."
argument-hint: "[path-to-repo] [--opus (use Opus)] [--full-budget (no budget cap)]"
user-invocable: true
context: fork
model: sonnet
effort: low
allowed-tools:
  - Bash
  - Read
  - AskUserQuestion
output-schema: "report listing each generated artifact path (PRODUCT/MANUAL/STACK + COMPREHENSION + the branch doc), total cost in USD summed across manifest steps, and a read-only-guarantee verdict (target HEAD unmoved + working tree unchanged)"
tool-annotations:
  Bash: { readOnlyHint: false, idempotentHint: false }
invocation-contexts:
  user-direct:
    verbosity: high
    confirmDestructive: false
  agent-spawned:
    verbosity: minimal
    confirmDestructive: false
---

# oxison — document any local repo (read-only)

Drives the [`oxison`](https://github.com/escotilha/oxison) CLI: point it at a local repo and it
comprehends the code (via read-only `claude -p` workers) and writes a product-doc suite into its
own output directory. **It never modifies the target repo** — that invariant is the entire point
of the tool, and this skill verifies it held after every run.

## Steps

### 1. Resolve the target repo

The first non-flag argument is the target repo path. If the user gave none, ask which local repo
to document. Expand `~`. Confirm the path exists and is a git repo
(`git -C <path> rev-parse --git-dir`); if not, stop and say so — oxison exits 2 on a bad target.

### 2. Locate (or install) the oxison CLI — portably

Resolve `oxison` from `PATH` first; do not assume any particular install location:

```bash
command -v oxison && oxison --version
```

If it is not found, install it via one of the documented methods (pick what's available):

```bash
# zero-install, always-latest (no permanent install; needs read access to the repo)
uvx --from git+https://github.com/escotilha/oxison oxison --version
# or a persistent global command
uv tool install git+https://github.com/escotilha/oxison    # → `oxison` on PATH
# or
pipx install git+https://github.com/escotilha/oxison
```

Prerequisites: Python ≥ 3.11, and the **Claude Code CLI** installed and signed in (oxison uses
the user's existing Claude Code OAuth login by default — nothing to configure). If you installed
via `uvx --from …`, use that same `uvx --from … oxison run …` form in step 5.

### 3. Pick model + budget (cost discipline)

**Default to Sonnet.** oxison's own default model is Opus (~$1–2 per AI call), and a run makes
~5 calls for a small repo — more for large repos (map-reduce spawns one comprehension worker per
top-level directory plus a synthesis pass), so cost scales with repo size.

| User intent | Flags to pass oxison |
|---|---|
| default (this skill's default) | `--model claude-sonnet-4-6 --max-budget-usd 2` |
| user passed `--opus` | omit `--model` (uses oxison's Opus default) |
| user passed `--full-budget` | omit `--max-budget-usd` (no per-call cap) |

⚠️ **`--max-budget-usd` is a PER-CALL cap, not a per-run total.** A whole run can sum to several
multiples of that number. Report the *total* (summed across manifest steps) in the final output,
and for a large repo, warn the user of the likely total *before* launching rather than after.

Output dir defaults to `oxison-output/` in the **current working directory** where `oxison run`
is invoked (not inside the target). Override with `--output-dir` if the user named one — and if
the cwd is inside the target repo, pass an `--output-dir` *outside* the target so the artifacts
don't land in the target's tree (and don't muddy the read-only check in step 6, where untracked
artifact files inside the target would otherwise look like new changes).

### 4. Snapshot the target's git state (to prove read-only afterward)

```bash
git -C <target> rev-parse HEAD            # record pre-run HEAD
git -C <target> status --porcelain        # record pre-run dirty set
```

### 5. Run oxison (background — it makes several minutes of read-only claude -p calls)

```bash
oxison run <target> \
  --output-dir <output-dir> --model claude-sonnet-4-6 --max-budget-usd 2
```

Run with `run_in_background: true`. Poll the output dir's `.oxison-run.json` manifest for per-step
status + cost rather than scraping stdout. If a prior run was interrupted, re-run the **same
command with `--resume`** appended — it skips steps already marked done (the free deterministic
map step always re-runs).

### 6. Verify the read-only guarantee (mandatory — it is the tool's core promise)

After the run, confirm the target is byte-for-byte unchanged:

```bash
git -C <target> rev-parse HEAD            # must equal the pre-run HEAD
git -C <target> status --porcelain        # must equal the pre-run dirty set (no new tracked changes)
```

If HEAD moved or tracked files changed, that is a serious regression in oxison — stop and report
it loudly; do not bury it.

### 7. Report

Per the output-schema: list each generated artifact with its path, the **total** cost summed
across all manifest steps, and the read-only verdict (`✓ HEAD unmoved + tree clean`). Point the
user at the output dir, and note which branch artifact was produced — `ROADMAP-ANALYSIS.md` if the
repo has a roadmap, otherwise `SECURITY-NOTES.md` plus the "add a ROADMAP.md" tip.

## Notes

- **Nested Claude Code is expected.** Invoked from inside a Claude Code session, oxison spawns its
  own read-only `claude -p` workers (`--allowedTools Read,Glob,Grep` — no shell, no write tools).
  It uses argv-form spawning, so a prompt can never be shell-interpreted and the read-only guarantee
  holds regardless of the caller's shell aliases. (The `oxison build` worker is the deliberate
  exception — it gets full write tools to build in an isolated worktree.)
- **Auth** is the user's existing Claude Code login by default. For CI/headless, oxison supports
  `--bare` with `OXISON_API_KEY`/`ANTHROPIC_API_KEY`.
- This skill is read-only with respect to the *target*; the only writes are oxison's own artifacts
  into the chosen output directory.

## Exit codes (from oxison)

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | config error (bad target path or flag) |
| 3 | preflight failed (Claude CLI missing / not authed) |
| 4 | comprehension failed |
| 5 | artifact generation failed |
| 6 | branch (roadmap/security) failed |
