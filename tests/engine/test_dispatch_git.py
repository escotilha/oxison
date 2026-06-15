"""Real-git integration test for diff detection (N5: committed-worker diff)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from oxison.engine.dispatch import _changed_files

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(repo):
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("hello")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "init"], repo)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.asyncio
async def test_changed_files_detects_committed_change(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    wt = tmp_path / "wt"
    _git(["worktree", "add", "-b", "feat/x", str(wt), "HEAD"], repo)
    # A worker that does what it's told: makes a change AND commits it.
    (wt / "new.py").write_text("x = 1\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "work"], wt)
    # Diffing against the captured base SHA (not HEAD) detects the committed change.
    changed = await _changed_files(wt, base)
    assert "new.py" in changed


@pytest.mark.asyncio
async def test_changed_files_detects_uncommitted_change(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    wt = tmp_path / "wt"
    _git(["worktree", "add", "-b", "feat/y", str(wt), "HEAD"], repo)
    (wt / "uncommitted.py").write_text("y = 2\n")  # not committed
    changed = await _changed_files(wt, base)
    assert "uncommitted.py" in changed


@pytest.mark.asyncio
async def test_launch_worker_uses_valid_uuid_session_id(tmp_path, monkeypatch):
    # Regression: claude requires a UUID --session-id. Passing the task
    # identifier (oxpz-...) makes the worker exit 1 before doing any work.
    import uuid

    from oxison.engine import dispatch as ed
    from oxison.engine.engconfig import EngineConfig

    repo = tmp_path / "repo"
    _init_repo(repo)
    captured: dict[str, str] = {}

    def spy_build_argv(prompt, **kw):
        captured["session_id"] = kw["session_id"]
        return ["/nonexistent-binary-xyz"]  # force a clean FileNotFoundError

    monkeypatch.setattr(ed, "build_argv", spy_build_argv)
    await ed.launch_worker(
        repo, task_identifier="oxpz-mult01", task_title="t", rationale="",
        acceptance=["x"], files_hint=[], engine_config=EngineConfig(),
        auth_mode="oauth", api_key=None, model=None,
        worktree_root=tmp_path / "wt", log_path=tmp_path / "log" / "x.log",
    )
    sid = captured["session_id"]
    assert sid != "oxpz-mult01"
    uuid.UUID(sid)  # raises if not a valid UUID


@pytest.mark.asyncio
async def test_launch_worker_wraps_in_srt_when_enabled(tmp_path, monkeypatch):
    # When sandbox is enabled, the worker argv is prepended with the srt wrapper
    # (resolved binary, not "npx") and a per-worker settings file is written.
    import asyncio

    from oxison.engine import dispatch as ed
    from oxison.engine.engconfig import EngineConfig

    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(ed, "resolve_srt_binary", lambda configured=None: "/fake/srt")

    real_cse = asyncio.create_subprocess_exec
    captured: dict[str, list] = {}

    class _Stop(Exception):
        pass

    async def spy(*argv, **kw):
        # let the real `git` worktree calls through; intercept only the worker
        if argv and (argv[0] == "/fake/srt" or "claude" in str(argv[0])):
            captured["argv"] = list(argv)
            raise _Stop()
        return await real_cse(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    with pytest.raises(_Stop):
        await ed.launch_worker(
            repo, task_identifier="oxpz-a", task_title="t", rationale="",
            acceptance=["x"], files_hint=[], engine_config=EngineConfig(sandbox_enabled=True),
            auth_mode="oauth", api_key=None, model=None,
            worktree_root=repo / "oxison-build" / "worktrees",
            log_path=repo / "oxison-build" / "logs" / "oxpz-a.log",
        )
    argv = captured["argv"]
    assert argv[0] == "/fake/srt"
    assert argv[1] == "--settings"
    assert argv[2].endswith("oxpz-a.srt.json")
    assert "npx" not in argv  # C2: invoke the binary directly, never npx
    assert (repo / "oxison-build" / "logs" / "oxpz-a.srt.json").is_file()


@pytest.mark.asyncio
async def test_launch_worker_no_srt_when_disabled(tmp_path, monkeypatch):
    import asyncio

    from oxison.engine import dispatch as ed
    from oxison.engine.engconfig import EngineConfig

    repo = tmp_path / "repo"
    _init_repo(repo)
    real_cse = asyncio.create_subprocess_exec
    captured: dict[str, list] = {}

    class _Stop(Exception):
        pass

    async def spy(*argv, **kw):
        if argv and "claude" in str(argv[0]):
            captured["argv"] = list(argv)
            raise _Stop()
        return await real_cse(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    with pytest.raises(_Stop):
        await ed.launch_worker(
            repo, task_identifier="oxpz-b", task_title="t", rationale="",
            acceptance=["x"], files_hint=[], engine_config=EngineConfig(sandbox_enabled=False),
            auth_mode="oauth", api_key=None, model=None,
            worktree_root=repo / "oxison-build" / "worktrees",
            log_path=repo / "oxison-build" / "logs" / "oxpz-b.log",
        )
    assert "/fake/srt" not in captured["argv"]
    assert "srt" not in captured["argv"][0]  # claude invoked directly, no wrapper
