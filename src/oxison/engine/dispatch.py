"""Write-worker dispatch — implement one task in an isolated git worktree.

Unlike Phase-1's read-only ``oxison.dispatch.invoke`` (bounded, in-memory event
drain), an Oxfaz build worker is **unbounded** and **writes code**, so this
module:

* launches the worker with ``ToolSet.FULL_WRITE`` (the only path to write tools
  in engine code — the C2 chokepoint in ``engine.invoke``);
* runs it in a **fresh git worktree** on its own branch, so parallel workers
  never collide on the working tree;
* streams stdout+stderr to a **log file, never a PIPE** (D2 — an unbounded
  worker would deadlock a fixed OS pipe buffer);
* reports the actual changed files so the grader can judge the real diff.

The pure helpers (prompt construction, porcelain parsing) are unit-tested; the
full ``launch_worker`` is integration-only (needs ``git`` + ``claude``) and is
faked in the loop's tests, per the build-engine plan's test strategy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path

from oxison.dispatch import generate_session_id

from .engconfig import EngineConfig
from .invoke import ToolSet, build_argv, build_env, kill_process_group
from .sandbox import (
    DEFAULT_SANDBOX_DOMAINS,
    build_srt_settings,
    resolve_srt_binary,
    srt_wrap,
    write_srt_settings,
)

#: A generous default wall-clock cap for one write worker.
DEFAULT_WORKER_TIMEOUT_S = 30 * 60.0


@dataclass
class DispatchOutcome:
    """Result of one write-worker run."""

    ok: bool
    branch: str
    worktree_path: str
    changed_files: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    timed_out: bool = False
    adapter_failure: bool = False
    error: str | None = None
    log_path: str | None = None


def build_worker_prompt(task_title: str, *, rationale: str, acceptance: list[str],
                        files_hint: list[str], repo_name: str) -> str:
    """The instruction a write worker receives to implement one task.

    Encodes the acceptance criteria as the definition of done — the worker is
    driven by the same observable end-states the plan-gate required, so "done"
    is checkable rather than vibes.
    """
    accept = "\n".join(f"- {a}" for a in acceptance) or "- (none specified)"
    hints = ", ".join(files_hint) if files_hint else "(use your judgment)"
    return (
        "You are an Oxfaz build worker implementing ONE task in a git worktree "
        f"of the project `{repo_name}`. You have full read/write tools.\n\n"
        f"TASK: {task_title}\n"
        f"WHY: {rationale}\n\n"
        "DONE means ALL of these acceptance criteria hold (verify each before "
        "you finish):\n"
        f"{accept}\n\n"
        f"Likely files to touch: {hints}\n\n"
        "Rules:\n"
        "- Implement the task and make it actually work; run the project's "
        "tests/build to verify before finishing.\n"
        "- Do NOT touch CI config, .env, lockfiles, .git/, or oxison-build/.\n"
        "- Keep the change focused on this task; do not refactor unrelated code.\n"
        "- Commit your work with a clear message when the acceptance criteria pass."
    )


def parse_changed_files(porcelain: str) -> list[str]:
    """Parse ``git status --porcelain`` output into a list of changed paths.

    Handles renames (``R  old -> new`` → the new path) and quoted paths.
    """
    files: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        if " -> " in rest:  # rename/copy
            rest = rest.split(" -> ", 1)[1]
        files.append(rest.strip().strip('"'))
    return files


async def _git(args: list[str], *, cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=os.fspath(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, (out or err).decode("utf-8", errors="replace")


async def _changed_files(worktree: Path, base_sha: str) -> list[str]:
    """All files the worker changed vs. the worktree's base commit.

    Unions uncommitted changes (``status --porcelain``) with committed ones
    (``diff base..HEAD``). Diffing against the captured *base* SHA — not
    ``HEAD`` — is what lets a worker that **commits** its work still have its
    changes detected (after a commit, ``diff HEAD`` is empty).
    """
    rc1, porcelain = await _git(["status", "--porcelain"], cwd=worktree)
    files = set(parse_changed_files(porcelain))
    rc2, committed = await _git(["diff", "--name-only", base_sha, "HEAD"], cwd=worktree)
    if rc2 == 0:
        files.update(f for f in committed.splitlines() if f.strip())
    return sorted(files)


def _extract_cost_from_log(log_path: Path) -> float:
    """Pull ``total_cost_usd`` from the worker's stream-json ``result`` event."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(evt, dict) and evt.get("type") == "result":
            return float(evt.get("total_cost_usd", 0.0))
    return 0.0


