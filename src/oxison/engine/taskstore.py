"""The engine spine — ``state.db``: task + lock tables, crash-safe writes.

This is the single durable source of truth for an Oxfaz build run. Everything
the loop coordinates (select → dispatch → record) flows through here. The
schema is the **folded 2-table** design from the build-engine plan (F2/H4): the
worker ledger is collapsed into columns on the ``task`` row (no separate
``worker`` table), and locks are **hard-deleted** on release (``row present =
locked``, F9 — no ``released_at`` to corrupt).

The load-bearing invariants (named regression tests assert each):

* **I1/I2 — never re-dispatch in-flight work.** ``mark_dispatched`` is guarded
  ``WHERE status IN ('planned','planning')``; a second call (or a call on a row
  that is not awaiting dispatch) is a no-op. This is the structural fix for the
  "72×-storm" re-dispatch bug.
* **I4 — an engine outage doesn't burn a task's retries.** ``mark_adapter_failure``
  decrements ``dispatch_count`` (floored at 0) and returns the task to
  ``planned``; ``mark_failed`` burns one retry.
* **I5/L2/L3 — atomic, conflict-returning lock claims.** ``locks_claim`` is
  all-or-nothing under ``BEGIN IMMEDIATE`` and returns the conflicting paths
  (not a bool); re-claiming one's own paths is idempotent.
* **L4/F9 — the one surviving stale-lock sweep.** ``locks_release`` DELETEs
  rows; ``locks_expire`` deletes only TTL-expired locks whose holder is dead.

``state.db`` lives under ``oxison-build/`` (a dedicated namespace, never
``.oxi/``), and ``oxison-build/`` is a protected path so a build worker can
never be planned to touch the engine's own state.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Engine state namespace (distinct from ``.oxi/``).
STATE_DIRNAME = "oxison-build"
STATE_DB_FILENAME = "state.db"

#: Valid task states. The machine is: planned -> planning -> dispatched ->
#: merged | failed. ``mark_adapter_failure`` sends a dispatched task back to
#: planned without burning a retry.
STATUS_PLANNED = "planned"
STATUS_PLANNING = "planning"
STATUS_DISPATCHED = "dispatched"
STATUS_MERGED = "merged"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier      TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'planned',
    priority        INTEGER NOT NULL DEFAULT 3,
    kind            TEXT NOT NULL DEFAULT '',
    rationale       TEXT NOT NULL DEFAULT '',
    acceptance      TEXT NOT NULL DEFAULT '[]',
    depends_on      TEXT NOT NULL DEFAULT '[]',
    plan_json       TEXT NOT NULL DEFAULT '',
    plan_status     TEXT NOT NULL DEFAULT '',
    files_touched   TEXT NOT NULL DEFAULT '[]',
    branch          TEXT NOT NULL DEFAULT '',
    pr_number       INTEGER,
    dispatched_at   TEXT,
    merged_at       TEXT,
    failed_at       TEXT,
    failure_reason  TEXT NOT NULL DEFAULT '',
    failure_class   TEXT NOT NULL DEFAULT '',
    dispatch_count  INTEGER NOT NULL DEFAULT 0,
    pid             INTEGER,
    worktree_path   TEXT,
    last_heartbeat_at TEXT,
    heartbeat_path  TEXT
);
CREATE TABLE IF NOT EXISTS lock (
    path         TEXT PRIMARY KEY,
    task_id      INTEGER NOT NULL,
    acquired_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status);
-- Serves find_next_planned's `WHERE status=? AND dispatch_count<? ORDER BY
-- priority, id` — the status prefix filters, the priority/id suffix orders.
CREATE INDEX IF NOT EXISTS idx_task_status_priority_id ON task(status, priority, id);
"""


@dataclass
class Task:
    """A typed view of one ``task`` row."""

    id: int
    identifier: str
    title: str
    status: str
    priority: int
    kind: str = ""
    rationale: str = ""
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    plan_json: str = ""
    plan_status: str = ""
    files_touched: list[str] = field(default_factory=list)
    branch: str = ""
    pr_number: int | None = None
    dispatched_at: str | None = None
    merged_at: str | None = None
    failed_at: str | None = None
    failure_reason: str = ""
    failure_class: str = ""
    dispatch_count: int = 0
    pid: int | None = None
    worktree_path: str | None = None
    last_heartbeat_at: str | None = None
    heartbeat_path: str | None = None


