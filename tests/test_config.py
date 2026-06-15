from __future__ import annotations

from pathlib import Path

import pytest

from oxison.config import (
    READ_ONLY_TOOLS,
    ConfigError,
    build_run_config,
    resolve_api_key,
    resolve_auth_mode,
    resolve_target,
)


def test_read_only_tools_never_include_write_exec_or_edit() -> None:
    # The #1 invariant, asserted at the constant that defines it. Bash is a
    # write/exec primitive under bypassPermissions, so it must NOT be here — a
    # read-only worker is structurally incapable of mutating the repo.
    assert "Edit" not in READ_ONLY_TOOLS
    assert "Write" not in READ_ONLY_TOOLS
    assert "MultiEdit" not in READ_ONLY_TOOLS
    assert "Bash" not in READ_ONLY_TOOLS
    assert set(READ_ONLY_TOOLS) == {"Read", "Glob", "Grep"}


def test_resolve_target_ok(tmp_path: Path) -> None:
    assert resolve_target(str(tmp_path)) == tmp_path.resolve()


def test_resolve_target_missing() -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        resolve_target("/no/such/path/oxison-xyz")


def test_resolve_target_is_file(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        resolve_target(str(f))


def test_resolve_api_key_precedence() -> None:
    assert resolve_api_key("explicit", env={"OXISON_API_KEY": "a"}) == "explicit"
    assert resolve_api_key(None, env={"OXISON_API_KEY": "a"}) == "a"
    assert resolve_api_key(None, env={"ANTHROPIC_API_KEY": "b"}) == "b"
    assert resolve_api_key(None, env={"OXISON_API_KEY": "a", "ANTHROPIC_API_KEY": "b"}) == "a"
    assert resolve_api_key(None, env={}) is None


def test_resolve_auth_mode() -> None:
    assert resolve_auth_mode(bare=False, api_key=None) == "oauth"
    assert resolve_auth_mode(bare=True, api_key=None) == "bare"
    assert resolve_auth_mode(bare=False, api_key="k") == "bare"


def test_build_run_config_oauth_default(tmp_path: Path) -> None:
    cfg = build_run_config(
        target=str(tmp_path),
        output_dir=None,
        bare=False,
        api_key=None,
        model=None,
        max_budget_usd=None,
        chunk_threshold=100_000,
        max_concurrency=4,
        resume=False,
        env={},
    )
    assert cfg.auth_mode == "oauth"
    assert cfg.api_key is None
    assert cfg.output_dir.name == "oxison-output"


def test_build_run_config_bare_requires_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="bare mode requires an API key"):
        build_run_config(
            target=str(tmp_path),
            output_dir=None,
            bare=True,
            api_key=None,
            model=None,
            max_budget_usd=None,
            chunk_threshold=100_000,
            max_concurrency=4,
            resume=False,
            env={},
        )


def test_build_run_config_bare_with_key(tmp_path: Path) -> None:
    cfg = build_run_config(
        target=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        bare=True,
        api_key="sk-test",
        model="claude-opus-4-8",
        max_budget_usd=5.0,
        chunk_threshold=50_000,
        max_concurrency=2,
        resume=True,
        env={},
    )
    assert cfg.auth_mode == "bare"
    assert cfg.api_key == "sk-test"
    assert cfg.model == "claude-opus-4-8"
    assert cfg.max_budget_usd == 5.0
    assert cfg.resume is True


def test_build_run_config_rejects_bad_concurrency(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max-concurrency"):
        build_run_config(
            target=str(tmp_path),
            output_dir=None,
            bare=False,
            api_key=None,
            model=None,
            max_budget_usd=None,
            chunk_threshold=100_000,
            max_concurrency=0,
            resume=False,
            env={},
        )


def test_run_config_carries_sources(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    extra = tmp_path / "spec.pdf"
    extra.write_bytes(b"%PDF-1.4")
    cfg = build_run_config(
        target=str(repo), output_dir=None, bare=False, api_key=None, model=None,
        max_budget_usd=None, chunk_threshold=100_000, max_concurrency=4, resume=False,
        extra_sources=[str(extra)], ocr_enabled=True, stt_key="sk", stt_provider="deepgram",
    )
    assert cfg.extra_sources == [str(extra)]
    assert cfg.ocr_enabled is True
    assert cfg.stt_key == "sk"
    assert cfg.stt_provider == "deepgram"


def test_run_config_sources_default_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = build_run_config(
        target=str(repo), output_dir=None, bare=False, api_key=None, model=None,
        max_budget_usd=None, chunk_threshold=100_000, max_concurrency=4, resume=False,
    )
    assert cfg.extra_sources == []
    assert cfg.ocr_enabled is False
    assert cfg.stt_key is None
    assert cfg.stt_provider == "openai"
