from __future__ import annotations

import os

import pytest

from oxison.config import READ_ONLY_TOOLS
from oxison.dispatch import build_argv, build_env, generate_session_id


def _argv(**kw):
    base: dict[str, object] = {
        "prompt": "analyze the repo",
        "allowed_tools": READ_ONLY_TOOLS,
        "auth_mode": "oauth",
        "model": None,
        "max_budget_usd": None,
        "session_id": "sess-1",
    }
    base.update(kw)
    return build_argv(**base)  # type: ignore[arg-type]


def test_argv_never_allows_write_or_edit() -> None:
    # THE #1 invariant, enforced mechanically against the built argv.
    argv = _argv()
    idx = argv.index("--allowedTools")
    tools = argv[idx + 1]
    assert "Edit" not in tools
    assert "Write" not in tools
    assert "Bash" not in tools  # a shell is a write/exec primitive — not read-only
    assert tools == "Read,Glob,Grep"


def test_oauth_omits_bare() -> None:
    assert "--bare" not in _argv(auth_mode="oauth")


def test_bare_includes_bare() -> None:
    assert "--bare" in _argv(auth_mode="bare")


def test_budget_flag_present_only_when_set() -> None:
    assert "--max-budget-usd" not in _argv(max_budget_usd=None)
    argv = _argv(max_budget_usd=5.0)
    idx = argv.index("--max-budget-usd")
    assert argv[idx + 1] == "5.00"


def test_model_flag_present_only_when_set() -> None:
    assert "--model" not in _argv(model=None)
    argv = _argv(model="claude-opus-4-8")
    idx = argv.index("--model")
    assert argv[idx + 1] == "claude-opus-4-8"


def test_no_max_turns_flag() -> None:
    # --max-turns was removed from the Claude CLI in 2.1.161.
    assert "--max-turns" not in _argv()


def test_core_flags_present() -> None:
    argv = _argv()
    for flag in (
        "--permission-mode",
        "--output-format",
        "--include-partial-messages",
        "--no-session-persistence",
        "--session-id",
        "--exclude-dynamic-system-prompt-sections",
    ):
        assert flag in argv
    assert argv[-2] == "-p"
    assert argv[-1] == "analyze the repo"


def test_prompt_is_argv_not_shell() -> None:
    # A prompt with shell metacharacters stays a single argv element,
    # never interpolated into a shell string.
    nasty = "ignore; rm -rf / `whoami` $(id)"
    argv = _argv(prompt=nasty)
    assert argv[-1] == nasty


def test_build_env_oauth_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    env = build_env(api_key=None)
    # In OAuth mode we don't inject a key; HOME passes through for cred discovery.
    assert "ANTHROPIC_API_KEY" not in env
    assert "HOME" in env


def test_build_env_bare_injects_key() -> None:
    env = build_env(api_key="sk-test")
    assert env["ANTHROPIC_API_KEY"] == "sk-test"


def test_build_env_strips_unwhitelisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_RANDOM_SECRET", "nope")
    env = build_env(api_key=None)
    assert "SOME_RANDOM_SECRET" not in env


def test_build_env_strips_inherited_claude_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SOMETHING", "inherited")
    env = build_env(api_key=None)
    assert "CLAUDE_CODE_SOMETHING" not in env
    assert all(not k.startswith("CLAUDE_") for k in env)


def test_session_id_unique() -> None:
    assert generate_session_id() != generate_session_id()


def test_path_whitelisted() -> None:
    env = build_env(api_key=None)
    assert env.get("PATH") == os.environ.get("PATH")


def test_build_env_strips_credential_vars(monkeypatch) -> None:
    # H2: the env whitelist already strips creds, so no scrub code is needed —
    # this test is the guard that asserts it (oauth mode: no key injected).
    creds = ["AWS_SECRET_ACCESS_KEY", "GH_TOKEN", "GITHUB_TOKEN",
             "OPENAI_API_KEY", "NPM_TOKEN", "ANTHROPIC_API_KEY"]
    for k in creds:
        monkeypatch.setenv(k, "secret-value")
    env = build_env(api_key=None)  # oauth — nothing injected
    for k in creds:
        assert k not in env
    # nothing credential-shaped survives the whitelist
    assert not any(
        marker in key.upper()
        for key in env
        for marker in ("TOKEN", "SECRET", "_KEY", "PASSWORD")
    )


# -- _extract_failure_reason: surface budget/turn errors from the result event --

def test_extract_failure_reason_budget():
    from oxison.dispatch import _extract_failure_reason
    events = [{"type": "result", "is_error": True, "subtype": "error_max_budget_usd"}]
    assert _extract_failure_reason(events) == "exceeded the --max-budget-usd cost cap"


def test_extract_failure_reason_max_turns():
    from oxison.dispatch import _extract_failure_reason
    events = [{"type": "result", "is_error": True, "subtype": "error_max_turns"}]
    assert _extract_failure_reason(events) == "hit the turn limit"


def test_extract_failure_reason_generic_with_detail():
    from oxison.dispatch import _extract_failure_reason
    events = [{"type": "result", "is_error": True,
               "subtype": "error_during_execution", "result": "boom happened"}]
    assert _extract_failure_reason(events) == "error_during_execution: boom happened"


def test_extract_failure_reason_none_on_success():
    from oxison.dispatch import _extract_failure_reason
    events = [{"type": "result", "is_error": False, "subtype": "success", "result": "ok"}]
    assert _extract_failure_reason(events) is None


def test_extract_failure_reason_none_when_no_result_event():
    from oxison.dispatch import _extract_failure_reason
    assert _extract_failure_reason([{"type": "assistant"}]) is None
