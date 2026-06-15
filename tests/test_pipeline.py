from __future__ import annotations

import json
from pathlib import Path

import pytest

import oxison.pipeline as pipe
from oxison.branch import BranchResult
from oxison.comprehend import Comprehension
from oxison.config import build_run_config
from oxison.generate import ARTIFACTS, GeneratedArtifact
from oxison.manifest import RunManifest


def _fake_branch_factory(calls: list[int]):
    async def fake_branch(cfg_, repo_map, comprehension):
        calls.append(1)
        path = cfg_.output_dir / "SECURITY-NOTES.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Security Notes\nscan", encoding="utf-8")
        return BranchResult(
            kind="security", filename="SECURITY-NOTES.md", path=path, cost_usd=0.07
        )

    return fake_branch


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
    (r / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
    return r


@pytest.mark.asyncio
async def test_full_pipeline_offline(tmp_path: Path, monkeypatch) -> None:
    _repo(tmp_path)
    cfg = _cfg(tmp_path)

    async def fake_comprehend(cfg_, repo_map, *, extra_context=""):
        return Comprehension(text="# Understanding\nstuff", total_cost_usd=0.10)

    async def fake_generate(cfg_, comprehension, repo_map, *, steps=None, extra_context=""):
        out = []
        for step in (steps or list(ARTIFACTS)):
            filename = ARTIFACTS[step]
            path = cfg_.output_dir / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {step}\nbody", encoding="utf-8")
            out.append(GeneratedArtifact(step=step, filename=filename, path=path, cost_usd=0.05))
        return out

    branch_calls: list[int] = []
    monkeypatch.setattr(pipe, "comprehend", fake_comprehend)
    monkeypatch.setattr(pipe, "generate", fake_generate)
    monkeypatch.setattr(pipe, "run_branch", _fake_branch_factory(branch_calls))

    manifest = RunManifest.load_or_create(cfg.output_dir, target=str(cfg.target), started_at="t")
    rc = await pipe.run_pipeline(cfg, manifest)
    assert rc == 0

    # All artifacts written into the output dir.
    for filename in ("repomap.json", "COMPREHENSION.md", "SECURITY-NOTES.md", *ARTIFACTS.values()):
        assert (cfg.output_dir / filename).exists()

    # Manifest marks every stage done with costs.
    for step in ("map", "comprehend", "product", "manual", "stack", "branch"):
        assert manifest.is_complete(step)
    # total = comprehend 0.10 + 3*0.05 + branch 0.07 = 0.32
    # (map/ingest/comprehension_json are free)
    assert manifest.total_cost_usd() == pytest.approx(0.32)


@pytest.mark.asyncio
async def test_resume_skips_completed(tmp_path: Path, monkeypatch) -> None:
    _repo(tmp_path)
    cfg = _cfg(tmp_path, resume=True)

    comp_calls = 0
    gen_steps: list[list[str]] = []

    async def fake_comprehend(cfg_, repo_map, *, extra_context=""):
        nonlocal comp_calls
        comp_calls += 1
        return Comprehension(text="# U\nx", total_cost_usd=0.10)

    async def fake_generate(cfg_, comprehension, repo_map, *, steps=None, extra_context=""):
        gen_steps.append(list(steps or list(ARTIFACTS)))
        out = []
        for step in (steps or list(ARTIFACTS)):
            path = cfg_.output_dir / ARTIFACTS[step]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# d\nb", encoding="utf-8")
            out.append(
                GeneratedArtifact(
                    step=step, filename=ARTIFACTS[step], path=path, cost_usd=0.05
                )
            )
        return out

    branch_calls: list[int] = []
    monkeypatch.setattr(pipe, "comprehend", fake_comprehend)
    monkeypatch.setattr(pipe, "generate", fake_generate)
    monkeypatch.setattr(pipe, "run_branch", _fake_branch_factory(branch_calls))

    manifest = RunManifest.load_or_create(cfg.output_dir, target=str(cfg.target), started_at="t")

    # First run: everything executes.
    await pipe.run_pipeline(cfg, manifest)
    assert comp_calls == 1
    assert gen_steps[0] == list(ARTIFACTS)
    assert len(branch_calls) == 1

    # Second run with --resume: everything cached, nothing re-run.
    await pipe.run_pipeline(cfg, manifest)
    assert comp_calls == 1  # not called again
    assert len(gen_steps) == 1  # generate not called a second time
    assert len(branch_calls) == 1  # branch not called a second time


@pytest.mark.asyncio
async def test_pipeline_ingests_sources_and_writes_comprehension_json(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print(1)", encoding="utf-8")
    extra = tmp_path / "notes.md"
    extra.write_text("the product plans X", encoding="utf-8")

    captured: dict[str, str] = {}

    async def fake_comprehend(cfg_, repo_map, *, extra_context=""):
        captured["extra"] = extra_context
        return Comprehension(text="# comp", total_cost_usd=0.0)

    async def fake_generate(cfg_, comp_text, repo_map, *, steps=None, extra_context=""):
        captured["gen_extra"] = extra_context
        out = []
        for s in steps or list(ARTIFACTS):
            path = cfg_.output_dir / ARTIFACTS[s]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
            out.append(GeneratedArtifact(step=s, filename=ARTIFACTS[s], path=path, cost_usd=0.0))
        return out

    branch_calls: list[int] = []
    monkeypatch.setattr(pipe, "comprehend", fake_comprehend)
    monkeypatch.setattr(pipe, "generate", fake_generate)
    monkeypatch.setattr(pipe, "run_branch", _fake_branch_factory(branch_calls))

    cfg = _cfg(tmp_path, target=str(repo), extra_sources=[str(extra)])
    manifest = RunManifest.load_or_create(cfg.output_dir, target=str(repo), started_at="t")

    rc = await pipe.run_pipeline(cfg, manifest)
    assert rc == 0

    # the extra source text reached comprehension
    assert "the product plans X" in captured["extra"]
    assert "the product plans X" in captured["gen_extra"]   # reached generate too

    # comprehension.json written, valid, with the source in the ledger
    cj = cfg.output_dir / "comprehension.json"
    assert cj.exists()
    blob = json.loads(cj.read_text(encoding="utf-8"))
    assert blob["schema_version"] == "1.0"
    origins = {s["origin"] for s in blob["sources"]}
    assert str(extra) in origins        # the .md source is in the ledger
    assert str(repo) in origins         # the git repo is in the ledger too
