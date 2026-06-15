"""Subprocess wrapper for ``claude -p`` — oxison's only AI entrypoint.

This is a deliberately trimmed reimplementation of oxi-core's
``dispatch_invoke.py``. It keeps every hard part that exists because of
a documented prior incident, and drops everything oxison doesn't need
(SSH/multi-host, retry/escalation, rate-limit model-fallback, task-DB
coupling). oxison runs one host, runs each step once, and surfaces
failures rather than retrying.

KEPT (do not remove without understanding the incident behind it):

1.  **Process-group isolation** (``start_new_session=True``). Survives
    claude-code's Bash-tool SIGTERM reaching the process group.
2.  **Concurrent stdout + stderr drain.** A 64KB OS pipe buffer
    deadlocks the child if only one stream is drained.
3.  **1MB StreamReader limit.** A single stream-json event (tool result
    with file contents) overruns the default 64KB.
4.  **Env whitelist.** Nested claude inherits ``CLAUDE_*``/``ANTHROPIC_*``
    from the parent and silently overrides per-call settings; also a
    secrets boundary.
5.  **Truncated-JSON tolerance.** On SIGKILL the last stdout line may be
    partial — swallow ``JSONDecodeError`` on the trailing line only.
6.  **Wall-clock timeout** with ``os.killpg`` escalation to SIGKILL.
7.  **Cost extraction** from the ``result`` event's ``total_cost_usd``.
8.  **argv-form spawn** (the list-of-args form) — never a shell string —
    so a prompt containing shell metacharacters can never be
    interpreted by a shell.

DROPPED vs oxi-core: ssh wrapping, retry taxonomy, rate-limit
exhaustion detection, ``--max-turns`` (removed from the Claude CLI in
2.1.161 — budget is bounded by ``--max-budget-usd``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import RunConfig

#: 1 MB — large enough for a realistic tool-result stream-json event.
STREAM_READER_LIMIT = 1024 * 1024

EXIT_SUCCESS = 0

#: Env vars passed through to the child. Anything else is stripped.
_BASE_ENV_WHITELIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
    }
)


@dataclass
class InvokeResult:
    """Outcome of one ``claude -p`` call."""

    ok: bool
    text: str
    cost_usd: float
    exit_code: int | None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False


def generate_session_id() -> str:
    return str(uuid.uuid4())


def build_env(*, api_key: str | None, whitelist: Sequence[str] = ()) -> dict[str, str]:
    """Compose a whitelisted child env.

    Starts from the base whitelist intersected with the parent env. The
    API key is injected only when supplied (bare mode); in OAuth mode it
    is None and the child uses the host's Claude Code credential store
    (discoverable via the whitelisted ``HOME``).
    """
    allowed = set(_BASE_ENV_WHITELIST) | set(whitelist)
    env = {k: v for k, v in os.environ.items() if k in allowed}
    if api_key is not None:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


def build_argv(
    prompt: str,
    *,
    allowed_tools: Sequence[str],
    auth_mode: str,
    model: str | None,
    max_budget_usd: float | None,
    session_id: str,
    binary: str = "claude",
) -> list[str]:
    """Build the exact argv for a ``claude -p`` invocation.

    Public so tests can assert on flags without spawning a process.
    ``--bare`` is added in bare mode; omitted in OAuth mode so the
    binary's normal credential path runs. ``allowed_tools`` is always
    the read-only set for comprehension/generation — the safety
    invariant is enforced by callers passing ``READ_ONLY_TOOLS``.
    """
    argv = [binary]
    if auth_mode == "bare":
        argv.append("--bare")
    argv.extend(
        [
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            ",".join(allowed_tools),
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--no-session-persistence",
            "--session-id",
            session_id,
            "--exclude-dynamic-system-prompt-sections",
        ]
    )
    if max_budget_usd is not None:
        argv.extend(["--max-budget-usd", f"{max_budget_usd:.2f}"])
    if model:
        argv.extend(["--model", model])
    argv.extend(["-p", prompt])
    return argv


def _extract_text(events: list[dict[str, Any]]) -> str:
    """Pull the final result text from stream-json events.

    Prefer the ``result`` event's ``result`` field; fall back to the
    last assistant text block.
    """
    for evt in events:
        if evt.get("type") == "result":
            result = evt.get("result")
            if isinstance(result, str):
                return result
    for evt in reversed(events):
        if evt.get("type") == "assistant":
            msg = evt.get("message", {})
            blocks = msg.get("content", []) if isinstance(msg, dict) else []
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""


def _extract_cost(events: list[dict[str, Any]]) -> float:
    for evt in events:
        if evt.get("type") == "result":
            return float(evt.get("total_cost_usd", 0.0))
    return 0.0


#: Exact reason string for a budget-cap failure — the caller keys on this (not a
#: substring) to decide whether to append the "raise --max-budget-usd" hint.
BUDGET_FAILURE_REASON = "exceeded the --max-budget-usd cost cap"


def _extract_failure_reason(events: list[dict[str, Any]]) -> str | None:
    """Pull a human-readable failure reason from the final ``result`` event.

    ``claude`` reports budget / turn-limit failures in the stream-json
    ``result`` event (``is_error`` + ``subtype``), NOT on stderr — so an exit-1
    with empty stderr still carries the real reason here. Returns ``None`` when
    no error result is present (caller falls back to the exit code + stderr).
    """
    for evt in events:
        if evt.get("type") == "result" and evt.get("is_error"):
            subtype = str(evt.get("subtype") or "")
            if subtype == "error_max_budget_usd":
                return BUDGET_FAILURE_REASON
            if subtype == "error_max_turns":
                return "hit the turn limit"
            detail = evt.get("result")
            if isinstance(detail, str) and detail.strip():
                return f"{subtype or 'error'}: {detail.strip()[:200]}"
            return subtype or "claude reported an error"
    return None


def _kill_process_group(
    proc: asyncio.subprocess.Process, pgid: int | None, sig: int = signal.SIGTERM
) -> None:
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


async def invoke(
    prompt: str,
    *,
    cfg: RunConfig,
    allowed_tools: Sequence[str],
    cwd: Path,
    timeout_s: float,
    binary: str = "claude",
) -> InvokeResult:
    """Spawn ``claude -p`` in ``cwd``, stream events, return the outcome.

    The whole subprocess lifecycle: spawn in a new process group, drain
    stdout+stderr concurrently, enforce a wall-clock timeout, classify.
    ``cwd`` is the target repo so Read/Glob/Grep resolve against it.
    """
    session_id = generate_session_id()
    argv = build_argv(
        prompt,
        allowed_tools=allowed_tools,
        auth_mode=cfg.auth_mode,
        model=cfg.model,
        max_budget_usd=cfg.max_budget_usd,
        session_id=session_id,
        binary=binary,
    )
    env = build_env(api_key=cfg.api_key)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=os.fspath(cwd.resolve()),
        env=env,
        start_new_session=True,
        limit=STREAM_READER_LIMIT,
    )

    try:
        pgid: int | None = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None

    events: list[dict[str, Any]] = []
    trailing_line: str | None = None
    stderr_chunks: list[bytes] = []

    async def drain_stdout() -> None:
        nonlocal trailing_line
        if proc.stdout is None:
            return
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                stderr_chunks.append(b"oxison: stream-json line exceeded 1MB limit\n")
                continue
            if not line:
                return
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            if not decoded:
                continue
            try:
                events.append(json.loads(decoded))
            except json.JSONDecodeError:
                trailing_line = decoded

    async def drain_stderr() -> None:
        if proc.stderr is None:
            return
        cap = 64 * 1024
        collected = 0
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            if collected < cap:
                room = cap - collected
                stderr_chunks.append(chunk[:room])
                collected += min(len(chunk), room)

    drain_task = asyncio.gather(drain_stdout(), drain_stderr(), return_exceptions=True)

    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.shield(_wait_for_exit(proc, drain_task)), timeout=timeout_s
        )
    except TimeoutError:
        timed_out = True
        _kill_process_group(proc, pgid)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(drain_task, timeout=5.0)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            _kill_process_group(proc, pgid, signal.SIGKILL)
            await proc.wait()

    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    exit_code = proc.returncode
    ok = (not timed_out) and exit_code == EXIT_SUCCESS
    error: str | None = None
    if timed_out:
        error = f"timed out after {timeout_s:.0f}s"
    elif not ok:
        reason = _extract_failure_reason(events)
        if reason == BUDGET_FAILURE_REASON:
            cap = (
                f" (currently ${cfg.max_budget_usd:.2f}); raise it with --max-budget-usd"
                if cfg.max_budget_usd is not None
                else "; raise it with --max-budget-usd"
            )
            error = f"claude {reason}{cap}"
        elif reason:
            error = f"claude failed: {reason} (exit {exit_code})"
        else:
            # No error result event — surface the exit code + whatever stderr held.
            error = f"claude exited {exit_code}: {stderr_text.strip()[:500]}"

    return InvokeResult(
        ok=ok,
        text=_extract_text(events),
        cost_usd=_extract_cost(events),
        exit_code=exit_code,
        error=error,
        events=events,
        timed_out=timed_out,
    )


async def _wait_for_exit(
    proc: asyncio.subprocess.Process, drain_task: asyncio.Future[Any]
) -> None:
    await drain_task
    await proc.wait()


__all__ = [
    "EXIT_SUCCESS",
    "STREAM_READER_LIMIT",
    "InvokeResult",
    "build_argv",
    "build_env",
    "generate_session_id",
    "invoke",
]
