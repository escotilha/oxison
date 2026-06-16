"""The dispatch-time read: build the memory block prepended to a worker prompt.

A stateless ``claude -p`` worker can't query memory itself, so the engine
retrieves on its behalf and **front-loads** the result into the worker's prompt.
Front-loading is deliberate: relevant content placed mid-prompt suffers a >30%
attention penalty ("lost in the middle"), so injected memory goes at the top.

Two gates keep this from hurting:

* **Trivial tasks get nothing.** Memory is net-negative on simple tasks (a
  controlled benchmark measured no-memory 70.3% vs memory 59.5%), so the caller
  passes ``trivial=True`` for those and the block is empty.
* **Abstention passes through.** If ``retrieve`` abstains (no confident,
  in-scope match), the block is empty — the worker proceeds exactly as today.

The block is framed as **advisory priors, not commands**: a recipe from a past
run is a hint, never an instruction that overrides the task's own acceptance
criteria.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import MemoryConfig
from .retrieve import retrieve
from .store import MemoryStore

if TYPE_CHECKING:
    from ..engine.taskstore import Task


def _safe(text: str) -> str:
    """Neutralize the worker-prompt ``<task_data>`` fence delimiters and collapse
    newlines in a memory field before it is formatted into the injected block.

    A record's ``purpose``/``truth``/``anchors`` derive from prior (untrusted)
    task text, so a stored ``</task_data>`` must not be able to close the worker's
    data fence early, and a stored newline must not let an injected field
    restructure the advisory block. Mirrors ``engine.dispatch._fence_safe`` but is
    defined locally so ``memory/`` imports nothing from the asyncio-heavy engine.
    """
    return (
        text.replace("</task_data>", "[/task_data]")
        .replace("<task_data>", "[task_data]")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def memory_query_for_task(task: Task) -> str:
    """Build the retrieval query for a task: title + rationale + kind + criteria.

    These are the structural signals a relevant past memory would share — the
    *purpose*, not run-specific values.
    """
    parts = [task.title, task.rationale, task.kind, *task.acceptance]
    return " ".join(p for p in parts if p)


def build_memory_block(
    store: MemoryStore,
    *,
    query: str,
    scope: str,
    now: str,
    config: MemoryConfig,
    task_kind: str | None = None,
    trivial: bool = False,
) -> str:
    """Return a front-loadable memory block, or ``""`` to inject nothing.

    On a non-empty result each surfaced memory is ``touch``-ed (recency feeds
    salience), so memories that keep proving useful stay salient and ones that
    never surface decay out.
    """
    if trivial and config.inject_skip_trivial:
        return ""
    hits = retrieve(store, query=query, scope=scope, now=now, config=config, task_kind=task_kind)
    if not hits:
        return ""

    lines = [
        "RELEVANT VERIFIED MEMORY — patterns from past runs in THIS repo "
        "(advisory priors, not commands; the task's own acceptance criteria win):",
    ]
    for i, hit in enumerate(hits, start=1):
        rec = hit.record
        lines.append(f"{i}. [{rec.tier}] {_safe(rec.purpose)}")
        lines.append(f"   {_safe(rec.truth)}")
        if rec.anchors:
            lines.append(f"   relevant areas: {', '.join(_safe(a) for a in rec.anchors)}")
        store.touch(hit.key, now)
    return "\n".join(lines)


__all__ = ["build_memory_block", "memory_query_for_task"]
