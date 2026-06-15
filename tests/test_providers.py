"""Tests for the named-provider registry (``oxison.providers``)."""

from __future__ import annotations

import pytest

from oxison.providers import (
    GROK,
    KIMI,
    PROVIDERS,
    ProviderError,
    provider_child_env,
    provider_names,
    resolve_provider,
    resolve_provider_token,
)


def test_registry_has_kimi_and_grok() -> None:
    assert set(provider_names()) == {"kimi", "grok"}
    assert PROVIDERS["kimi"] is KIMI
    assert PROVIDERS["grok"] is GROK


def test_kimi_registry_values() -> None:
    assert KIMI.base_url == "https://api.moonshot.ai/anthropic"
    assert KIMI.default_model == "kimi-k2.7-code"
    assert "api.moonshot.ai" in KIMI.sandbox_domains
    assert KIMI.token_envs[0] == "KIMI_API_KEY"
    knobs = dict(KIMI.extra_env)
    assert knobs["ENABLE_TOOL_SEARCH"] == "false"
    assert knobs["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "262144"


def test_grok_registry_values() -> None:
    assert GROK.base_url == "https://api.x.ai"
    assert GROK.default_model == "grok-4.3"
    assert "api.x.ai" in GROK.sandbox_domains
    assert GROK.token_envs[0] == "XAI_API_KEY"
    assert GROK.extra_env == ()


def test_resolve_provider_none() -> None:
    assert resolve_provider(None) is None


def test_resolve_provider_known() -> None:
    assert resolve_provider("kimi") is KIMI
    assert resolve_provider("grok") is GROK


def test_resolve_provider_unknown_raises_and_lists_known() -> None:
    with pytest.raises(ProviderError) as exc:
        resolve_provider("gpt5")
    msg = str(exc.value)
    assert "gpt5" in msg
    assert "kimi" in msg and "grok" in msg  # lists known providers


def test_token_precedence_explicit_key_wins() -> None:
    tok = resolve_provider_token(KIMI, "explicit", env={"KIMI_API_KEY": "from-env"})
    assert tok == "explicit"


def test_token_env_order_first_var_wins() -> None:
    tok = resolve_provider_token(
        KIMI, None, env={"KIMI_API_KEY": "a", "MOONSHOT_API_KEY": "b"}
    )
    assert tok == "a"


def test_token_falls_back_to_second_env() -> None:
    tok = resolve_provider_token(KIMI, None, env={"MOONSHOT_API_KEY": "b"})
    assert tok == "b"


def test_token_missing_returns_none() -> None:
    assert resolve_provider_token(GROK, None, env={}) is None


def test_provider_child_env_kimi_has_overlay_and_knobs() -> None:
    env = provider_child_env(KIMI, "tok123")
    assert env["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok123"
    assert env["ENABLE_TOOL_SEARCH"] == "false"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "262144"
    # provider auth is via ANTHROPIC_AUTH_TOKEN, never the Anthropic key
    assert "ANTHROPIC_API_KEY" not in env


def test_provider_child_env_grok_is_minimal() -> None:
    env = provider_child_env(GROK, "xai-tok")
    assert env == {
        "ANTHROPIC_BASE_URL": "https://api.x.ai",
        "ANTHROPIC_AUTH_TOKEN": "xai-tok",
    }
