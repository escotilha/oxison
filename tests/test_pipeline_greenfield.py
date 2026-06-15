"""greenfield_pipeline — offline (comprehend/generate/plan mocked, brief-only)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import oxison.oxipensa as oxipensa
import oxison.pipeline as pipeline
from oxison.comprehend import Comprehension
from oxison.config import build_greenfield_config
from oxison.generate import GeneratedArtifact
from oxison.oxipensa import PlanResult
from oxison.roadmap_doc import build_roadmap_doc

_ARTIFACTS = ("COMPREHENSION.md", "comprehension.json", "PRODUCT.md", "ROADMAP.md", "roadmap.json")


def _fake_plan_result() -> PlanResult:
    raw = {
        "summary": "s",
        "tasks": [
            {"title": "Scaffold project", "kind": "infra", "priority": 1,
             "acceptance": ["the project builds"]}
        ],
    }
    doc = build_roadmap_doc(
        raw=raw,
        source={"schema_version": "1.0", "generated_at": "t", "product_what": "App"},
        generated_at="t",
    )
    return PlanResult(doc=doc, cost_usd=0.3, attempts=1)


def test_greenfield_pipeline_writes_all_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    cfg = build_greenfield_config(
        output_dir=str(out), bare=False, api_key=None, model=None,
        max_budget_usd=None, brief="build a todo app", env={},
    )

    async def fake_comprehend(cfg_, repo_map, *, extra_context="", mode="repo"):
        assert mode == "greenfield"
        assert "brief:idea" in extra_context  # the brief reached the worker
        return Comprehension(text="# Understanding\nA todo app.", total_cost_usd=0.1)

    async def fake_generate(
        cfg_, comprehension, repo_map, *, steps=None, extra_context="", mode="repo"
    ):
        assert mode == "greenfield"
        assert steps == ["product"]
        cfg_.output_dir.mkdir(parents=True, exist_ok=True)
        path = cfg_.output_dir / "PRODUCT.md"
        path.write_text("# Product", encoding="utf-8")
        return [GeneratedArtifact(step="product", filename="PRODUCT.md", path=path, cost_usd=0.2)]

    async def fake_plan(
        cfg_, comprehension, *, generated_at, user_guidance="", max_tasks=40, greenfield=False
    ):
        assert greenfield is True
        return _fake_plan_result()

    monkeypatch.setattr(pipeline, "comprehend", fake_comprehend)
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    monkeypatch.setattr(oxipensa, "plan", fake_plan)

    rc = asyncio.run(pipeline.greenfield_pipeline(cfg))
    assert rc == 0

    for name in _ARTIFACTS:
        assert (out / name).is_file(), f"missing {name}"

    # ledger: the brief is recorded; there is NO synthetic "git" source.
    ledger = json.loads((out / "comprehension.json").read_text())["sources"]
    types = {s["type"] for s in ledger}
    assert "brief" in types
    assert "git" not in types


def test_greenfield_pipeline_no_input_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # brief None, no sources, no urls → empty extra_context → non-zero, no AI call.
    cfg = build_greenfield_config(
        output_dir=str(tmp_path / "out"), bare=False, api_key=None, model=None,
        max_budget_usd=None, brief=None, env={},
    )

    async def _should_not_run(*_a, **_k):
        raise AssertionError("comprehend should not be called with no input")
    monkeypatch.setattr(pipeline, "comprehend", _should_not_run)

    rc = asyncio.run(pipeline.greenfield_pipeline(cfg))
    assert rc == 4