def _loads_list(raw: Any) -> list[str]:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        identifier=row["identifier"],
        title=row["title"],
        status=row["status"],
        priority=row["priority"],
        kind=row["kind"],
        rationale=row["rationale"],
        acceptance=_loads_list(row["acceptance"]),
        depends_on=_loads_list(row["depends_on"]),
        plan_json=row["plan_json"],
        plan_status=row["plan_status"],
        files_touched=_loads_list(row["files_touched"]),
        branch=row["branch"],
        pr_number=row["pr_number"],
        dispatched_at=row["dispatched_at"],
        merged_at=row["merged_at"],
        failed_at=row["failed_at"],
        failure_reason=row["failure_reason"],
        failure_class=row["failure_class"],
        dispatch_count=row["dispatch_count"],
        pid=row["pid"],
        worktree_path=row["worktree_path"],
        last_heartbeat_at=row["last_heartbeat_at"],
        heartbeat_path=row["heartbeat_path"],
    )


class TaskStore:
    """A connection to one ``state.db`` with the lifecycle + lock helpers."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = self._connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # World-readable but not group/other-writable — generic, no secrets here.
        with contextlib.suppress(OSError):
            os.chmod(db_path, 0o644)

    # -- construction ----------------------------------------------------
    @classmethod
    def open(cls, repo_root: Path) -> TaskStore:
        """Open (creating if needed) ``<repo>/oxison-build/state.db``."""
        state_dir = repo_root / STATE_DIRNAME
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(state_dir / STATE_DB_FILENAME)

    @staticmethod
    def _connect(db_path: Path, *, retries: int = 5) -> sqlite3.Connection:
        """Connect with a small CANTOPEN/locked backoff (robustness port)."""
        last_exc: sqlite3.Error | None = None
        for attempt in range(retries):
            try:
                conn = sqlite3.connect(os.fspath(db_path), timeout=30.0)
                conn.row_factory = sqlite3.Row
                # Autocommit: we manage transactions explicitly. Without this,
                # pysqlite opens an implicit transaction before DML, which would
                # collide with the explicit BEGIN IMMEDIATE in locks_claim.
                conn.isolation_level = None
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                return conn
            except sqlite3.OperationalError as exc:  # CANTOPEN / locked
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
        raise last_exc if last_exc is not None else sqlite3.OperationalError("connect failed")

    def close(self) -> None:
        self._conn.close()

    # -- task lifecycle --------------------------------------------------
    def add_task(
        self,
        identifier: str,
        title: str,
        *,
        priority: int = 3,
        kind: str = "",
        rationale: str = "",
        acceptance: list[str] | None = None,
        depends_on: list[str] | None = None,
        files_touched: list[str] | None = None,
        plan_status: str = "",
    ) -> int | None:
        """Insert a new task. Returns the row id, or ``None`` if the identifier
        already exists (dedup by the UNIQUE constraint — re-ingest is a no-op).
        """
        try:
            cur = self._conn.execute(
                "INSERT INTO task (identifier, title, status, priority, kind, "
                "rationale, acceptance, depends_on, files_touched, plan_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier, title, STATUS_PLANNED, priority, kind, rationale,
                    json.dumps(acceptance or []), json.dumps(depends_on or []),
                    json.dumps(files_touched or []), plan_status,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid) if cur.lastrowid is not None else None
        except sqlite3.IntegrityError:
            return None  # duplicate identifier — dedup

    def get_task(self, identifier: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM task WHERE identifier = ?", (identifier,)
        ).fetchone()
        return _row_to_task(row) if row else None

    def get(self, task_id: int) -> Task | None:
        row = self._conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def all_tasks(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM task ORDER BY priority, id").fetchall()
        return [_row_to_task(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM task GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def find_next_planned(self, limit: int = 1, *, redispatch_cap: int = 3) -> list[Task]:
        """Planned tasks under the redispatch cap, highest priority first."""
        rows = self._conn.execute(
            "SELECT * FROM task WHERE status = ? AND dispatch_count < ? "
            "ORDER BY priority, id LIMIT ?",
            (STATUS_PLANNED, redispatch_cap, limit),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def inflight_tasks(self) -> list[Task]:
        # status='dispatched' already implies merged_at IS NULL (a merge moves the
        # row to 'merged'), so the extra predicate was dead — dropped.
        rows = self._conn.execute(
            "SELECT * FROM task WHERE status = ?",
            (STATUS_DISPATCHED,),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def merged_identifiers(self) -> set[str]:
        """Identifiers of tasks that have merged — for dependency gating."""
        rows = self._conn.execute(
            "SELECT identifier FROM task WHERE status = ?", (STATUS_MERGED,)
        ).fetchall()
        return {r["identifier"] for r in rows}

    def mark_planning(self, identifier: str) -> None:
        self._conn.execute(
            "UPDATE task SET status = ? WHERE identifier = ? AND status = ?",
            (STATUS_PLANNING, identifier, STATUS_PLANNED),
        )
        self._conn.commit()

    def reset_planning(self) -> int:
        """Return every ``planning`` task to ``planned`` — startup reconciliation.

        A task left in ``planning`` is the residue of a crash mid-transition; it
        is not ``dispatched`` (so ``inflight_tasks`` won't catch it) yet counts as
        not-complete, so without this it would wedge the loop forever. Resetting
        to ``planned`` (not ``failed``) burns no retry — no dispatch happened.
        Returns the number of tasks reset.
        """
        cur = self._conn.execute(
            "UPDATE task SET status = ? WHERE status = ?",
            (STATUS_PLANNED, STATUS_PLANNING),
        )
        self._conn.commit()
        return cur.rowcount

    def record_plan_verdict(
        self, identifier: str, *, plan_status: str, plan_json: str = "",
        files_touched: list[str] | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE task SET plan_status = ?, plan_json = ?, files_touched = ? "
            "WHERE identifier = ?",
            (plan_status, plan_json, json.dumps(files_touched or []), identifier),
        )
        self._conn.commit()

    def mark_dispatched(
        self,
        identifier: str,
        branch: str,
        *,
        now: str,
        pid: int | None = None,
        worktree_path: str | None = None,
        heartbeat_path: str | None = None,
    ) -> bool:
        """Transition planned|planning -> dispatched, exactly once (I1/I2).

        Guarded so a second call — or a call on a row that is not awaiting
        dispatch — is a no-op. Returns True iff this call performed the
        transition. The status is durably written here, BEFORE the loop spawns
        the worker, so a crash mid-dispatch never loses the in-flight marker.
        """
        cur = self._conn.execute(
            "UPDATE task SET status = ?, branch = ?, dispatched_at = ?, "
            "dispatch_count = dispatch_count + 1, pid = ?, worktree_path = ?, "
            "heartbeat_path = ?, last_heartbeat_at = ? "
            "WHERE identifier = ? AND status IN (?, ?)",
            (
                STATUS_DISPATCHED, branch, now, pid, worktree_path, heartbeat_path,
                now, identifier, STATUS_PLANNED, STATUS_PLANNING,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def heartbeat(self, identifier: str, now: str) -> None:
        self._conn.execute(
            "UPDATE task SET last_heartbeat_at = ? WHERE identifier = ?", (now, identifier)
        )
        self._conn.commit()

    def mark_merged(self, identifier: str, *, now: str, pr_number: int | None = None) -> None:
        self._conn.execute(
            "UPDATE task SET status = ?, merged_at = ?, pr_number = ? WHERE identifier = ?",
            (STATUS_MERGED, now, pr_number, identifier),
        )
        self._conn.commit()

    def mark_failed(
        self, identifier: str, *, now: str, reason: str = "", failure_class: str = ""
    ) -> None:
        """Burn a retry: status -> failed (dispatch_count already counted)."""
        self._conn.execute(
            "UPDATE task SET status = ?, failed_at = ?, failure_reason = ?, "
            "failure_class = ? WHERE identifier = ?",
            (STATUS_FAILED, now, reason, failure_class, identifier),
        )
        self._conn.commit()

    def mark_adapter_failure(self, identifier: str, *, reason: str = "") -> None:
        """Engine-side outage: do NOT burn a retry (I4).

        Decrement ``dispatch_count`` (floored at 0) and return the task to
        ``planned`` so a transient engine failure is retried for free. Also
        clears all liveness residue (pid / worktree / heartbeat / dispatched_at)
        so a requeued task carries no stale in-flight columns that a later
        liveness check could misread.
        """
        self._conn.execute(
            "UPDATE task SET status = ?, dispatch_count = MAX(0, dispatch_count - 1), "
            "failure_reason = ?, pid = NULL, worktree_path = NULL, "
            "dispatched_at = NULL, heartbeat_path = NULL, last_heartbeat_at = NULL "
            "WHERE identifier = ?",
            (STATUS_PLANNED, reason, identifier),
        )
        self._conn.commit()

    # -- locks (the spine owns them, F8) ---------------------------------
    def locks_claim(self, task_id: int, paths: list[str], *, now_epoch: float) -> list[str]:
        """Atomically claim ``paths`` for ``task_id`` (I5/L2/L3).

        All-or-nothing under ``BEGIN IMMEDIATE``. Returns the list of conflict
        paths (held by *another* task) — empty list means the claim succeeded.
        Re-claiming one's own already-held paths is idempotent. ``acquired_at``
        is stored as an epoch string so ``locks_expire`` is pure arithmetic.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Full-scan + Python filter (the lock table is small) — avoids any
            # dynamically-built SQL, so there is no injection surface at all.
            held = {r["path"]: r["task_id"] for r in
                    self._conn.execute("SELECT path, task_id FROM lock").fetchall()}
            conflicts = [p for p in paths if p in held and held[p] != task_id]
            if conflicts:
                self._conn.execute("ROLLBACK")
                return conflicts
            for path in paths:
                self._conn.execute(
                    "INSERT OR IGNORE INTO lock (path, task_id, acquired_at) "
                    "VALUES (?, ?, ?)",
                    (path, task_id, str(now_epoch)),
                )
            self._conn.execute("COMMIT")
            return []
        except sqlite3.Error:
            # Suppress a secondary ROLLBACK error (e.g. if the failing statement
            # was the COMMIT itself, the transaction is already closed) so the
            # original, meaningful exception is the one that propagates.
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise

    def locks_release(self, task_id: int) -> int:
        """Hard-delete all locks held by ``task_id`` (F9). Returns rows deleted."""
        cur = self._conn.execute("DELETE FROM lock WHERE task_id = ?", (task_id,))
        self._conn.commit()
        return cur.rowcount

    def held_locks(self) -> list[tuple[str, int]]:
        rows = self._conn.execute("SELECT path, task_id FROM lock ORDER BY path").fetchall()
        return [(r["path"], r["task_id"]) for r in rows]

    def locks_expire(self, *, now_epoch: float, ttl_seconds: int, live_task_ids: set[int]) -> int:
        """Delete TTL-expired locks whose holder is dead (L4). Returns deleted.

        A lock survives only while its holder is live OR it is younger than the
        TTL — the one stale-lock sweep that the hard-delete schema still needs.
        """
        rows = self._conn.execute("SELECT path, task_id, acquired_at FROM lock").fetchall()
        to_delete: list[str] = []
        for r in rows:
            age = now_epoch - _epoch(r["acquired_at"])
            if age > ttl_seconds and r["task_id"] not in live_task_ids:
                to_delete.append(r["path"])
        if to_delete:
            # Batch the deletes into a single executemany call (static SQL — no
            # dynamic IN-clause, so no bandit B608) rather than N execute()s (L2).
            self._conn.executemany(
                "DELETE FROM lock WHERE path = ?", [(p,) for p in to_delete]
            )
            self._conn.commit()
        return len(to_delete)


# Locks store the acquire time as an epoch string (set by ``locks_claim``) so
# ``locks_expire`` can do pure arithmetic without parsing ISO timestamps.
def _epoch(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "STATE_DB_FILENAME",
    "STATE_DIRNAME",
    "STATUS_DISPATCHED",
    "STATUS_FAILED",
    "STATUS_MERGED",
    "STATUS_PLANNED",
    "STATUS_PLANNING",
    "Task",
    "TaskStore",
]
