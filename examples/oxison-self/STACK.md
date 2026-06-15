# Tech Stack

## Languages

| Language | Files | LOC | Role |
|---|---|---|---|
| **Python** | 94 | 12,542 | Entire application runtime — CLI, pipeline, engine, all tooling |
| **Markdown** | 7 | 1,447 | Project documentation and the Claude Code plugin definition (`SKILL.md`) |
| **YAML** | 1 | 178 | GitHub Actions CI workflow |
| **TOML** | 1 | 74 | Project manifest (`pyproject.toml`) |
| **JSON** | 2 | 29 | Schema fixtures and lock metadata |

Python ≥ 3.11 is required. The minimum is load-bearing: `tomllib` (manifest parsing) is stdlib from 3.11, and the codebase uses `match` statements and other 3.10+ syntax throughout. CI runs the matrix on **3.11** and **3.12**.

---

## Frameworks and Key Libraries

All versions below are pinned in `uv.lock` and represent the resolved install.

### Required runtime dependency

| Package | Declared constraint | Locked version | Purpose |
|---|---|---|---|
| **PyYAML** | `>=6.0` | 6.0.3 | YAML parsing (service-hint detection in `repomap.py`, config handling) |

### Optional runtime extras

These extras are not installed by default. Each source adapter degrades gracefully when its extra is absent.

| Extra | Package | Declared constraint | Locked version | Purpose |
|---|---|---|---|---|
| `pretty` | **rich** | `>=13` | 15.0.0 | Enhanced terminal output (progress, panels, colour) |
| `pdf` / `sources` | **pypdf** | `>=4` | 6.13.2 | Text extraction from PDF files in `sources/pdf.py` |
| `pptx` / `sources` | **python-pptx** | `>=0.6` | 1.0.2 | Slide text extraction from PowerPoint files in `sources/pptx.py` |
| `docx` / `sources` | **python-docx** | `>=1.1` | 1.2.0 | Paragraph text extraction from Word documents in `sources/docx.py` |

### Standard-library modules used directly

| Module | Purpose |
|---|---|
| `tomllib` | Parses `pyproject.toml` / `Cargo.toml` dependency manifests in `repomap.py` |
| `sqlite3` | Backing store for the Oxfaz `TaskStore` (`state.db`) and the `memory/` subsystem (`memory.db`) |
| `asyncio` | Concurrent comprehension workers, concurrent artifact generation, subprocess draining |
| `argparse` | CLI entry point in `cli.py` |
| `importlib` | Dynamic import of `pipeline` from `cli.py` to keep the import graph decoupled |
| `subprocess` | Spawning all `claude -p` worker processes via `dispatch.invoke()` |
| `json` | Manifest I/O, `comprehension.json`, `roadmap.json`, cost extraction from worker stdout |

### Indirect transitive dependencies (selected)

These are pulled in by the optional extras above and are visible in `uv.lock`:

| Package | Locked version | Pulled in by |
|---|---|---|
| `lxml` | 6.1.1 | `python-pptx`, `python-docx` |
| `pillow` | 12.2.0 | `python-pptx` |
| `xlsxwriter` | 3.2.9 | `python-pptx` |
| `markdown-it-py` | 4.2.0 | `rich` |
| `pygments` | 2.20.0 | `rich` |
| `colorama` | 0.4.6 | `rich` (Windows console colour) |

---

## Build and Developer Tooling

| Tool | Declared constraint | Locked version | Role |
|---|---|---|---|
| **setuptools** | `>=68` | build backend | PEP-517 build backend declared in `[build-system]` |
| **uv** | — | — | Dependency resolver and environment manager; `uv.lock` is the reproducible lockfile |
| **ruff** | `>=0.6` | 0.15.17 | Linting (`E`, `F`, `I`, `UP`, `B`, `S`, `C4`, `SIM` rule sets) and import sorting; `target-version = "py311"`, line length 100 |
| **mypy** | `>=1.10` | 2.1.0 | Static type checking in strict mode (`strict = true`); targets `src/`; optional-dep modules (`pypdf`, `pptx`, `docx`) configured with `ignore_missing_imports` |
| **pytest** | `>=8` | 9.1.0 | Test framework; `testpaths = ["tests"]` |
| **pytest-asyncio** | `>=0.23` | 1.4.0 | `asyncio_mode = "auto"` — all async test functions run natively without extra decorators |

