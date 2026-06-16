"""The Oxfaz build loop — tick coordinator + the three guardrails.

Each tick: check the guardrails, select the next planned task(s), **durably mark
them dispatched before spawning** (crash-safe ordering + the I1/I2 idempotency
guard), run the worker, grade the diff, and record the outcome. The loop halts
on completion or on any of the three net-new guardrails — the headline safety
work of the build engine, each one a different runaway axis:

* **LP1 — iteration cap.** A hard ceiling on ticks; the backstop when
  completion never trips.
* **LP2 — no-progress halt.** After N consecutive ticks with no task reaching a
  terminal state (merged/failed), the loop **halts** — the structural bound on
  the "keeps retrying, never advances" runaway (e.g. an engine outage that
  re-queues the same task forever).
* **LP3 — budget ceiling.** A cost cap checked each tick. A timed-out worker
  (no ``result`` event → ``$0`` cost) is charged its per-worker cap as a
  **floor**, so the most expensive workers are visible to the meter and the
  ceiling actually trips. An *unset* ceiling is simply inactive — it never reads
  as infinite; LP1/LP2 still bound the run.

The dispatcher and grader are injected (the build-engine plan's "fake every
peer" test strategy): the loop owns sequencing and the guardrails; the worker
launch and diff grading are pluggable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .dispatch import DispatchOutcome
from .gates import GradeVerdict
from .integrate import Integrator
from .taskstore import STATUS_PLANNED, STATUS_PLANNING, Task, TaskStore

Dispatcher = Callable[[Task, str], Awaitable[DispatchOutcome]]
Grader = Callable[[DispatchOutcome], GradeVerdict]
NowFn = Callable[[], str]
NowEpochFn = Callable[[], float]

#: Generous candidate pool size — roadmaps are capped well below this by the
#: plan-gate, so this reads "all planned tasks" for dependency filtering.
_CANDIDATE_POOL = 1000

HALT_COMPLETE = "complete"
HALT_MAX_TICKS = "max_ticks"
HALT_NO_PROGRESS = "no_progress"
HALT_BUDGET = "budget"


@dataclass
class LoopOptions:
    branch_prefix: str = "feat/oxison-"
    max_workers: int = 1
    max_ticks: int | None = None
    budget_ceiling_usd: float | None = None
    no_progress_ticks: int = 5
    redispatch_cap: int = 3
    #: Per-worker cap charged as the floor for a timed-out (no-result) worker.
    worker_budget_floor: float = 5.0
    #: TTL for the per-tick stale-lock sweep (L4). A lock outlives this only
    #: while its holder is live.
    lock_ttl_seconds: int = 4 * 60 * 60


@dataclass
class LoopSummary:
    ticks: int
    dispatched: int
    merged: int
    failed: int
    spent_usd: float
    halt_reason: str
    #: How many graded tasks were git-merged into main (only when an integrator
    #: is wired, i.e. ``--integrate``); 0 in the default per-branch mode.
    integrated: int = 0


def _eligible(store: TaskStore, options: LoopOptions, merged_ids: set[str]) -> list[Task]:
    """Planned tasks ready to dispatch: under the redispatch cap, highest
    priority first, and with every ``depends_on`` already merged (so a task
    never runs before its prerequisites). ``merged_ids`` is passed in (not
    re-queried) so the tick can reuse its cached snapshot (M5)."""
    candidates = store.find_next_planned(
        limit=_CANDIDATE_POOL, redispatch_cap=options.redispatch_cap
    )
    ready = [t for t in candidates if all(d in merged_ids for d in t.depends_on)]
    return ready[: options.max_workers]


async def run_build_loop(
    store: TaskStore,
    *,
    options: LoopOptions,
    dispatcher: Dispatcher,
    grader: Grader,
    now_fn: NowFn,
    now_epoch_fn: NowEpochFn,
    integrator: Integrator | None = None,
) -> LoopSummary:
    """Drive the build loop to a halt. Returns a summary of what happened.

    When ``integrator`` is set, each graded-accepted task is git-merged into the
    repo's current branch (composing the roadmap into one product); a merge
    conflict fails the task (``failure_class="integration"``) and never advances
    main. When ``None`` (the default), accepted tasks are marked merged in the DB
    only — the per-branch "human merge boundary" behavior.
    """
    # Startup reconciliation: a task left ``dispatched`` is the residue of a
    # crash between mark_dispatched and recording its outcome. Return it to
    # ``planned`` (free, via I4) and free any locks it orphaned, so the durable
    # in-flight marker actually drives recovery instead of stranding the task.
    for stale in store.inflight_tasks():
        store.mark_adapter_failure(
            stale.identifier, reason="reconciled: stale dispatched on restart"
        )
        store.locks_release(stale.id)
    # A task left ``planning`` (crash mid plan-transition) is caught by neither the
    # inflight sweep nor completion (it counts as not-complete), so it would wedge
    # the loop — reset it to ``planned`` so it gets re-driven (M2).
    store.reset_planning()

    tick = 0
    spent = 0.0
    no_progress = 0
    dispatched = 0
    merged = 0
    failed = 0
    integrated = 0

    def summary(reason: str) -> LoopSummary:
        return LoopSummary(
            ticks=tick, dispatched=dispatched, merged=merged, failed=failed,
            spent_usd=round(spent, 6), halt_reason=reason, integrated=integrated,
        )

    # M5 — per-tick query cache. merged_ids / status_counts / inflight / eligible
    # are stable between ticks until a task changes state, so re-querying them on
    # every 20 ms no-progress spin is pure churn (~250 q/s when blocked). Cache
    # them and invalidate (set to None) only when a tick mutates task state — so a
    # blocked spin runs zero of these queries while LP2 counts down.
    cache: tuple[set[str], dict[str, int], set[int], list[Task]] | None = None

    while True:
        # LP1 — iteration cap (checked before doing tick work).
        if options.max_ticks is not None and tick >= options.max_ticks:
            return summary(HALT_MAX_TICKS)
        # LP3 — budget ceiling (only when a ceiling is set).
        if options.budget_ceiling_usd is not None and spent >= options.budget_ceiling_usd:
            return summary(HALT_BUDGET)

        tick += 1
        if cache is None:
            merged_ids = store.merged_identifiers()
            counts = store.status_counts()
            live_ids = {t.id for t in store.inflight_tasks()}
            eligible = _eligible(store, options, merged_ids)
            cache = (merged_ids, counts, live_ids, eligible)
        else:
            merged_ids, counts, live_ids, eligible = cache

        # L4 — sweep stale locks whose holder is dead (TTL-expired). The live
        # set is the currently in-flight tasks; anything else holding an old
        # lock is an orphan and is reclaimed so it can't block forever.
        store.locks_expire(
            now_epoch=now_epoch_fn(), ttl_seconds=options.lock_ttl_seconds,
            live_task_ids=live_ids,
        )
        # Completion is "no task is planned/planning at all" — checked via status
        # counts (NOT find_next_planned, which also excludes cap-exhausted tasks
        # and would falsely report COMPLETE while they remain). Planned tasks
        # that are merely blocked (unmet deps / cap-exhausted) make no progress
        # and are bounded by LP2 — a dependency deadlock surfaces there.
        if counts.get(STATUS_PLANNED, 0) == 0 and counts.get(STATUS_PLANNING, 0) == 0:
            return summary(HALT_COMPLETE)

        progressed = False
        mutated = False  # any task-state change this tick → invalidate the cache
        for task in eligible:
            # File-serialization: claim the task's declared files before
            # dispatch. A conflict means another in-flight task holds them —
            # skip this task this tick (it stays planned, retried later).
            if store.locks_claim(task.id, task.files_touched, now_epoch=now_epoch_fn()):
                continue
            branch = f"{options.branch_prefix}{task.identifier}"
            # Crash-safe: durably record dispatched BEFORE spawning the worker.
            # The guard makes a second/raced dispatch a no-op (I1/I2).
            if not store.mark_dispatched(task.identifier, branch, now=now_fn()):
                store.locks_release(task.id)
                continue
            dispatched += 1
            mutated = True  # dispatched + the terminal mark below change state

            try:
                outcome = await dispatcher(task, branch)
                # LP3 accounting: charge actual cost; a timed-out worker that
                # reported no cost is charged its cap floor so the meter is honest.
                charged = outcome.cost_usd
                if outcome.timed_out and charged <= 0:
                    charged = options.worker_budget_floor
                spent += charged

                if outcome.adapter_failure:
                    # Engine-side problem: retry for free, NOT progress (so a
                    # persistent outage trips LP2).
                    store.mark_adapter_failure(
                        task.identifier, reason=outcome.error or "adapter failure"
                    )
                elif not outcome.ok:
                    store.mark_failed(task.identifier, now=now_fn(),
                                      reason=outcome.error or "worker failed",
                                      failure_class="worker")
                    failed += 1
                    progressed = True
                else:
                    verdict = grader(outcome)
                    if not verdict.ok:
                        store.mark_failed(task.identifier, now=now_fn(),
                                          reason=verdict.reason, failure_class="grader")
                        failed += 1
                    elif integrator is None:
                        # Default: DB-only merge boundary (per-branch, no git advance).
                        store.mark_merged(task.identifier, now=now_fn())
                        merged += 1
                    else:
                        # Integration mode: actually merge the branch into main.
                        merge = await integrator(task, outcome)
                        if merge.ok:
                            store.mark_merged(task.identifier, now=now_fn())
                            merged += 1
                            integrated += 1
                        else:
                            # Conflict/failure: main is NOT advanced. Burn a retry
                            # under a distinct class so LP2 accounting stays honest.
                            store.mark_failed(task.identifier, now=now_fn(),
                                              reason=merge.reason, failure_class="integration")
                            failed += 1
                    progressed = True
            except Exception as exc:  # noqa: BLE001 — dispatcher infra error is engine-side
                store.mark_adapter_failure(task.identifier, reason=f"dispatch error: {exc}")
            finally:
                store.locks_release(task.id)

            # LP3 — stop dispatching within this tick the moment the ceiling is
            # crossed, so a wide tick can't overshoot before the top-of-loop check.
            if options.budget_ceiling_usd is not None and spent >= options.budget_ceiling_usd:
                break

        # M5 — invalidate the per-tick cache iff a task changed state this tick;
        # an unchanged (blocked) tick keeps the cache so the next spin re-queries
        # nothing.
        if mutated:
            cache = None

        # LP2 — no-progress halt.
        no_progress = 0 if progressed else no_progress + 1
        if no_progress >= options.no_progress_ticks:
            return summary(HALT_NO_PROGRESS)

        # Yield to the event loop. On a no-progress tick (all tasks blocked,
        # no slow dispatcher await happened) this prevents a hot CPU spin
        # against state.db while LP2 counts down.
        await asyncio.sleep(0 if progressed else 0.02)


__all__ = [
    "HALT_BUDGET",
    "HALT_COMPLETE",
    "HALT_MAX_TICKS",
    "HALT_NO_PROGRESS",
    "Dispatcher",
    "Grader",
    "Integrator",
    "LoopOptions",
    "LoopSummary",
    "run_build_loop",
]
