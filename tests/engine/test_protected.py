"""Phase 0 — the segment-anchored protected-path matcher (C1/H3 corpus).

This exact bypass corpus is the named regression test from the plan's Phase-0
acceptance criteria, and it is reused by both the planner test (Phase 4) and
the grader test (Phase 5) — proving one matcher, two consumers (H3).
"""

from __future__ import annotations

import pytest

from oxison.engine.engconfig import DEFAULT_PROTECTED_PATHS
from oxison.engine.protected import is_protected

# A small rule set covering both file-segment rules and directory rules,
# including an ``alembic/`` directory rule to exercise the monorepo-nesting
# case (which a root-anchored ``startswith`` would miss).
RULES = (*DEFAULT_PROTECTED_PATHS, "alembic/")


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        ".env.local",
        ".github/workflows/deploy.yml",
        "apps/api/alembic/versions/072.py",  # nested dir rule (no startswith bypass)
        ".git/config",
        ".ssh/id_rsa",
        "oxison-build/state.db",  # the engine's own state is protected
        "pnpm-lock.yaml",
        "services/web/.env.production",  # dotted child nested deep
    ],
)
def test_protected_paths_are_caught(path: str) -> None:
    assert is_protected(path, RULES) is True


@pytest.mark.parametrize(
    "path",
    [
        "envoy.py",  # starts with ".env" chars but is a different name
        "preview.env",  # ends with ".env" but is not the .env segment
        "my_env.py",
        "src/oxison/config.py",
        "README.md",
        "docs/env-setup.md",  # contains "env" substring, not a protected segment
        "alembican/migrate.py",  # "alembic" is a substring, not a segment
    ],
)
def test_non_protected_paths_pass(path: str) -> None:
    assert is_protected(path, RULES) is False


def test_directory_rule_requires_a_file_beneath() -> None:
    # A directory rule matches when the dir has something under it...
    assert is_protected("apps/api/alembic/versions/072.py", ("alembic/",)) is True
    # ...the bare directory path itself (last segment == the dir name) is a
    # segment match only if a file rule, not a directory rule.
    assert is_protected("apps/api/alembic", ("alembic/",)) is False


def test_backslash_paths_are_normalized() -> None:
    assert is_protected(r".github\workflows\deploy.yml", (".github/workflows/",)) is True


def test_leading_dot_slash_is_stripped() -> None:
    assert is_protected("./.env", (".env",)) is True


def test_empty_path_is_not_protected() -> None:
    assert is_protected("", RULES) is False


# --- defense-in-depth fail-safes (review F2/F3) ---


def test_dotdot_segment_is_protected_failsafe() -> None:
    # A `..` could traverse into a protected dir while dodging a segment rule;
    # a build worker has no legit reason to emit one, so flag it outright.
    assert is_protected(".github/sub/../workflows/x.yml", (".github/workflows/",)) is True
    assert is_protected("a/../b.py", (".env",)) is True


def test_matching_is_case_insensitive() -> None:
    # On case-insensitive hosts (macOS/Windows) `.GITHUB/workflows` IS the real
    # protected dir; a case-sensitive compare would wave it through.
    assert is_protected(".GITHUB/workflows/deploy.yml", (".github/workflows/",)) is True
    assert is_protected(".ENV.PRODUCTION", (".env",)) is True
    assert is_protected("APPS/API/Alembic/versions/1.py", ("alembic/",)) is True
