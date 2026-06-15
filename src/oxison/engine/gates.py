"""The grader — judge a worker's actual diff before its work is accepted.

The plan-gate (Oxipensa) checks a task's *declared* intent; the grader checks
the *actual* changed files a worker produced. They must be separate: a worker
can touch a protected path the plan never declared (C1), so the grader re-runs
the **same** ``engine.protected.is_protected`` matcher against the real diff. A
diff that touches a protected location fails the grade regardless of what the
plan said.

This MVP grader is the protected-path fence plus an empty-diff check. The
fuller grader surface from the build-engine plan (diff-size cap, AI critique,
anti-cheat, coverage) is deliberately deferred — see the PR description.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .protected import is_protected_path


@dataclass
class GradeVerdict:
    """Outcome of grading one worker's diff. ``ok`` iff accepted."""

    ok: bool
    reason: str
    protected_hits: list[str] = field(default_factory=list)


def grade_diff(
    changed_files: list[str],
    *,
    protected_paths: tuple[str, ...],
    diff_size_cap: int | None = None,
    changed_line_count: int | None = None,
) -> GradeVerdict:
    """Grade a worker's diff.

    Rejects when the diff is empty (the worker did nothing), touches a protected
    path, or — when a cap is given — exceeds the diff-size ceiling.
    """
    if not changed_files:
        return GradeVerdict(ok=False, reason="empty diff — worker changed nothing")

    hits = [f for f in changed_files if is_protected_path(f, protected_paths)]
    if hits:
        return GradeVerdict(
            ok=False,
            reason=f"diff touches protected path(s): {', '.join(hits)}",
            protected_hits=hits,
        )

    if (
        diff_size_cap is not None
        and changed_line_count is not None
        and changed_line_count > diff_size_cap
    ):
        return GradeVerdict(
            ok=False,
            reason=f"diff too large ({changed_line_count} lines > cap {diff_size_cap})",
        )

    return GradeVerdict(ok=True, reason="clean")


__all__ = ["GradeVerdict", "grade_diff"]
