from __future__ import annotations

from pathlib import Path

import pytest

import oxison.branch as branch_mod
from oxison.branch import (
    ROADMAP_ANALYSIS_FILENAME,
    SECURITY_NOTES_FILENAME,
    BranchError,
    detect_roadmap,
    run_branch,
)
from oxison.config import READ_ONLY_TOOLS, build_run_config
from oxison.dispatch import InvokeResult


def _cfg(tmp_path: Path, **kw):
    base: dict[str, object] = {
        "target": str(tmp_path / "repo"),
        "output_dir": str(tmp_path / "out"),
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


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / "main.py").write_text("print('x')\n", encoding="utf-8")
    return r


def test_detect_roadmap_priority(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    assert detect_roadmap(r) is None
    (r / "BACKLOG.md").write_text("# backlog\n", encoding="utf-8")
    assert detect_roadmap(r).name == "BACKLOG.md"
    # ROADMAP.md outranks BACKLOG.md.
    (r / "ROADMAP.md").write_text("# roadmap\n", encoding="utf-8")
    assert detect_roadmap(r).name == "ROADMAP.md"


@pytest.mark.asyncio
async def test_branch_takes_roadmap_arm(tmp_path: Path, monkeypatch) -> None:
    from oxison.repomap import build_repo_map

    r = _repo(tmp_path)
    (r / "ROADMAP.md").write_text("# Roadmap\n- ship v1\n", encoding="utf-8")
    seen: list[tuple[str, ...]] = []

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        seen.append(tuple(allowed_tools))
        assert "ROADMAP ANALYSIS" in prompt or "roadmap" in prompt.lower()
        return InvokeResult(ok=True, text="# Roadmap Analysis\nplan", cost_usd=0.04, exit_code=0)

    monkeypatch.setattr(branch_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path)
    rm = build_repo_map(cfg.target)
    result = await run_branch(cfg, rm, "comprehension")

    assert result.kind == "roadmap"
    assert result.filename == ROADMAP_ANALYSIS_FILENAME
    assert (cfg.output_dir / ROADMAP_ANALYSIS_FILENAME).exists()
    assert seen[0] == READ_ONLY_TOOLS  # read-only invariant on the branch path


@pytest.mark.asyncio
async def test_branch_takes_security_arm(tmp_path: Path, monkeypatch) -> None:
    from oxison.repomap import build_repo_map

    _repo(tmp_path)  # no roadmap file
    seen: list[tuple[str, ...]] = []

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        seen.append(tuple(allowed_tools))
        assert "security" in prompt.lower()
        return InvokeResult(
            ok=True, text="# Security Notes\nlightweight scan", cost_usd=0.02, exit_code=0
        )

    monkeypatch.setattr(branch_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path)
    rm = build_repo_map(cfg.target)
    result = await run_branch(cfg, rm, "comprehension")

    assert result.kind == "security"
    assert result.filename == SECURITY_NOTES_FILENAME
    assert (cfg.output_dir / SECURITY_NOTES_FILENAME).exists()
    assert seen[0] == READ_ONLY_TOOLS


@pytest.mark.asyncio
async def test_branch_failure_surfaces(tmp_path: Path, monkeypatch) -> None:
    from oxison.repomap import build_repo_map

    _repo(tmp_path)

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=False, text="", cost_usd=0.0, exit_code=1, error="nope")

    monkeypatch.setattr(branch_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path)
    rm = build_repo_map(cfg.target)
    with pytest.raises(BranchError, match="nope"):
        await run_branch(cfg, rm, "comprehension")


def test_oxi_enrichment_absent_returns_none() -> None:
    # oxi_core isn't installed in the test venv → enrichment is a no-op.
    assert branch_mod._try_oxi_parse("anything") is None


def test_oxi_enrichment_uses_parser_when_present(monkeypatch) -> None:
    # Simulate oxi-core being importable with a parser that yields items.
    import sys
    import types

    fake_planner = types.ModuleType("oxi_core.planner")

    class _Item:
        def __init__(self, identifier, title):
            self.identifier = identifier
            self.title = title

    def parse_roadmap(text):
        return [_Item("T0-1", "do thing"), _Item("T0-2", "do other")]

    fake_planner.parse_roadmap = parse_roadmap  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("oxi_core")
    monkeypatch.setitem(sys.modules, "oxi_core", fake_pkg)
    monkeypatch.setitem(sys.modules, "oxi_core.planner", fake_planner)

    items = branch_mod._try_oxi_parse("## Tier 0\n**T0-1 · do thing**")
    assert items == ["T0-1 · do thing", "T0-2 · do other"]
