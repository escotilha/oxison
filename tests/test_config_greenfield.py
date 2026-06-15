"""build_greenfield_config + resolve_staging."""
from __future__ import annotations

from pathlib import Path

import pytest

from oxison.config import ConfigError, build_greenfield_config, resolve_staging


def test_resolve_staging_creates_and_is_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "oxison-output"
    s1 = resolve_staging(out)
    assert s1.is_dir()
    assert s1.name == ".oxison-staging"
    s2 = resolve_staging(out)  # idempotent
    assert s1 == s2


def test_build_greenfield_config_basics(tmp_path: Path) -> None:
    out = tmp_path / "out"
    cfg = build_greenfield_config(
        output_dir=str(out),
        bare=False,
        api_key=None,
        model="claude-sonnet-4-6",
        max_budget_usd=1.0,
        brief="build a todo app",
        urls=["https://example.com"],
        extra_sources=["/some/deck.pdf"],
        env={},
    )
    assert cfg.brief == "build a todo app"
    assert cfg.urls == ["https://example.com"]
    assert cfg.extra_sources == ["/some/deck.pdf"]
    assert cfg.target_is_git is False
    assert cfg.target.is_dir() and cfg.target.name == ".oxison-staging"
    assert cfg.auth_mode == "oauth"
    assert cfg.max_concurrency == 1
    assert cfg.resume is False


def test_build_greenfield_config_bare_without_key_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        build_greenfield_config(
            output_dir=str(tmp_path / "out"),
            bare=True,
            api_key=None,
            model=None,
            max_budget_usd=None,
            brief="idea",
            env={},  # no OXISON_API_KEY / ANTHROPIC_API_KEY
        )
