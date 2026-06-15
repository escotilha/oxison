# Markdown Standup Digest CLI

> **Status:** Greenfield — nothing is built yet. This document describes the product to be designed and implemented.

---

## Overview

**Markdown Standup Digest** is a lightweight command-line tool that reads a local folder of Markdown notes, extracts open TODO items and recently modified files, and posts a concise standup-style digest to a Slack channel via an incoming webhook. It is designed to run unattended on a cron schedule, requiring no manual intervention once configured. The entire tool ships as a single self-contained binary with all runtime settings stored in a TOML configuration file.

---

## Problem It Solves & Who It Is For

### The Problem

Developers, writers, and small teams who maintain notes in Markdown (using tools such as Obsidian, Foam, or a plain text editor) accumulate TODOs and file changes that are invisible to teammates. Writing a daily standup update is a manual ritual that is easy to skip and rarely reflects what is actually recorded in a person's notes. There is currently no lightweight, automation-friendly bridge between a local notes folder and a team communication channel.

### Target Users

This tool is intended for:

- **Individual contributors** — developers, writers, researchers — who keep working notes as `.md` files in a local folder and want to surface their daily activity without extra effort.
- **Small, async-friendly teams** that use Slack as their primary communication layer and value automation over ceremony.
- **CLI-native practitioners** who are comfortable configuring tooling in a text file and scheduling tasks via cron.

This is explicitly **not** targeting large enterprises, non-technical users, or teams that do not already have a Markdown notes habit.

### Representative Use Cases

| User | Scenario |
|------|----------|
| Solo developer | Runs the tool each morning via cron; a digest of yesterday's open TODOs lands in their personal Slack DM before they start work. |
| Small engineering team | Each member has the tool configured; digests are posted to a shared `#standup` channel, replacing a manual written update. |
| Independent writer | Tracks project TODOs in Markdown; the tool posts a daily reminder of outstanding items to a Slack workspace shared with an editor. |

---

## Proposed Core Features & Capabilities

### 1. Markdown Folder Scanning

The tool will scan a configured local folder for `.md` files. The folder path is specified in the TOML configuration file.

> **Open question:** Whether the scan is recursive into subdirectories, or limited to the top level, is not yet defined and must be resolved during design. [brief:idea]

---

### 2. Open TODO Extraction

The tool will parse each Markdown file and extract lines that represent open, incomplete TODO items. The most natural candidate syntax is the GitHub Flavoured Markdown (GFM) task list format (`- [ ] item text`), but whether additional patterns (e.g. freeform `TODO:` prefixes) are also supported is an open question.

> **Open question:** The exact syntax rules for what constitutes an "open TODO" — GFM checkboxes only, freeform prefixes, or both; case sensitivity; deduplification across files — must be explicitly specified before implementation. [brief:idea]

---

### 3. Recently-Changed File Detection

The tool will inspect filesystem modification metadata and identify which files in the watched folder have been changed within a recent time window. Given the daily standup cadence, a 24-hour lookback is the natural default.

> **Open question:** Whether this window is hardcoded or configurable in TOML, and how missed runs (e.g. weekends) are handled, must be decided. [brief:idea]

---

### 4. Digest Composition

Extracted TODOs and references to recently-changed files will be assembled into a human-readable standup-style summary, structured to answer the implicit questions: *what did I work on recently?* and *what is still open?*

> **Open question:** The exact message format — plain text, Slack `mrkdwn`, or Slack Block Kit rich formatting — and whether the template is user-customisable or fixed, must be defined. [brief:idea]

---

### 5. Slack Webhook Delivery

The composed digest will be HTTP-POSTed to a Slack incoming webhook URL specified in the configuration. This uses Slack's simple incoming webhook mechanism, not the full Slack Bot/OAuth API.

> **Open question:** Error handling on delivery failure (non-zero exit for cron alerting, silent retry, local log) must be specified. The security posture of the webhook URL (plaintext in TOML vs. read from an environment variable) should also be addressed. [brief:idea]

---

### 6. TOML Configuration

All runtime parameters — at minimum the notes folder path and the Slack webhook URL — will be read from a single TOML configuration file. No command-line flags should be required for normal daily operation.

> **Open question:** The expected location of the config file (e.g. `~/.config/standupbot/config.toml`, current working directory, or a flag-specified path) is not yet defined. [brief:idea]

