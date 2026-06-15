"""Tests for the memory spine — memory.db, content keys, supersede, streams."""

from __future__ import annotations

from oxison.engine.taskstore import STATE_DIRNAME
from oxison.memory.config import MEMORY_DB_FILENAME, TIER_PROCEDURAL
from oxison.memory.store import (
    MemoryStore,
    content_key,
    cosine,
    normalize_purpose,
    tokenize,
)

NOW = "2026-06-15T00:00:00Z"


def _bag_embedder(texts):
    """Deterministic 16-dim bag-of-tokens embedder (no hashseed dependency)."""
    out = []
    for t in texts:
        v = [0.0] * 16
        for tok in tokenize(t):
            v[sum(ord(c) for c in tok) % 16] += 1.0
        out.append(v)
    return out


def _store(tmp_path, *, embedder=None):
    return MemoryStore.open(tmp_path, embedder=embedder)


def test_memory_db_under_oxison_build(tmp_path):
    _store(tmp_path)
    assert (tmp_path / STATE_DIRNAME / MEMORY_DB_FILENAME).is_file()


def test_content_key_is_stable_and_scope_sensitive():
    a = content_key(TIER_PROCEDURAL, "repoA", "Add  Login")
    b = content_key(TIER_PROCEDURAL, "repoA", "add login")  # normalized-equal
    c = content_key(TIER_PROCEDURAL, "repoB", "add login")  # different scope
    assert a == b
    assert a != c
    assert normalize_purpose("Add  Login") == "add login"


def test_put_returns_key_and_roundtrips(tmp_path):
    s = _store(tmp_path)
    key = s.put(
        tier=TIER_PROCEDURAL, scope="repoA", purpose="Add login", truth="touch src/auth",
        task_kind="feature", anchors=["src/auth"], triggers=["feature"],
        provenance={"task_id": "oxpz-a"}, verified=True, now=NOW,
    )
    rec = s.get(key)
    assert rec is not None
    assert rec.purpose == "Add login" and rec.tier == TIER_PROCEDURAL
    assert rec.anchors == ["src/auth"] and rec.verified is True
    assert rec.provenance == {"task_id": "oxpz-a"}


def test_put_is_compiled_truth_rewrite_not_duplicate(tmp_path):
    s = _store(tmp_path)
    k1 = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="Add login", truth="v1", now=NOW)
    k2 = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="add LOGIN", truth="v2 better", now=NOW)
    assert k1 == k2  # same content key -> same row
    assert len(s.all_records()) == 1
    assert s.get(k1).truth == "v2 better"  # truth rewritten, not appended
    # the timeline IS append-only (two outcome entries)
    assert len(s.timeline(k1)) == 2


def test_verified_latches_on(tmp_path):
    s = _store(tmp_path)
    k = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="p", truth="t", verified=True, now=NOW)
    s.put(tier=TIER_PROCEDURAL, scope="r", purpose="p", truth="t2", verified=False, now=NOW)
    assert s.get(k).verified is True  # never silently reverts to unverified


def test_supersede_hides_old_and_records_edge_and_timeline(tmp_path):
    s = _store(tmp_path)
    old = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="old way", truth="t", now=NOW)
    new = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="new way", truth="t", now=NOW)
    s.supersede(old, new, now=NOW, note="replaced")
    live = s.live_in_scope("r")
    assert new in live and old not in live  # old hidden from retrieval
    assert s.get(old) is not None  # but kept on disk (audit trail)
    assert any(e["source"] == "supersede" for e in s.timeline(old))


def test_live_in_scope_filters_scope_and_kind(tmp_path):
    s = _store(tmp_path)
    s.put(tier=TIER_PROCEDURAL, scope="A", purpose="a", truth="t", task_kind="fix", now=NOW)
    s.put(tier=TIER_PROCEDURAL, scope="B", purpose="b", truth="t", task_kind="fix", now=NOW)
    assert set(s.live_in_scope("A")) == {content_key(TIER_PROCEDURAL, "A", "a")}
    assert s.live_in_scope("A", task_kind="feature") == {}


