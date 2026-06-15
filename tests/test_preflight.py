from __future__ import annotations

from pathlib import Path

import pytest

from oxison.config import build_run_config
from oxison.preflight import PreflightError, check_claude_cli, preflight


def _cfg(tmp_path: Path, **kw: object):
    base: dict[str, object] = {
        "target": str(tmp_path),
        "output_dir": None,
        "bare": False,
        "api_key": None,
        "model": None,
        "max_budget_usd": None,
        "chunk_threshold": 100_000,
        "max_concurrency": 4,
        "resume": False,
        "env": {},
    }
    base.update(kw)
    return build_run_config(**base)  # type: ignore[arg-type]


def test_check_claude_cli_missing() -> None:
    with pytest.raises(PreflightError, match="not installed or not on PATH"):
        check_claude_cli(binary="oxison-no-such-binary-xyz")


def test_preflight_missing_binary(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with pytest.raises(PreflightError):
        preflight(cfg, binary="oxison-no-such-binary-xyz")


def test_preflight_bare_without_key_defensive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # build_run_config already guards this, so construct a cfg via bare+key
    # then simulate a stripped key to exercise preflight's defensive check.
    cfg = _cfg(tmp_path, bare=True, api_key="sk-test", env={})
    object.__setattr__(cfg, "api_key", None)
    with pytest.raises(PreflightError, match="no API key resolved"):
        # Use a fake binary that "exists" by pointing check at python via PATH.
        # We only need to reach the bare-key check, so stub check_claude_cli.
        monkeypatch.setattr(
            "oxison.preflight.check_claude_cli",
            lambda binary="claude": None,  # type: ignore[return-value]
        )
        preflight(cfg)
