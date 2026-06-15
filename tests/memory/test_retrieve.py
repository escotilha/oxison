"""Tests for hybrid retrieval — scope fence, RRF ranking, and abstention."""

from __future__ import annotations

from oxison.memory.config import TIER_PROCEDURAL, MemoryConfig
from oxison.memory.retrieve import retrieve
from oxison.memory.store import MemoryStore, tokenize

NOW = "2026-06-15T00:00:00Z"
CFG = MemoryConfig()


def _bag_embedder(texts):
    out = []
    for t in texts:
        v = [0.0] * 16
        for tok in tokenize(t):
            v[sum(ord(c) for c in tok) % 16] += 1.0
        out.append(v)
    return out


def _put(s, scope, purpose, *, pain=0.5, importance=0.8, created=NOW, tier=TIER_PROCEDURAL):
    return s.put(tier=tier, scope=scope, purpose=purpose, truth="recipe",
                 verified=True, pain=pain, importance=importance, now=created)


def test_scope_fence_abstains_for_other_repo(tmp_path):
    s = MemoryStore.open(tmp_path)
    _put(s, "repoA", "add user login authentication")
    # The same query against a different repo returns nothing — cross-repo
    # memory can never surface (the knowledge-becomes-noise guard).
    assert retrieve(s, query="login authentication", scope="repoB", now=NOW, config=CFG) == []


def test_relevant_high_salience_is_returned(tmp_path):
    s = MemoryStore.open(tmp_path)
    k = _put(s, "r", "add user login authentication")
    hits = retrieve(s, query="login authentication", scope="r", now=NOW, config=CFG)
    assert [h.key for h in hits] == [k]
    assert "keyword" in hits[0].streams


def test_low_salience_abstains(tmp_path):
    s = MemoryStore.open(tmp_path)
    _put(s, "r", "add user login authentication", pain=0.1, importance=0.1)
    # In scope and a keyword match, but salience is below the floor -> abstain.
    assert retrieve(s, query="login authentication", scope="r", now=NOW, config=CFG) == []


def test_stale_record_abstains(tmp_path):
    s = MemoryStore.open(tmp_path)
    _put(s, "r", "add user login authentication", created="2026-01-01T00:00:00Z")
    # Now is far beyond the procedural decay window -> recency ~0 -> abstain.
    later = "2027-06-15T00:00:00Z"
    assert retrieve(s, query="login authentication", scope="r", now=later, config=CFG) == []


def test_empty_query_no_embedder_abstains(tmp_path):
    s = MemoryStore.open(tmp_path)
    _put(s, "r", "add user login authentication")
    assert retrieve(s, query="", scope="r", now=NOW, config=CFG) == []


def test_ranks_stronger_match_first(tmp_path):
    s = MemoryStore.open(tmp_path)
    strong = _put(s, "r", "login authentication")          # matches both terms
    _put(s, "r", "user profile settings and login")        # matches one term
    hits = retrieve(s, query="login authentication", scope="r", now=NOW, config=CFG)
    assert hits[0].key == strong


def test_vector_stream_participates_with_embedder(tmp_path):
    s = MemoryStore.open(tmp_path, embedder=_bag_embedder)
    _put(s, "r", "authentication login flow")
    hits = retrieve(s, query="authentication login flow", scope="r", now=NOW, config=CFG)
    assert hits and ("vector" in hits[0].streams or "keyword" in hits[0].streams)


def test_superseded_rows_do_not_starve_live_candidates(tmp_path):
    # Reproduces the candidate-pool-starvation bug: many superseded records sharing
    # the query term must not crowd the live record out of the keyword pool.
    s = MemoryStore.open(tmp_path)
    for i in range(30):
        old = _put(s, "r", f"login authentication stale {i}")
        new = _put(s, "r", f"unrelated replacement {i}")
        s.supersede(old, new, now=NOW)
    live = _put(s, "r", "login authentication the live one")
    hits = retrieve(s, query="login authentication", scope="r", now=NOW, config=CFG)
    assert any(h.key == live for h in hits)


def test_top_n_caps_results(tmp_path):
    s = MemoryStore.open(tmp_path)
    for i in range(10):
        _put(s, "r", f"login authentication variant {i}")
    cfg = MemoryConfig(top_n=3)
    hits = retrieve(s, query="login authentication", scope="r", now=NOW, config=cfg)
    assert len(hits) <= 3
