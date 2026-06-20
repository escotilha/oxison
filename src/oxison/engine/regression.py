"""The regression guard — run the project's tests, gate green→red transitions.

The structural grader (:mod:`engine.gates`) inspects a worker's diff but never
runs it. This module supplies the *behavioural* evidence the grader's
:func:`~engine.gates.grade_regression` decides on: it runs the host project's
own test command (``EngineConfig.pre_push_test_command``) and reports pass/fail.

Two invariants shape the design:

* **The test command runs under the SAME srt sandbox as the build worker.** A
  project's test suite is untrusted code — a prompt-injected worker could even
  have *written* the failing test — so the engine must never run it with its own
  privileges. We reuse :func:`engine.sandbox.build_srt_settings` verbatim: writes
  confined to the worktree (+ scoped ``.git``), credentials denied, egress
  allowlisted. ``--no-sandbox`` runs the command bare, matching the worker's own
  trust posture in that mode.

* **Only a green→red transition is a regression.** The baseline suite is run
  ONCE per build run (the repo ``HEAD`` is stable across a run — default mode is
  DB-only and ``--integrate`` composes onto a side branch, never advancing
  ``HEAD``); a repo whose suite is already red (or whose baseline can't be
  established) leaves the guard inactive rather than failing every task. That
  fail-*open*-for-the-guard choice is deliberate: the guard is an opt-in extra
  net, so an infra hiccup or a pre-existing red suite must never block a build.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from pathlib import Path

from .engconfig import EngineConfig
from .gates import GradeVerdict, grade_regression
from .gitutil import git_cmd
from .invoke import build_env, kill_process_group
from .sandbox import (
    DEFAULT_SANDBOX_DOMAINS,
    build_srt_settings,
    resolve_srt_binary,
    srt_wrap,
    write_srt_settings,
)
from .types import DispatchOutcome


def _append_log(log_path: Path, message: str) -> None:
    """Best-effort diagnostic line into the regression run's log."""
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(message.rstrip("\n") + "\n")


