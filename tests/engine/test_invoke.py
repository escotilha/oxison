"""Phase 0 — the shared ``claude -p`` leaf: ToolSet chokepoint, argv reuse, teardown.

Covers:
* C2 — ``ToolSet.FULL_WRITE`` is the only write tool set; ``READ_ONLY`` has
  exact parity with Phase-1's tuple (so the Phase-7 migration is a no-op swap).
* M2 — the argv builder is reused from Phase-1, not duplicated.
* H1 — the teardown helper escalates SIGTERM -> SIGKILL via the kill helper.
"""

from __future__ import annotations

import signal

from oxison.config import READ_ONLY_TOOLS
from oxison.engine.invoke import ToolSet, build_argv, build_env, kill_process_group


def test_read_only_parity_with_phase1_tuple() -> None:
    # Parity test: Phase-7 migration (READ_ONLY_TOOLS -> ToolSet.READ_ONLY)
    # must be a behavioral no-op. ToolSet.READ_ONLY derives from
    # READ_ONLY_TOOLS, so assert against a LITERAL (not the same imported
    # tuple) — otherwise the check is a tautology and any change to the
    # read-only set would slip through silently. Pinning the literal makes
    # any change a visible, intentional test edit.
    assert ToolSet.READ_ONLY.tools == ("Read", "Glob", "Grep")
    # And confirm Phase-1's tuple still matches that literal (so the two
    # sources agree today; Phase-7 collapses them).
    assert tuple(READ_ONLY_TOOLS) == ("Read", "Glob", "Grep")


def test_full_write_is_superset_with_write_tools() -> None:
    ro = set(ToolSet.READ_ONLY.tools)
    fw = set(ToolSet.FULL_WRITE.tools)
    assert ro < fw  # strict superset
    # Bash is a write/exec tool — only the build worker (FULL_WRITE) gets it.
    assert {"Bash", "Edit", "Write", "MultiEdit"} <= fw
    # No write/exec tool leaked into the read-only set (the chokepoint, C2).
    assert not ({"Bash", "Edit", "Write", "MultiEdit"} & ro)


def test_only_two_tool_sets_exist() -> None:
    # The enum is the single constructor; there is no third/ad-hoc write set.
    assert {m.name for m in ToolSet} == {"READ_ONLY", "FULL_WRITE"}


def test_build_argv_reuses_phase1_and_threads_toolset() -> None:
    argv = build_argv(
        "do the thing",
        tool_set=ToolSet.FULL_WRITE,
        auth_mode="oauth",
        model="claude-sonnet-4-6",
        max_budget_usd=5.0,
        session_id="sid-123",
    )
    # The tools land in the argv exactly as Phase-1 formats them.
    assert "--allowedTools" in argv
    tools_value = argv[argv.index("--allowedTools") + 1]
    assert tools_value == ",".join(ToolSet.FULL_WRITE.tools)
    # Phase-1 flags are present (proves reuse, not a re-implementation).
    assert "--permission-mode" in argv
    assert "bypassPermissions" in argv
    assert argv[-2:] == ["-p", "do the thing"]
    # Budget cap is formatted by Phase-1's builder.
    assert "--max-budget-usd" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "5.00"


def test_build_argv_read_only_has_no_write_tools() -> None:
    argv = build_argv(
        "read only",
        tool_set=ToolSet.READ_ONLY,
        auth_mode="oauth",
        model=None,
        max_budget_usd=None,
        session_id="sid",
    )
    tools_value = argv[argv.index("--allowedTools") + 1]
    assert "Edit" not in tools_value
    assert "Write" not in tools_value


def test_build_env_reuses_phase1() -> None:
    env = build_env(api_key="sk-test", whitelist=("CUSTOM_VAR",))
    assert env.get("ANTHROPIC_API_KEY") == "sk-test"


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess; records signals sent."""

    def __init__(self) -> None:
        self.signals: list[int] = []

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)


def test_teardown_falls_back_to_send_signal_when_no_pgid() -> None:
    # When pgid is None, the helper signals the proc directly (H1 teardown path).
    proc = _FakeProc()
    kill_process_group(proc, None, signal.SIGTERM)  # type: ignore[arg-type]
    assert proc.signals == [signal.SIGTERM]
    kill_process_group(proc, None, signal.SIGKILL)  # type: ignore[arg-type]
    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]