def test_keyword_rank_finds_by_term(tmp_path):
    s = _store(tmp_path)
    s.put(tier=TIER_PROCEDURAL, scope="r", purpose="add user authentication login", truth="t",
          now=NOW)
    s.put(tier=TIER_PROCEDURAL, scope="r", purpose="render the dashboard chart", truth="t", now=NOW)
    keys = s.keyword_rank("authentication", limit=5)
    assert content_key(TIER_PROCEDURAL, "r", "add user authentication login") in keys


def test_keyword_fallback_when_no_fts(tmp_path):
    s = _store(tmp_path)
    s.fts = False  # force the pure-Python path (portability fallback)
    s.put(tier=TIER_PROCEDURAL, scope="r", purpose="add user authentication", truth="t", now=NOW)
    keys = s.keyword_rank("authentication user", limit=5)
    assert keys == [content_key(TIER_PROCEDURAL, "r", "add user authentication")]
    assert s.keyword_rank("", limit=5) == []


def test_supersede_excludes_old_from_keyword_rank(tmp_path):
    # A retired row must not keep consuming keyword candidate slots (the FTS-leak
    # / candidate-pool-starvation bug). Holds whether FTS is on or off.
    s = _store(tmp_path)
    old = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="login authentication old", truth="t",
                now=NOW)
    new = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="login authentication new", truth="t",
                now=NOW)
    s.supersede(old, new, now=NOW)
    keys = s.keyword_rank("authentication", limit=10)
    assert old not in keys and new in keys


def test_concurrent_put_same_key_does_not_raise(tmp_path):
    # Two stores (~ two parallel workers) on the same db distill the same lesson:
    # the atomic upsert must not raise UNIQUE-constraint, and must converge to one
    # row with last-writer truth.
    s1 = MemoryStore.open(tmp_path)
    s2 = MemoryStore.open(tmp_path)
    k1 = s1.put(tier=TIER_PROCEDURAL, scope="r", purpose="same lesson", truth="v1", now=NOW)
    k2 = s2.put(tier=TIER_PROCEDURAL, scope="r", purpose="same lesson", truth="v2", now=NOW)
    assert k1 == k2
    s3 = MemoryStore.open(tmp_path)
    assert len(s3.all_records()) == 1
    assert s3.get(k1).truth == "v2"


def test_vector_rank_with_embedder(tmp_path):
    s = _store(tmp_path, embedder=_bag_embedder)
    k_auth = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="authentication login flow", truth="t",
                   now=NOW)
    s.put(tier=TIER_PROCEDURAL, scope="r", purpose="chart rendering", truth="t", now=NOW)
    qvec = s.embed_query("authentication login flow")
    ranked = s.vector_rank(qvec, list(s.live_in_scope("r")), limit=5)
    assert ranked and ranked[0][0] == k_auth  # nearest by cosine


def test_no_embedder_means_no_vectors(tmp_path):
    s = _store(tmp_path)  # no embedder
    s.put(tier=TIER_PROCEDURAL, scope="r", purpose="p", truth="t", now=NOW)
    assert s.embed_query("p") == []
    assert s.vector_rank([1.0], list(s.live_in_scope("r")), limit=5) == []


def test_neighbors_graph_expansion(tmp_path):
    s = _store(tmp_path)
    a = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="a", truth="t", now=NOW)
    b = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="b", truth="t", now=NOW)
    s.add_edge(a, b)
    nb = s.neighbors([a], hops=2, decay=0.6)
    assert b in nb and abs(nb[b] - 0.6) < 1e-9
    assert s.neighbors([a], hops=0, decay=0.6) == {}


def test_touch_bumps_use_and_recency(tmp_path):
    s = _store(tmp_path)
    k = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="p", truth="t", now=NOW)
    s.touch(k, "2026-06-16T00:00:00Z")
    rec = s.get(k)
    assert rec.use_count == 1 and rec.last_used_at == "2026-06-16T00:00:00Z"


def test_prune_deletes_records_and_satellites(tmp_path):
    s = _store(tmp_path, embedder=_bag_embedder)
    k = s.put(tier=TIER_PROCEDURAL, scope="r", purpose="p", truth="t", now=NOW)
    assert s.prune(keys=[k]) == 1
    assert s.get(k) is None
    assert s.all_records() == []


def test_cosine_basic():
    assert abs(cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
    assert cosine([1.0], [1.0, 2.0]) == 0.0  # mismatched dims -> 0