The project is packaged as `oxi-son` (PyPI name) version `0.1.0` with a single console script entry point:

```
oxison = "oxison.cli:main"
```

Zero-install execution is supported via `uvx`:

```
uvx --from git+https://github.com/escotilha/oxison oxison run /path/to/repo
```

---

## External Binary Dependencies

These are not Python packages. They must be present on the host (or inside the container) at run time.

| Binary / package | Source | Required for | Notes |
|---|---|---|---|
| **`claude`** (Claude Code CLI) | `npm install -g @anthropic-ai/claude-code` | Everything — all AI calls | The sole AI backend; every inference request is a `claude -p` subprocess. `preflight.py` validates its presence via `claude --version` before any pipeline step runs. |
| **`srt`** (`@anthropic-ai/sandbox-runtime`) | npm global | Oxfaz Layer-1 sandbox | OS-level filesystem + egress allowlist around build workers; optional — falls back to unsandboxed if absent |
| **`git`** | system | Oxfaz engine | Worktree creation, diff inspection, branch management for autonomous build tasks |
| **`ripgrep`** | system / container | Build workers | Claude's `Grep` tool inside the worker container |
| **`podman`** or **`docker`** | system | Oxfaz Layer-2 sandbox | Rootless container isolation for build workers; optional — only required when `--sandbox-layer container` is specified |

---

## Infrastructure and External Services

### AI backend

All inference goes through the **Anthropic API** via the `claude` CLI subprocess. Authentication is either:

- **OAuth mode** (default on a developer workstation) — the CLI's own stored session
- **Bare mode** (CI, containers) — `ANTHROPIC_API_KEY` / `OXISON_API_KEY` environment variable; the only mode available inside the Layer-2 Docker container because the macOS Keychain is absent

### Databases

| Database | Engine | Location | Used by |
|---|---|---|---|
| Task store | SQLite (stdlib `sqlite3`) | `<output-dir>/oxison-build/state.db` | Oxfaz engine — task lifecycle, file-level locks |
| Memory store | SQLite (stdlib `sqlite3`) | `<output-dir>/oxison-build/memory.db` | Experimental cross-run memory subsystem; FTS5 full-text search, optional vector retrieval, graph edge table |

No external database server is required; both databases are file-local.

### Container image (Oxfaz Layer-2 worker)

`docker/oxfaz-worker/Dockerfile` builds the build-worker sandbox image:

| Layer | Detail |
|---|---|
| Base image | `node:22-slim` |
| System packages | `git`, `ca-certificates`, `ripgrep`, `tini` |
| Node package | `@anthropic-ai/claude-code` (npm global) |
| Process model | `ENTRYPOINT ["tini", "--"]` — clean PID-1 reaping |
| Security posture | Runs as unprivileged `worker` user; only the task worktree is bind-mounted; `--cap-drop ALL` at run time; host filesystem (`~/.ssh`, credentials, main repo) is physically absent |
| Auth | `ANTHROPIC_API_KEY` injected at run time — never baked into the image |

### Optional cloud service

| Service | Purpose | When used |
|---|---|---|
| Cloud STT API (e.g. Deepgram, OpenAI Whisper) | Audio / video transcription | Only when `--stt-key` is passed to `oxison run`; the one off-host network call in the `run` pipeline |

### CI/CD

GitHub Actions (`ubuntu-latest`; overridable via `OXISON_RUNNER` repo variable). Five jobs run on every push to `main` and every pull request:

| Job | Tool | Version |
|---|---|---|
| `python` (matrix: 3.11, 3.12) | ruff → mypy → pytest | per `dev` extra |
| `gitleaks` | gitleaks CLI | 8.21.2 (SHA-pinned download) |
| `pip-audit` | pip-audit | latest at CI install time |
| `bandit` | bandit\[toml\] | ≥ 1.8 |
| `codeql` | GitHub CodeQL Python analysis | v3.35.2 (SHA-pinned action) |

All third-party Actions are pinned to full commit SHAs (not mutable version tags).
