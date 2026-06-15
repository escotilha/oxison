"""Real-git tests for the sequential-integration merge step (integrate_branch)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from oxison.engine.integrate import integrate_branch

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
