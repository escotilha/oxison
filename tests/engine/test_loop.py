"""Named regression tests for the Oxfaz loop guardrails (LP1/LP2/LP3)."""

from __future__ import annotations

import asyncio

import pytest

from oxison.engine.dispatch import DispatchOutcome
from oxison.engine.gates import GradeVerdict
from oxison.engine.integrate import MergeOutcome
from oxison.engine.loop import (
    HALT_BUDGET,
    HALT_COMPLETE,
    HALT_MAX_TICKS,
    HALT_NO_PROGRESS,
    LoopOptions,
    run_build_loop,
)
from oxison.engine.taskstore import STATUS_FAILED, STATUS_MERGED, TaskStore


def _store_with(tmp_path, n):
    s = TaskStore.open(tmp_path)
    for i in range(n):
        s.add_task(f"oxpz-{i}", f"Task {i}", priority=i + 1, acceptance=["x"])
    return s


def _ok_outcome(branch="feat/x"):
    return DispatchOutcome(ok=True, branch=branch, worktree_path="/wt",
                           changed_files=["src/x.py"], cost_usd=1.0)


def _grader_ok(_outcome):
    return GradeVerdict(ok=True, reason="ok")


def _now():
    return "t"


async def _run(store, *, options, dispatcher, grader=_grader_ok):
    return await run_build_loop(store, options=options, dispatcher=dispatcher,
                                grader=grader, now_fn=_now, now_epoch_fn=lambda: 0.0)


@pytest.mark.asyncio
async def test_completion_when_all_merge(tmp_path):
    s = _store_with(tmp_path, 3)

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(max_workers=1), dispatcher=disp)
    assert summary.halt_reason == HALT_COMPLETE
    assert summary.merged == 3
    assert s.status_counts().get(STATUS_MERGED) == 3


@pytest.mark.asyncio
async def test_LP1_iteration_cap(tmp_path):
    s = _store_with(tmp_path, 10)

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(max_workers=1, max_ticks=2), dispatcher=disp)
    assert summary.halt_reason == HALT_MAX_TICKS
    assert summary.ticks == 2
    assert summary.merged == 2  # only 2 of 10 tasks reached


@pytest.mark.asyncio
async def test_LP2_no_progress_halt(tmp_path):
    s = _store_with(tmp_path, 1)

    async def disp(task, branch):
        # Engine outage every time: re-queues the task, never progresses.
        return DispatchOutcome(ok=False, branch=branch, worktree_path="/wt",
                               adapter_failure=True, error="rate limited")

    summary = await _run(s, options=LoopOptions(no_progress_ticks=3, max_ticks=50),
                         dispatcher=disp)
    assert summary.halt_reason == HALT_NO_PROGRESS
    assert summary.ticks == 3
    assert summary.merged == 0


@pytest.mark.asyncio
async def test_LP2_counter_resets_on_progress(tmp_path):
    s = _store_with(tmp_path, 2)
    calls = {"n": 0}

    async def disp(task, branch):
        calls["n"] += 1
        # tick1: adapter fail (no progress). tick2: success (progress -> reset).
        # then task pool drains -> complete, never hitting no_progress=3.
        if task.identifier == "oxpz-0" and calls["n"] == 1:
            return DispatchOutcome(ok=False, branch=branch, worktree_path="/wt",
                                   adapter_failure=True, error="transient")
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(no_progress_ticks=3, max_workers=1, max_ticks=50),
                         dispatcher=disp)
    assert summary.halt_reason == HALT_COMPLETE  # progress reset kept it alive
    assert summary.merged == 2


@pytest.mark.asyncio
async def test_LP3_timed_out_worker_charged_cap_floor_then_halts(tmp_path):
    s = _store_with(tmp_path, 3)

    async def disp(task, branch):
        # Timed out, reported $0 cost -> must be charged the cap floor.
        return DispatchOutcome(ok=False, branch=branch, worktree_path="/wt",
                               timed_out=True, cost_usd=0.0, error="timeout")

    # cap floor 5.0; ceiling 4.0 (below the floor) -> after one charge, next tick halts.
    summary = await _run(
        s,
        options=LoopOptions(worker_budget_floor=5.0, budget_ceiling_usd=4.0, max_ticks=50),
        dispatcher=disp,
    )
    assert summary.halt_reason == HALT_BUDGET
    assert summary.spent_usd == pytest.approx(5.0)  # the cap floor, not $0


