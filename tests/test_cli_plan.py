"""Tests for the `oxison plan` CLI subcommand (planner mocked)."""

from __future__ import annotations

import json
import types
from pathlib import Path

import oxison.cli as cli
import oxison.oxipensa as oxipensa
from oxison.oxipensa import PlanError, PlanResult
from oxison.roadmap_doc import build_roadmap_doc


def _write_comprehension(tmp_path: Path) -> Path:
    data = {
        "schema_version": "1.0",
        "generated_at": "t",
        "product": {"what": "App"},
        "open_questions": [],
        "comprehension_markdown": "# App",
    }
    p = tmp_path / "comprehension.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _fake_result() -> PlanResult:
    raw = {"summary": "s", "tasks": [
        {"title": "Do X", "kind": "feature", "priority": 1, "acceptance": ["X works"]}]}
    doc = build_roadmap_doc(
        raw=raw,
        source={"schema_version": "1.0", "generated_at": "t", "product_what": "App"},
        generated_at="t",
    )
    return PlanResult(doc=doc, cost_usd=0.05, attempts=1)


def _patch_preflight(monkeypatch):
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test-cli")
    )


def test_parser_accepts_plan_subcommand(tmp_path):
    args = cli.build_parser().parse_args(
        ["plan", str(tmp_path), "--max-tasks", "10", "--repo", str(tmp_path)]
    )
    assert args.command == "plan"
    assert args.max_tasks == 10


def test_cmd_plan_writes_artifacts(tmp_path, monkeypatch, capsys):
    _write_comprehension(tmp_path)
    _patch_preflight(monkeypatch)

    async def fake_plan(cfg, comprehension, **kwargs):
        return _fake_result()

    monkeypatch.setattr(oxipensa, "plan", fake_plan)

    args = cli.build_parser().parse_args(["plan", str(tmp_path)])
    rc = args.func(args)
    assert rc == 0
    assert (tmp_path / "roadmap.json").is_file()
    assert (tmp_path / "ROADMAP.md").is_file()
    roadmap = json.loads((tmp_path / "roadmap.json").read_text())
    assert roadmap["tasks"][0]["title"] == "Do X"


def test_cmd_plan_missing_comprehension(tmp_path, monkeypatch, capsys):
    _patch_preflight(monkeypatch)
    args = cli.build_parser().parse_args(["plan", str(tmp_path)])
    rc = args.func(args)
    assert rc == 2
    assert "no comprehension.json" in capsys.readouterr().out


def test_cmd_plan_repo_grounds_target(tmp_path, monkeypatch, capsys):
    _write_comprehension(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_preflight(monkeypatch)
    captured = {}

    async def fake_plan(cfg, comprehension, **kwargs):
        captured["target"] = cfg.target
        return _fake_result()

    monkeypatch.setattr(oxipensa, "plan", fake_plan)
    args = cli.build_parser().parse_args(["plan", str(tmp_path), "--repo", str(repo)])
    rc = args.func(args)
    assert rc == 0
    assert captured["target"] == repo.resolve()


def test_cmd_plan_planner_error(tmp_path, monkeypatch, capsys):
    _write_comprehension(tmp_path)
    _patch_preflight(monkeypatch)

    async def boom(cfg, comprehension, **kwargs):
        raise PlanError("gate rejected")

    monkeypatch.setattr(oxipensa, "plan", boom)
    args = cli.build_parser().parse_args(["plan", str(tmp_path)])
    rc = args.func(args)
    assert rc == 5
    assert "planning failed" in capsys.readouterr().out


def test_cmd_plan_answers_file_missing(tmp_path, monkeypatch, capsys):
    _write_comprehension(tmp_path)
    _patch_preflight(monkeypatch)
    args = cli.build_parser().parse_args(
        ["plan", str(tmp_path), "--answers-file", str(tmp_path / "nope.txt")]
    )
    rc = args.func(args)
    assert rc == 2
    assert "answers-file not found" in capsys.readouterr().out
