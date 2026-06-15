# Contributing to oxison

Thanks for your interest in improving oxison. This guide covers the dev setup,
the checks your change must pass, and the safety invariants every contribution
must preserve.

## Development setup

Requires **Python ≥ 3.11**. The repo uses [uv](https://github.com/astral-sh/uv);
plain `pip` + `venv` works too.

```bash
uv venv --python 3.12 && . .venv/bin/activate
uv pip install -e ".[dev]"          # add ".[dev,sources]" when touching source adapters
```

The `oxison` CLI drives the **[Claude Code](https://claude.com/claude-code) CLI**
as a subprocess, so it must be installed and signed in to exercise the AI stages
end-to-end (see [Auth](README.md#auth)). Pure unit tests don't need it.

## Checks (run before every PR)

```bash
ruff check src tests        # lint (line-length 100)
mypy src                    # strict type-check
pytest -q                   # tests (pytest-asyncio, auto mode)
```

CI runs the same three on Python 3.11 and 3.12, plus **gitleaks**, **pip-audit**,
**bandit**, and **CodeQL**. A PR merges only once every check is green.

## Pull request workflow

1. Branch from `main` (`feat/…`, `fix/…`, `docs/…`, `chore/…`).
2. Keep the change focused and match the surrounding style.
3. Use **Conventional Commits**: `type(scope): description`
   (e.g. `fix(oxfaz): clear stale manifest errors`).
4. Open a PR against `main` and make sure CI is green.

## Safety invariants — do not break these

oxison's core guarantee is that **`oxison run` and `oxison plan` never modify the
repo they analyze.** Two invariants enforce it, and your change must keep both:

1. **AI workers in `run`/`plan` are structurally read-only** — launched with
   `--allowedTools Read,Glob,Grep`: no shell, no write tools (`Bash` is
   deliberately excluded, since under `bypassPermissions` a shell is a full
   write/exec primitive). A unit test asserts the exclusion against the built
   command line — keep it passing.
2. **oxison owns every write**, exclusively into `./oxison-output/`. Workers
   return text; oxison writes the files. Don't hand a read-only worker write
   tools, and don't let oxison write outside its output dir.

`oxison build` (Oxfaz) is the one stage that writes code by design. It is
contained by three layers you must not weaken: a filesystem + network **sandbox**
(srt or a rootless container), **worktree isolation**, and a **grader** that
rejects any diff touching a protected path (CI config, `.env`, lockfiles,
`.git/`, `oxison-build/`). New build-stage code must stay inside these boundaries.

## Reporting issues

Open a GitHub issue with steps to reproduce, expected vs. actual behavior, and
your OS + Python version. For anything security-sensitive, please report it
privately (GitHub's private vulnerability reporting on the Security tab) rather
than opening a public issue.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
