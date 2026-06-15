"""Hybrid retrieval with repo-scoping and abstention.

This is the read path — and it is where most of the memory subsystem's *safety*
lives, because the dominant failure mode of agent memory is not bad storage, it
is **injecting a plausible-but-wrong memory**. Two structural guards:

1. **Repo-scope is a hard pre-filter, not a ranking signal.** Only records in
   the target ``scope`` (and optionally ``task_kind``) are ever ranked — a
   pattern from another repo can never surface, because cross-repo file paths
   and APIs don't exist in the current tree (the "knowledge becomes noise"
   failure). Scoping happens before any ranking, via ``live_in_scope``.

2. **Retrieval abstains.** Three signals (BM25 keyword, optional vector, graph
   expansion) are fused with Reciprocal Rank Fusion, reranked by salience
   (recency x pain x importance), and then thresholded: if nothing clears
   ``abstain_min_score`` the function returns ``[]`` and the worker gets *no*
   injected memory. Injecting nothing beats injecting a weak match — a wrong
   memory moves the build farther from done, which is worse than no memory.

RRF (``1/(k+rank)``, k=60) is used because it is normalization-free: it fuses
ranked lists whose raw scores (BM25 magnitudes vs cosine vs graph decay) are not
comparable, without any global score calibration.
"""

from __future__ import annotations

from .config import MemoryConfig
from .salience import salience
from .store import MemoryStore, RetrievalHit


def retrieve(
    store: MemoryStore,
    *,
    query: str,
    scope: str,
    now: str,
    config: MemoryConfig,
    task_kind: str | None = None,
) -> list[RetrievalHit]:
    """Return up to ``config.top_n`` scoped, salience-reranked hits — or ``[]``.

    ``[]`` is a first-class, common result: it means "abstain — inject nothing",
    which happens when the scope is empty, no stream matches, or no candidate
    clears the abstention threshold.
    """
    live = store.live_in_scope(scope, task_kind=task_kind)
    if not live:
        return []  # nothing in scope — abstain
    livekeys = set(live)

    # Stream 1 — keyword (BM25 via FTS5, or pure-Python overlap fallback).
    keyword = [k for k in store.keyword_rank(query, limit=config.candidate_pool) if k in livekeys]

    # Stream 2 — vector (only if an embedder is wired; otherwise skipped).
    qvec = store.embed_query(query)
    vector = (
        [k for k, _ in store.vector_rank(qvec, livekeys, limit=config.candidate_pool)]
        if qvec
        else []
    )

    # Stream 3 — graph expansion from the top keyword/vector seeds.
    seeds = list(dict.fromkeys(keyword[:5] + vector[:5]))
    graph_scores = store.neighbors(seeds, hops=config.graph_hops, decay=config.graph_hop_decay)
    graph = [
        k
        for k, _ in sorted(graph_scores.items(), key=lambda t: t[1], reverse=True)
        if k in livekeys
    ]

    streams = {"keyword": keyword, "vector": vector, "graph": graph}
    active = [ranked for ranked in streams.values() if ranked]
    if not active:
        return []  # no stream matched anything in scope — abstain

    # RRF fuse, tracking which streams supported each key.
    k = config.rrf_k
    rrf: dict[str, float] = {}
    supported: dict[str, set[str]] = {}
    for name, ranked in streams.items():
        for rank, key in enumerate(ranked, start=1):
            rrf[key] = rrf.get(key, 0.0) + 1.0 / (k + rank)
            supported.setdefault(key, set()).add(name)

    # Normalize to a 0..1 confidence (max = appearing rank-1 in every active
    # stream), then rerank by salience and apply the abstention threshold.
    max_possible = len(active) * (1.0 / (k + 1))
    hits: list[RetrievalHit] = []
    for key, raw in rrf.items():
        rec = live[key]
        conf = min(1.0, raw / max_possible) if max_possible > 0 else 0.0
        sal = salience(
            tier=rec.tier,
            pain=rec.pain,
            importance=rec.importance,
            last_used_at=rec.last_used_at,
            created_at=rec.created_at,
            now=now,
            config=config,
        )
        score = conf * sal
        if score >= config.abstain_min_score:
            hits.append(
                RetrievalHit(
                    key=key, record=rec, score=score, streams=tuple(sorted(supported[key]))
                )
            )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[: config.top_n]


__all__ = ["retrieve"]
