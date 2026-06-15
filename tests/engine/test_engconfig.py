"""Phase 0 — EngineConfig constant surface.

Named regression test (C3 prerequisite): the per-worker ``max_budget_usd``
defaults non-None and rejects an explicit ``None`` on the build path, so the
loop's budget floor (LP3) can never degenerate to ``$0.00``.
"""

from __future__ import annotations

import dataclasses

import pytest

from oxison.engine.engconfig import EngineConfig


def test_defaults_construct() -> None:
    cfg = EngineConfig()
    assert cfg.max_workers == 3
    assert cfg.redispatch_cap == 3
    assert cfg.branch_prefix == "feat/oxison-"
    assert cfg.env_task_id == "OXISON_TASK_ID"


def test_per_worker_budget_is_non_none_by_default() -> None:
    # C3: the per-worker cap is always set.
    cfg = EngineConfig()
    assert cfg.worker_max_budget_usd is not None
    assert cfg.worker_max_budget_usd > 0


def test_per_worker_budget_rejects_none() -> None:
    # C3: a None cap reopens the budget-floor hole — the build path rejects it.
    with pytest.raises(ValueError, match="worker_max_budget_usd"):
        EngineConfig(worker_max_budget_usd=None)  # type: ignore[arg-type]


def test_per_worker_budget_rejects_zero() -> None:
    with pytest.raises(ValueError, match="worker_max_budget_usd"):
        EngineConfig(worker_max_budget_usd=0.0)


def test_per_worker_budget_rejects_negative() -> None:
    with pytest.raises(ValueError, match="worker_max_budget_usd"):
        EngineConfig(worker_max_budget_usd=-1.0)


def test_per_worker_budget_rejects_bool() -> None:
    # bool is a subclass of int — True must NOT slip through as 1.0 (review F1).
    with pytest.raises(ValueError, match="worker_max_budget_usd"):
        EngineConfig(worker_max_budget_usd=True)  # type: ignore[arg-type]


def test_per_worker_budget_rejects_nan_and_inf() -> None:
    # nan: all comparisons are False, so a naive `cap <= 0` would miss it.
    with pytest.raises(ValueError, match="worker_max_budget_usd"):
        EngineConfig(worker_max_budget_usd=float("nan"))
    with pytest.raises(ValueError, match="worker_max_budget_usd"):
        EngineConfig(worker_max_budget_usd=float("inf"))


def test_run_level_ceiling_may_be_unset() -> None:
    # Distinct from the per-worker cap: the run-level ceiling is allowed None.
    cfg = EngineConfig()
    assert cfg.budget_ceiling_usd is None


def test_pre_push_test_command_is_not_hardcoded() -> None:
    # Must default to "discover the host project's own" (None), never ruff/pytest.
    cfg = EngineConfig()
    assert cfg.pre_push_test_command is None


def test_no_contably_strings_in_defaults() -> None:
    # Clean-room: no project-specific constant leaked into a default.
    cfg = EngineConfig()
    blob = repr(dataclasses.asdict(cfg)).lower()
    for needle in ("contably", "nuvini", "oxi_core", "receitaws", "pluggy", "/users/"):
        assert needle not in blob


def test_is_frozen() -> None:
    cfg = EngineConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.max_workers = 5  # type: ignore[misc]
