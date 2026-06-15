# SECURITY-NOTES.md — oxison

> **Scope disclaimer:** This is a lightweight, best-effort surface scan of the oxison source tree performed with static reading only — no fuzzing, no dynamic analysis, no CVE database lookup, and no review of the Claude Code CLI binary itself. It is not a substitute for a full SAST audit.

---

## Summary

oxison's core security posture is deliberately conservative: AI workers in `run`/`plan` mode are structurally limited to three read-only tools (`Read`, `Glob`, `Grep`) enforced at the subprocess argv level, not merely by instruction. `oxison build` (Oxfaz) grants write tools but isolates workers in git worktrees behind a sandbox (Layer 1: srt filesystem + egress allowlist; Layer 2: rootless container). No hardcoded secrets, no `shell=True` subprocess calls, and no unsafe deserialization were found. The findings below are a small set of genuine gaps and improvement areas.

---

## Findings

### 1. Container (Layer 2) sandbox has unrestricted network egress — **Medium**

**File:** `src/oxison/engine/container.py`, `build_run_argv()`

The Layer 2 container worker is launched without a `--network` restriction. The code comment explicitly acknowledges this:

> `--network` left at the rootless default so the worker can reach the Anthropic API + registries (egress narrowing is a Layer-2 follow-up)

By contrast, Layer 1 (srt) enforces `DEFAULT_SANDBOX_DOMAINS` — a tight nine-domain allowlist (`api.anthropic.com`, the major registries, `github.com`). A container worker operating under the rootless default can reach any host on the internet, which could allow a prompt-injected worker to exfiltrate content or phone home. The filesystem isolation of Layer 2 is strong; the network isolation is weaker than Layer 1.

**Recommendation:** Add `--network=<custom_network>` or `--network=slirp4netns:allow_host_loopback=false` (Podman) / a custom bridge with iptables rules (Docker) to narrow the container's egress to the same domain set used by srt.

---

### 2. Several modern lockfile formats absent from `DEFAULT_PROTECTED_PATHS` — **Medium**

**File:** `src/oxison/engine/engconfig.py`, `DEFAULT_PROTECTED_PATHS`

The protected-path list covers `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `poetry.lock`, and `Cargo.lock`. The following common lockfiles are **not** included:

| Lockfile | Ecosystem |
|---|---|
| `uv.lock` | Python / uv (used by oxison itself) |
| `Gemfile.lock` | Ruby |
| `go.sum` | Go |
| `Pipfile.lock` | Python / Pipenv |
| `composer.lock` | PHP |

A build worker assigned a task that plausibly touches dependencies could modify an unprotected lockfile and introduce a malicious or downgraded package version — a supply-chain risk that the grader would not catch because only the protected-path fence is enforced post-diff.

**Recommendation:** Add the missing lockfile names to `DEFAULT_PROTECTED_PATHS`. They follow the same segment-anchored matching used for the existing entries so no changes to `protected.py` are needed.

---

### 3. Docker worker image installs Claude Code CLI without a version pin — **Low**

**File:** `docker/oxfaz-worker/Dockerfile`

```dockerfile
RUN npm install -g @anthropic-ai/claude-code \
    && claude --version
```

No `@x.y.z` version is specified. Every `docker build` run will pull whatever the current `latest` tag is on npm at that moment. If the Anthropic npm package were ever compromised or introduced a breaking change, any image rebuilt after that point would silently pick it up.

**Recommendation:** Pin to an explicit version (e.g. `@anthropic-ai/claude-code@1.2.3`) and record the version in `CHANGELOG.md`. Update deliberately rather than implicitly.

---

### 4. PyYAML declared as a required runtime dependency but never imported — **Low**

**File:** `pyproject.toml` — `dependencies = ["PyYAML>=6.0"]`

A search across all Python source files under `src/oxison/` finds no `import yaml` or `from yaml import …` statement anywhere. PyYAML is a required (non-optional) install dep that installs C extensions and is a historically high-CVE package (the `yaml.load()` RCE family, most recently patched in 5.4 / 6.0). Carrying it as a required dep without using it expands the transitive attack surface of every oxison install for no benefit.

**Recommendation:** Remove PyYAML from `[project.dependencies]`. If it is needed by a future feature, add it then, or move it to an optional extra.

---

### 5. API key visible in parent process argv when passed via `--api-key` — **Low**

**File:** `src/oxison/cli.py` — `run_p.add_argument("--api-key", …)`

When a user runs `oxison run --api-key sk-ant-… /path/to/repo`, the raw key is present in the parent process's argv for the duration of the run and is visible to other users on the same host via `ps aux`. (The key is correctly forwarded to child processes via the environment, not their argv — this concern is specific to the parent process.)

This is a common trade-off for CLI tools, acceptable on single-user machines, but worth noting for shared-host or CI environments.

**Recommendation:** Document in `MANUAL.md` that `OXISON_API_KEY`/`ANTHROPIC_API_KEY` environment variables are preferred over `--api-key` on shared hosts. The env-var path avoids the argv exposure entirely.

---

### 6. Worker log files written with no disk-space cap — **Low**

**File:** `src/oxison/engine/dispatch.py` — `launch_worker()` / `launch_worker_container()`

The build worker's combined stdout+stderr is streamed to `log_path` with a plain `open(log_path, "wb")` and no size limit. A worker that produces verbose or runaway output (e.g., a loop that prints continuously before being killed) could fill the filesystem under `oxison-build/logs/`. The Phase 1 `invoke()` path correctly caps the in-memory event stream at 1 MB; no equivalent cap exists for the build worker's log files.

**Recommendation:** Either enforce a file-size ceiling in the log writer (e.g., stop writing after N MB and record a truncation marker), or document the absence of a cap so operators know to monitor disk space during long builds.

---

## What Was Not Found

The following categories were explicitly checked and no issues were found:

- **Hardcoded secrets or credentials** — none present in source, config, or committed files. API keys flow exclusively through environment variables.
- **Shell injection** — all subprocess calls use argv-form (`create_subprocess_exec` with a list, never `shell=True`). The prompt is passed as a positional `-p` argument and cannot be interpreted by a shell.
- **SQL injection** — all SQLite queries in `engine/taskstore.py` and `memory/store.py` use parameterized `?` placeholders; no string interpolation into query text.
- **Unsafe deserialization** — no `pickle`, `marshal`, `yaml.load()` without `Loader`, or `eval()` found.
- **Prompt injection escape from read-only workers** — the `run`/`plan` AI workers are structurally limited to `Read,Glob,Grep` at the subprocess level. Even if a malicious file in the target repo injects adversarial instructions, the worker cannot act on them (no write or exec tools available). The practical blast radius is limited to a misleading output document.

---

> **Note:** oxison currently has no roadmap file, so this security scan was generated in place of a roadmap analysis. Adding a `ROADMAP.md` (or `BACKLOG.md`) to the repository would cause future `oxison run` invocations to produce a `ROADMAP-ANALYSIS.md` — a prioritised, feasibility-grounded analysis of planned work — instead of this security scan. Consider adding one if you have features or improvements in mind.