@pytest.mark.asyncio
async def test_LP3_clean_cost_reconciles_to_actual(tmp_path):
    s = _store_with(tmp_path, 1)

    async def disp(task, branch):
        return DispatchOutcome(ok=True, branch=branch, worktree_path="/wt",
                               changed_files=["src/x.py"], cost_usd=2.0)

    summary = await _run(s, options=LoopOptions(worker_budget_floor=5.0), dispatcher=disp)
    # A clean worker is charged its actual cost (2.0), not the cap floor (5.0).
    assert summary.spent_usd == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_LP3_unset_ceiling_never_halts_on_budget(tmp_path):
    s = _store_with(tmp_path, 2)

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(budget_ceiling_usd=None), dispatcher=disp)
    assert summary.halt_reason == HALT_COMPLETE  # not budget
    assert summary.spent_usd > 0


@pytest.mark.asyncio
async def test_grader_rejection_fails_task(tmp_path):
    s = _store_with(tmp_path, 1)

    async def disp(task, branch):
        # Worker ran fine but touched a protected path.
        return DispatchOutcome(ok=True, branch=branch, worktree_path="/wt",
                               changed_files=[".github/workflows/ci.yml"], cost_usd=1.0)

    def grader(outcome):
        return GradeVerdict(ok=False, reason="touched protected path")

    summary = await _run(s, options=LoopOptions(), dispatcher=disp, grader=grader)
    assert summary.failed == 1 and summary.merged == 0


@pytest.mark.asyncio
async def test_crash_recovery_reconciles_stale_dispatched(tmp_path):
    # Simulate a crash: a task is left 'dispatched' with no recorded outcome.
    s = TaskStore.open(tmp_path)
    s.add_task("oxpz-0", "Crashed task", priority=1, acceptance=["x"])
    s.mark_dispatched("oxpz-0", "feat/x", now="t0")
    assert len(s.inflight_tasks()) == 1

    async def disp(task, branch):
        return _ok_outcome(branch)

    # On restart the loop must reconcile the stale row back to planned and
    # then build it — not strand it forever.
    summary = await _run(s, options=LoopOptions(), dispatcher=disp)
    assert summary.halt_reason == HALT_COMPLETE
    assert s.status_counts().get(STATUS_MERGED) == 1
    assert s.inflight_tasks() == []


@pytest.mark.asyncio
async def test_locks_claimed_during_dispatch_and_released_after(tmp_path):
    s = TaskStore.open(tmp_path)
    s.add_task("oxpz-0", "T", priority=1, acceptance=["x"], files_touched=["src/a.py"])
    held_during = {}

    async def disp(task, branch):
        # While the worker runs, its files must be locked.
        held_during["locks"] = dict(s.held_locks())
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(), dispatcher=disp)
    assert summary.halt_reason == HALT_COMPLETE
    assert held_during["locks"].get("src/a.py") is not None  # claimed during dispatch
    assert s.held_locks() == []  # released after


@pytest.mark.asyncio
async def test_depends_on_enforced_ordering(tmp_path):
    # B (higher priority) depends on A (lower priority). B must NOT run first.
    s = TaskStore.open(tmp_path)
    s.add_task("oxpz-a", "A", priority=2, acceptance=["x"])
    s.add_task("oxpz-b", "B", priority=1, acceptance=["x"], depends_on=["oxpz-a"])
    order = []

    async def disp(task, branch):
        order.append(task.identifier)
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(max_workers=1), dispatcher=disp)
    assert summary.halt_reason == HALT_COMPLETE
    assert order == ["oxpz-a", "oxpz-b"]  # dependency respected despite priority


@pytest.mark.asyncio
async def test_dependency_deadlock_halts_no_progress(tmp_path):
    # B depends on A, but A fails terminally -> B can never become eligible.
    s = TaskStore.open(tmp_path)
    s.add_task("oxpz-a", "A", priority=1, acceptance=["x"])
    s.add_task("oxpz-b", "B", priority=2, acceptance=["x"], depends_on=["oxpz-a"])
    s.mark_failed("oxpz-a", now="t0", reason="cannot build")

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(no_progress_ticks=2, max_ticks=20),
                         dispatcher=disp)
    assert summary.halt_reason == HALT_NO_PROGRESS  # B is forever blocked
    assert summary.merged == 0