async def run_tests_sandboxed(
    *,
    worktree: Path,
    repo: Path,
    test_command: str,
    engine_config: EngineConfig,
    srt_binary: str | None,
    settings_path: Path,
    scratch_dir: Path,
    log_path: Path,
    timeout_s: float,
) -> bool:
    """Run ``test_command`` inside ``worktree``; return True iff it exits 0.

    Wrapped in srt when ``engine_config.sandbox_enabled`` (same profile as the
    build worker), bare otherwise. A timeout kills the whole process group and
    counts as red. Output (stdout+stderr) is captured to ``log_path``; the
    per-run scratch dir is removed on every exit path.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # A user-provided command is a shell string ("pytest -q", "npm test &&
    # ..."), so run it through /bin/sh; srt confines the whole process tree.
    inner = ["/bin/sh", "-lc", test_command]

    if engine_config.sandbox_enabled:
        if srt_binary is None:
            # Preflight resolves srt for the worker, so this is unexpected; fail
            # closed (red) rather than silently running the suite unsandboxed.
            _append_log(log_path, "regression: sandbox enabled but srt binary unavailable")
            return False
        settings = build_srt_settings(
            worktree=worktree,
            repo=repo,
            task_identifier="regression",
            home=Path.home(),
            allowed_domains=engine_config.sandbox_allowed_domains or DEFAULT_SANDBOX_DOMAINS,
            extra_write_paths=engine_config.sandbox_extra_write_paths,
            tmpdir=str(scratch_dir),
        )
        write_srt_settings(settings_path, settings)
        argv = srt_wrap(srt_binary, settings_path, inner)
    else:
        argv = inner

    # Build the SAME whitelisted child env the build worker gets (base whitelist ∩
    # parent env, NO credentials) — never raw os.environ. Untrusted project test
    # code must not receive ambient secrets (provider keys like XAI_API_KEY,
    # GITHUB_TOKEN, AWS_*, DB URLs); the srt egress allowlist permits github/pypi/npm
    # and so is NOT a credential boundary. This is parity with the worker
    # (engine/dispatch.launch_worker → build_env). TMPDIR → sandbox-writable scratch.
    env = build_env(
        api_key=None,
        extra={"TMPDIR": str(scratch_dir), "TMP": str(scratch_dir), "TEMP": str(scratch_dir)},
    )

    timed_out = False
    proc: asyncio.subprocess.Process | None = None
    try:
        with open(log_path, "ab") as logf:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=os.fspath(worktree),
                env=env,
                stdout=logf,
                stderr=logf,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                pgid: int | None = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                kill_process_group(proc, pgid, signal.SIGTERM)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                if proc.returncode is None:
                    kill_process_group(proc, pgid, signal.SIGKILL)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except FileNotFoundError as exc:
        # /bin/sh or the srt binary missing — an engine/infra problem, not a
        # test failure. Fail closed (red); the verifier's baseline handling turns
        # an infra-failed baseline into "guard inactive", so this never wrongly
        # rejects a task on its own.
        _append_log(log_path, f"regression: could not launch test command: {exc}")
        return False
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    if timed_out:
        _append_log(
            log_path,
            f"regression: test command exceeded {timeout_s:.0f}s — killed, treated as red",
        )
        return False
    return proc is not None and proc.returncode == 0


class RegressionVerifier:
    """Stateful grader stage: establish a baseline once, then gate each task.

    Construct only when ``engine_config.pre_push_test_command`` is set. The
    baseline test run is lazy + lock-guarded so it happens exactly once even with
    ``max_workers > 1``; per-task runs proceed concurrently in their own
    worktrees. ``cleanup()`` removes the baseline worktree at build teardown.
    """

    def __init__(
        self,
        *,
        repo: Path,
        engine_config: EngineConfig,
        work_dir: Path,
        srt_binary: str | None = None,
    ) -> None:
        if not engine_config.pre_push_test_command:
            raise ValueError("RegressionVerifier requires engine_config.pre_push_test_command")
        self._repo = repo
        self._cfg = engine_config
        self._work_dir = work_dir
        self._test_command = engine_config.pre_push_test_command
        # Resolve srt once (the worker preflight already validated it exists when
        # the sandbox is on); bare mode needs no binary.
        self._srt = srt_binary
        if self._srt is None and engine_config.sandbox_enabled:
            self._srt = resolve_srt_binary(engine_config.srt_binary)
        self._baseline_green: bool | None = None
        self._baseline_wt: Path | None = None
        self._lock = asyncio.Lock()

    async def _run(self, worktree: Path, *, label: str) -> bool:
        # Scope the srt write-allowlist to the RIGHT git dir. A *linked* worktree
        # (srt mode) has a ``.git`` FILE pointing into the main repo, so it
        # genuinely needs the main repo's ``.git`` writable → ``repo=self._repo``.
        # A *self-contained clone* (container mode) has its own ``.git`` DIR inside
        # the worktree and must NOT get write access to the main repo's ``.git`` —
        # that cross-repo write is exactly the isolation the container layer exists
        # to provide, and the test code here is untrusted. So scope to the clone
        # itself (its ``.git`` is already inside ``worktree``, so the deny-write
        # carve-outs for ``config``/``hooks`` still apply).
        effective_repo = worktree if (worktree / ".git").is_dir() else self._repo
        return await run_tests_sandboxed(
            worktree=worktree,
            repo=effective_repo,
            test_command=self._test_command,
            engine_config=self._cfg,
            srt_binary=self._srt,
            settings_path=self._work_dir / f"{label}.regression.srt.json",
            scratch_dir=self._work_dir / "tmp" / label,
            log_path=self._work_dir / f"{label}.regression.log",
            timeout_s=float(self._cfg.regression_timeout_seconds),
        )

    async def _ensure_baseline(self) -> bool:
        async with self._lock:
            if self._baseline_green is not None:
                return self._baseline_green
            self._work_dir.mkdir(parents=True, exist_ok=True)
            wt = self._work_dir / "_baseline"
            # Drop any stale baseline worktree from a prior run (deterministic path).
            await git_cmd(["worktree", "remove", "--force", os.fspath(wt)], cwd=self._repo)
            await git_cmd(["worktree", "prune"], cwd=self._repo)
            rc, msg = await git_cmd(
                ["worktree", "add", "--detach", os.fspath(wt), "HEAD"], cwd=self._repo
            )
            if rc != 0:
                # Can't establish a baseline → leave the guard inactive (treat as
                # red), so an infra hiccup doesn't fail every task. Loud, not silent.
                _append_log(
                    self._work_dir / "_baseline.regression.log",
                    f"regression: could not create baseline worktree (rc={rc}): {msg.strip()[:300]}"
                    " — regression guard INACTIVE for this run",
                )
                self._baseline_green = False
                return False
            self._baseline_wt = wt
            green = await self._run(wt, label="_baseline")
            if not green:
                _append_log(
                    self._work_dir / "_baseline.regression.log",
                    "regression: baseline test suite is RED — guard INACTIVE"
                    " (only a green→red transition is gated)",
                )
            self._baseline_green = green
            return green

    @staticmethod
    def _label(outcome: DispatchOutcome) -> str:
        # Prefer the task-id worktree dir name; fall back to a filesystem-safe
        # branch slug so two empty-name outcomes can't share a settings/scratch/log
        # path — the srt settings file is a confinement input and must never be
        # clobbered by a concurrent run.
        return Path(outcome.worktree_path).name or outcome.branch.replace("/", "_") or "task"

    async def check(self, outcome: DispatchOutcome) -> GradeVerdict:
        """Grade one worker's worktree against the cached baseline."""
        baseline_green = await self._ensure_baseline()
        if not baseline_green:
            # Guard inactive (baseline red or unavailable): accept without
            # spending a per-task run — grade_regression ignores post in this case.
            return grade_regression(baseline_green=False, post_green=False)
        worktree = Path(outcome.worktree_path)
        label = self._label(outcome)
        post_green = await self._run(worktree, label=label)
        if not post_green:
            # Confirm before rejecting. A flaky suite, or a transient slow/timeout
            # run under worker concurrency, must not fail a legitimate change — and
            # a real regression is deterministic, so it stays red on the re-run.
            post_green = await self._run(worktree, label=f"{label}-confirm")
        return grade_regression(baseline_green=True, post_green=post_green)

    async def cleanup(self) -> None:
        """Remove the baseline worktree (fail-soft) at build teardown."""
        if self._baseline_wt is not None:
            await git_cmd(
                ["worktree", "remove", "--force", os.fspath(self._baseline_wt)], cwd=self._repo
            )
            await git_cmd(["worktree", "prune"], cwd=self._repo)
            self._baseline_wt = None


__all__ = ["RegressionVerifier", "run_tests_sandboxed"]
