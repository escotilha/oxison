"""The salience formula — ``recency x pain x importance``, per-tier decay.

A memory's weight at retrieval time is the product of three 0..1 signals:

* **recency** — decays linearly over a per-tier window (procedural skills are
  kept longest; episodic bug-notes decay fastest), so a stale memory naturally
  falls below the abstention threshold instead of misleading a worker;
* **pain** — how costly the thing it encodes was (a hard, repeatedly-failing
  task earns higher pain);
* **importance** — how broadly applicable it is.

This is the same multiplicative formula the operator-memory system uses; here it
also doubles as the prune signal (low salience -> eligible for eviction).
"""

from __future__ import annotations

from datetime import datetime

from .config import (
    TIER_EPISODIC,
    TIER_PROCEDURAL,
    MemoryConfig,
)


def parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse (tolerates a trailing ``Z``)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def clamp01(x: float) -> float:
    """Clamp to ``[0, 1]``."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def decay_days(tier: str, config: MemoryConfig) -> int:
    """The recency-decay window (days) for ``tier``."""
    if tier == TIER_PROCEDURAL:
        return config.decay_days_procedural
    if tier == TIER_EPISODIC:
        return config.decay_days_episodic
    return config.decay_days_semantic  # semantic + any unknown tier


def recency(anchor_iso: str | None, now_iso: str, *, tier: str, config: MemoryConfig) -> float:
    """Linear recency decay in ``[0, 1]`` from ``anchor`` to ``now``.

    Unknown/unparseable timestamps return ``1.0`` (do not penalize) — a brand
    new record with no usage history should not be pre-decayed to zero.
    """
    anchor = parse_iso(anchor_iso)
    now = parse_iso(now_iso)
    if anchor is None or now is None:
        return 1.0
    days = (now - anchor).total_seconds() / 86400.0
    if days <= 0:
        return 1.0
    window = decay_days(tier, config)
    if window <= 0:
        return 1.0
    return clamp01(1.0 - days / window)


def salience(
    *,
    tier: str,
    pain: float,
    importance: float,
    last_used_at: str | None,
    created_at: str | None,
    now: str,
    config: MemoryConfig,
) -> float:
    """``recency x pain x importance`` in ``[0, 1]``.

    Recency anchors on ``last_used_at`` when present (a recently-useful memory
    stays salient), else ``created_at``.
    """
    anchor = last_used_at or created_at
    return recency(anchor, now, tier=tier, config=config) * clamp01(pain) * clamp01(importance)


__all__ = ["clamp01", "decay_days", "parse_iso", "recency", "salience"]
