"""Tests for dispatch-time injection — front-loaded block, gates, abstention."""

from __future__ import annotations

from oxison.engine.taskstore import Task
from oxison.memory.config import TIER_PROCEDURAL, MemoryConfig
from oxison.memory.inject import build_memory_block, memory_query_for_task
from oxison.memory.store import MemoryStore

NOW = "2026-06-15T00:00:00Z"
CFG = MemoryConfig()


def _store_with_hit(tmp_path):
    s = MemoryStore.open(tmp_path)
    s.put(tier=TIER_PROCEDURAL, scope="r", purpose="add user login authentication",
          truth="change src/auth", anchors=["src/auth"], verified=True,
          pain=0.5, importance=0.8, now=NOW)
    return s


def test_trivial_task_injects_nothing(tmp_path):
    s = _store_with_hit(tmp_path)
    block = build_memory_block(
        s, query="login authentication", scope="r", now=NOW, config=CFG, trivial=True
    )
    assert block == ""  # memory is net-negative on trivial tasks


def test_abstain_injects_nothing(tmp_path):
    s = _store_with_hit(tmp_path)
    # Out of scope -> retrieve abstains -> empty block.
    assert build_memory_block(s, query="login", scope="other", now=NOW, config=CFG) == ""


def test_hit_builds_front_loaded_block_and_touches(tmp_path):
    s = _store_with_hit(tmp_path)
    block = build_memory_block(s, query="login authentication", scope="r", now=NOW, config=CFG)
    assert block.startswith("RELEVANT VERIFIED MEMORY")
    assert "add user login authentication" in block
    assert "src/auth" in block
    # surfacing a memory records a use (recency feeds salience next time)
    from oxison.memory.store import content_key

    rec = s.get(content_key(TIER_PROCEDURAL, "r", "add user login authentication"))
    assert rec.use_count == 1


def test_memory_query_for_task_uses_structural_fields():
    t = Task(id=1, identifier="oxpz-a", title="Add login", status="planned", priority=1,
             kind="feature", rationale="users need access", acceptance=["login works"])
    q = memory_query_for_task(t)
    assert "Add login" in q and "feature" in q and "login works" in q