---

### 7. Single Binary Distribution

The tool will be packaged and distributed as one self-contained executable with no runtime dependencies (no interpreter, no package manager). This implies implementation in a compiled language (Go, Rust, or similar).

> **Open question:** Target platforms (Linux x86-64, macOS ARM/Intel, Windows) and whether pre-built binaries are provided for all of them must be confirmed. [brief:idea]

---

### 8. Cron-Driven Execution Model

The tool is designed to be invoked by an external scheduler (system cron or equivalent) as a one-shot process. It starts, performs its work, and exits. It does not run as a persistent daemon or background service.

---

## Intended User-Facing Mental Model / UX

The user experience is deliberately invisible once set up. The intended mental model is:

> *"I configure it once, add a cron line, and my standup shows up in Slack every morning without me thinking about it."*

**The setup flow (one time):**
1. Download the single binary and place it on `$PATH`.
2. Create a TOML config file specifying the notes folder and Slack webhook URL.
3. Add a single cron entry (e.g. `0 9 * * 1-5 /usr/local/bin/standup-digest`).

**The daily runtime flow (automated, zero-touch):**
1. Cron fires the binary at the configured time.
2. The binary reads config, scans the notes folder, extracts TODOs and recent changes.
3. A digest message appears in the configured Slack channel.
4. The binary exits cleanly.

There is no interactive prompt, no GUI, and no ongoing process to manage. The only touchpoint after initial setup is the Slack message itself and, if something goes wrong, the cron daemon's error output.

---

## Explicit Non-Goals

The following are **out of scope** for this product, based on the available inputs. They should not be added without a deliberate decision to expand scope.

| Non-Goal | Rationale |
|----------|-----------|
| Persistent filesystem event daemon (`inotify`/`FSEvents` style) | The execution model is cron-triggered one-shot; a persistent watcher is a different architecture entirely. |
| Destinations other than Slack | Only Slack incoming webhooks are specified. No email, Microsoft Teams, Discord, or other targets. |
| Two-way Slack integration | The tool only posts; it does not read from Slack or respond to messages. |
| Note-taking or file editing | The tool is read-only with respect to the notes folder. |
| Web service or API endpoint | This is a local CLI tool only; no server component. |
| Multi-folder scanning in one invocation | A single configured folder is the unit of operation. |
| Large-enterprise features | No LDAP, SSO, audit logging, or multi-tenant concerns. |
| Non-technical end users | Setup requires comfort with a terminal, a text editor, and cron. |

---

## Key Assumptions

| Assumption | Basis |
|------------|-------|
| Markdown files use `.md` extension | Implied by "folder of Markdown notes" [brief:idea] |
| The host machine has outbound HTTPS access to Slack's webhook endpoints | Required for delivery; no mention of proxy or air-gapped support |
| A compiled language will be used to achieve the single-binary requirement | No runtime (Python, Node, etc.) is viable for a true single binary without bundling |
| The cron schedule itself is the user's responsibility | The tool does not install or manage its own cron entry |
| Slack incoming webhooks (not the Slack API) are sufficient | No OAuth, bot tokens, or channel management are needed |
| Primary platforms are macOS and Linux | "Single binary + cron" most naturally maps to Unix-family systems; Windows support is unconfirmed |

---

## Open Questions Summary

The following must be answered before implementation begins:

1. **TODO syntax** — GFM `- [ ]` only, or also freeform `TODO:` patterns?
2. **Recency time window** — Hardcoded 24 h, or configurable in TOML?
3. **Subdirectory recursion** — Top-level only, or full recursive scan?
4. **Slack message format** — Plain text, `mrkdwn`, or Block Kit? User-customisable template?
5. **Config file location** — Fixed path convention, CWD, or flag-specified?
6. **Delivery failure behaviour** — Non-zero exit, retry, or local log?
7. **Webhook URL security** — Plaintext in TOML acceptable, or should an env-var override be supported?
8. **Duplicate TODO handling** — If the same TODO item appears in multiple files, is it shown once or once per file?
9. **Platform targets** — Which OS/architecture combinations require pre-built binaries?
10. **Weekend / missed-run behaviour** — Does the recency window expand, or does the tool always look back exactly N hours regardless of gaps?
