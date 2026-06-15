from __future__ import annotations

from pathlib import Path

import pytest

import oxison.comprehend as comp_mod
from oxison.comprehend import ComprehensionError, comprehend
from oxison.config import READ_ONLY_TOOLS, build_run_config
from oxison.dispatch import InvokeResult
from oxison.repomap import build_repo_map


def _cfg(tmp_path: Path, **kw):
    base: dict[str, object] = {
        "target": str(tmp_path),
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


def _fixture(root: Path) -> None:
    (root / "src").mkdir()
    (root / "lib").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "lib" / "b.py").write_text("b = 2\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_single_pass_when_below_threshold(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)
    calls: list[dict] = []

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        calls.append({"allowed_tools": tuple(allowed_tools), "cwd": cwd})
        return InvokeResult(ok=True, text="comprehension text", cost_usd=0.01, exit_code=0)

    monkeypatch.setattr(comp_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path, chunk_threshold=10_000_000)
    rm = build_repo_map(tmp_path)
    result = await comprehend(cfg, rm)

    assert result.chunked is False
    assert len(calls) == 1  # single pass
    # Every worker is read-only and runs in the target repo.
    assert calls[0]["allowed_tools"] == READ_ONLY_TOOLS
    assert calls[0]["cwd"] == cfg.target


@pytest.mark.asyncio
async def test_mapreduce_when_above_threshold(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)
    calls: list[str] = []

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        assert tuple(allowed_tools) == READ_ONLY_TOOLS  # invariant on EVERY call
        calls.append(prompt)
        return InvokeResult(ok=True, text="slice or synth text", cost_usd=0.02, exit_code=0)

    monkeypatch.setattr(comp_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path, chunk_threshold=1)  # force map-reduce
    rm = build_repo_map(tmp_path)
    result = await comprehend(cfg, rm)

    assert result.chunked is True
    # 2 slices (src, lib) + 1 synthesis = 3 calls.
    assert len(calls) == 3
    assert {s.directory for s in result.slices} == {"src", "lib"}
    assert result.total_cost_usd == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_worker_failure_surfaces(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=False, text="", cost_usd=0.0, exit_code=1, error="boom")

    monkeypatch.setattr(comp_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path, chunk_threshold=10_000_000)
    rm = build_repo_map(tmp_path)
    with pytest.raises(ComprehensionError, match="boom"):
        await comprehend(cfg, rm)


@pytest.mark.asyncio
async def test_empty_output_is_failure(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=True, text="   ", cost_usd=0.0, exit_code=0)

    monkeypatch.setattr(comp_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path, chunk_threshold=10_000_000)
    rm = build_repo_map(tmp_path)
    with pytest.raises(ComprehensionError, match="empty"):
        await comprehend(cfg, rm)


@pytest.mark.asyncio
async def test_comprehend_passes_extra_context_to_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """extra_context kwarg is threaded into the prompt builder."""
    captured: dict[str, str] = {}

    def fake_single_pass_prompt(
        *, root: str, repo_map_context: str, extra_context: str = ""
    ) -> str:
        captured["extra"] = extra_context
        return "PROMPT"

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=True, text="comprehension", cost_usd=0.0, exit_code=0)

    monkeypatch.setattr(comp_mod, "single_pass_prompt", fake_single_pass_prompt)
    monkeypatch.setattr(comp_mod, "invoke", fake_invoke)

    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")
    rm = build_repo_map(tmp_path)
    cfg = _cfg(tmp_path, chunk_threshold=10_000_000)  # force single-pass
    await comprehend(cfg, rm, extra_context="EXTRA-XYZ")
    assert captured["extra"] == "EXTRA-XYZ"


@pytest.mark.asyncio
async def test_comprehend_mapreduce_threads_extra_context_to_slice_and_synthesis(
    tmp_path: Path, monkeypatch
) -> None:
    """extra_context reaches BOTH slice_prompt and synthesis_prompt on the map-reduce path."""
    captured: dict[str, object] = {"slice": [], "synth": None}

    def fake_slice(*, root, repo_map_context, slice_dir, extra_context=""):
        captured["slice"].append(extra_context)  # type: ignore[union-attr]
        return "SLICE_PROMPT"

    def fake_synth(*, root, repo_map_context, slice_summaries, extra_context=""):
        captured["synth"] = extra_context
        return "SYNTH_PROMPT"

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=True, text="out", cost_usd=0.0, exit_code=0)

    monkeypatch.setattr(comp_mod, "slice_prompt", fake_slice)
    monkeypatch.setattr(comp_mod, "synthesis_prompt", fake_synth)
    monkeypatch.setattr(comp_mod, "invoke", fake_invoke)

    # Two top-level packages → two slices; tiny chunk_threshold forces map-reduce.
    (tmp_path / "pkg_a").mkdir()
    (tmp_path / "pkg_a" / "a.py").write_text("print(1)\n" * 50, encoding="utf-8")
    (tmp_path / "pkg_b").mkdir()
    (tmp_path / "pkg_b" / "b.py").write_text("print(2)\n" * 50, encoding="utf-8")

    rm = build_repo_map(tmp_path)
    cfg = _cfg(tmp_path, chunk_threshold=1)  # force map-reduce
    result = await comprehend(cfg, rm, extra_context="MR-XYZ")

    assert result.chunked is True  # confirms the map-reduce branch actually ran
    assert captured["synth"] == "MR-XYZ"  # synthesis got it (the easy-to-miss path)
    assert captured["slice"]  # at least one slice ran
    assert all(ec == "MR-XYZ" for ec in captured["slice"])  # type: ignore[union-attr]
