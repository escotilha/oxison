"""Real-git tests for the sequential-integration merge step (integrate_branch)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from oxison.engine.integrate import (
    DEFAULT_PROTECTED_BRANCHES,
    INTEGRATION_BRANCH,
    current_branch,
    ensure_integration_branch,
    integrate_branch,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _head(repo) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


def _count(repo) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip())


def _init_repo(repo, *, branch="main"):
    repo.mkdir()
    _git(["init", "-q", "-b", branch], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "README.md").write_text("base\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "init"], repo)


def _worktree(repo, tmp_path, branch, name):
    wt = tmp_path / name
    _git(["worktree", "add", "-b", branch, str(wt), "HEAD"], repo)
    return wt


@pytest.mark.asyncio
async def test_fast_forwards_committed_branch(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = _worktree(repo, tmp_path, "feat/oxison-a", "wt-a")
    (wt / "a.py").write_text("x = 1\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "work a"], wt)

    res = await integrate_branch(repo, branch="feat/oxison-a", worktree=wt,
                                 title="Task A", identifier="oxpz-a")
    assert res.ok
    assert res.reason == "fast-forward"
    assert (repo / "a.py").is_file()           # composed onto main's working tree
    assert res.merged_sha == _head(repo)


@pytest.mark.asyncio
async def test_commits_uncommitted_worker_changes(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = _worktree(repo, tmp_path, "feat/oxison-b", "wt-b")
    (wt / "b.py").write_text("y = 2\n")        # left UNCOMMITTED by the worker

    res = await integrate_branch(repo, branch="feat/oxison-b", worktree=wt,
                                 title="Task B", identifier="oxpz-b")
    assert res.ok
    assert (repo / "b.py").is_file()           # integrator committed it, then ff-merged


@pytest.mark.asyncio
async def test_two_tasks_compose_on_main(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    start = _count(repo)

    # Task A — branch from HEAD, commit, integrate.
    wt_a = _worktree(repo, tmp_path, "feat/oxison-a", "wt-a")
    (wt_a / "a.py").write_text("x = 1\n")
    _git(["add", "-A"], wt_a)
    _git(["commit", "-qm", "work a"], wt_a)
    assert (await integrate_branch(repo, branch="feat/oxison-a", worktree=wt_a,
                                   title="A", identifier="oxpz-a")).ok

    # Task B — branch from the ADVANCED HEAD (mirrors the loop re-reading HEAD).
    wt_b = _worktree(repo, tmp_path, "feat/oxison-b", "wt-b")
    assert (wt_b / "a.py").is_file()           # B's worktree already contains A's work
    (wt_b / "b.py").write_text("y = 2\n")
    _git(["add", "-A"], wt_b)
    _git(["commit", "-qm", "work b"], wt_b)
    assert (await integrate_branch(repo, branch="feat/oxison-b", worktree=wt_b,
                                   title="B", identifier="oxpz-b")).ok

    # Both compose on main; linear history (+2, no merge commits).
    assert (repo / "a.py").is_file() and (repo / "b.py").is_file()
    assert _count(repo) == start + 2


@pytest.mark.asyncio
async def test_non_ff_refused_main_unchanged(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = _worktree(repo, tmp_path, "feat/oxison-c", "wt-c")
    (wt / "c.py").write_text("z = 3\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "work c"], wt)
    # Diverge main so the branch is no longer a descendant → ff impossible.
    (repo / "README.md").write_text("diverged\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "diverge main"], repo)
    before = _head(repo)

    res = await integrate_branch(repo, branch="feat/oxison-c", worktree=wt,
                                 title="C", identifier="oxpz-c")
    assert not res.ok
    assert "non-fast-forward" in res.reason
    assert _head(repo) == before                # main NEVER left dirty/advanced


@pytest.mark.asyncio
async def test_detached_head_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = _worktree(repo, tmp_path, "feat/oxison-d", "wt-d")
    (wt / "d.py").write_text("w = 4\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "work d"], wt)
    _git(["checkout", "--detach", "HEAD"], repo)   # repo root now detached

    res = await integrate_branch(repo, branch="feat/oxison-d", worktree=wt,
                                 title="D", identifier="oxpz-d")
    assert not res.ok
    assert "detached" in res.reason


@pytest.mark.asyncio
async def test_default_branch_master_detected(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo, branch="master")          # not "main"
    wt = _worktree(repo, tmp_path, "feat/oxison-e", "wt-e")
    (wt / "e.py").write_text("v = 5\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "work e"], wt)

    res = await integrate_branch(repo, branch="feat/oxison-e", worktree=wt,
                                 title="E", identifier="oxpz-e")
    assert res.ok                               # targets master via symbolic-ref
    assert (repo / "e.py").is_file()


@pytest.mark.asyncio
async def test_backstop_refuses_protected_branch_in_place(tmp_path):
    """With the backstop armed, integrate_branch refuses to advance main in place."""
    repo = tmp_path / "repo"
    _init_repo(repo)                            # on "main"
    wt = _worktree(repo, tmp_path, "feat/oxison-p", "wt-p")
    (wt / "p.py").write_text("p = 1\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "work p"], wt)
    before = _head(repo)

    res = await integrate_branch(repo, branch="feat/oxison-p", worktree=wt,
                                 title="P", identifier="oxpz-p",
                                 protected_branches=DEFAULT_PROTECTED_BRANCHES)
    assert not res.ok
    assert "protected branch" in res.reason and "main" in res.reason
    assert _head(repo) == before                # main NEVER advanced
    assert not (repo / "p.py").exists()         # nothing composed onto main


@pytest.mark.asyncio
async def test_current_branch_resolves_and_detached_is_none(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert await current_branch(repo) == "main"
    _git(["checkout", "--detach", "HEAD"], repo)
    assert await current_branch(repo) is None


@pytest.mark.asyncio
async def test_ensure_integration_branch_creates_then_reuses(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    main_head = _head(repo)

    ok, name = await ensure_integration_branch(repo, base_branch="main")
    assert ok and name == INTEGRATION_BRANCH
    assert await current_branch(repo) == INTEGRATION_BRANCH
    assert _head(repo) == main_head             # created at the live branch's tip

    # Reuse path: back on main, ensure_integration_branch checks the existing one out.
    _git(["checkout", "main"], repo)
    ok2, name2 = await ensure_integration_branch(repo, base_branch="main")
    assert ok2 and name2 == INTEGRATION_BRANCH
    assert await current_branch(repo) == INTEGRATION_BRANCH


@pytest.mark.asyncio
async def test_ensure_integration_branch_refuses_when_stale(tmp_path):
    """A leftover integration branch that predates new commits on the live branch is
    refused, not silently reused on a stale base (would build on outdated code)."""
    repo = tmp_path / "repo"
    _init_repo(repo)                            # on "main"
    assert (await ensure_integration_branch(repo, base_branch="main"))[0]
    _git(["checkout", "main"], repo)
    # main advances past where the integration branch sits.
    (repo / "newer.py").write_text("n = 1\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "advance main"], repo)

    ok, msg = await ensure_integration_branch(repo, base_branch="main")
    assert not ok
    assert "stale" in msg and INTEGRATION_BRANCH in msg
    assert await current_branch(repo) == "main"   # never switched onto the stale branch


@pytest.mark.asyncio
async def test_redirect_composes_on_integration_branch_main_untouched(tmp_path):
    """Full option-1 flow: on main, redirect to the integration branch, compose two
    tasks onto it with the backstop armed, and main never moves."""
    repo = tmp_path / "repo"
    _init_repo(repo)                            # on "main"
    main_before = _head(repo)

    assert (await ensure_integration_branch(repo, base_branch="main"))[0]
    assert await current_branch(repo) == INTEGRATION_BRANCH

    # Task A — worker branches from HEAD (= integration branch), commits, integrates.
    wt_a = _worktree(repo, tmp_path, "feat/oxison-a", "wt-a")
    (wt_a / "a.py").write_text("x = 1\n")
    _git(["add", "-A"], wt_a)
    _git(["commit", "-qm", "work a"], wt_a)
    assert (await integrate_branch(repo, branch="feat/oxison-a", worktree=wt_a,
                                   title="A", identifier="oxpz-a",
                                   protected_branches=DEFAULT_PROTECTED_BRANCHES)).ok

    # Task B — branches from the ADVANCED integration tip (already carries A).
    wt_b = _worktree(repo, tmp_path, "feat/oxison-b", "wt-b")
    assert (wt_b / "a.py").is_file()
    (wt_b / "b.py").write_text("y = 2\n")
    _git(["add", "-A"], wt_b)
    _git(["commit", "-qm", "work b"], wt_b)
    assert (await integrate_branch(repo, branch="feat/oxison-b", worktree=wt_b,
                                   title="B", identifier="oxpz-b",
                                   protected_branches=DEFAULT_PROTECTED_BRANCHES)).ok

    # Both compose on the integration branch; main is exactly where it started.
    assert (repo / "a.py").is_file() and (repo / "b.py").is_file()
    main_after = subprocess.run(["git", "rev-parse", "main"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
    assert main_after == main_before            # main NEVER advanced
