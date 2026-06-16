"""Tests for the `oxison build` CLI subcommand (Oxfaz entrypoint)."""

from __future__ import annotations

import json
import types
from pathlib import Path

import oxison.cli as cli
import oxison.engine.loop as engine_loop
from oxison.engine.loop import LoopSummary


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # cmd_build only checks for .git existence
    return repo


def _write_roadmap(where: Path) -> Path:
    rm = {
        "schema_version": "1.0",
        "tasks": [
            {"identifier": "oxpz-a", "title": "Build A", "kind": "feature",
             "priority": 1, "acceptance": ["a works"], "files_hint": ["src/a.py"]},
            {"identifier": "oxpz-b", "title": "Build B", "kind": "fix",
             "priority": 2, "acceptance": ["b works"], "depends_on": ["oxpz-a"]},
        ],
    }
    p = where / "roadmap.json"
    p.write_text(json.dumps(rm), encoding="utf-8")
    return p


def test_build_parser():
    args = cli.build_parser().parse_args(
        ["build", "roadmap.json", "--repo", "some/repo", "--max-ticks", "5", "--dry-run"]
    )
    assert args.command == "build"
    assert args.max_ticks == 5 and args.dry_run is True


def test_build_parser_accepts_known_provider():
    args = cli.build_parser().parse_args(
        ["build", "roadmap.json", "--repo", "r", "--provider", "kimi", "--dry-run"]
    )
    assert args.provider == "kimi"


def test_build_parser_rejects_unknown_provider():
    import pytest
    with pytest.raises(SystemExit):  # argparse choices rejects at parse time
        cli.build_parser().parse_args(
            ["build", "roadmap.json", "--repo", "r", "--provider", "gpt5"]
        )


def test_build_dry_run_ingests(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    rm = _write_roadmap(tmp_path)
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(repo), "--dry-run"])
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    assert "oxpz-a" in out and "oxpz-b" in out
    # the taskstore was created under oxison-build/
    assert (repo / "oxison-build" / "state.db").is_file()


def test_build_repo_not_git(tmp_path, capsys):
    notrepo = tmp_path / "plain"
    notrepo.mkdir()
    rm = _write_roadmap(tmp_path)
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(notrepo), "--dry-run"])
    rc = args.func(args)
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().out


def test_build_missing_roadmap(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    args = cli.build_parser().parse_args(
        ["build", str(tmp_path / "nope.json"), "--repo", str(repo), "--dry-run"]
    )
    rc = args.func(args)
    assert rc == 2
    assert "no roadmap.json" in capsys.readouterr().out


def _patch_loop(monkeypatch):
    async def fake_loop(store, **kwargs):
        return LoopSummary(ticks=2, dispatched=2, merged=2, failed=0,
                           spent_usd=3.0, halt_reason="complete")
    monkeypatch.setattr(engine_loop, "run_build_loop", fake_loop)


def test_build_runs_loop(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    rm = _write_roadmap(tmp_path)
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test")
    )
    # sandbox is default-on → the srt preflight must pass; fake the binary so the
    # test doesn't depend on srt being installed in CI.
    import oxison.engine.sandbox as sb
    monkeypatch.setattr(sb, "resolve_srt_binary", lambda configured=None: "/fake/srt")
    _patch_loop(monkeypatch)
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(repo)])
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "build loop halted: complete" in out
    assert "merged=2" in out
    assert "sandbox" in out and "srt" in out  # status surfaced


def test_build_srt_missing_fails_at_preflight(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    rm = _write_roadmap(tmp_path)
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test")
    )
    import oxison.engine.sandbox as sb
    monkeypatch.setattr(sb, "resolve_srt_binary", lambda configured=None: None)
    _patch_loop(monkeypatch)
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(repo)])
    rc = args.func(args)
    assert rc == 3
    assert "srt runtime is not installed" in capsys.readouterr().out


def test_build_memory_on_by_default(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    rm = _write_roadmap(tmp_path)
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test")
    )
    import oxison.engine.sandbox as sb
    monkeypatch.setattr(sb, "resolve_srt_binary", lambda configured=None: "/fake/srt")
    seen = {}

    async def fake_loop(store, **kwargs):
        seen["recorder"] = kwargs.get("recorder")
        return LoopSummary(ticks=1, dispatched=1, merged=1, failed=0,
                           spent_usd=1.0, halt_reason="complete")
    monkeypatch.setattr(engine_loop, "run_build_loop", fake_loop)
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(repo)])
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory        : on" in out  # banner reflects default-on
    assert seen["recorder"] is not None  # capture hook wired into the loop
    assert (repo / "oxison-build" / "memory.db").exists()  # store opened


def test_build_no_memory_flag(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    rm = _write_roadmap(tmp_path)
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test")
    )
    import oxison.engine.sandbox as sb
    monkeypatch.setattr(sb, "resolve_srt_binary", lambda configured=None: "/fake/srt")
    seen = {}

    async def fake_loop(store, **kwargs):
        seen["recorder"] = kwargs.get("recorder")
        return LoopSummary(ticks=1, dispatched=1, merged=1, failed=0,
                           spent_usd=1.0, halt_reason="complete")
    monkeypatch.setattr(engine_loop, "run_build_loop", fake_loop)
    args = cli.build_parser().parse_args(
        ["build", str(rm), "--repo", str(repo), "--no-memory"]
    )
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory        : off (--no-memory)" in out
    assert seen["recorder"] is None  # no capture hook when disabled
    assert not (repo / "oxison-build" / "memory.db").exists()  # store never opened


def test_build_no_sandbox_warns_and_runs(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    rm = _write_roadmap(tmp_path)
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test")
    )
    _patch_loop(monkeypatch)  # no srt needed when sandbox is off
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(repo), "--no-sandbox"])
    rc = args.func(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert "UNSANDBOXED" in captured.err  # the warning is on stderr
    assert "sandbox       : OFF" in captured.out
