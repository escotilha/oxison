"""The cross-run memory spine — ``memory.db``: a portable, stdlib-only store.

Today an Oxfaz build worker is **stateless**: every run rediscovers the repo
from scratch and forgets every lesson the moment it exits. This module is the
durable substrate that lets oxison *learn across runs* — accumulating verified
build recipes (procedural), distilled repo heuristics (semantic), and abstracted
mistakes (episodic), so a future worker (or the planner) can consult what worked
before.

Design constraints that shaped every decision here:

* **Runs on any Mac with the standard library alone.** No external DB, no vector
  server, no network. Storage is one SQLite file via the stdlib ``sqlite3`` —
  the same engine ``state.db`` already uses. Full-text search uses **FTS5 when
  the host's SQLite has it, and degrades to a pure-Python keyword scan when it
  doesn't** (FTS5 is absent on some macOS Python builds). Vector recall is an
  **optional, pluggable** enhancement — an injected embedder callable; with no
  embedder the store runs on BM25 + graph + salience alone. Crucially we never
  call ``enable_load_extension`` (disabled on stock macOS Python), so there is
  no ``sqlite-vec``/extension dependency: embeddings are stored as plain BLOBs
  and cosine is computed in Python. This is what "portable, any Mac" requires.

* **``memory.db`` lives under the protected ``oxison-build/`` dir** (beside
  ``state.db``), so a build worker can never be planned to touch the engine's
  own memory — the same C1 protection that fences ``state.db``.

* **The store is the *substrate*; safety lives at the seams.** Writes are gated
  by the grader (see ``capture``) and reads abstain on weak matches (see
  ``retrieve``). This module just stores and ranks faithfully.

Identity is **content-addressed** (``sha1(tier|scope|normalized_purpose)``) so a
re-distilled lesson lands on the same row — the same deterministic-id discipline
the roadmap uses, which is what makes supersede-not-append possible without
duplicate rows.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from array import array
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from math import sqrt
from pathlib import Path
from typing import Any

from ..engine.taskstore import STATE_DIRNAME
from .config import (
    EDGE_RELATED,
    EDGE_SUPERSEDES,
    MEMORY_DB_FILENAME,
    SRC_OUTCOME,
    SRC_SUPERSEDE,
)

#: An embedder turns texts into vectors. Injected, never required — the store
#: works fully without one (BM25 + graph + salience). Kept deliberately abstract
#: so a caller can wire any local model (or none) without a hard dependency.
Embedder = Callable[[list[str]], list[list[float]]]

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    key            TEXT UNIQUE NOT NULL,
    tier           TEXT NOT NULL,
    scope          TEXT NOT NULL DEFAULT '',
    task_kind      TEXT NOT NULL DEFAULT '',
    purpose        TEXT NOT NULL,
    truth          TEXT NOT NULL,
    triggers       TEXT NOT NULL DEFAULT '[]',
    anchors        TEXT NOT NULL DEFAULT '[]',
    provenance     TEXT NOT NULL DEFAULT '{}',
    verified       INTEGER NOT NULL DEFAULT 0,
    pain           REAL NOT NULL DEFAULT 0.5,
    importance     REAL NOT NULL DEFAULT 0.5,
    use_count      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    last_used_at   TEXT,
    valid_until    TEXT,
    superseded_by  TEXT
);
CREATE TABLE IF NOT EXISTS memory_vec (
    memory_id  INTEGER PRIMARY KEY,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_timeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_key  TEXT NOT NULL,
    at          TEXT NOT NULL,
    source      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS memory_edge (
    src     TEXT NOT NULL,
    dst     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    weight  REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(scope, superseded_by);
CREATE INDEX IF NOT EXISTS idx_edge_src ON memory_edge(src);
CREATE INDEX IF NOT EXISTS idx_timeline_key ON memory_timeline(memory_key);
"""

_FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
    "key UNINDEXED, purpose, truth, triggers, anchors, tokenize='porter')"
)


