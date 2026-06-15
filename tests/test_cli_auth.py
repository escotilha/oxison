"""Tests for the provider-key funnel + `oxison auth` subcommand."""

from __future__ import annotations

import types

import oxison.cli as cli


def _args(**kw):
    base = {"provider": None, "api_key": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


# --- _resolve_provider_key precedence: --api-key > env > keystore > prompt -----

def test_resolve_no_provider_returns_plain_api_key():
    assert cli._resolve_provider_key(_args(provider=None, api_key="sk-x")) == "sk-x"
    assert cli._resolve_provider_key(_args(provider=None, api_key=None)) is None


def test_resolve_explicit_key_wins(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "from-env")
    assert cli._resolve_provider_key(_args(provider="grok", api_key="from-flag")) == "from-flag"


def test_resolve_env_beats_keystore(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "from-env")
    monkeypatch.setattr(cli, "get_saved_key", lambda name: "from-keystore")
    assert cli._resolve_provider_key(_args(provider="grok")) == "from-env"


def test_resolve_keystore_when_no_flag_or_env(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setattr(cli, "get_saved_key", lambda name: "from-keystore")
    assert cli._resolve_provider_key(_args(provider="grok")) == "from-keystore"


def test_resolve_prompts_only_on_tty(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setattr(cli, "get_saved_key", lambda name: None)
    monkeypatch.setattr(cli, "_prompt_and_maybe_save", lambda prov: "from-prompt")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    assert cli._resolve_provider_key(_args(provider="grok")) == "from-prompt"


def test_resolve_headless_no_key_returns_none(monkeypatch):
    # The CI-safety gate: no key + not a TTY → None (builder raises clear error).
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setattr(cli, "get_saved_key", lambda name: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli._resolve_provider_key(_args(provider="grok")) is None


# --- `oxison auth` parsing + handlers -----------------------------------------

def test_auth_parsers():
    p = cli.build_parser()
    a = p.parse_args(["auth", "set", "grok"])
    assert a.func is cli.cmd_auth_set and a.provider == "grok"
    a = p.parse_args(["auth", "status"])
    assert a.func is cli.cmd_auth_status
    a = p.parse_args(["auth", "rm", "kimi"])
    assert a.func is cli.cmd_auth_rm and a.provider == "kimi"
    # bare `auth` defaults to status
    a = p.parse_args(["auth"])
    assert a.func is cli.cmd_auth_status


def test_auth_rejects_unknown_provider():
    import pytest
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["auth", "set", "gpt5"])


def test_auth_set_noninteractive(monkeypatch, capsys):
    saved = {}

    def fake_set(name, key):
        saved[name] = key
        return "keychain"

    monkeypatch.setattr(cli, "set_saved_key", fake_set)
    rc = cli.cmd_auth_set(_args(provider="grok", api_key="xai-abcd"))
    out = capsys.readouterr().out
    assert rc == 0 and saved["grok"] == "xai-abcd"
    assert "saved grok key to keychain" in out
    # No part of the key is ever echoed (not the full key, not the last 4).
    assert "xai-abcd" not in out and "abcd" not in out


def test_auth_status_never_leaks_key(monkeypatch, capsys):
    monkeypatch.setattr(cli, "detect_backend", lambda: "keychain")
    monkeypatch.setattr(
        cli, "saved_key_status",
        lambda name: (True, "keychain") if name == "grok" else (False, None),
    )
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    rc = cli.cmd_auth_status(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "grok" in out and "saved" in out
    assert "kimi" in out and "not saved" in out
    # status must NOT echo any part of the key (not even the last 4)
    assert "wxyz" not in out


def test_auth_rm(monkeypatch, capsys):
    monkeypatch.setattr(cli, "delete_saved_key", lambda name: True)
    rc = cli.cmd_auth_rm(_args(provider="grok"))
    assert rc == 0 and "removed saved grok key" in capsys.readouterr().out
