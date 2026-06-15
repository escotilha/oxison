# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

Use GitHub's **private vulnerability reporting**:
[**Report a vulnerability**](https://github.com/escotilha/oxison/security/advisories/new)
(repo → **Security** tab → **Report a vulnerability**). This opens a private
advisory visible only to the maintainers.

Include, where you can: affected version/commit, your OS + Python version, steps
to reproduce, and the impact. We aim to acknowledge reports within a few days;
this is a community project, so there is no formal SLA.

## Supported versions

oxison is pre-1.0 — only the latest `main` (and the most recent release) receives
fixes. Please reproduce on the latest commit before reporting.

## Threat model

oxison's design centers on not trusting the repository it analyzes:

- **`oxison run` / `oxison plan` are read-only.** The AI worker is launched with
  `--allowedTools Read,Glob,Grep` — no shell, no write tools (`Bash` is
  deliberately excluded) — so it physically cannot modify, create, delete, or
  execute anything in the target repo. oxison itself writes only into its own
  `./oxison-output/` directory.
- **`oxison build` (Oxfaz) writes code by design**, so it is contained by three
  layers: a filesystem + network **sandbox** (srt or a rootless container),
  per-task **git worktree isolation**, and a **grader** that rejects any diff
  touching a protected path (CI config, `.env`, lockfiles, `.git/`,
  `oxison-build/`). With the sandbox on, build mode is safe to point at repos you
  don't fully trust.

Reports that demonstrate a way to escape these boundaries — a read-only stage
that writes/executes, a write outside `./oxison-output/`, or a build worker that
escapes the sandbox/worktree or touches a protected path — are especially
valuable.

## Data handling

oxison processes everything locally and sends nothing off-host **except**:

- the AI calls to Anthropic via the Claude Code CLI you have configured, and
- the opt-in `--stt-key` recording path, which uploads audio/video to the
  third-party cloud STT API you supply a key for.

No other adapter transmits your data anywhere.
