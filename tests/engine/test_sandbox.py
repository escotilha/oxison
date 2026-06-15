"""Tests for the srt sandbox policy builder (pure / deterministic)."""

from __future__ import annotations

import os
from pathlib import Path

from oxison.engine.sandbox import (
    DEFAULT_CRED_DENY,
    DEFAULT_SANDBOX_DOMAINS,
    build_srt_settings,
    resolve_srt_binary,
    srt_wrap,
    write_srt_settings,
)


def _settings(tmp_path, **over):
    repo = tmp_path / "repo"
    worktree = repo / "oxison-build" / "worktrees" / "oxpz-a"
    kw = {
        "worktree": worktree, "repo": repo, "task_identifier": "oxpz-a",
        "home": Path("/home/u"), "allowed_domains": DEFAULT_SANDBOX_DOMAINS,
        "tmpdir": str(tmp_path / "tmp"),
    }
    kw.update(over)
    return build_srt_settings(**kw), repo, worktree


def test_allowwrite_git_with_dangerous_carveouts(tmp_path):
    s, repo, worktree = _settings(tmp_path)
    aw = s["filesystem"]["allowWrite"]
    dw = s["filesystem"]["denyWrite"]
    assert str(worktree.resolve()) in aw
    # whole .git is writable (so commit's refs/objects/lock files all work)...
    assert str(repo / ".git") in aw
    # ...but the code-execution surfaces are carved back out via denyWrite, so a
    # worker still cannot install a hook or rewrite git config.
    assert str(repo / ".git" / "config") in dw
    assert str(repo / ".git" / "hooks") in dw


def test_claude_home_writable_and_never_denied_read(tmp_path):
    # H3: OAuth creds live under ~/.claude — must stay writable AND readable.
    s, _, _ = _settings(tmp_path)
    aw = s["filesystem"]["allowWrite"]
    dr = s["filesystem"]["denyRead"]
    assert "/home/u/.claude" in aw
    assert "/home/u/.claude.json" in aw
    assert "/home/u/.claude" not in dr
    assert "/home/u/.claude.json" not in dr


def test_denyread_covers_credentials(tmp_path):
    s, _, _ = _settings(tmp_path)
    dr = s["filesystem"]["denyRead"]
    for rel in DEFAULT_CRED_DENY:
        assert f"/home/u/{rel}" in dr


def test_settings_has_all_four_required_keys(tmp_path):
    # srt 1.0 rejects a config missing any of these (falls back to deny-all).
    s, _, _ = _settings(tmp_path)
    assert set(s["filesystem"]) == {"allowWrite", "denyWrite", "denyRead"}
    assert set(s["network"]) == {"allowedDomains", "deniedDomains"}


def test_network_allowlist_passthrough(tmp_path):
    s, _, _ = _settings(tmp_path, allowed_domains=("api.anthropic.com", "pypi.org"))
    assert s["network"]["allowedDomains"] == ["api.anthropic.com", "pypi.org"]
    # default set includes the registries + git host (owner decision)
    assert "github.com" in DEFAULT_SANDBOX_DOMAINS
    assert "registry.npmjs.org" in DEFAULT_SANDBOX_DOMAINS
    assert "api.anthropic.com" in DEFAULT_SANDBOX_DOMAINS


def test_extra_write_paths_appended(tmp_path):
    s, _, _ = _settings(tmp_path, extra_write_paths=("/shared/cache",))
    assert "/shared/cache" in s["filesystem"]["allowWrite"]


def test_tmpdir_writable(tmp_path):
    scratch = str(tmp_path / "scratch")
    s, _, _ = _settings(tmp_path, tmpdir=scratch)
    assert scratch in s["filesystem"]["allowWrite"]


def test_write_srt_settings_roundtrips(tmp_path):
    import json
    s, _, _ = _settings(tmp_path)
    p = tmp_path / "out" / "oxpz-a.srt.json"
    write_srt_settings(p, s)
    assert json.loads(p.read_text()) == s


def test_resolve_srt_binary_absolute_executable(tmp_path):
    exe = tmp_path / "srt"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o700)  # owner-executable is enough for the X_OK check
    assert resolve_srt_binary(str(exe)) == str(exe)


def test_resolve_srt_binary_absolute_missing(tmp_path):
    assert resolve_srt_binary(str(tmp_path / "nope")) is None


def test_resolve_srt_binary_on_path():
    # sh is always on PATH — proves the which() branch works.
    assert resolve_srt_binary("sh") is not None
    assert resolve_srt_binary("definitely-not-a-real-binary-xyz") is None


def test_srt_wrap_prepends():
    argv = srt_wrap("/abs/srt", Path("/cfg.json"), ["claude", "-p", "hi"])
    assert argv == ["/abs/srt", "--settings", "/cfg.json", "claude", "-p", "hi"]
