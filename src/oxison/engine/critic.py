"""AI critic — a read-only quality gate on a worker's diff before it's accepted.

The deterministic ``grade_diff`` is the mechanical fence (protected paths, empty
diff, size cap). The critic is the *judgment* layer ``gates`` flagged as future
work: a read-only ``claude -p`` reviews the worker's changes in its worktree
against the task's acceptance criteria and renders a strict PASS/FAIL.

It is **opt-in** (``oxison build --critic``) — an extra AI call per graded task —
and the loop runs it ONLY after the deterministic grader passes, so a diff the
mechanical gate already rejected never pays for a review. The review is
**read-only** (``Read,Glob,Grep``): it physically cannot modify the worktree, so
it needs no sandbox.

**Fail-open on infra:** if the review call errors or its verdict is unparseable,
the critic defers to the grader's PASS rather than reject good-by-grader work on a
critic hiccup. It vetoes only on an explicit ``VERDICT: FAIL``.
"""
from __future__ import annotations

from pathlib import Path

from ..config import READ_ONLY_TOOLS, RunConfig
from ..dispatch import invoke
from .gates import GradeVerdict
from .loop import Critic
from .taskstore import Task
from .types import DispatchOutcome

#: Generous wall-clock cap for one read-only review.
CRITIC_TIMEOUT_S = 5 * 60.0


def _build_critic_prompt(task: Task, changed_files: list[str]) -> str:
    accept = "\n".join(f"- {a}" for a in task.acceptance) or "- (none specified)"
    files = ", ".join(changed_files) if changed_files else "(none reported)"
    return (
        "You are a strict code reviewer (critic) for an autonomous build. A worker "
        "implemented ONE task in this worktree. Judge whether its changes ACTUALLY "
        "satisfy the acceptance criteria and are correct — not whether they merely "
        "look plausible. You have read-only tools; read the changed files (and any "
        "context you need) before deciding.\n\n"
        f"TASK: {task.title}\n"
        "ACCEPTANCE CRITERIA (the definition of done):\n"
        f"{accept}\n"
        f"CHANGED FILES: {files}\n\n"
        "Reject if any acceptance criterion is unmet, the implementation is "
        "incorrect or incomplete, or it introduces an obvious bug. Accept only if "
        "the criteria are genuinely met.\n\n"
        "End your reply with EXACTLY one line and nothing after it:\n"
        "  VERDICT: PASS\n"
        "or\n"
        "  VERDICT: FAIL — <one-line reason>"
    )


def _parse_verdict(text: str) -> tuple[bool | None, str]:
    """``(passed, reason)``. ``passed`` is ``None`` if no clear VERDICT line found
    (the caller treats that as defer-to-grader, not a rejection)."""
    for line in reversed(text.splitlines()):
        s = line.strip()
        up = s.upper()
        if up.startswith("VERDICT: FAIL"):
            reason = s[len("VERDICT:"):].lstrip().lstrip("FAILfail").lstrip(" —-:").strip()
            return (False, reason or "critic rejected the diff")
        if up.startswith("VERDICT: PASS"):
            return (True, "critic approved")
    return (None, "critic verdict unparseable")


def make_critic(cfg: RunConfig, *, timeout_s: float = CRITIC_TIMEOUT_S) -> Critic:
    """Bind a critic to ``cfg``'s auth/model for the build loop. The review runs in
    the task's worktree (read-only), so it sees exactly what the worker produced."""

    async def critic(task: Task, outcome: DispatchOutcome) -> GradeVerdict:
        prompt = _build_critic_prompt(task, outcome.changed_files)
        result = await invoke(
            prompt, cfg=cfg, allowed_tools=READ_ONLY_TOOLS,
            cwd=Path(outcome.worktree_path), timeout_s=timeout_s,
        )
        if not result.ok:
            # Infra failure (timeout / engine error) — don't reject good-by-grader
            # work on a critic hiccup; defer to the grader's PASS.
            return GradeVerdict(
                ok=True, reason=f"critic skipped (review failed: {result.error or 'unknown'})",
                cost_usd=result.cost_usd,
            )
        passed, reason = _parse_verdict(result.text)
        if passed is False:
            return GradeVerdict(
                ok=False, reason=f"critic: {reason}", failure_class="critic",
                cost_usd=result.cost_usd,
            )
        # PASS or ambiguous → accept (ambiguous defers to the grader's pass).
        return GradeVerdict(ok=True, reason=reason, cost_usd=result.cost_usd)

    return critic


__all__ = ["CRITIC_TIMEOUT_S", "make_critic"]