@pytest.mark.asyncio
async def test_orphan_lock_swept_by_loop(tmp_path):
    # A lock held by a task that is NOT in-flight, acquired long ago, must be
    # swept by the per-tick L4 expire (which the loop now actually calls).
    s = _store_with(tmp_path, 1)
    assert s.locks_claim(999, ["orphan/file.py"], now_epoch=0.0) == []

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await run_build_loop(
        s, options=LoopOptions(lock_ttl_seconds=100), dispatcher=disp,
        grader=_grader_ok, now_fn=_now, now_epoch_fn=lambda: 1_000_000.0,
    )
    assert summary.halt_reason == HALT_COMPLETE
    assert all(p != "orphan/file.py" for p, _ in s.held_locks())  # orphan reclaimed


@pytest.mark.asyncio
async def test_complete_not_reported_when_planned_but_cap_exhausted(tmp_path):
    # A planned task over the redispatch cap is excluded by find_next_planned;
    # the loop must NOT call that COMPLETE (it isn't merged) — it makes no
    # progress and LP2 bounds it. (Regression for the find_next_planned-based
    # completion check.)
    s = _store_with(tmp_path, 1)
    s._conn.execute("UPDATE task SET dispatch_count = 9 WHERE identifier = 'oxpz-0'")

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await run_build_loop(
        s, options=LoopOptions(redispatch_cap=3, no_progress_ticks=2, max_ticks=20),
        dispatcher=disp, grader=_grader_ok, now_fn=_now, now_epoch_fn=lambda: 0.0,
    )
    assert summary.halt_reason == HALT_NO_PROGRESS
    assert summary.halt_reason != HALT_COMPLETE
    assert summary.merged == 0


@pytest.mark.asyncio
async def test_dispatcher_exception_is_adapter_failure(tmp_path):
    s = _store_with(tmp_path, 1)

    async def disp(task, branch):
        raise RuntimeError("worktree blew up")

    # An infra exception must not crash the loop; it re-queues (no progress) and
    # LP2 bounds it.
    summary = await _run(s, options=LoopOptions(no_progress_ticks=2, max_ticks=50),
                         dispatcher=disp)
    assert summary.halt_reason == HALT_NO_PROGRESS
    assert summary.merged == 0


# --- Sequential integration (injected integrator) ---------------------------


async def _merge_ok(_task, _outcome):
    return MergeOutcome(ok=True, reason="fast-forward")


async def _merge_conflict(_task, _outcome):
    return MergeOutcome(ok=False, reason="non-fast-forward merge refused")


@pytest.mark.asyncio
async def test_integrator_success_marks_merged_and_integrated(tmp_path):
    s = _store_with(tmp_path, 2)

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await run_build_loop(
        s, options=LoopOptions(max_workers=1), dispatcher=disp, grader=_grader_ok,
        now_fn=_now, now_epoch_fn=lambda: 0.0, integrator=_merge_ok,
    )
    assert summary.halt_reason == HALT_COMPLETE
    assert summary.merged == 2
    assert summary.integrated == 2


@pytest.mark.asyncio
async def test_integrator_conflict_fails_task_with_integration_class(tmp_path):
    s = _store_with(tmp_path, 1)

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await run_build_loop(
        s, options=LoopOptions(max_workers=1, max_ticks=10), dispatcher=disp,
        grader=_grader_ok, now_fn=_now, now_epoch_fn=lambda: 0.0,
        integrator=_merge_conflict,
    )
    assert summary.integrated == 0
    assert summary.failed == 1
    t = s.get_task("oxpz-0")
    assert t is not None
    assert t.status == STATUS_FAILED
    assert t.failure_class == "integration"


@pytest.mark.asyncio
async def test_no_integrator_is_db_only_merge(tmp_path):
    # Back-compat: without an integrator, an accepted task is marked merged in the
    # DB only and `integrated` stays 0 (the per-branch human-merge-boundary mode).
    s = _store_with(tmp_path, 1)

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(max_workers=1), dispatcher=disp)
    assert summary.merged == 1
    assert summary.integrated == 0
    assert s.status_counts().get(STATUS_MERGED) == 1


