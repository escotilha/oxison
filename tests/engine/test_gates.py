"""Tests for the Oxfaz grader (protected-path fence on the actual diff)."""

from __future__ import annotations

from oxison.engine.engconfig import EngineConfig
from oxison.engine.gates import grade_diff

PROTECTED = EngineConfig().protected_paths


def test_clean_diff_passes():
    v = grade_diff(["src/match.py", "tests/test_match.py"], protected_paths=PROTECTED)
    assert v.ok


def test_empty_diff_rejected():
    v = grade_diff([], protected_paths=PROTECTED)
    assert not v.ok
    assert "empty diff" in v.reason


def test_protected_ci_path_rejected():
    v = grade_diff(["src/x.py", ".github/workflows/ci.yml"], protected_paths=PROTECTED)
    assert not v.ok
    assert ".github/workflows/ci.yml" in v.protected_hits


def test_protected_env_and_lockfile_rejected():
    v = grade_diff(["apps/api/.env", "pnpm-lock.yaml"], protected_paths=PROTECTED)
    assert not v.ok
    assert len(v.protected_hits) == 2


def test_engine_state_path_rejected():
    # A worker must never touch the engine's own state dir.
    v = grade_diff(["oxison-build/state.db"], protected_paths=PROTECTED)
    assert not v.ok


def test_grader_rejects_bare_protected_directory():
    # The grader now uses the same bare-dir-aware matcher as the plan-gate, so a
    # diff path that IS a protected directory (no child) is caught too.
    for path in ("oxison-build", ".github/workflows", ".git"):
        v = grade_diff([path], protected_paths=PROTECTED)
        assert not v.ok, path
        assert path in v.protected_hits


def test_diff_size_cap():
    v = grade_diff(["src/x.py"], protected_paths=PROTECTED,
                   diff_size_cap=100, changed_line_count=250)
    assert not v.ok
    assert "too large" in v.reason


def test_diff_under_cap_passes():
    v = grade_diff(["src/x.py"], protected_paths=PROTECTED,
                   diff_size_cap=100, changed_line_count=50)
    assert v.ok
