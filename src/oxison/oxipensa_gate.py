"""The Oxipensa plan-gate — validate a roadmap before it becomes a contract.

A deterministic fence that a proposed roadmap must pass before oxison writes
it. A roadmap that fails the
gate is never written as ``roadmap.json``; instead the violations are fed back
to the planner for one self-correction pass (see :mod:`oxison.oxipensa`).

The gate is a pure function (no I/O, no AI) so it is trivially testable and
its verdict is reproducible. It reuses the **same** segment-anchored
``engine.protected.is_protected`` matcher the build engine's grader uses (H3) —
a planned task must never target a protected path (CI config, ``.env``,
``.git/``, lockfiles, the engine's own ``oxison-build/`` state).

The acceptance-criteria rule is the load-bearing one: every task must carry at
least one *observable* acceptance criterion. That is what lets Oxfaz run a
goal-driven build loop (verify against a checkable end-state) instead of
guessing when a task is "done" — the Karpathy goal-driven discipline encoded
into the contract itself.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .engine.engconfig import EngineConfig
from .engine.protected import is_protected_path
from .roadmap_doc import ALLOWED_KINDS, RoadmapDoc, RoadmapTask

#: Default scope fence on a single roadmap. A plan with more tasks than this is
#: almost certainly under-decomposed reasoning, not a real backlog — reject and
#: ask the planner to consolidate.
DEFAULT_MAX_TASKS = 40

#: Default relevance floor for the plan-boundary filter. Mirrors the memory
#: subsystem's ``abstain_min_score`` (0.25): prune only *clearly* off-target
#: tasks (relevance < 0.25), so the filter removes speculative gold-plating
#: without second-guessing the planner on genuinely-marginal calls. This is the
#: numeric enforcement of "build today's product simply, not tomorrow's
#: prematurely."
DEFAULT_RELEVANCE_MIN_SCORE = 0.25


@dataclass
class GateResult:
    """Outcome of gating a roadmap. ``ok`` iff ``violations`` is empty."""

    ok: bool
    violations: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        """A compact, model-readable list of what to fix (for the retry pass)."""
        return "\n".join(f"- {v}" for v in self.violations)


def _check_cycle(id_to_deps: dict[str, list[str]]) -> list[str]:
    """Return the identifiers involved in the first dependency cycle, or []."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(id_to_deps, WHITE)
    cycle: list[str] = []

    def visit(node: str, stack: list[str]) -> bool:
        color[node] = GRAY
        stack.append(node)
        for dep in id_to_deps.get(node, []):
            if dep not in color:
                continue  # dangling dep — reported separately, not a cycle here
            if color[dep] == GRAY:
                start = stack.index(dep)
                cycle.extend(stack[start:])
                return True
            if color[dep] == WHITE and visit(dep, stack):
                return True
        stack.pop()
        color[node] = BLACK
        return False

    for node in id_to_deps:
        if color[node] == WHITE and visit(node, []):
            break
    return cycle