@pytest.mark.asyncio
async def test_planning_task_reconciled_on_startup(tmp_path):
    # #15 (M2): a task stranded in `planning` (crash mid plan-transition) must be
    # reset to `planned` at startup, else it wedges completion forever.
    s = _store_with(tmp_path, 2)
    s.mark_planning("oxpz-0")
    assert s.status_counts().get("planning") == 1

    async def disp(task, branch):
        return _ok_outcome(branch)

    summary = await _run(s, options=LoopOptions(max_workers=1), dispatcher=disp)
    assert summary.halt_reason == HALT_COMPLETE
    assert summary.merged == 2  # the reset task was driven to completion
    assert s.status_counts().get("planning", 0) == 0


@pytest.mark.asyncio
async def test_no_progress_tick_reuses_cached_queries(tmp_path):
    # #17 (M5): a task blocked on an unmet dependency makes no progress; the loop
    # spins via LP2. The stable queries must be cached, not re-run every spin.
    s = TaskStore.open(tmp_path)
    s.add_task("a", "A", priority=1, acceptance=["x"], depends_on=["never-merges"])

    calls = {"status_counts": 0, "merged_identifiers": 0}
    for name in calls:
        orig = getattr(s, name)

        def make(orig, name):
            def wrapped(*a, **k):
                calls[name] += 1
                return orig(*a, **k)
            return wrapped

        setattr(s, name, make(orig, name))

    async def disp(task, branch):  # never called — "a" is never eligible
        raise AssertionError("dispatcher must not run for a blocked task")

    summary = await _run(
        s, options=LoopOptions(max_workers=1, no_progress_ticks=3, max_ticks=20),
        dispatcher=disp,
    )
    assert summary.halt_reason == HALT_NO_PROGRESS
    # Nothing mutates across the spin, so the cache is populated once and reused:
    # each stable query runs exactly once despite multiple no-progress ticks.
    assert calls["status_counts"] == 1
    assert calls["merged_identifiers"] == 1


def _peak_tracking_dispatcher():
    """A dispatcher that records the peak concurrent in-flight count."""
    state = {"active": 0, "peak": 0}

    async def disp(task, branch):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.02)  # hold the slot open so overlap is observable
        state["active"] -= 1
        return _ok_outcome(branch)

    return disp, state


@pytest.mark.asyncio
async def test_max_workers_dispatches_batch_concurrently(tmp_path):
    # #16 (M3): independent eligible tasks run concurrently at max_workers>1.
    s = _store_with(tmp_path, 3)  # 3 tasks, no deps, no file overlap
    disp, state = _peak_tracking_dispatcher()
    summary = await _run(s, options=LoopOptions(max_workers=3), dispatcher=disp)
    assert summary.merged == 3
    assert state["peak"] >= 2  # genuinely concurrent (serial peak would be 1)


@pytest.mark.asyncio
async def test_integration_mode_stays_serial(tmp_path):
    # #16: with an integrator the ff-only invariant requires serial dispatch even
    # if max_workers>1.
    s = _store_with(tmp_path, 3)
    disp, state = _peak_tracking_dispatcher()

    async def integ(task, outcome):
        return MergeOutcome(ok=True, reason="ff", merged_sha="abc")

    summary = await run_build_loop(
        s, options=LoopOptions(max_workers=3), dispatcher=disp, grader=_grader_ok,
        now_fn=_now, now_epoch_fn=lambda: 0.0, integrator=integ,
    )
    assert summary.integrated == 3
    assert state["peak"] == 1  # serial despite max_workers=3


@pytest.mark.asyncio
async def test_parallel_overlapping_files_serialize_via_locks(tmp_path):
    # #16: even in the parallel path, two tasks declaring the same file never run
    # concurrently — the file lock skips the second until the first releases.
    s = TaskStore.open(tmp_path)
    s.add_task("a", "A", priority=1, acceptance=["x"], files_touched=["src/shared.py"])
    s.add_task("b", "B", priority=2, acceptance=["x"], files_touched=["src/shared.py"])
    disp, state = _peak_tracking_dispatcher()
    summary = await _run(s, options=LoopOptions(max_workers=2), dispatcher=disp)
    assert summary.merged == 2  # both eventually merge (across ticks)
    assert state["peak"] == 1  # the shared-file lock prevented concurrent dispatch
