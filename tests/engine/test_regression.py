"""Tests for the regression guard (engine/regression.py).

Most tests run with ``sandbox_enabled=False`` so they exercise the orchestration
(baseline once, per-task gating, green→red rule, cleanup) with trivial shell
commands and never depend on the srt binary being installed in CI. The opt-in
``sandbox_enabled=True`` integration tests at the bottom run the command under the
REAL srt binary and are skipped when it isn't installed.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from oxison.engine.engconfig import EngineConfig
from oxison.engine.regression import RegressionVerifier, run_tests_sandboxed
from oxison.engine.sandbox import resolve_srt_binary
from oxison.engine.types import DispatchOutcome

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

_SRT = resolve_srt_binary()


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


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


def _bare_cfg(test_command=None):
    return EngineConfig(sandbox_enabled=False, pre_push_test_command=test_command)


def _outcome(worktree, branch="feat/oxison-a"):
    return DispatchOutcome(
        ok=True, branch=branch, worktree_path=str(worktree), changed_files=["a.py"]
    )


# --- run_tests_sandboxed (bare mode) ---

@pytest.mark.asyncio
async def test_run_tests_passes_on_exit_zero(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    ok = await run_tests_sandboxed(
        worktree=repo, repo=repo, test_command="exit 0", engine_config=_bare_cfg(),
        srt_binary=None, settings_path=tmp_path / "s.json",
        scratch_dir=tmp_path / "scratch", log_path=tmp_path / "t.log", timeout_s=30,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_run_tests_fails_on_nonzero_exit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    ok = await run_tests_sandboxed(
        worktree=repo, repo=repo, test_command="exit 1", engine_config=_bare_cfg(),
        srt_binary=None, settings_path=tmp_path / "s.json",
        scratch_dir=tmp_path / "scratch", log_path=tmp_path / "t.log", timeout_s=30,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_run_tests_timeout_is_red(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    ok = await run_tests_sandboxed(
        worktree=repo, repo=repo, test_command="sleep 30", engine_config=_bare_cfg(),
        srt_binary=None, settings_path=tmp_path / "s.json",
        scratch_dir=tmp_path / "scratch", log_path=tmp_path / "t.log", timeout_s=0.5,
    )
    assert ok is False


# --- RegressionVerifier (baseline + green→red gating) ---

@pytest.mark.asyncio
async def test_verifier_rejects_green_to_red(tmp_path):
    # Suite passes iff a BROKEN marker is absent. Baseline (HEAD) has none → green;
    # the worker's worktree adds it → red. That green→red transition is the regression.
    repo = tmp_path / "repo"
    _init_repo(repo)
    v = RegressionVerifier(
        repo=repo, engine_config=_bare_cfg("test ! -f BROKEN"), work_dir=tmp_path / "reg"
    )
    wt = _worktree(repo, tmp_path, "feat/oxison-a", "wt-a")
    (wt / "BROKEN").write_text("x")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "break the suite"], wt)

    verdict = await v.check(_outcome(wt))
    assert not verdict.ok
    assert verdict.failure_class == "regression"
    await v.cleanup()


@pytest.mark.asyncio
async def test_verifier_accepts_clean_change(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    v = RegressionVerifier(
        repo=repo, engine_config=_bare_cfg("test ! -f BROKEN"), work_dir=tmp_path / "reg"
    )
    wt = _worktree(repo, tmp_path, "feat/oxison-a", "wt-a")
    (wt / "ok.py").write_text("x = 1\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "harmless change"], wt)

    verdict = await v.check(_outcome(wt))
    assert verdict.ok
    await v.cleanup()


@pytest.mark.asyncio
async def test_verifier_inactive_when_baseline_already_red(tmp_path):
    # A repo whose suite is red at HEAD must not have the guard fail every task.
    repo = tmp_path / "repo"
    _init_repo(repo)
    v = RegressionVerifier(
        repo=repo, engine_config=_bare_cfg("exit 1"), work_dir=tmp_path / "reg"
    )
    wt = _worktree(repo, tmp_path, "feat/oxison-a", "wt-a")
    (wt / "whatever.py").write_text("x = 1\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-qm", "change"], wt)

    verdict = await v.check(_outcome(wt))
    assert verdict.ok  # baseline red → guard inactive, accept
    await v.cleanup()


@pytest.mark.asyncio
async def test_verifier_confirms_before_rejecting_flaky(tmp_path, monkeypatch):
    # baseline green, first post red, confirm green → a flaky/transient red must
    # NOT fail a legitimate change.
    repo = tmp_path / "repo"
    _init_repo(repo)
    v = RegressionVerifier(
        repo=repo, engine_config=_bare_cfg("true"), work_dir=tmp_path / "reg"
    )
    wt = _worktree(repo, tmp_path, "feat/oxison-a", "wt-a")
    seq = iter([True, False, True])  # _run order: baseline, post, confirm

    async def fake_run(worktree, *, label):
        return next(seq)

    monkeypatch.setattr(v, "_run", fake_run)
    verdict = await v.check(_outcome(wt))
    assert verdict.ok
    await v.cleanup()


@pytest.mark.asyncio
async def test_verifier_rejects_deterministic_regression_after_confirm(tmp_path, monkeypatch):
    # baseline green, post red, confirm STILL red → a real (deterministic)
    # regression survives the confirm re-run and is rejected.
    repo = tmp_path / "repo"
    _init_repo(repo)
    v = RegressionVerifier(
        repo=repo, engine_config=_bare_cfg("true"), work_dir=tmp_path / "reg"
    )
    wt = _worktree(repo, tmp_path, "feat/oxison-a", "wt-a")
    seq = iter([True, False, False])  # baseline, post, confirm

    async def fake_run(worktree, *, label):
        return next(seq)

    monkeypatch.setattr(v, "_run", fake_run)
    verdict = await v.check(_outcome(wt))
    assert not verdict.ok
    assert verdict.failure_class == "regression"
    await v.cleanup()


@pytest.mark.asyncio
async def test_verifier_baseline_runs_once(tmp_path, monkeypatch):
    # The baseline suite is established exactly once across many graded tasks.
    repo = tmp_path / "repo"
    _init_repo(repo)
    v = RegressionVerifier(
        repo=repo, engine_config=_bare_cfg("test ! -f BROKEN"), work_dir=tmp_path / "reg"
    )
    calls = {"baseline": 0}
    real_run = v._run

    async def counting_run(worktree, *, label):
        if label == "_baseline":
            calls["baseline"] += 1
        return await real_run(worktree, label=label)

    monkeypatch.setattr(v, "_run", counting_run)

    for name in ("wt-a", "wt-b", "wt-c"):
        wt = _worktree(repo, tmp_path, f"feat/oxison-{name}", name)
        (wt / f"{name}.py").write_text("x = 1\n")
        _git(["add", "-A"], wt)
        _git(["commit", "-qm", name], wt)
        assert (await v.check(_outcome(wt, branch=f"feat/oxison-{name}"))).ok

    assert calls["baseline"] == 1
    await v.cleanup()


# --- container workspace model: check() works against a self-contained clone ---

@pytest.mark.asyncio
async def test_verifier_works_against_self_contained_clone(tmp_path):
    # In --sandbox-layer container, the worker's workspace is a `git clone`
    # (self-contained .git), NOT a linked worktree, and outcome.worktree_path is
    # that clone on the host. Prove check() is workspace-model-agnostic: baseline
    # (main repo HEAD) green, clone with a BROKEN marker → red → rejected.
    repo = tmp_path / "repo"
    _init_repo(repo)
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", "-q", str(repo), str(clone)],
        check=True, capture_output=True,
    )
    _git(["config", "user.email", "t@t"], clone)
    _git(["config", "user.name", "t"], clone)
    (clone / "BROKEN").write_text("x")
    _git(["add", "-A"], clone)
    _git(["commit", "-qm", "break the suite in the clone"], clone)

    v = RegressionVerifier(
        repo=repo, engine_config=_bare_cfg("test ! -f BROKEN"), work_dir=tmp_path / "reg"
    )
    outcome = DispatchOutcome(
        ok=True, branch="feat/oxison-c", worktree_path=str(clone), changed_files=["BROKEN"]
    )
    verdict = await v.check(outcome)
    assert not verdict.ok
    assert verdict.failure_class == "regression"
    await v.cleanup()


# --- opt-in integration: the REAL srt binary actually runs + confines the test ---

@pytest.mark.skipif(_SRT is None, reason="srt binary not installed")
@pytest.mark.asyncio
async def test_real_srt_runs_command_and_reports_exit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    cfg = EngineConfig(sandbox_enabled=True)  # real srt wrapping

    ok = await run_tests_sandboxed(
        worktree=repo, repo=repo, test_command="exit 0", engine_config=cfg, srt_binary=_SRT,
        settings_path=tmp_path / "s1.json", scratch_dir=tmp_path / "scratch1",
        log_path=tmp_path / "l1.log", timeout_s=60,
    )
    assert ok is True

    bad = await run_tests_sandboxed(
        worktree=repo, repo=repo, test_command="exit 3", engine_config=cfg, srt_binary=_SRT,
        settings_path=tmp_path / "s2.json", scratch_dir=tmp_path / "scratch2",
        log_path=tmp_path / "l2.log", timeout_s=60,
    )
    assert bad is False


@pytest.mark.skipif(_SRT is None, reason="srt binary not installed")
@pytest.mark.asyncio
async def test_real_srt_blocks_write_outside_worktree(tmp_path):
    # Confinement proof: a test command that writes OUTSIDE the worktree (+ scratch)
    # is denied by srt → the command fails (red) and the file is never created.
    repo = tmp_path / "repo"
    _init_repo(repo)
    escape = tmp_path / "escape_marker"  # not under worktree / scratch / ~/.claude
    cfg = EngineConfig(sandbox_enabled=True)

    ok = await run_tests_sandboxed(
        worktree=repo, repo=repo, test_command=f"echo pwned > {escape}",
        engine_config=cfg, srt_binary=_SRT,
        settings_path=tmp_path / "s.json", scratch_dir=tmp_path / "scratch",
        log_path=tmp_path / "l.log", timeout_s=60,
    )
    assert ok is False
    assert not escape.exists()
