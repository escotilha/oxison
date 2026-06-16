"""Integrate a graded branch into the repo's current branch — the missing merge.

The loop's default merge boundary is DB-only: ``taskstore.mark_merged`` flips a
status, git never advances, and every worker branches from the same static base —
so a multi-task roadmap yields N disjoint branches, not one product. This module
is the injected integrator that actually composes each graded branch onto the live
branch (main/master). Because the build loop runs one task to completion before
the next is dispatched (``--integrate`` forces ``max_workers=1``), each branch is a
strict descendant of the live branch, so every merge is a **fast-forward**: no
merge commit, no 3-way, no conflict. ``--ff-only`` enforces that invariant and, if
a fast-forward is ever impossible, refuses **without touching the working tree** —
the loop then fails the task and main is left exactly where it was.

Real-git only (faked in the loop tests). Reuses the async ``git_cmd`` helper from
``gitutil`` rather than re-implementing the subprocess plumbing.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .dispatch import DispatchOutcome
from .gitutil import git_cmd
from .taskstore import Task

Integrator = Callable[[Task, DispatchOutcome], Awaitable["MergeOutcome"]]


@dataclass
class MergeOutcome:
    """Result of integrating one graded branch into the repo's current branch.

    On ``ok=False`` the repo's branch is exactly where it was before — the
    integrator never leaves a half-applied or conflicted merge.
    """

    ok: bool
    reason: str
    merged_sha: str | None = None


async def integrate_branch(
    repo: Path,
    *,
    branch: str,
    worktree: Path,
    title: str,
    identifier: str,
) -> MergeOutcome:
    """Commit any dirty worktree state on ``branch``, then fast-forward the repo's
    current branch onto it. See the module docstring for the safety invariant."""
    # (a) The worker may leave changes uncommitted; an uncommitted change in a
    # linked worktree is invisible to a merge from the repo root. Commit it first.
    rc, out = await git_cmd(["status", "--porcelain"], cwd=worktree)
    if rc != 0:
        return MergeOutcome(ok=False, reason=f"git status failed in worktree: {out.strip()[:200]}")
    if out.strip():
        rc, out = await git_cmd(["add", "-A"], cwd=worktree)
        if rc != 0:
            return MergeOutcome(ok=False, reason=f"git add failed: {out.strip()[:200]}")
        rc, out = await git_cmd(["commit", "-m", f"oxfaz: {title} ({identifier})"], cwd=worktree)
        if rc != 0:
            return MergeOutcome(ok=False, reason=f"git commit failed: {out.strip()[:200]}")

    # (b) Resolve the live branch from HEAD — never hardcode main/master.
    rc, cur = await git_cmd(["symbolic-ref", "--short", "HEAD"], cwd=repo)
    target = cur.strip()
    if rc != 0 or not target:
        return MergeOutcome(ok=False, reason="repo is in detached HEAD; cannot integrate")

    # (c) Fast-forward only. Always a ff at max_workers=1; refuses cleanly otherwise.
    rc, msg = await git_cmd(["merge", "--ff-only", branch], cwd=repo)
    if rc == 0:
        rc2, sha = await git_cmd(["rev-parse", "HEAD"], cwd=repo)
        return MergeOutcome(
            ok=True, reason="fast-forward", merged_sha=sha.strip() if rc2 == 0 else None
        )
    # Non-ff: a ff-only refusal changes nothing, but abort defensively so the repo
    # is guaranteed clean for the next task, then report the conflict.
    await git_cmd(["merge", "--abort"], cwd=repo)
    return MergeOutcome(
        ok=False,
        reason=f"non-fast-forward merge of {branch} into {target} refused: {msg.strip()[:200]}",
    )


def make_integrator(repo: Path) -> Integrator:
    """Bind an integrator to ``repo``'s root working tree for the build loop."""

    async def integrate(task: Task, outcome: DispatchOutcome) -> MergeOutcome:
        return await integrate_branch(
            repo,
            branch=outcome.branch,
            worktree=Path(outcome.worktree_path),
            title=task.title,
            identifier=task.identifier,
        )

    return integrate


__all__ = ["Integrator", "MergeOutcome", "integrate_branch", "make_integrator"]
