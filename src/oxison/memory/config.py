"""Memory subsystem constants — the externalized knob surface.

Mirrors the engine's ``engconfig`` philosophy: no memory module hardcodes a
constant; every threshold / weight / window lives here with a generic, safe
default. The defaults encode what the research established for the build-engine
context:

* **Procedural skills are the highest-value tier** (kept longest) — a builder
  learns reusable, verified recipes, not conversation history.
* **Retrieval abstains by default** — injecting a plausible-but-wrong memory is
  measurably worse than injecting nothing (static unscoped retrieval was unsafe
  in ~75% of hard-negative cases), so a weak match yields silence.
* **Runs on any Mac with the standard library alone** — no external DB, no
  vector server; embeddings are optional and pluggable (see ``store``).
"""

from __future__ import annotations

from dataclasses import dataclass

#: ``memory.db`` lives beside ``state.db`` under the (protected) ``oxison-build/``
#: dir — so a build worker can never be planned to touch the engine's own memory
#: either (the same C1 property that protects ``state.db``).
MEMORY_DB_FILENAME = "memory.db"

# -- cognitive tiers (CoALA taxonomy) ------------------------------------
#: Verified, reusable recipes — the highest-value tier for a *builder*.
TIER_PROCEDURAL = "procedural"
#: Distilled repo heuristics / architecture facts.
TIER_SEMANTIC = "semantic"
#: Abstracted bug -> root-cause notes — used sparingly, decays fastest.
TIER_EPISODIC = "episodic"

VALID_TIERS = (TIER_PROCEDURAL, TIER_SEMANTIC, TIER_EPISODIC)

# -- typed graph edges (also drives supersession) ------------------------
EDGE_RELATED = "related"
EDGE_SUPERSEDES = "supersedes"
EDGE_CONTRADICTS = "contradicts"
EDGE_DEPENDS = "depends"

# -- typed timeline sources (append-only evidence trail) -----------------
SRC_OUTCOME = "outcome"
SRC_SUPERSEDE = "supersede"
SRC_CORRECTION = "correction"
SRC_USE = "use"


@dataclass(frozen=True)
class MemoryConfig:
    """Frozen knob surface for the memory subsystem. Construct with overrides
    per run; never mutate (frozen)."""

    # --- retrieval fusion ---
    rrf_k: int = 60
    """Reciprocal-rank-fusion constant (Cormack et al. 2009 industry default)."""
    top_n: int = 5
    """Max memories injected into a single worker prompt."""
    candidate_pool: int = 24
    """Per-stream candidate cap before fusion."""
    graph_hops: int = 2
    """BFS depth for the graph-expansion stream."""
    graph_hop_decay: float = 0.6
    """Score multiplier per graph hop away from a seed."""

    # --- abstention (the core safety lever) ---
    abstain_min_score: float = 0.25
    """Final (confidence x salience) score below which retrieval returns NOTHING.

    Research: static unscoped retrieval injected unsafe memory in ~75% of
    hard-negative cases; abstaining drives false positives toward zero. It is
    strictly better to inject nothing than a plausible-but-wrong memory, because
    a wrong memory can move the build *farther* from done (negative, not zero).
    """

    # --- salience: recency x pain x importance, per-tier decay (days) ---
    decay_days_procedural: int = 180
    decay_days_semantic: int = 90
    decay_days_episodic: int = 30

    # --- bank hygiene (CODESKILL stable-bank-size) ---
    max_bank_per_scope: int = 500
    """Soft cap per scope; prune the lowest-salience expired/superseded beyond
    this. An unbounded skill bank degrades retrieval quality."""
    prune_min_salience: float = 0.15
    """Records whose salience falls below this are eligible for pruning."""

    # --- injection ---
    inject_skip_trivial: bool = True
    """Inject memory only for non-trivial tasks. Memory is *net-negative* on
    simple tasks (no-memory 70.3% vs memory 59.5% in a controlled benchmark)."""


__all__ = [
    "EDGE_CONTRADICTS",
    "EDGE_DEPENDS",
    "EDGE_RELATED",
    "EDGE_SUPERSEDES",
    "MEMORY_DB_FILENAME",
    "SRC_CORRECTION",
    "SRC_OUTCOME",
    "SRC_SUPERSEDE",
    "SRC_USE",
    "TIER_EPISODIC",
    "TIER_PROCEDURAL",
    "TIER_SEMANTIC",
    "VALID_TIERS",
    "MemoryConfig",
]
