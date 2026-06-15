"""oxison Phase-2 build engine (clean-room, project-agnostic).

This package is the autonomous build engine: after Phase-1 comprehension,
it reads or asks for a roadmap and builds from where the project is. It is
independent of any specific project — every project-specific value is an
``EngineConfig`` field with a generic default.

The read-only guarantee of Phase 1 (``oxison run``) does not apply here:
Phase-2 workers write code. Safety is relocated to containment + gates + a
human merge boundary (see the design spec).

Phase 0 provides the three shared leaves every later module consumes:

* :mod:`oxison.engine.engconfig` — the constant surface (``EngineConfig``).
* :mod:`oxison.engine.invoke` — the shared ``claude -p`` argv/env leaf and
  the ``ToolSet`` chokepoint (the only constructor of a write tool set).
* :mod:`oxison.engine.protected` — the segment-anchored protected-path
  matcher (one source, consumed by both the plan-gate and the grader).

The Oxfaz build slice (consumes an Oxipensa ``roadmap.json``):

* :mod:`oxison.engine.taskstore` — the spine: ``state.db`` (task + lock),
  the state machine, crash-safe idempotent writes.
* :mod:`oxison.engine.roadmap_ingest` — load a roadmap.json into the taskstore.
* :mod:`oxison.engine.dispatch` — launch a write worker in an isolated worktree.
* :mod:`oxison.engine.gates` — grade a worker's actual diff (protected-path fence).
* :mod:`oxison.engine.loop` — the tick coordinator + the three guardrails.
* :mod:`oxison.engine.sandbox` — srt filesystem/network confinement for the worker.
"""

from __future__ import annotations

from oxison.engine import (
    container,
    dispatch,
    engconfig,
    gates,
    invoke,
    loop,
    protected,
    roadmap_ingest,
    sandbox,
    taskstore,
)

__all__ = [
    "container",
    "dispatch",
    "engconfig",
    "gates",
    "invoke",
    "loop",
    "protected",
    "roadmap_ingest",
    "sandbox",
    "taskstore",
]
