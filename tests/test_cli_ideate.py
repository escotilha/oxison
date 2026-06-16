"""`oxison ideate` CLI subcommand (greenfield pipeline mocked)."""
from __future__ import annotations

import types
from pathlib import Path

import oxison.cli as cli
import oxison.pipeline as pipeline


def _patch_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test-cli")
    )


def test_parser_accepts_ideate() -> None:
    args = cli.build_parser().parse_args(
        ["ideate", "--brief", "a todo app", "--url", "https://a.com", "--url", "https://b.com"]
    )
    assert args.command == "ideate"
    assert args.brief == "a todo app"
    assert args.url == ["https://a.com", "https://b.com"]


def test_ideate_requires_an_input(capsys) -> None:
    rc = cli.build_parser().parse_args(["ideate"]).func(
        cli.build_parser().parse_args(["ideate"])
    )
    assert rc == 2
    assert "at least one input" in capsys.readouterr().out


def test_ideate_brief_and_brief_file_mutually_exclusive(tmp_path: Path) -> None:
    bf = tmp_path / "brief.txt"
    bf.write_text("idea", encoding="utf-8")
    args = cli.build_parser().parse_args(
        ["ideate", "--brief", "x", "--brief-file", str(bf)]
    )
    assert args.func(args) == 2


def test_ideate_dispatches_with_brief(tmp_path: Path, monkeypatch) -> None:
    _patch_preflight(monkeypatch)
    captured = {}

    async def fake_greenfield(cfg, *, user_guidance="", max_tasks=40, relevance_min_score=0.25):
        captured["brief"] = cfg.brief
        captured["urls"] = cfg.urls
        captured["max_tasks"] = max_tasks
        captured["relevance_min_score"] = relevance_min_score
        return 0

    monkeypatch.setattr(pipeline, "greenfield_pipeline", fake_greenfield)

    args = cli.build_parser().parse_args(
        ["ideate", "--brief", "build a todo app", "--url", "https://x.com",
         "--output-dir", str(tmp_path / "out"), "--max-tasks", "7"]
    )
    rc = args.func(args)
    assert rc == 0
    assert captured["brief"] == "build a todo app"
    assert captured["urls"] == ["https://x.com"]
    assert captured["max_tasks"] == 7
    assert captured["relevance_min_score"] == 0.25  # flag default threads through


def test_ideate_reads_brief_file(tmp_path: Path, monkeypatch) -> None:
    _patch_preflight(monkeypatch)
    bf = tmp_path / "brief.txt"
    bf.write_text("  idea from file  ", encoding="utf-8")
    seen = {}

    async def fake_greenfield(cfg, *, user_guidance="", max_tasks=40, relevance_min_score=0.25):
        seen["brief"] = cfg.brief
        return 0

    monkeypatch.setattr(pipeline, "greenfield_pipeline", fake_greenfield)
    args = cli.build_parser().parse_args(
        ["ideate", "--brief-file", str(bf), "--output-dir", str(tmp_path / "out")]
    )
    assert args.func(args) == 0
    assert seen["brief"] == "idea from file"  # stripped