async def launch_worker(
    repo: Path,
    *,
    task_identifier: str,
    task_title: str,
    rationale: str,
    acceptance: list[str],
    files_hint: list[str],
    engine_config: EngineConfig,
    auth_mode: str,
    api_key: str | None,
    model: str | None,
    worktree_root: Path,
    log_path: Path,
    timeout_s: float = DEFAULT_WORKER_TIMEOUT_S,
) -> DispatchOutcome:
    """Create a worktree, run a write worker in it, return the outcome.

    Routes to Layer 2 (container) when ``engine_config.sandbox_layer ==
    "container"`` — that path runs the worker in a rootless container against a
    self-contained clone, so it has a different workspace model and is delegated
    to ``container.launch_worker_container``.
    """
    branch = f"{engine_config.branch_prefix}{task_identifier}"

    if engine_config.sandbox_enabled and engine_config.sandbox_layer == "container":
        from .container import launch_worker_container, resolve_container_runtime
        runtime = resolve_container_runtime(engine_config.container_runtime)
        if runtime is None:
            return DispatchOutcome(
                ok=False, branch=branch, worktree_path="", adapter_failure=True,
                error="container sandbox enabled but no runtime (podman/docker) found",
            )
        return await launch_worker_container(
            repo, task_identifier=task_identifier, task_title=task_title,
            rationale=rationale, acceptance=acceptance, files_hint=files_hint,
            engine_config=engine_config, api_key=api_key, model=model,
            runtime=runtime, image=engine_config.worker_image,
            clone_root=worktree_root.parent / "containers", log_path=log_path,
            timeout_s=timeout_s,
        )

    worktree = worktree_root / task_identifier
    worktree_root.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove any stale worktree+branch from a prior attempt. Both names are
    # deterministic per task id, so without this a re-dispatch would fail on
    # "branch already exists" — which looks like an engine outage and would
    # re-queue the task forever. Errors are ignored (nothing to clean is fine).
    await _git(["worktree", "remove", "--force", os.fspath(worktree)], cwd=repo)
    await _git(["worktree", "prune"], cwd=repo)
    await _git(["branch", "-D", branch], cwd=repo)

    # Capture the base commit so a worker that COMMITS still has its diff seen.
    # A failure here is a git/engine problem (not the task's fault): routing it
    # through adapter_failure gives a free retry + a clear error, instead of
    # silently using "HEAD" and surfacing the opaque "worker produced no changes".
    rc_base, base_out = await _git(["rev-parse", "HEAD"], cwd=repo)
    if rc_base != 0 or not base_out.strip():
        return DispatchOutcome(
            ok=False, branch=branch, worktree_path=str(worktree),
            adapter_failure=True,
            error=f"git rev-parse HEAD failed in {repo}: {base_out.strip()[:200]}",
        )
    base_sha = base_out.strip()

    rc, msg = await _git(["worktree", "add", "-b", branch, os.fspath(worktree), "HEAD"], cwd=repo)
    if rc != 0:
        # Couldn't even create the worktree — an engine/infra problem, not the
        # task's fault, so don't burn a retry.
        return DispatchOutcome(
            ok=False, branch=branch, worktree_path=str(worktree),
            adapter_failure=True, error=f"git worktree add failed: {msg.strip()[:300]}",
        )

    prompt = build_worker_prompt(
        task_title, rationale=rationale, acceptance=acceptance,
        files_hint=files_hint, repo_name=repo.name,
    )
    argv = build_argv(
        prompt, tool_set=ToolSet.FULL_WRITE, auth_mode=auth_mode, model=model,
        max_budget_usd=engine_config.worker_max_budget_usd,
        # claude requires a UUID session id — the task identifier (oxpz-...) is
        # NOT a valid UUID and makes the worker exit 1 before doing any work.
        session_id=generate_session_id(),
    )
    env = build_env(api_key=api_key, extra=dict(engine_config.provider_env))

    # Layer-1 sandbox: wrap the worker in srt so its writes are confined to the
    # worktree (+ scoped .git + ~/.claude) and its egress to the allowlist. The
    # settings file is written by the PARENT — srt reads it before sandboxing, so
    # it needs no allowlist entry. cwd stays the worktree; prompt stays positional.
    if engine_config.sandbox_enabled:
        srt_binary = resolve_srt_binary(engine_config.srt_binary)
        if srt_binary is None:
            return DispatchOutcome(
                ok=False, branch=branch, worktree_path=str(worktree),
                adapter_failure=True,
                error="sandbox enabled but srt not found "
                "(install: npm i -g @anthropic-ai/sandbox-runtime, or pass --no-sandbox)",
            )
        # Per-worker scratch dir (NOT the shared system $TMPDIR) so the worker
        # has writable temp without us allowlisting all of /tmp; point its
        # TMPDIR at it so node/claude scratch lands there.
        scratch = worktree_root.parent / "tmp" / task_identifier
        scratch.mkdir(parents=True, exist_ok=True)
        env = {**env, "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch)}
        settings = build_srt_settings(
            worktree=worktree, repo=repo, task_identifier=task_identifier,
            home=Path.home(),
            allowed_domains=engine_config.sandbox_allowed_domains or DEFAULT_SANDBOX_DOMAINS,
            extra_write_paths=engine_config.sandbox_extra_write_paths,
            tmpdir=str(scratch),
        )
        settings_path = log_path.parent / f"{task_identifier}.srt.json"
        write_srt_settings(settings_path, settings)
        argv = srt_wrap(srt_binary, settings_path, argv)

    timed_out = False
    try:
        with open(log_path, "wb") as logf:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=os.fspath(worktree), env=env,
                stdout=logf, stderr=logf, stdin=asyncio.subprocess.DEVNULL,
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
                    # Bound this wait too: a process wedged in uninterruptible
                    # (D-state) sleep can ignore even SIGKILL, and a bare
                    # `await proc.wait()` would hang the event loop forever. Mirror
                    # the container path's guarded teardown.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except FileNotFoundError as exc:
        # The claude binary isn't installed — an engine outage, not a task fault.
        return DispatchOutcome(
            ok=False, branch=branch, worktree_path=str(worktree),
            adapter_failure=True, error=f"worker binary not found: {exc}",
            log_path=str(log_path),
        )

    exit_code = proc.returncode
    changed = await _changed_files(worktree, base_sha)
    cost = engine_config.worker_max_budget_usd if timed_out else _extract_cost_from_log(log_path)
    ok = (not timed_out) and exit_code == 0 and bool(changed)
    error = None
    if timed_out:
        error = f"worker timed out after {timeout_s:.0f}s"
    elif exit_code != 0:
        error = f"worker exited {exit_code}"
    elif not changed:
        error = "worker produced no changes"
    return DispatchOutcome(
        ok=ok, branch=branch, worktree_path=str(worktree), changed_files=changed,
        cost_usd=cost, timed_out=timed_out, error=error, log_path=str(log_path),
    )


__all__ = [
    "DEFAULT_WORKER_TIMEOUT_S",
    "DispatchOutcome",
    "build_worker_prompt",
    "launch_worker",
    "parse_changed_files",
]
