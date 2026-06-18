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

from .gitutil import git_cmd
from .taskstore import Task
from .types import DispatchOutcome

Integrator = Callable[[Task, DispatchOutcome], Awaitable["MergeOutcome"]]

#: Branches the integrator must never fast-forward in place — the "never write
#: main directly" invariant. ``cmd_build`` redirects onto ``INTEGRATION_BRANCH``
#: when the repo sits on one of these, and arms the ``integrate_branch`` backstop
#: with the same set.
DEFAULT_PROTECTED_BRANCHES = frozenset({"main", "master"})

#: Dedicated branch the build loop composes onto when the live branch is protected,
#: so the user's main/master is never advanced in place.
INTEGRATION_BRANCH = "oxison/integration"


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
    protected_branches: frozenset[str] = frozenset(),
) -> MergeOutcome:
    """Commit any dirty worktree state on ``branch``, then fast-forward the repo's
    current branch onto it. See the module docstring for the safety invariant.

    ``protected_branches`` is a defense-in-depth backstop on the "never advance a
    protected branch in place" invariant: if the repo's current branch (resolved
    from HEAD) is in the set, the integrator **refuses** rather than fast-forward
    it. The primary mechanism lives at the caller — ``cmd_build`` checks out a
    dedicated integration branch up front (see ``ensure_integration_branch``) so
    the live branch is never the merge target — and arms this backstop with
    ``DEFAULT_PROTECTED_BRANCHES`` in case that redirect is ever bypassed. Default
    is empty (no protection), preserving the bare ``integrate_branch`` contract."""
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

    # (b2) Backstop: never fast-forward a protected branch in place. Should not
    # fire in the normal flow (cmd_build redirects onto a dedicated branch first),
    # but enforces the invariant at the primitive for any caller that doesn't.
    if target in protected_branches:
        return MergeOutcome(
            ok=False,
            reason=(
                f"refusing to advance protected branch {target!r} in place "
                "(never write main directly); integrate onto a dedicated branch"
            ),
        )

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


def make_integrator(
    repo: Path, *, protected_branches: frozenset[str] = frozenset()
) -> Integrator:
    """Bind an integrator to ``repo``'s root working tree for the build loop.

    ``protected_branches`` is forwarded to ``integrate_branch`` as the
    never-advance-in-place backstop (see its docstring)."""

    async def integrate(task: Task, outcome: DispatchOutcome) -> MergeOutcome:
        return await integrate_branch(
            repo,
            branch=outcome.branch,
            worktree=Path(outcome.worktree_path),
            title=task.title,
            identifier=task.identifier,
            protected_branches=protected_branches,
        )

    return integrate


async def current_branch(repo: Path) -> str | None:
    """The repo's checked-out branch name, or None if detached / unresolved."""
    rc, out = await git_cmd(["symbolic-ref", "--short", "HEAD"], cwd=repo)
    name = out.strip()
    return name if rc == 0 and name else None


async def ensure_integration_branch(
    repo: Path, *, base_branch: str, integration_branch: str = INTEGRATION_BRANCH
) -> tuple[bool, str]:
    """Check out ``integration_branch`` so the loop composes onto it instead of the
    live branch. If it doesn't exist, create it from the current HEAD. If it already
    exists (a prior run), reuse it ONLY when ``base_branch`` is fully contained in it
    (an ancestor); otherwise it predates commits on the live branch — reusing it
    would build on a stale base and turn the final merge into a 3-way — so refuse
    with guidance instead. The working tree must be clean (``cmd_build`` enforces
    this). Returns ``(ok, message)``; on success ``message`` is the branch name, on
    failure a human-readable reason."""
    rc, _ = await git_cmd(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{integration_branch}"], cwd=repo
    )
    if rc != 0:
        rc, msg = await git_cmd(["checkout", "-b", integration_branch], cwd=repo)
        return (rc == 0, integration_branch if rc == 0 else msg.strip()[:200])
    # Exists: reuse only if it already contains the live branch (no stale base).
    rc_anc, _ = await git_cmd(
        ["merge-base", "--is-ancestor", base_branch, integration_branch], cwd=repo
    )
    if rc_anc != 0:
        return (
            False,
            f"{integration_branch!r} is stale / has diverged from {base_branch!r} "
            f"(it predates commits on {base_branch!r}); merge it into {base_branch!r} "
            f"or delete it (git branch -D {integration_branch}), then re-run",
        )
    rc, msg = await git_cmd(["checkout", integration_branch], cwd=repo)
    return (rc == 0, integration_branch if rc == 0 else msg.strip()[:200])


__all__ = [
    "DEFAULT_PROTECTED_BRANCHES",
    "INTEGRATION_BRANCH",
    "Integrator",
    "MergeOutcome",
    "current_branch",
    "ensure_integration_branch",
    "integrate_branch",
    "make_integrator",
]
