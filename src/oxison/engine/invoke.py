"""The shared ``claude -p`` leaf — argv/env builder, ``ToolSet``, teardown.

This is the single home for *constructing* a ``claude -p`` invocation in the
engine (M2). Every engine caller — planner, grader, dispatch — builds its
argv/env through here, so there is exactly one argv/env builder in the
codebase (Phase-1's, reused by import).

**The ``ToolSet`` chokepoint (C2).** ``ToolSet`` is the only constructor of a
write-capable tool set. Read-only callers use ``ToolSet.READ_ONLY``;
write workers use ``ToolSet.FULL_WRITE``. There is no other path to a write
tool set in engine code, so "can this caller write?" is answerable by
grepping for ``FULL_WRITE``.

**Two intentional stream strategies, one shared surface (§0.4 / M2).** This
leaf owns the argv builder, the env whitelist, the ``ToolSet``, and the
process-group teardown (H1) — and deliberately *not* the stream-drain loop.
There are two correct draining strategies that must never be unified:

* in-memory events — for *bounded* planner/grader Opus calls (reuses
  Phase-1 ``dispatch.invoke``);
* log-to-file, never PIPE — for the *unbounded* write worker (built in
  ``engine.dispatch``, Phase 3).

Unifying them would regress one side (PIPE deadlock into a long worker, or
breaking Phase-1's bounded model), so the drain loop is intentionally left
to the caller.

**Phase-1 is imported, never edited (§0.3).** ``build_argv``/``build_env``
and the kill helper are imported from ``oxison.dispatch``; the Phase-1
``READ_ONLY_TOOLS`` tuple is imported from ``oxison.config``. No Phase-1
file is modified in Phase 0 — the migration of Phase-1 onto
``ToolSet.READ_ONLY`` is deferred to Phase 7.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Mapping, Sequence
from enum import Enum

# Phase 7 note: this is an engine -> Phase-1 import (engine reads Phase-1's
# tuple). The Phase-7 migration inverts the edge — config.py would import
# ``ToolSet`` from here — which is circular. At that step, move the canonical
# read-only tool names to a neutral module both can import, or have config
# re-export from here. Flagged now so it isn't a surprise at migration.
from oxison.config import READ_ONLY_TOOLS
from oxison.dispatch import (
    _kill_process_group as _p1_kill_process_group,
)
from oxison.dispatch import (
    build_argv as _p1_build_argv,
)
from oxison.dispatch import (
    build_env as _p1_build_env,
)

# Write/exec-capable tools added on top of the read-only set. ``Bash`` lives
# here (not in ``READ_ONLY_TOOLS``) because a shell under bypassPermissions can
# write and execute — only the build worker, which must run tests/build/commit,
# is granted it via ``FULL_WRITE``.
_WRITE_TOOLS: tuple[str, ...] = ("Bash", "Edit", "Write", "MultiEdit")


class ToolSet(Enum):
    """The only constructor of a tool set for an engine ``claude -p`` call.

    ``READ_ONLY`` mirrors Phase-1's ``READ_ONLY_TOOLS`` exactly (parity test
    in Phase 0 guarantees the Phase-7 migration is a no-op swap).
    ``FULL_WRITE`` is the *only* path to write-capable tools in engine code
    (C2 chokepoint).
    """

    READ_ONLY = tuple(READ_ONLY_TOOLS)
    FULL_WRITE = tuple(READ_ONLY_TOOLS) + _WRITE_TOOLS

    @property
    def tools(self) -> tuple[str, ...]:
        """The tuple of tool names this set grants."""
        return self.value


def build_argv(
    prompt: str,
    *,
    tool_set: ToolSet,
    auth_mode: str,
    model: str | None,
    max_budget_usd: float | None,
    session_id: str,
    binary: str = "claude",
) -> list[str]:
    """Build a ``claude -p`` argv, reusing Phase-1's builder (M2).

    Differs from Phase-1's ``build_argv`` only in taking a typed ``ToolSet``
    instead of a bare ``allowed_tools`` sequence — the chokepoint that makes
    "is this a write call?" a typed, greppable property (C2).
    """
    return _p1_build_argv(
        prompt,
        allowed_tools=tool_set.tools,
        auth_mode=auth_mode,
        model=model,
        max_budget_usd=max_budget_usd,
        session_id=session_id,
        binary=binary,
    )


def build_env(
    *,
    api_key: str | None,
    whitelist: Sequence[str] = (),
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Compose a whitelisted child env, reusing Phase-1's builder (M2).

    ``extra`` forwards the provider overlay (``ANTHROPIC_BASE_URL`` +
    ``ANTHROPIC_AUTH_TOKEN`` + knobs) to the build worker — see Phase-1's
    ``build_env`` and ``providers.provider_child_env``.
    """
    return _p1_build_env(api_key=api_key, whitelist=whitelist, extra=extra)


def kill_process_group(
    proc: asyncio.subprocess.Process,
    pgid: int | None,
    sig: int = signal.SIGTERM,
) -> None:
    """Process-group teardown (H1), reusing Phase-1's kill helper.

    Both stream strategies share this. A worker is spawned with
    ``start_new_session=True`` (its own process group), so the engine can
    escalate SIGTERM -> SIGKILL to the whole group and not leak children.
    """
    _p1_kill_process_group(proc, pgid, sig)


__all__ = ["ToolSet", "build_argv", "build_env", "kill_process_group"]
