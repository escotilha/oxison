"""The write path — distill a graded outcome into a memory, behind the grader.

This is the **mechanical verify-before-store gate**. The single most important
property of a learning loop that grades itself on green tests is that it must not
store a lesson from *unverified* output — otherwise it learns from its own
reward-hacking (an agent that emits ``sys.exit(0)`` to fake a pass) and collapses
(Shumailov: a model trained on its own ungated output degrades). So a memory is
written **only when the work it came from passed oxison's existing verifier** —
the grader — and merged. The grader is the producer-independent check; capture
never invents its own notion of success.

Two outcomes are storable, both grader-verified:

* a **verified success** (grader-clean + merged) -> a ``procedural`` recipe —
  the highest-value tier for a builder;
* a **grader-rejected attempt** (the grader *verified* it failed) -> an
  ``episodic`` anti-pattern, so the same dead end is not re-walked.

Everything else — adapter failures, timeouts, ungraded runs — is **not stored**
(``None``). An engine outage is not a lesson about the code.

What is stored is **structural, never narrative**: file paths are reduced to the
component dirs they live in (``src/oxison/engine`` — *where* things are), and the
raw diff, line numbers, and trajectory are dropped. Structural anchors transfer
across runs; run-specific detail contaminates the next one.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING

from .config import (
    SRC_OUTCOME,
    TIER_EPISODIC,
    TIER_PROCEDURAL,
    MemoryConfig,
)
from .salience import clamp01
from .store import MemoryStore

if TYPE_CHECKING:  # avoid importing the asyncio-heavy engine chain at runtime
    from ..engine.dispatch import DispatchOutcome
    from ..engine.gates import GradeVerdict
    from ..engine.taskstore import Task

#: Cap on stored structural anchors — a memory is a signpost, not a file listing.
_MAX_ANCHORS = 8


def components_from_files(changed_files: list[str]) -> list[str]:
    """Reduce changed files to the **component dirs** they live in.

    ``src/oxison/engine/loop.py`` -> ``src/oxison/engine``. This is the structural
    anchor (where the change lives) with the run-specific filename and line
    numbers stripped, so the memory transfers to a future task in the same area.
    """
    comps = set()
    for f in changed_files:
        norm = f.strip().strip('"').replace("\\", "/")
        if not norm:
            continue
        parent = posixpath.dirname(norm)
        comps.add(parent or norm)  # top-level files map to themselves
    return sorted(comps)[:_MAX_ANCHORS]


def _importance_from_priority(priority: int) -> float:
    """Higher-priority tasks (lower number) yield more important memories."""
    return clamp01(1.0 - max(0, priority - 1) * 0.15)


def _pain(*, failed: bool, dispatch_count: int) -> float:
    """Costlier work (failed, or repeatedly re-dispatched) earns higher pain."""
    base = 0.7 if failed else 0.4
    return clamp01(base + 0.1 * min(dispatch_count, 3))


def capture_from_outcome(
    store: MemoryStore,
    *,
    task: Task,
    outcome: DispatchOutcome,
    verdict: GradeVerdict,
    scope: str,
    now: str,
    merged: bool,
    config: MemoryConfig | None = None,
) -> str | None:
    """Distill one graded build outcome into a memory, or ``None`` if not storable.

    The gate, in order:

    * **adapter failure / timeout** -> ``None`` (an engine outage is not a lesson).
    * **grader-clean AND merged** -> a verified ``procedural`` recipe.
    * **grader-rejected** -> a verified ``episodic`` anti-pattern.
    * anything else (e.g. grader-clean but not yet merged) -> ``None`` (wait for
      the merge signal before promoting a success to memory).
    """
    _ = config  # reserved for future thresholds; signature stability
    if outcome.adapter_failure or outcome.timed_out:
        return None

    components = components_from_files(outcome.changed_files)
    importance = _importance_from_priority(task.priority)
    provenance = {"task_id": task.identifier, "branch": outcome.branch}

    if verdict.ok and outcome.ok and merged:
        where = ", ".join(components) if components else "(no files recorded)"
        first_accept = task.acceptance[0] if task.acceptance else ""
        truth = (
            f"Verified recipe for a `{task.kind or 'task'}` of this shape: change "
            f"{where}." + (f" Acceptance met: {first_accept}" if first_accept else "")
        )
        return store.put(
            tier=TIER_PROCEDURAL,
            scope=scope,
            task_kind=task.kind,
            purpose=task.title,
            truth=truth,
            triggers=[t for t in (task.kind,) if t],
            anchors=components,
            provenance=provenance,
            verified=True,
            pain=_pain(failed=False, dispatch_count=task.dispatch_count),
            importance=importance,
            now=now,
            source=SRC_OUTCOME,
            note="merged green",
        )

    if not verdict.ok:
        where = ", ".join(components) if components else "the attempted area"
        truth = (
            f"Anti-pattern: attempting a `{task.kind or 'task'}` of this shape by "
            f"changing {where} was rejected by the grader — {verdict.reason}. "
            "Take a different approach."
        )
        return store.put(
            tier=TIER_EPISODIC,
            scope=scope,
            task_kind=task.kind,
            purpose=task.title,
            truth=truth,
            triggers=[t for t in (task.kind, verdict.reason.split(":")[0]) if t],
            anchors=components,
            provenance=provenance,
            verified=True,
            pain=_pain(failed=True, dispatch_count=task.dispatch_count),
            importance=importance,
            now=now,
            source=SRC_OUTCOME,
            note="grader-rejected",
        )

    return None  # graded-clean but not merged, or otherwise inconclusive — wait


__all__ = ["capture_from_outcome", "components_from_files"]
