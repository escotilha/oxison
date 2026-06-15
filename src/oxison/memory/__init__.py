"""Cross-run memory for oxison — let the build engine learn across runs.

Portable by construction: one SQLite file (``oxison-build/memory.db``) via the
standard library, FTS5 when available with a pure-Python fallback when not, and
**optional** pluggable vector embeddings — so it runs on any Mac with no external
DB, no vector server, and no required third-party package.

The subsystem is three seams around one store:

* ``MemoryStore`` — the durable substrate (content-addressed, supersede-not-append).
* ``capture_from_outcome`` — the **write** path, gated by the grader (verify
  before store).
* ``retrieve`` / ``build_memory_block`` — the **read** path, repo-scoped and
  abstaining (inject nothing rather than a weak match).
"""

from __future__ import annotations

from .capture import capture_from_outcome, components_from_files
from .config import (
    TIER_EPISODIC,
    TIER_PROCEDURAL,
    TIER_SEMANTIC,
    MemoryConfig,
)
from .inject import build_memory_block, memory_query_for_task
from .retrieve import retrieve
from .salience import salience
from .store import (
    Embedder,
    MemoryRecord,
    MemoryStore,
    RetrievalHit,
    content_key,
)

__all__ = [
    "TIER_EPISODIC",
    "TIER_PROCEDURAL",
    "TIER_SEMANTIC",
    "Embedder",
    "MemoryConfig",
    "MemoryRecord",
    "MemoryStore",
    "RetrievalHit",
    "build_memory_block",
    "capture_from_outcome",
    "components_from_files",
    "content_key",
    "memory_query_for_task",
    "retrieve",
    "salience",
]
