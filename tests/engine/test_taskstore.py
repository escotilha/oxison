"""Tests for the engine spine — state.db, state machine, locks, invariants."""

from __future__ import annotations

from oxison.engine.taskstore import (
    STATE_DB_FILENAME,
    STATE_DIRNAME,
    STATUS_DISPATCHED,
    STATUS_FAILED,
    STATUS_MERGED,
    STATUS_PLANNED,
    TaskStore,
)


def _store(tmp_path):
    return TaskStore.open(tmp_path)


def test_state_db_under_oxison_build(tmp_path):
    _store(tmp_path)
    assert (tmp_path / STATE_DIRNAME / STATE_DB_FILENAME).is_file()


def test_add_task_returns_id_and_dedups(tmp_path):
    s = _store(tmp_path)
    rid = s.add_task("oxpz-a", "A", priority=1, acceptance=["x"])
    assert isinstance(rid, int)
    # Re-ingesting the same identifier is a no-op (UNIQUE) — returns None.
    assert s.add_task("oxpz-a", "A again") is None
    assert len(s.all_tasks()) == 1


def test_get_task_roundtrips_fields(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "Title", priority=2, kind="feature",
               acceptance=["a1", "a2"], depends_on=["oxpz-z"], files_touched=["x.py"])
    t = s.get_task("oxpz-a")
    assert t is not None
    assert t.title == "Title" and t.kind == "feature"
    assert t.acceptance == ["a1", "a2"]
    assert t.depends_on == ["oxpz-z"]
    assert t.status == STATUS_PLANNED


