"""Best-effort discovery of a project's own test command.

The regression guard (:mod:`engine.regression`) needs a command to run. When the
operator doesn't pass ``--test-cmd``, we infer one from the project's manifests so
the guard works out of the box.

Deliberately conservative + best-effort: every parse is wrapped so a malformed
manifest degrades to "not found" (``None``) rather than raising, and a wrong guess
degrades *safely* downstream — a command that doesn't exist or doesn't fit just
fails, which makes the baseline run red and leaves the guard inactive (the
green→red rule never turns an undetectable suite into a false rejection).

Precedence (first match wins): a ``Makefile`` ``test:`` target is the most
authoritative signal (the author defined it), then per-ecosystem defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

#: ``npm init`` writes this default ``scripts.test`` — present but means "no real
#: tests". Detecting it would always run red, so treat it as absent.
_NPM_TEST_PLACEHOLDER = "no test specified"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _make_test(repo: Path) -> str | None:
    text = _read(repo / "Makefile")
    if text is None:
        return None
    for line in text.splitlines():
        # A real target line ("test:" / "test :"), not a tab-indented recipe line,
        # a ".PHONY: test" declaration, or a comment.
        if line.startswith(("test:", "test :")):
            return "make test"
    return None


def _npm_test(repo: Path) -> str | None:
    text = _read(repo / "package.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if isinstance(test, str) and test.strip() and _NPM_TEST_PLACEHOLDER not in test:
        return "npm test"
    return None


def _pytest(repo: Path) -> str | None:
    # pytest mentioned in any config/manifest (dep or tool config), or a tests dir
    # with test_*.py — any one is a strong "this is a pytest project" signal.
    for name in (
        "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini",
        "requirements.txt", "requirements-dev.txt",
    ):
        text = _read(repo / name)
        if text is not None and "pytest" in text.lower():
            return "pytest"
    for d in ("tests", "test"):
        td = repo / d
        if td.is_dir() and any(td.glob("test_*.py")):
            return "pytest"
    return None


def _cargo(repo: Path) -> str | None:
    return "cargo test" if (repo / "Cargo.toml").is_file() else None


def _go(repo: Path) -> str | None:
    return "go test ./..." if (repo / "go.mod").is_file() else None


def discover_test_command(repo: Path) -> str | None:
    """Infer the project's test command from its manifests, or ``None``.

    Never raises — a parse error or unreadable file just skips that detector.
    """
    for detector in (_make_test, _npm_test, _pytest, _cargo, _go):
        try:
            cmd = detector(repo)
        except Exception:  # noqa: BLE001 — discovery is best-effort, never fatal
            cmd = None
        if cmd:
            return cmd
    return None


__all__ = ["discover_test_command"]
