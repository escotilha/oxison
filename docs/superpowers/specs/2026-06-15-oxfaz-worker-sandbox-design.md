# Oxfaz Build-Worker Filesystem Sandbox — Design Spec

**Date:** 2026-06-15
**Status:** Layer 1 IMPLEMENTED (`feat/oxfaz-srt-sandbox`), `/cto`-reviewed,
empirically verified on macOS. Layer 2 (container) still deferred.

> **Implementation notes — verified against `srt` 1.0.0 (corrections to §2.2):**
> - srt **requires all config keys present** (`filesystem.allowWrite` /
>   `denyWrite` / `denyRead`, `network.allowedDomains` / `deniedDomains`) — an
>   incomplete config is rejected and srt falls back to deny-all. Deny lists are
>   empty except the credential `denyRead`.
> - Git allowlist is **`allowWrite: .git` (whole) + `denyWrite: [.git/config,
>   .git/hooks]`**, not the granular per-subdir list first proposed: a commit
>   also writes top-level locks (`packed-refs.lock`) a granular list misses,
>   while the denyWrite carve-out still blocks the code-exec surfaces. (The live
>   spike caught the `packed-refs.lock` EPERM.)
> - Temp is a **per-worker scratch dir** (`oxison-build/tmp/<task>`, worker
>   `TMPDIR` pointed at it), not the shared system `$TMPDIR`.
> - Invoked as the resolved **`srt` binary** (not `npx`), validated at preflight.
> - Code: `engine/sandbox.py` + `engine/dispatch.py` + `cli.py` (`--no-sandbox`);
>   tests: `test_sandbox.py` (unit) + `test_sandbox_spike.py` (live, skipif).
**Scope:** the `oxison build` (Oxfaz) write-worker only. `oxison run`/`plan` are
already structurally read-only (`--allowedTools Read,Glob,Grep`, no shell — see
PR #13) and need no sandbox.

---

## 0. Why this exists

Oxfaz dispatches a write-worker per task as
`claude --permission-mode bypassPermissions --allowedTools Bash,Edit,Write,...`
in a git worktree (`engine/dispatch.py::launch_worker`). The containment today is
**worktree isolation + a protected-path grader on the in-tree diff**. The
whole-platform audit (`.cto/raw/security.md`, PRs #12/#13) found the gap this spec
closes:

- The grader only inspects the **worktree's own `git diff`**. A worker with a
  shell (`Bash`) under `bypassPermissions` can write **outside** the worktree —
  the main repo, `~/.ssh`, `~/.claude` credentials — or exfiltrate over the
  network, and **none of it shows up in the worktree diff**. So the grade passes
  and the task merges.
- This is a *trust-model* gap, acceptable when you point Oxfaz at your **own**
  repo, but unsafe against a **hostile / prompt-injecting** target repo.

**Goal:** confine the build worker's filesystem writes to its worktree (+ the few
paths Claude itself needs) and bound its network egress, on **both macOS**
(developer laptops) **and Linux** (CI/servers), without breaking the worker's
legitimate need to run the project's build/tests/git inside the worktree.

---

## 1. Decision (from deep-research 2026-06-15, 90% confidence)

The decisive fact: **the worker *is* `claude -p`**, so the sandbox is Claude
Code's own tooling — but the *built-in* `/sandbox` is **insufficient by itself**.

| Option | Verdict |
| --- | --- |
| **Built-in Claude `/sandbox` settings** | ❌ alone — confines **only Bash** subprocesses. `Read/Edit/Write` run inside the Claude process under the permission layer, which `bypassPermissions` **skips**. Our exact threat (Edit/Write escaping the worktree) stays open. (Anthropic docs, verified.) |
| **`@anthropic-ai/sandbox-runtime` (`srt`)** | ✅ **Layer 1 (adopt first).** Wraps the **whole process tree** (Edit/Write/MCP/hooks/subprocesses) in **Seatbelt (macOS) + bubblewrap/seccomp (Linux)** from one code path. Deny-all default + FS allowlist + domain-based egress proxy. Anthropic's documented answer for unattended `--dangerously-skip-permissions`. Single argv-prepend. Natively allows `.git/worktrees` writes so `git commit` works. |
| **Rootless Podman/Docker container** | ✅ **Layer 2 (CI / untrusted-repo hardening).** Mount-namespace isolation *under* `srt`; `~/.ssh`/main-repo literally not mounted. Heavier (image to maintain). |
| Hand-rolled `sandbox-exec` / `bwrap` | ❌ Don't. Reinvents `srt`; deny-default profiles are brittle and break on every OS update (prior incident: `pattern_sandbox-exec-allow-default-deny-dangerous`, took 5 iterations). `srt` ships the lesson. |
| Firecracker / gVisor / Apple `container` | ❌ Not first — Linux-only / KVM-required / days-old respectively. |

**Caveats baked into the plan:** `srt` is **beta** (pin the exact version; gate
upgrades behind the verification probes below). Its egress proxy does **not** do
TLS inspection, so allowlist only the **specific domains** the build needs —
broad domains reopen a domain-fronting bypass.

---

## 2. Layer 1 — wrap every worker in `srt`

### 2.1 Integration point

`engine/dispatch.py::launch_worker` currently builds `argv` via
`engine.invoke.build_argv(...)` and spawns it with `create_subprocess_exec(*argv, ...)`.
Change: **prepend the `srt` wrapper** to that argv when sandboxing is enabled.

```python
# pseudocode — in launch_worker, after build_argv(...)
if engine_config.sandbox_enabled:
    settings_path = _write_srt_settings(worktree, engine_config)   # temp JSON, per worker
    argv = ["npx", "@anthropic-ai/sandbox-runtime", "--settings", settings_path, *argv]
```

`cwd` stays the worktree; the prompt is still passed the same way. The `srt`
binary is installed once at setup (`npm i -g @anthropic-ai/sandbox-runtime@<pinned>`),
so the per-worker spawn does not depend on registry egress to fetch the wrapper.

### 2.2 Per-worker `srt` settings (the security policy)

```jsonc
{
  "filesystem": {
    "allowWrite": [
      "<ABS_WORKTREE_PATH>",          // the task's worktree — the only place it builds
      "<MAIN_REPO>/.git/worktrees",    // so `git commit` in the linked worktree works
      "~/.claude", "~/.claude.json"    // Claude's own session/projects state (else EPERM)
    ],
    "denyRead": [
      "~/.ssh", "~/.aws", "~/.config/gcloud", "~/.config/gh", "~/.netrc",
      "~/.config/git/credentials"
    ]
  },
  "network": {
    // OWNER DECISION (2026-06-15): allow package registries + git host so a
    // worker can install deps and fetch during a build. Each domain widens the
    // (no-TLS-inspection) egress surface — keep this list tight and reviewed.
    "allowedDomains": [
      "api.anthropic.com",
      "pypi.org", "files.pythonhosted.org",
      "registry.npmjs.org",
      "crates.io", "static.crates.io",
      "github.com", "codeload.github.com", "objects.githubusercontent.com"
    ]
  }
}
```

Notes:
- **Deny-all default** → fail-closed: anything not allow-listed is blocked, not leaked.
- `denyRead` is explicit because `srt`'s default read policy still permits reading
  those cred paths — deny them so a shelled-out `cat ~/.ssh/id_*` can't feed exfil.
- Prompt via **STDIN**, never a trailing positional arg (variadic `--allowedTools`
  swallows it — see `tech_insight_claude_cli_acceptedits_not_write_scoped`).
- Belt-and-suspenders: scrub Anthropic/cloud creds from the worker's child env
  before spawn (don't rely solely on the network boundary).

### 2.3 CLI surface (proposed — confirm at implementation)

Recommended posture: **secure-by-default**. `oxison build` sandboxes the worker
by default; `--no-sandbox` opts out (for trusted local runs / debugging). If
`srt` isn't installed, fail with a clear `npm i -g …` hint rather than silently
running unsandboxed. (The default-on-vs-opt-in call was left for implementation;
this is the recommendation.)

New `EngineConfig` fields: `sandbox_enabled: bool = True`,
`sandbox_allowed_domains: tuple[str, ...]` (the §2.2 list as the generic default),
`sandbox_extra_write_paths: tuple[str, ...] = ()`.

### 2.4 Linux CI bootstrap (one-time)

Ubuntu 24.04+ blocks unprivileged user namespaces via AppArmor, so `bwrap`
(which `srt` uses) needs a one-time `/etc/apparmor.d/bwrap` profile — Anthropic's
docs give the exact snippet. Add it to the CI runner provisioning. (On macOS,
Seatbelt needs no bootstrap.)

---

## 3. Layer 2 — rootless container — IMPLEMENTED

Selected with `oxison build --sandbox-layer container`. Each worker runs in a
**rootless Podman/Docker** container whose **only bind-mount is its workspace**,
so the host filesystem (`~/.ssh`, the main repo, every credential) is physically
absent — `--cap-drop ALL`, `--security-opt no-new-privileges`. As-built (it
diverged from the original "srt-inside-a-container" sketch — that nesting needs
nested user namespaces and is brittle; the container alone is the boundary):

- **Self-contained clone, not a linked worktree.** A linked worktree's `.git`
  points outside the mount, so the container path uses a `git clone
  --no-hardlinks` whose `.git` lives inside `/work`; the worker commits there and
  the host reads the diff from the clone. (`engine/container.py::prepare_clone`.)
- **Bare-mode auth.** The macOS Keychain / OAuth store isn't reachable in the
  Linux VM, so the worker uses `ANTHROPIC_API_KEY` forwarded into the container
  by name (never in the image). Preflight fails if no key is set.
- **The worker image** (`docker/oxfaz-worker/Dockerfile`): node + git + ripgrep +
  the `@anthropic-ai/claude-code` CLI; runs as an unprivileged `worker` user.
- **macOS constraint:** the repo must live under `$HOME` to mount into the podman
  VM (`/tmp` is not shared). Production repos in `~/code` qualify.
- **Code:** `engine/container.py` (clone + `podman run` argv + the launch path),
  routed from `engine/dispatch.py::launch_worker` when `sandbox_layer ==
  "container"`; CLI `--sandbox-layer container`; `EngineConfig.{sandbox_layer,
  container_runtime,worker_image}`. Tests: `test_container.py` (unit) +
  `test_container_spike.py` (clone self-containment + real host-isolation,
  skipif-guarded).

Verified live (macOS + podman): a real worker built + committed its task inside
the container; `/Users`, `~/.ssh`, and out-of-`/work` writes confirmed
inaccessible from inside. **Remaining tightening (follow-up):** the container
keeps default network egress — narrow it (run srt's domain proxy inside the
container, or a network policy) before treating Layer 2 as egress-safe; and the
whole-`.git`-in-clone is acceptable here since the clone is throwaway.

---

## 4. Verification (per `verification-cadence.md` — file-check, never trust narration)

A spike must prove BOTH directions on **macOS and Linux**:
- **Positive:** an in-worktree write + `git commit` + the project's test command
  all **succeed** inside the sandbox.
- **Negative (file-checked):** the worker attempting `echo x > ~/.ssh/probe`
  and `curl https://evil.example.com` both **fail** — assert by checking the
  probe file was NOT created and the curl returned non-zero, not by reading the
  worker's self-report.

---

## 5. Implementation checklist (for the future session)

- [ ] Add `srt` to setup/bootstrap (pinned global install) + document the Node prereq for build mode.
- [ ] `EngineConfig`: `sandbox_enabled`, `sandbox_allowed_domains` (default = §2.2), `sandbox_extra_write_paths`.
- [ ] `engine/dispatch.py`: `_write_srt_settings(worktree, cfg) -> Path` + prepend the `srt` argv in `launch_worker` when enabled; STDIN prompt; cred-env scrub.
- [ ] `cli.py`: `--no-sandbox` flag on `oxison build`; fail-with-hint if `srt` absent and sandbox enabled.
- [ ] Linux CI: AppArmor `bwrap` profile in runner provisioning.
- [ ] Tests: unit-test the settings-JSON builder (worktree allowlist, cred denyRead, domain list) + the argv prepend; a `@skipif(no srt)` integration spike running the positive+negative probes.
- [ ] README: update the "oxison build is different" section — sandbox now bounds the worker; document `--no-sandbox` + the Node/`srt` prereq.
- [ ] (Layer 2, later) rootless-Podman wrapper for CI.

---

## 6. Open decisions / risks

1. **`srt` beta schema churn** — pin the version; the verification probes gate upgrades.
2. **Per-worker overhead** — Seatbelt/bwrap overhead is "minimal" per docs but unmeasured for a full-test-suite build; measure wall-clock on a real build before fleet rollout.
3. **Go tools (`gh`, `terraform`) + Seatbelt TLS** — a documented macOS compat issue; if a build shells out to these, add them to `excludedCommands` or relax network isolation for them specifically.
4. **Default-on vs opt-in** — RESOLVED: default-on + `--no-sandbox`.
5. **`.git` allowWrite is whole-dir (tighten before concurrency / auto-merge).**
   To catch the top-level lock files a commit writes (`packed-refs.lock`), Layer 1
   allowlists the whole `.git` with `denyWrite` carve-outs for `.git/config` +
   `.git/hooks`. A worker can therefore still write arbitrary `.git/refs/*`,
   `.git/objects`, and other-worktree metadata. This is **harmless in the current
   slice** (`/cto` review, PR #16): workers run strictly sequentially
   (`max_workers` default 1 — no concurrent peer to poison), the
   `config.worktree → core.hooksPath` hook-injection path is unreachable (it needs
   `extensions.worktreeConfig` in the main `.git/config`, which is `denyWrite`),
   and the grader reads each worker's OWN worktree diff vs its captured base SHA,
   not a shared ref. **Before enabling `max_workers > 1` or an automated
   `git merge` consumer**, tighten `allowWrite` to `.git/worktrees/<this-task>` +
   `.git/objects` + `.git/refs/heads/<this-branch>` + the top-level lock files
   (or add cross-worktree paths to `denyWrite`).

---

## 7. Sources

- Anthropic — Claude Code sandboxing (built-in scope: Bash-only) — code.claude.com/docs/en/sandboxing
- Anthropic — sandbox-environments (`srt` recommended for `--dangerously-skip-permissions`) — code.claude.com/docs/en/sandbox-environments
- `@anthropic-ai/sandbox-runtime` — github.com/anthropic-experimental/sandbox-runtime
- Internal: `.cto/raw/security.md` (the gap), PRs #12/#13 (prior hardening), deep-research report 2026-06-15.
