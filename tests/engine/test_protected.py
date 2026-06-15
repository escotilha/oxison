"""Phase 0 — the segment-anchored protected-path matcher (C1/H3 corpus).

This exact bypass corpus is the named regression test from the plan's Phase-0
acceptance criteria, and it is reused by both the planner test (Phase 4) and
the grader test (Phase 5) — proving one matcher, two consumers (H3).
"""

from __future__ import annotations

import pytest

from oxison.engine.engconfig import DEFAULT_PROTECTED_PATHS
from oxison.engine.protected import is_protected, is_protected_path

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


# --- is_protected_path: the bare-directory probe (H2 — the grader's gate) -----
# is_protected requires a child segment for a dir rule, so a path that NAMES a
# protected directory itself slips through it. is_protected_path closes that —
# this is the function the grader actually calls on diff paths, so it gets a
# direct corpus (it was previously only exercised once, indirectly via the gate).


@pytest.mark.parametrize("bare_dir", [
    ".github/workflows",   # CI dir, no trailing child
    "oxison-build",        # the engine's own state dir
    ".git",                # git internals
    "alembic",             # monorepo dir rule, named directly
    "apps/api/alembic",    # nested, named directly
])
def test_bare_protected_dir_caught_by_probe(bare_dir):
    # is_protected MISSES the bare dir (the gap)...
    assert not is_protected(bare_dir, RULES)
    # ...but is_protected_path catches it (what the grader relies on).
    assert is_protected_path(bare_dir, RULES)


def test_bare_protected_dir_with_trailing_slash_caught():
    assert is_protected_path(".github/workflows/", RULES)
    assert is_protected_path("oxison-build/", RULES)


@pytest.mark.parametrize("child", [
    ".github/workflows/ci.yml",
    "oxison-build/state.db",
    "apps/api/alembic/versions/072.py",
    ".env",
    ".env.production",
])
def test_children_of_protected_still_caught(child):
    assert is_protected_path(child, RULES)


@pytest.mark.parametrize("safe", [
    "src/app.py",
    "README.md",
    "internal/config/config.go",
    "workflows.py",          # not under .github/
    "my_alembic_notes.md",   # not the alembic/ dir
])
def test_normal_paths_not_false_positived_by_probe(safe):
    assert not is_protected_path(safe, RULES)
