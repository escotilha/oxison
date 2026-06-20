"""The grader — judge a worker's actual diff before its work is accepted.

The plan-gate (Oxipensa) checks a task's *declared* intent; the grader checks
the *actual* changed files a worker produced. They must be separate: a worker
can touch a protected path the plan never declared (C1), so the grader re-runs
the **same** ``engine.protected.is_protected`` matcher against the real diff. A
diff that touches a protected location fails the grade regardless of what the
plan said.

The structural grader (:func:`grade_diff`) is the protected-path fence, the
empty-diff check, and the diff-size cap — all pure functions of the changed
file list. :func:`grade_regression` adds the behavioural check (did the change
break a passing test suite?) as separate pure decision logic; its *evidence* is
produced out-of-band by :mod:`engine.regression`, which runs the project's test
command under the same srt sandbox as the worker. The remaining deferred
surface (AI critique, coverage deltas) is still future work.
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
    #: Which gate rejected, recorded as the task's ``failure_class`` so the
    #: store (and the cross-run memory recorder) can tell a protected-path/size
    #: rejection ("grader") apart from a broke-the-tests rejection ("regression").
    #: Only meaningful when ``ok`` is False; the loop ignores it on acceptance.
    failure_class: str = "grader"


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


def grade_regression(*, baseline_green: bool, post_green: bool) -> GradeVerdict:
    """Decide whether a worker's change is a regression, given test outcomes.

    Pure decision logic — the *evidence* (running the project's test command in
    the sandbox, before and after) lives in :mod:`engine.regression`; this only
    encodes the rule. We count **only a green→red transition** as a regression:

    * baseline already red — the suite was broken before this worker touched
      anything, so a still-red result is not its fault. Accept (and say so), so
      a repo with pre-existing failures isn't held hostage by the guard.
    * baseline green, post red — the worker broke a passing suite. Reject under
      the ``regression`` failure class.
    * baseline green, post green — no regression. Accept.
    """
    if not baseline_green:
        return GradeVerdict(ok=True, reason="regression check skipped — baseline already red")
    if not post_green:
        return GradeVerdict(
            ok=False,
            reason="regression: tests passed at baseline but fail after this change",
            failure_class="regression",
        )
    return GradeVerdict(ok=True, reason="no regression")


__all__ = ["GradeVerdict", "grade_diff", "grade_regression"]
