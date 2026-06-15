from __future__ import annotations

from pathlib import Path

import pytest

import oxison.generate as gen_mod
from oxison.config import READ_ONLY_TOOLS, build_run_config
from oxison.dispatch import InvokeResult
from oxison.generate import ARTIFACTS, GenerationError, generate
from oxison.mdutil import strip_preamble
from oxison.repomap import build_repo_map


def test_strip_preamble_removes_chatter() -> None:
    raw = "Now I have enough information.\n\n# Tech Stack\n\nbody"
    assert strip_preamble(raw).startswith("# Tech Stack")


def test_strip_preamble_keeps_clean_body() -> None:
    raw = "# Product\n\nbody"
    assert strip_preamble(raw).startswith("# Product")


def test_strip_preamble_no_heading_untouched() -> None:
    raw = "just some text with no heading"
    assert strip_preamble(raw).strip() == "just some text with no heading"


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
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["click"]\n', encoding="utf-8"
    )
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_generate_all_three(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)
    seen_tools: list[tuple[str, ...]] = []

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        seen_tools.append(tuple(allowed_tools))
        # Echo which doc by inspecting the prompt for a unique marker.
        kind = "PRODUCT" if "PRODUCT document" in prompt else (
            "MANUAL" if "USER MANUAL" in prompt else "TECH STACK"
        )
        return InvokeResult(ok=True, text=f"# {kind}\nbody", cost_usd=0.03, exit_code=0)

    monkeypatch.setattr(gen_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path)
    rm = build_repo_map(tmp_path)
    arts = await generate(cfg, "comprehension text", rm)

    # All three artifacts written by oxison, into the output dir.
    assert {a.filename for a in arts} == set(ARTIFACTS.values())
    for a in arts:
        assert a.path.exists()
        assert a.path.parent == cfg.output_dir
        assert a.path.read_text().startswith("# ")

    # EVERY worker was read-only — the #1 invariant on the generation path.
    assert seen_tools  # at least one call
    for tools in seen_tools:
        assert tools == READ_ONLY_TOOLS


@pytest.mark.asyncio
async def test_generate_subset_for_resume(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=True, text="# DOC\nbody", cost_usd=0.01, exit_code=0)

    monkeypatch.setattr(gen_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path)
    rm = build_repo_map(tmp_path)
    arts = await generate(cfg, "c", rm, steps=["stack"])
    assert len(arts) == 1
    assert arts[0].filename == "STACK.md"


@pytest.mark.asyncio
async def test_generate_failure_surfaces(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=False, text="", cost_usd=0.0, exit_code=1, error="kaboom")

    monkeypatch.setattr(gen_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path)
    rm = build_repo_map(tmp_path)
    with pytest.raises(GenerationError, match="kaboom"):
        await generate(cfg, "c", rm)


@pytest.mark.asyncio
async def test_generate_empty_surfaces(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=True, text="  ", cost_usd=0.0, exit_code=0)

    monkeypatch.setattr(gen_mod, "invoke", fake_invoke)
    cfg = _cfg(tmp_path)
    rm = build_repo_map(tmp_path)
    with pytest.raises(GenerationError, match="empty"):
        await generate(cfg, "c", rm)


@pytest.mark.asyncio
async def test_generate_passes_extra_context(tmp_path: Path, monkeypatch) -> None:
    """extra_context kwarg is threaded into the prompt builder."""
    _fixture(tmp_path)
    captured: dict[str, str] = {}

    def fake_product_prompt(
        *, root: str, comprehension: str, repo_map_context: str, extra_context: str = ""
    ) -> str:
        captured["extra"] = extra_context
        return "PROMPT"

    async def fake_invoke(prompt, *, cfg, allowed_tools, cwd, timeout_s, binary="claude"):
        return InvokeResult(ok=True, text="# md\nbody", cost_usd=0.0, exit_code=0)

    monkeypatch.setattr(gen_mod, "product_prompt", fake_product_prompt)
    monkeypatch.setattr(gen_mod, "invoke", fake_invoke)

    rm = build_repo_map(tmp_path)
    cfg = _cfg(tmp_path)
    await generate(cfg, "COMP", rm, steps=["product"], extra_context="EXTRA-XYZ")
    assert captured["extra"] == "EXTRA-XYZ"