def gate_roadmap(
    doc: RoadmapDoc,
    *,
    config: EngineConfig | None = None,
    max_tasks: int = DEFAULT_MAX_TASKS,
) -> GateResult:
    """Validate ``doc`` against the plan-gate rules.

    ``config`` supplies the protected-path rule set (defaults to
    ``EngineConfig()``'s generic, project-agnostic list).
    """
    cfg = config or EngineConfig()
    violations: list[str] = []
    tasks = doc.tasks

    if not tasks:
        return GateResult(ok=False, violations=["roadmap has no tasks"])

    if len(tasks) > max_tasks:
        violations.append(
            f"too many tasks ({len(tasks)} > max {max_tasks}); consolidate the plan"
        )

    # Per-task semantic checks.
    seen_ids: dict[str, int] = {}
    for i, task in enumerate(tasks):
        label = f"task[{i}] {task.identifier!r}"
        if not task.title.strip():
            violations.append(f"{label}: empty title")
        if task.kind not in ALLOWED_KINDS:
            violations.append(
                f"{label}: invalid kind {task.kind!r} (allowed: {', '.join(ALLOWED_KINDS)})"
            )
        if task.priority < 1:
            violations.append(f"{label}: priority must be >= 1 (got {task.priority})")
        if not task.acceptance:
            violations.append(
                f"{label}: no acceptance criteria — every task needs at least one "
                "observable, checkable end-state"
            )
        for fpath in task.files_hint:
            if is_protected_path(fpath, cfg.protected_paths):
                violations.append(
                    f"{label}: files_hint targets a protected path: {fpath!r}"
                )
        seen_ids[task.identifier] = seen_ids.get(task.identifier, 0) + 1

    # Duplicate identifiers (collision on (kind, title) → same deterministic id).
    for ident, count in seen_ids.items():
        if count > 1:
            violations.append(
                f"duplicate identifier {ident!r} ({count} tasks) — "
                "two tasks share the same kind + title"
            )

    # Dangling dependencies (a depends_on entry that resolved to no known id).
    known = set(seen_ids)
    id_to_deps: dict[str, list[str]] = {}
    for task in tasks:
        # Exclude a self-loop from the cycle graph — it's reported explicitly
        # below, so the cycle pass shouldn't also flag it as "X -> X".
        resolved = [d for d in task.depends_on if d in known and d != task.identifier]
        id_to_deps.setdefault(task.identifier, []).extend(resolved)
        for dep in task.depends_on:
            if dep not in known:
                violations.append(
                    f"task {task.identifier!r}: depends_on references unknown task {dep!r}"
                )
            elif dep == task.identifier:
                violations.append(f"task {task.identifier!r}: depends on itself")

    # Dependency cycle.
    cycle = _check_cycle(id_to_deps)
    if cycle:
        violations.append("dependency cycle: " + " -> ".join(cycle + [cycle[0]]))

    return GateResult(ok=not violations, violations=violations)


def filter_by_relevance(
    doc: RoadmapDoc,
    *,
    min_score: float = DEFAULT_RELEVANCE_MIN_SCORE,
) -> tuple[RoadmapDoc, list[RoadmapTask]]:
    """Prune tasks whose ``relevance`` is below ``min_score``.

    Pure, like :func:`gate_roadmap` — no I/O, no AI. The planner emits a
    ``relevance`` self-assessment per task; this is the deterministic
    *enforcement* of that judgment (the same emit-then-enforce split oxison uses
    for priority and acceptance). It is the numeric form of Karpathy
    Simplicity-First: drop speculative gold-plating *before* it becomes a build
    contract.

    **Transitive-keep.** A low-relevance task that a *kept* task depends on is
    retained — otherwise pruning would leave a dangling dependency the plan-gate
    would (rightly) reject. So the relevance filter never produces a roadmap the
    gate can't accept; it only removes leaves that nothing relevant needs.

    Returns ``(filtered_doc, pruned_tasks)``. ``pruned_tasks`` is empty when
    nothing was dropped (including the ``min_score <= 0`` opt-out and the
    all-default-``1.0`` case), so callers can cheaply detect a no-op.
    """
    if min_score <= 0.0:
        return doc, []

    by_id = {t.identifier: t for t in doc.tasks}
    keep_ids = {t.identifier for t in doc.tasks if t.relevance >= min_score}

    # Transitively pull in any below-floor task that a kept task depends on, so
    # the survivor set is dependency-closed and the gate never sees a dangling
    # dep we introduced.
    frontier = list(keep_ids)
    while frontier:
        current = frontier.pop()
        for dep in by_id[current].depends_on:
            if dep in by_id and dep not in keep_ids:
                keep_ids.add(dep)
                frontier.append(dep)

    if len(keep_ids) == len(doc.tasks):
        return doc, []

    # Preserve original task order in both outputs.
    kept = [t for t in doc.tasks if t.identifier in keep_ids]
    pruned = [t for t in doc.tasks if t.identifier not in keep_ids]
    return dataclasses.replace(doc, tasks=kept), pruned


__all__ = [
    "DEFAULT_MAX_TASKS",
    "DEFAULT_RELEVANCE_MIN_SCORE",
    "GateResult",
    "filter_by_relevance",
    "gate_roadmap",
]
