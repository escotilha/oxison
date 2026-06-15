"""Tests for the salience formula — recency x pain x importance, per-tier decay."""

from __future__ import annotations

from oxison.memory.config import TIER_EPISODIC, TIER_PROCEDURAL, MemoryConfig
from oxison.memory.salience import clamp01, decay_days, recency, salience

CFG = MemoryConfig()


def test_clamp01():
    assert clamp01(-1.0) == 0.0
    assert clamp01(2.0) == 1.0
    assert clamp01(0.5) == 0.5


def test_decay_windows_per_tier():
    assert decay_days(TIER_PROCEDURAL, CFG) == CFG.decay_days_procedural
    assert decay_days(TIER_EPISODIC, CFG) == CFG.decay_days_episodic
    assert decay_days("unknown", CFG) == CFG.decay_days_semantic


def test_recency_fresh_is_one():
    assert recency("2026-06-15T00:00:00Z", "2026-06-15T00:00:00Z",
                   tier=TIER_PROCEDURAL, config=CFG) == 1.0


def test_recency_decays_linearly():
    # procedural window is 180d; 90 days old -> ~0.5
    r = recency("2026-03-17T00:00:00Z", "2026-06-15T00:00:00Z",
                tier=TIER_PROCEDURAL, config=CFG)
    assert 0.45 < r < 0.55


def test_recency_past_window_is_zero():
    r = recency("2025-01-01T00:00:00Z", "2026-06-15T00:00:00Z",
                tier=TIER_EPISODIC, config=CFG)  # 30d window, >1y old
    assert r == 0.0


def test_recency_unknown_timestamp_is_one():
    assert recency(None, "2026-06-15T00:00:00Z", tier=TIER_PROCEDURAL, config=CFG) == 1.0
    assert recency("garbage", "2026-06-15T00:00:00Z", tier=TIER_PROCEDURAL, config=CFG) == 1.0


def test_salience_is_product():
    s = salience(tier=TIER_PROCEDURAL, pain=0.5, importance=0.8,
                 last_used_at=None, created_at="2026-06-15T00:00:00Z",
                 now="2026-06-15T00:00:00Z", config=CFG)
    assert abs(s - (1.0 * 0.5 * 0.8)) < 1e-9


def test_salience_uses_last_used_over_created():
    # created long ago, but used today -> recency anchors on last_used (stays high)
    s = salience(tier=TIER_PROCEDURAL, pain=1.0, importance=1.0,
                 last_used_at="2026-06-15T00:00:00Z", created_at="2020-01-01T00:00:00Z",
                 now="2026-06-15T00:00:00Z", config=CFG)
    assert s == 1.0
