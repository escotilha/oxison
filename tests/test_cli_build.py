"""Tests for the `oxison build` CLI subcommand (Oxfaz entrypoint)."""

from __future__ import annotations

import json
import shutil
import subprocess
import types
from pathlib import Path

import pytest

import oxison.cli as cli
import oxison.engine.loop as engine_loop
from oxison.engine.loop import LoopSummary


def _real_git_repo(tmp_path: Path) -> Path:
    """A real git repo on `main` with one commit — needed for the --integrate
    redirect path, which runs real git (current_branch / ensure_integration_branch)."""
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return repo


def _branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()


def _branch_exists(repo: Path, name: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True, text=True,
    ).returncode == 0


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


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


def _write_roadmap_with_hint(where: Path, files_hint: list[str]) -> Path:
    rm = {
        "schema_version": "1.0",
        "tasks": [
            {"identifier": "oxpz-x", "title": "Tamper", "kind": "infra",
             "priority": 1, "acceptance": ["x"], "files_hint": files_hint},
        ],
    }
    p = where / "roadmap.json"
    p.write_text(json.dumps(rm), encoding="utf-8")
    return p


def test_build_rejects_roadmap_targeting_protected_path(tmp_path, capsys):
    # SECURITY-AUDIT.md F5: a hand-crafted roadmap.json fed straight to
    # `oxison build` must be rejected at ingest if any task targets a protected
    # path — before any worker is dispatched, not only by the post-diff grader.
    repo = _git_repo(tmp_path)
    rm = _write_roadmap_with_hint(tmp_path, ["uv.lock"])
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(repo), "--dry-run"])
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 2
    assert "protected path" in out
    assert "uv.lock" in out
    # rejection happens before ingest — no taskstore work, no DRY RUN output.
    assert "DRY RUN" not in out


def test_build_rejects_ci_config_and_git(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    rm = _write_roadmap_with_hint(tmp_path, [".github/workflows/ci.yml", ".git/config"])
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(repo), "--dry-run"])
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 2
    assert "protected path" in out


def test_build_allows_benign_files_hint(tmp_path, capsys):
    # Regression guard: a legitimate terse roadmap (non-protected paths) must
    # still ingest unchanged — the gate is protected-path-only, not a full re-gate.
    repo = _git_repo(tmp_path)
    rm = _write_roadmap_with_hint(tmp_path, ["src/feature.py", "tests/test_feature.py"])
    args = cli.build_parser().parse_args(["build", str(rm), "--repo", str(repo), "--dry-run"])
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    assert "protected path" not in out


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


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_build_integrate_redirects_to_branch_and_restores(tmp_path, monkeypatch, capsys):
    """On a protected branch, --integrate composes onto oxison/integration, leaves
    main unmoved, and restores the user to main at the end."""
    repo = _real_git_repo(tmp_path)
    rm = _write_roadmap(tmp_path)
    main_head = _head(repo)
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test")
    )
    import oxison.engine.sandbox as sb
    monkeypatch.setattr(sb, "resolve_srt_binary", lambda configured=None: "/fake/srt")

    async def fake_loop(store, **kwargs):
        return LoopSummary(ticks=1, dispatched=1, merged=1, failed=0,
                           spent_usd=1.0, halt_reason="complete", integrated=1)
    monkeypatch.setattr(engine_loop, "run_build_loop", fake_loop)

    args = cli.build_parser().parse_args(
        ["build", str(rm), "--repo", str(repo), "--integrate"]
    )
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert _branch(repo) == "main"                       # original branch restored
    assert _branch_exists(repo, "oxison/integration")    # redirect target created
    assert _head(repo) == main_head                      # main NEVER advanced
    assert "oxison/integration" in out                   # reporting points at it


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_build_integrate_respects_custom_protected_branches(tmp_path, monkeypatch, capsys):
    """--protected-branches makes a non-default branch protected too: on `develop`
    with --protected-branches main,develop, --integrate redirects off it instead of
    advancing it in place."""
    repo = _real_git_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "develop"], check=True)
    develop_head = _head(repo)
    rm = _write_roadmap(tmp_path)
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test")
    )
    import oxison.engine.sandbox as sb
    monkeypatch.setattr(sb, "resolve_srt_binary", lambda configured=None: "/fake/srt")

    async def fake_loop(store, **kwargs):
        return LoopSummary(ticks=1, dispatched=1, merged=1, failed=0,
                           spent_usd=1.0, halt_reason="complete", integrated=1)
    monkeypatch.setattr(engine_loop, "run_build_loop", fake_loop)

    args = cli.build_parser().parse_args(
        ["build", str(rm), "--repo", str(repo), "--integrate",
         "--protected-branches", "main,develop"]
    )
    assert args.func(args) == 0
    assert _branch(repo) == "develop"                    # restored to develop
    assert _branch_exists(repo, "oxison/integration")    # redirected off develop
    assert _head(repo) == develop_head                   # develop NEVER advanced


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_build_integrate_dry_run_has_no_side_effect(tmp_path, capsys):
    """--integrate --dry-run must not check out / create the integration branch."""
    repo = _real_git_repo(tmp_path)
    rm = _write_roadmap(tmp_path)
    args = cli.build_parser().parse_args(
        ["build", str(rm), "--repo", str(repo), "--integrate", "--dry-run"]
    )
    rc = args.func(args)
    assert rc == 0
    assert _branch(repo) == "main"                       # still on main
    assert not _branch_exists(repo, "oxison/integration")  # no branch created


def test_scaffold_repo_inits_fresh_repo(tmp_path: Path) -> None:
    # Greenfield: --scaffold git-inits a fresh repo with an initial commit so the
    # build loop has a HEAD to branch workers from.
    from oxison.cli import _scaffold_repo
    target = tmp_path / "newproj"
    assert _scaffold_repo(target) == 0
    assert (target / ".git").is_dir()
    head = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    assert head.returncode == 0 and head.stdout.strip()  # an initial commit exists


def test_scaffold_repo_refuses_nonempty_nongit_dir(tmp_path: Path, capsys) -> None:
    # Safety: never scaffold over an existing non-git directory's files.
    from oxison.cli import _scaffold_repo
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("don't clobber me")
    assert _scaffold_repo(target) == 2
    assert not (target / ".git").exists()
    assert "non-empty" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_build_scaffold_inits_then_runs(tmp_path: Path, monkeypatch, capsys) -> None:
    # `oxison build --scaffold` on a not-yet-existing --repo: git-inits it, then
    # proceeds into the (faked) build loop — the greenfield plan->build path.
    rm = _write_roadmap(tmp_path)
    target = tmp_path / "fresh"  # does not exist yet
    monkeypatch.setattr(
        cli, "preflight", lambda cfg: types.SimpleNamespace(claude_version="test")
    )
    import oxison.engine.sandbox as sb
    monkeypatch.setattr(sb, "resolve_srt_binary", lambda configured=None: "/fake/srt")
    _patch_loop(monkeypatch)
    args = cli.build_parser().parse_args(
        ["build", str(rm), "--repo", str(target), "--scaffold"]
    )
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert (target / ".git").is_dir()              # scaffolded
    assert "scaffolded a fresh git repo" in out
    assert "build loop halted" in out              # proceeded into the loop
