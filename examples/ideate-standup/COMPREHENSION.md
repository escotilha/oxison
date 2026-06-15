# Product Understanding: Markdown Standup Digest CLI

---

## 1. Vision

A lightweight, self-contained command-line tool that acts as an automated daily standup assistant for individuals or small teams who keep notes in Markdown files. It monitors a designated notes folder, surfaces what's unfinished and what's been recently touched, and delivers a concise digest to a Slack channel — turning a passive notes habit into an active daily awareness signal, with zero manual effort. [brief:idea]

---

## 2. Problem & Target Users

**Problem:** People who maintain Markdown-based notes (in tools like Obsidian, Foam, plain editors, etc.) accumulate TODOs and file changes that are never surfaced to teammates. Writing a daily standup update is a manual, often-skipped ritual. There is no lightweight bridge between a local notes folder and a team communication channel.

**Target users:** Individual developers, writers, or small teams who:
- Keep personal or project notes as Markdown files in a local folder [brief:idea]
- Use Slack as their team communication layer [brief:idea]
- Prefer CLI tooling and automation over GUI apps
- Already run scheduled tasks (cron) on their machines

There is no signal in the sources indicating this targets large enterprises or non-technical users.

---

## 3. Core Capabilities

Derived directly from [brief:idea]:

| # | Capability | Detail |
|---|-----------|--------|
| 1 | **Folder watching / scanning** | Reads a configured folder of `.md` files to gather raw material for the digest |
| 2 | **Open TODO extraction** | Parses Markdown files and identifies lines that represent open/incomplete TODO items (e.g. `- [ ] …` GFM task syntax, or similar patterns) |
| 3 | **Recently-changed file detection** | Identifies which files in the watched folder have been modified within a recent time window (presumably the last ~24 hours, given the daily standup cadence) |
| 4 | **Digest composition** | Assembles extracted TODOs and recently-changed file references into a human-readable standup-style summary |
| 5 | **Slack webhook posting** | HTTP-POSTs the composed digest to a configured Slack incoming webhook URL |
| 6 | **TOML configuration** | All runtime parameters (folder path, webhook URL, time window, etc.) are read from a single TOML config file — no flags required for normal operation |
| 7 | **Single binary distribution** | Ships as one self-contained executable; no runtime dependencies to install |
| 8 | **Cron-driven execution** | Designed to be invoked by a system cron job (or equivalent scheduler) rather than running as a persistent daemon |

---

## 4. Scope & Non-Goals

**In scope** (supported by [brief:idea]):
- Local filesystem scanning of a single Markdown notes folder
- TODO line extraction from Markdown content
- File-change recency detection (filesystem metadata)
- Slack delivery via incoming webhook
- TOML-based configuration
- Single-binary packaging
- Cron/scheduled invocation model

**Implied non-goals** (not mentioned in sources; calling out to flag, not invent):
- **Not** a persistent file-watcher daemon — "watches" in the brief reads contextually as periodic scanning on cron, not `inotify`/`FSEvents`-style real-time watching (ambiguous — see Open Questions)
- **Not** a multi-destination notifier — only Slack webhooks are mentioned; no email, Teams, Discord, etc.
- **Not** a note-taking application itself — it only reads an existing folder
- **Not** a two-way integration — no reading from or writing back to Slack
- **Not** a web service or API — purely a local CLI tool
- **Not** multi-user/multi-folder in a single invocation — the brief implies a single configured folder

> ⚠️ Note: [web:example.com] contains no product-relevant information and has not influenced this analysis.

---

## 5. Constraints & Assumptions

| Category | Constraint / Assumption | Source |
|----------|------------------------|--------|
| **Distribution** | Must ship as a single binary (implies a compiled language — Go, Rust, or similar) | [brief:idea] |
| **Configuration** | TOML format specifically; no mention of env-var or CLI-flag overrides | [brief:idea] |
| **Integration** | Slack only, via incoming webhook (not the Slack Bot/OAuth API) | [brief:idea] |
| **Scheduling** | Relies on external cron; the binary itself is not a scheduler | [brief:idea] |
| **Input format** | Markdown files; specific TODO syntax (e.g. GFM `- [ ]` vs. freeform `TODO:`) is unspecified | [brief:idea] |
| **Platform** | Not stated explicitly; "single binary + cron" implies Unix/macOS/Linux primary targets; Windows cron compatibility is unaddressed | [brief:idea] |
| **Network** | Requires outbound HTTPS to the Slack webhook endpoint at runtime | [brief:idea] |

---

## 6. Open Questions

These are genuine gaps a builder would need resolved before implementation:

1. **TODO syntax definition** — What constitutes an "open TODO line"? GFM checkboxes (`- [ ] item`) only? Freeform `TODO:` prefixes? Both? Case-sensitive? The extraction logic depends entirely on this. [brief:idea]

2. **"Recently changed" time window** — Is the recency window hardcoded (e.g. last 24 h), configurable in TOML, or inferred from the cron schedule? What happens on weekends or missed runs? [brief:idea]

3. **"Watches a folder" — polling vs. event-driven** — Does "watches" mean a persistent daemon with filesystem events, or a one-shot scan triggered by cron? These are architecturally very different. [brief:idea]

4. **Digest format / Slack message structure** — Should the Slack message use plain text, Slack Block Kit, mrkdwn formatting? Is there a desired template the user can customise, or is it fixed? [brief:idea]

5. **Subdirectory handling** — Does the tool scan the top-level folder only, or recurse into subdirectories? [brief:idea]

6. **Config file location** — Where is the TOML file expected? A fixed path (`~/.config/standupbot/config.toml`), a flag-specified path, or the current working directory? [brief:idea]

7. **Error handling & delivery failures** — If the Slack webhook is unreachable, does the tool exit with a non-zero code (for cron alerting), silently retry, or write to a local log? [brief:idea]

8. **Duplicate suppression** — If the same TODO appears across multiple files, is it deduplicated in the digest? [brief:idea]

9. **Platform targets** — Are Windows and ARM (e.g. Apple Silicon, Raspberry Pi) explicit targets for the binary build? [brief:idea]

10. **Authentication / webhook security** — Is the webhook URL treated as a secret (e.g. should it support reading from an env var to avoid storing it in plaintext TOML)? [brief:idea]