@dataclass
class MemoryRecord:
    """A typed view of one ``memory`` row — one compiled-truth memory."""

    id: int
    key: str
    tier: str
    scope: str
    task_kind: str
    purpose: str
    truth: str
    triggers: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    pain: float = 0.5
    importance: float = 0.5
    use_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None
    valid_until: str | None = None
    superseded_by: str | None = None


@dataclass
class RetrievalHit:
    """One ranked retrieval result: the record plus how it scored and why."""

    key: str
    record: MemoryRecord
    score: float
    streams: tuple[str, ...] = ()


def normalize_purpose(purpose: str) -> str:
    """Lowercase + whitespace-collapse — the canonical form for content keys."""
    return " ".join(purpose.lower().split())


def content_key(tier: str, scope: str, purpose: str) -> str:
    """Stable content-addressed id: ``sha1(tier|scope|normalized_purpose)[:16]``.

    Re-distilling the same lesson yields the same key, so an update lands on the
    existing row (compiled-truth rewrite) rather than duplicating it.
    """
    raw = f"{tier}|{scope}|{normalize_purpose(purpose)}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def tokenize(text: str) -> list[str]:
    """Alnum tokens, lowercased — safe to splice into an FTS5 ``MATCH`` query."""
    return _TOKEN_RE.findall(text.lower())


