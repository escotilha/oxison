"""Live sandbox spike — runs the REAL srt policy and file-checks containment.

Skipped unless `srt` + `git` are on PATH (so it runs on a dev box with srt
installed, and is skipped in CI where srt isn't provisioned — like the OCR/
recording adapter tests). This is the regression guard for the verification the
design spec calls for: a worker CAN build+commit in its worktree, and CANNOT
escape it. Containment is asserted by FILE-CHECK, never by trusting output
(per verification-cadence).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from oxison.engine.sandbox import (
    DEFAULT_SANDBOX_DOMAINS,
    build_srt_settings,
    write_srt_settings,
)

pytestmark = pytest.mark.skipif(
    not (shutil.which("srt") and shutil.which("git")),
    reason="srt + git required for the live sandbox spike",
)


def _srt(cfg: Path, cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["srt", "--settings", str(cfg), "-c", cmd],
        capture_output=True, text=True, timeout=60,
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*a: str) -> None:
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "f.txt").write_text("v1")
    g("add", "-A")
    g("commit", "-qm", "init")
    wt = repo / "oxison-build" / "worktrees" / "oxpz-a"
    g("worktree", "add", "-q", str(wt), "-b", "feat/oxison-oxpz-a", "HEAD")
    cfg = tmp_path / "oxpz-a.srt.json"
    write_srt_settings(
        cfg,
        build_srt_settings(
            worktree=wt, repo=repo, task_identifier="oxpz-a",
            home=Path.home(), allowed_domains=DEFAULT_SANDBOX_DOMAINS,
            # scoped scratch (mirrors launch_worker) — NOT the system temp, which
            # contains tmp_path itself and would make the outside-write check moot.
            tmpdir=str(tmp_path / "scratch"),
        ),
    )
    return repo, wt, cfg


def test_spike_positive_build_and_commit_in_worktree(tmp_path):
    repo, wt, cfg = _setup(tmp_path)
    r = _srt(cfg, f"cd {wt} && echo multiply > calc.py && git add -A && "
                  f"git commit -qm work && echo COMMIT_OK")
    assert "COMMIT_OK" in r.stdout, f"commit failed under sandbox: {r.stderr}"
    # the change is committed in the worktree's branch
    log = subprocess.run(["git", "-C", str(wt), "log", "--oneline"],
                         capture_output=True, text=True)
    assert "work" in log.stdout


def test_spike_blocks_git_hook_install(tmp_path):
    repo, wt, cfg = _setup(tmp_path)
    _srt(cfg, f"echo '#!/bin/sh' > {repo}/.git/hooks/post-commit")
    assert not (repo / ".git" / "hooks" / "post-commit").exists()


def test_spike_blocks_git_config_rewrite(tmp_path):
    repo, wt, cfg = _setup(tmp_path)
    before = (repo / ".git" / "config").read_text()
    _srt(cfg, f"echo '[evil]' >> {repo}/.git/config")
    assert (repo / ".git" / "config").read_text() == before


def test_spike_blocks_write_outside_worktree(tmp_path):
    repo, wt, cfg = _setup(tmp_path)
    _srt(cfg, f"echo escape > {repo}/escaped.txt")
    assert not (repo / "escaped.txt").exists()