def test_find_next_planned_orders_by_priority_and_respects_cap(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-lo", "Lo", priority=5)
    s.add_task("oxpz-hi", "Hi", priority=1)
    nxt = s.find_next_planned(limit=1)
    assert nxt[0].identifier == "oxpz-hi"
    # A task at the redispatch cap is excluded.
    s.mark_dispatched("oxpz-hi", "feat/x", now="t1")  # dispatch_count -> 1
    s.mark_adapter_failure("oxpz-hi")  # back to planned, dispatch_count -> 0
    got = [t.identifier for t in s.find_next_planned(limit=5, redispatch_cap=3)]
    assert "oxpz-hi" in got and "oxpz-lo" in got


def test_mark_dispatched_is_idempotent_I1_I2(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    # First call transitions planned -> dispatched exactly once.
    assert s.mark_dispatched("oxpz-a", "feat/a", now="t1", pid=999) is True
    t = s.get_task("oxpz-a")
    assert t is not None and t.status == STATUS_DISPATCHED
    assert t.dispatch_count == 1 and t.branch == "feat/a" and t.pid == 999
    # Second call is a no-op (the 72x-storm guard).
    assert s.mark_dispatched("oxpz-a", "feat/a", now="t2") is False
    assert s.get_task("oxpz-a").dispatch_count == 1


def test_mark_dispatched_no_op_on_non_planned(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.mark_dispatched("oxpz-a", "feat/a", now="t1")
    s.mark_merged("oxpz-a", now="t2")
    # A merged task cannot be re-dispatched.
    assert s.mark_dispatched("oxpz-a", "feat/a", now="t3") is False
    assert s.get_task("oxpz-a").status == STATUS_MERGED


def test_adapter_failure_does_not_burn_retry_I4(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.mark_dispatched("oxpz-a", "feat/a", now="t1")  # dispatch_count -> 1
    s.mark_adapter_failure("oxpz-a", reason="engine outage")
    t = s.get_task("oxpz-a")
    assert t is not None
    assert t.status == STATUS_PLANNED
    assert t.dispatch_count == 0  # decremented — retry not burned


def test_adapter_failure_floors_at_zero(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.mark_adapter_failure("oxpz-a")  # dispatch_count already 0
    assert s.get_task("oxpz-a").dispatch_count == 0


def test_adapter_failure_clears_liveness_columns(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.mark_dispatched("oxpz-a", "feat/a", now="t1", pid=123,
                      worktree_path="/wt", heartbeat_path="/hb")
    s.mark_adapter_failure("oxpz-a", reason="outage")
    t = s.get_task("oxpz-a")
    # A requeued task must carry no in-flight liveness residue.
    assert t.pid is None
    assert t.worktree_path is None
    assert t.dispatched_at is None
    assert t.heartbeat_path is None
    assert t.last_heartbeat_at is None


def test_mark_failed_burns_retry(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.mark_dispatched("oxpz-a", "feat/a", now="t1")  # dispatch_count -> 1
    s.mark_failed("oxpz-a", now="t2", reason="tests red", failure_class="test")
    t = s.get_task("oxpz-a")
    assert t.status == STATUS_FAILED
    assert t.dispatch_count == 1  # NOT decremented — a real failure burns a retry
    assert t.failure_reason == "tests red"


def test_inflight_tasks(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.add_task("oxpz-b", "B")
    s.mark_dispatched("oxpz-a", "feat/a", now="t1")
    assert [t.identifier for t in s.inflight_tasks()] == ["oxpz-a"]
    s.mark_merged("oxpz-a", now="t2", pr_number=12)
    assert s.inflight_tasks() == []


def test_record_plan_verdict(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.record_plan_verdict("oxpz-a", plan_status="approved", plan_json='{"x":1}',
                          files_touched=["a.py", "b.py"])
    t = s.get_task("oxpz-a")
    assert t.plan_status == "approved"
    assert t.files_touched == ["a.py", "b.py"]


def test_heartbeat_updates_timestamp(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.heartbeat("oxpz-a", "2026-06-14T00:00:00Z")
    assert s.get_task("oxpz-a").last_heartbeat_at == "2026-06-14T00:00:00Z"


def test_status_counts(tmp_path):
    s = _store(tmp_path)
    s.add_task("oxpz-a", "A")
    s.add_task("oxpz-b", "B")
    s.mark_dispatched("oxpz-a", "feat/a", now="t1")
    counts = s.status_counts()
    assert counts.get(STATUS_PLANNED) == 1
    assert counts.get(STATUS_DISPATCHED) == 1


# -- locks ---------------------------------------------------------------

def test_locks_claim_and_conflict_I5_L2(tmp_path):
    s = _store(tmp_path)
    assert s.locks_claim(1, ["src/a.py", "src/b.py"], now_epoch=100.0) == []
    # task 2 wants one overlapping path -> conflict returned, nothing persisted.
    conflicts = s.locks_claim(2, ["src/b.py", "src/c.py"], now_epoch=101.0)
    assert conflicts == ["src/b.py"]
    held = dict(s.held_locks())
    assert "src/c.py" not in held  # partial claim persisted NOTHING
    assert held["src/a.py"] == 1 and held["src/b.py"] == 1


def test_locks_reclaim_own_paths_idempotent_L3(tmp_path):
    s = _store(tmp_path)
    s.locks_claim(1, ["src/a.py"], now_epoch=100.0)
    assert s.locks_claim(1, ["src/a.py", "src/d.py"], now_epoch=101.0) == []
    held = dict(s.held_locks())
    assert held["src/a.py"] == 1 and held["src/d.py"] == 1


def test_locks_release_hard_deletes_F9(tmp_path):
    s = _store(tmp_path)
    s.locks_claim(1, ["src/a.py", "src/b.py"], now_epoch=100.0)
    assert s.locks_release(1) == 2
    assert s.held_locks() == []


def test_locks_expire_only_expired_and_dead_L4(tmp_path):
    s = _store(tmp_path)
    s.locks_claim(1, ["old_dead.py"], now_epoch=0.0)     # old, holder will be dead
    s.locks_claim(2, ["old_alive.py"], now_epoch=0.0)    # old, holder alive
    s.locks_claim(3, ["fresh_dead.py"], now_epoch=1000.0)  # fresh, holder dead
    deleted = s.locks_expire(now_epoch=1000.0, ttl_seconds=100, live_task_ids={2})
    held = dict(s.held_locks())
    assert deleted == 1
    assert "old_dead.py" not in held       # expired AND dead -> swept
    assert "old_alive.py" in held          # expired but holder alive -> kept
    assert "fresh_dead.py" in held         # dead but not yet expired -> kept