def _loads_list(raw: Any) -> list[str]:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def _loads_obj(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(val) if isinstance(val, dict) else {}


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"],
        key=row["key"],
        tier=row["tier"],
        scope=row["scope"],
        task_kind=row["task_kind"],
        purpose=row["purpose"],
        truth=row["truth"],
        triggers=_loads_list(row["triggers"]),
        anchors=_loads_list(row["anchors"]),
        provenance=_loads_obj(row["provenance"]),
        verified=bool(row["verified"]),
        pain=float(row["pain"]),
        importance=float(row["importance"]),
        use_count=int(row["use_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
        valid_until=row["valid_until"],
        superseded_by=row["superseded_by"],
    )


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in pure Python — no numpy, runs anywhere."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sqrt(sum(x * x for x in a))
    nb = sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 0.0 and nb > 0.0 else 0.0


class MemoryStore:
    """A connection to one ``memory.db`` with put / supersede / search helpers."""

    def __init__(self, db_path: Path, *, embedder: Embedder | None = None):
        self.db_path = db_path
        self._embedder = embedder
        self._conn = self._connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # FTS5 is not present on every host SQLite build (notably some macOS
        # Python builds). Probe by creating the table; fall back to a pure-Python
        # keyword scan if it isn't available, so the store stays portable.
        self.fts = self._try_enable_fts()
        with contextlib.suppress(OSError):
            os.chmod(db_path, 0o644)  # generic, no secrets here

    # -- construction ----------------------------------------------------
    @classmethod
    def open(cls, repo_root: Path, *, embedder: Embedder | None = None) -> MemoryStore:
        """Open (creating if needed) ``<repo>/oxison-build/memory.db``."""
        state_dir = repo_root / STATE_DIRNAME
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(state_dir / MEMORY_DB_FILENAME, embedder=embedder)

    @staticmethod
    def _connect(db_path: Path, *, retries: int = 5) -> sqlite3.Connection:
        last_exc: sqlite3.Error | None = None
        for attempt in range(retries):
            try:
                conn = sqlite3.connect(os.fspath(db_path), timeout=30.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                return conn
            except sqlite3.OperationalError as exc:
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
        raise last_exc if last_exc is not None else sqlite3.OperationalError("connect failed")

    def _try_enable_fts(self) -> bool:
        try:
            self._conn.execute(_FTS_SCHEMA)
            self._conn.commit()
            return True
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        self._conn.close()

    # -- vector (de)serialization ----------------------------------------
    @staticmethod
    def _pack(vec: list[float]) -> bytes:
        return array("f", vec).tobytes()

    @staticmethod
    def _unpack(blob: bytes) -> list[float]:
        arr = array("f")
        arr.frombytes(blob)
        return list(arr)

    def _embed_and_store(self, memory_id: int, purpose: str, truth: str) -> None:
        if self._embedder is None:
            return
        try:
            vecs = self._embedder([f"{purpose}\n{truth}"])
        except Exception:  # noqa: BLE001 — an embedder failure must never block a write
            return
        if not vecs or not vecs[0]:
            return
        vec = [float(x) for x in vecs[0]]
        self._conn.execute(
            "INSERT OR REPLACE INTO memory_vec (memory_id, dim, vec) VALUES (?, ?, ?)",
            (memory_id, len(vec), self._pack(vec)),
        )
        self._conn.commit()

    # -- write -----------------------------------------------------------
    def put(
        self,
        *,
        tier: str,
        scope: str,
        purpose: str,
        truth: str,
        now: str,
        task_kind: str = "",
        triggers: list[str] | None = None,
        anchors: list[str] | None = None,
        provenance: dict[str, Any] | None = None,
        verified: bool = False,
        pain: float = 0.5,
        importance: float = 0.5,
        source: str = SRC_OUTCOME,
        note: str = "",
    ) -> str:
        """Insert-or-update a memory by content key. Returns the key.

        On a re-distill of the same ``(tier, scope, purpose)`` the row's compiled
        truth is **rewritten** (not appended) and a timeline entry is added — the
        GBrain compiled-truth + append-only-timeline pattern. ``verified`` is
        latched on (a verified memory never silently reverts to unverified).
        """
        key = content_key(tier, scope, purpose)
        trig = json.dumps(triggers or [])
        anch = json.dumps(anchors or [])
        prov = json.dumps(provenance or {})
        # Atomic upsert in a SINGLE statement: two workers distilling the same
        # content key concurrently can never race a SELECT-then-INSERT into a
        # UNIQUE violation (the engine dispatches workers in parallel). On
        # conflict the compiled truth is rewritten, ``created_at`` is preserved
        # (set only on insert), ``verified`` latches on, and a non-empty
        # ``task_kind`` wins.
        self._conn.execute(
            "INSERT INTO memory (key, tier, scope, task_kind, purpose, truth, "
            "triggers, anchors, provenance, verified, pain, importance, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "truth = excluded.truth, triggers = excluded.triggers, "
            "anchors = excluded.anchors, provenance = excluded.provenance, "
            "verified = MAX(memory.verified, excluded.verified), "
            "pain = excluded.pain, importance = excluded.importance, "
            "task_kind = CASE WHEN excluded.task_kind != '' THEN excluded.task_kind "
            "ELSE memory.task_kind END, updated_at = excluded.updated_at",
            (key, tier, scope, task_kind, purpose, truth, trig, anch, prov,
             int(verified), pain, importance, now, now),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT id FROM memory WHERE key = ?", (key,)).fetchone()
        memory_id = int(row["id"]) if row else 0
        self._sync_fts(key, purpose, truth, trig, anch)
        self._embed_and_store(memory_id, purpose, truth)
        self._append_timeline(key, now=now, source=source, note=note)
        return key

    def _sync_fts(self, key: str, purpose: str, truth: str, triggers: str, anchors: str) -> None:
        if not self.fts:
            return
        self._conn.execute("DELETE FROM memory_fts WHERE key = ?", (key,))
        self._conn.execute(
            "INSERT INTO memory_fts (key, purpose, truth, triggers, anchors) "
            "VALUES (?,?,?,?,?)",
            (key, purpose, truth, triggers, anchors),
        )
        self._conn.commit()

    def _append_timeline(self, key: str, *, now: str, source: str, note: str) -> None:
        self._conn.execute(
            "INSERT INTO memory_timeline (memory_key, at, source, note) VALUES (?,?,?,?)",
            (key, now, source, note),
        )
        self._conn.commit()

    def add_edge(
        self, src: str, dst: str, kind: str = EDGE_RELATED, *, weight: float = 1.0
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO memory_edge (src, dst, kind, weight) VALUES (?,?,?,?)",
            (src, dst, kind, weight),
        )
        self._conn.commit()

    def supersede(self, old_key: str, new_key: str, *, now: str, note: str = "") -> None:
        """Retire ``old_key`` in favor of ``new_key`` without deleting history.

        Sets ``superseded_by`` (so retrieval skips it) and ``valid_until`` (the
        temporal validity window), records a typed ``supersedes`` edge, and
        appends a timeline entry. The old row stays on disk as an audit trail —
        supersede-not-append.
        """
        self._conn.execute(
            "UPDATE memory SET superseded_by = ?, valid_until = ? WHERE key = ?",
            (new_key, now, old_key),
        )
        # Drop the retired row from the FTS index so it stops consuming keyword
        # candidate slots. Without this, superseded rows accumulate in the index
        # forever and — because the candidate pool is capped before the live-scope
        # filter — a supersede-heavy scope eventually starves its live records out
        # of the pool (false abstention). The pure-Python fallback already filters
        # ``superseded_by IS NULL``, so this keeps both backends in parity.
        if self.fts:
            self._conn.execute("DELETE FROM memory_fts WHERE key = ?", (old_key,))
        self._conn.commit()
        self.add_edge(new_key, old_key, EDGE_SUPERSEDES)
        self._append_timeline(old_key, now=now, source=SRC_SUPERSEDE, note=note or new_key)

    def touch(self, key: str, now: str) -> None:
        """Record a use: bump ``use_count`` and ``last_used_at`` (feeds recency)."""
        self._conn.execute(
            "UPDATE memory SET use_count = use_count + 1, last_used_at = ? WHERE key = ?",
            (now, key),
        )
        self._conn.commit()

    # -- read ------------------------------------------------------------
    def get(self, key: str) -> MemoryRecord | None:
        row = self._conn.execute("SELECT * FROM memory WHERE key = ?", (key,)).fetchone()
        return _row_to_record(row) if row else None

    def all_records(self) -> list[MemoryRecord]:
        rows = self._conn.execute("SELECT * FROM memory ORDER BY id").fetchall()
        return [_row_to_record(r) for r in rows]

    def live_in_scope(self, scope: str, *, task_kind: str | None = None) -> dict[str, MemoryRecord]:
        """Live (non-superseded) records for ``scope`` — the retrieval candidate
        set. Optionally narrowed to a ``task_kind``. This is the repo-scope fence:
        retrieval only ever ranks records returned here."""
        if task_kind:
            rows = self._conn.execute(
                "SELECT * FROM memory WHERE scope = ? AND task_kind = ? "
                "AND superseded_by IS NULL",
                (scope, task_kind),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM memory WHERE scope = ? AND superseded_by IS NULL",
                (scope,),
            ).fetchall()
        return {r["key"]: _row_to_record(r) for r in rows}

    def timeline(self, key: str) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT at, source, note FROM memory_timeline WHERE memory_key = ? ORDER BY id",
            (key,),
        ).fetchall()
        return [{"at": r["at"], "source": r["source"], "note": r["note"]} for r in rows]

    # -- ranked streams (scope filtering is the caller's job) -------------
    def keyword_rank(self, query: str, *, limit: int) -> list[str]:
        """Keys best-matching ``query`` by keyword, best first.

        Uses FTS5 ``bm25()`` when available, else a pure-Python term-overlap scan
        — so keyword recall works on any host SQLite.
        """
        terms = tokenize(query)
        if not terms:
            return []
        if self.fts:
            match = " OR ".join(f'"{t}"' for t in terms)
            rows = self._conn.execute(
                "SELECT key FROM memory_fts WHERE memory_fts MATCH ? "
                "ORDER BY bm25(memory_fts) LIMIT ?",
                (match, limit),
            ).fetchall()
            return [r["key"] for r in rows]
        return self._keyword_fallback(set(terms), limit=limit)

    def _keyword_fallback(self, terms: set[str], *, limit: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT key, purpose, truth, triggers, anchors FROM memory "
            "WHERE superseded_by IS NULL"
        ).fetchall()
        scored: list[tuple[int, str]] = []
        for r in rows:
            hay = set(tokenize(" ".join((r["purpose"], r["truth"], r["triggers"], r["anchors"]))))
            overlap = len(terms & hay)
            if overlap:
                scored.append((overlap, r["key"]))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [k for _, k in scored[:limit]]

    def vector_rank(
        self, query_vec: list[float], keys: Iterable[str], *, limit: int
    ) -> list[tuple[str, float]]:
        """Cosine-rank ``keys`` against ``query_vec`` (only keys with a stored
        vector participate). Brute-force in Python — fine at build-agent scale.

        Candidates are filtered in Python rather than via a dynamically-built SQL
        ``IN`` clause, so the SQL stays fully static — the same no-dynamic-SQL
        discipline ``taskstore.locks_claim`` uses, leaving zero injection surface.
        """
        keyset = set(keys)
        if not keyset or not query_vec:
            return []
        rows = self._conn.execute(
            "SELECT m.key AS key, v.vec AS vec FROM memory_vec v "
            "JOIN memory m ON m.id = v.memory_id"
        ).fetchall()
        scored = [
            (r["key"], cosine(query_vec, self._unpack(r["vec"])))
            for r in rows
            if r["key"] in keyset
        ]
        scored = [(k, s) for k, s in scored if s > 0.0]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:limit]

    def embed_query(self, query: str) -> list[float]:
        """Embed a query with the store's embedder (``[]`` if none/failed)."""
        if self._embedder is None:
            return []
        try:
            vecs = self._embedder([query])
        except Exception:  # noqa: BLE001 — vector recall is optional; never raise
            return []
        return [float(x) for x in vecs[0]] if vecs and vecs[0] else []

    def neighbors(
        self, seeds: Iterable[str], *, hops: int, decay: float
    ) -> dict[str, float]:
        """Graph-expand ``seeds`` over the edge table (undirected BFS).

        Returns ``{key: graph_score}`` for neighbors NOT in ``seeds``; score is
        ``decay ** hop_distance``. Lets a relevant-but-keyword-silent memory
        surface via an author-curated ``related``/``depends`` edge.
        """
        seedset = set(seeds)
        if not seedset or hops <= 0:
            return {}
        adj: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT src, dst FROM memory_edge").fetchall():
            adj.setdefault(r["src"], set()).add(r["dst"])
            adj.setdefault(r["dst"], set()).add(r["src"])
        out: dict[str, float] = {}
        frontier = deque((s, 0) for s in seedset)
        seen = set(seedset)
        while frontier:
            node, dist = frontier.popleft()
            if dist >= hops:
                continue
            for nb in adj.get(node, ()):
                if nb in seen:
                    continue
                seen.add(nb)
                out[nb] = decay ** (dist + 1)
                frontier.append((nb, dist + 1))
        return out

    # -- maintenance -----------------------------------------------------
    def prune(self, *, keys: Iterable[str]) -> int:
        """Hard-delete the given keys and their vectors/edges/timeline. The
        salience computation that selects eviction candidates lives in
        ``retrieve``/maintenance callers; this is the mechanical delete."""
        n = 0
        for key in keys:
            rec = self.get(key)
            if rec is None:
                continue
            self._conn.execute("DELETE FROM memory_vec WHERE memory_id = ?", (rec.id,))
            self._conn.execute("DELETE FROM memory WHERE key = ?", (key,))
            self._conn.execute("DELETE FROM memory_timeline WHERE memory_key = ?", (key,))
            self._conn.execute("DELETE FROM memory_edge WHERE src = ? OR dst = ?", (key, key))
            if self.fts:
                self._conn.execute("DELETE FROM memory_fts WHERE key = ?", (key,))
            n += 1
        self._conn.commit()
        return n


__all__ = [
    "Embedder",
    "MemoryRecord",
    "MemoryStore",
    "RetrievalHit",
    "content_key",
    "cosine",
    "normalize_purpose",
    "tokenize",
]
