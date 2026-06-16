"""Tests for the Layer-2 container dispatch builders (pure)."""

from __future__ import annotations

import os

import pytest

from oxison.engine.container import (
    DEFAULT_WORKER_IMAGE,
    build_run_argv,
    resolve_container_runtime,
)


def test_run_argv_mounts_only_the_workspace_and_drops_privilege(tmp_path):
    ws = tmp_path / "clone"
    ws.mkdir()
    argv = build_run_argv(
        runtime="/usr/bin/podman", image=DEFAULT_WORKER_IMAGE, workspace=ws,
        inner_argv=["claude", "-p", "--bare", "hi"],
    )
    assert argv[0] == "/usr/bin/podman"
    assert argv[1] == "run"
    assert "--rm" in argv
    # privilege drop
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    # the ONLY bind mount is the workspace (host fs otherwise absent)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert mounts == [f"{ws.resolve()}:/work:rw"]
    assert argv[argv.index("-w") + 1] == "/work"
    # the API key is forwarded by NAME, never as a literal value
    assert argv[argv.index("-e") + 1] == "ANTHROPIC_API_KEY"
    assert not any("sk-" in a for a in argv)
    # the inner claude argv comes after the image
    img_i = argv.index(DEFAULT_WORKER_IMAGE)
    assert argv[img_i + 1:] == ["claude", "-p", "--bare", "hi"]


def test_run_argv_forwards_provider_env_names(tmp_path):
    ws = tmp_path / "clone"
    ws.mkdir()
    argv = build_run_argv(
        runtime="/usr/bin/podman", image=DEFAULT_WORKER_IMAGE, workspace=ws,
        inner_argv=["claude", "-p", "--bare", "hi"],
        extra_env_names=["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"],
    )
    # Each provider var is forwarded by NAME (value stays in the host env).
    forwarded = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert "ANTHROPIC_API_KEY" in forwarded  # the default key forward stays
    assert "ANTHROPIC_BASE_URL" in forwarded
    assert "ANTHROPIC_AUTH_TOKEN" in forwarded
    # no literal token value ever lands in argv
    assert not any("sk-" in a or a.startswith("xai-") for a in argv)
    # forwards come before the image; inner argv stays after it
    img_i = argv.index(DEFAULT_WORKER_IMAGE)
    assert argv[img_i + 1:] == ["claude", "-p", "--bare", "hi"]


def test_resolve_runtime_absolute(tmp_path):
    exe = tmp_path / "podman"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o700)
    assert resolve_container_runtime(str(exe)) == str(exe)
    assert resolve_container_runtime(str(tmp_path / "nope")) is None


def test_resolve_runtime_discovers_on_path():
    # 'sh' always exists; a nonsense name does not — proves the which() branch.
    assert resolve_container_runtime("sh") is not None
    assert resolve_container_runtime("definitely-not-a-runtime-xyz") is None


def test_resolve_runtime_default_prefers_podman_then_docker(monkeypatch):
    seen = {}

    def fake_which(name):
        seen[name] = True
        return f"/usr/bin/{name}" if name == "docker" else None

    monkeypatch.setattr("oxison.engine.container.shutil.which", fake_which)
    # podman absent, docker present -> docker; and podman was tried first
    assert resolve_container_runtime() == "/usr/bin/docker"
    assert "podman" in seen


def test_run_argv_includes_name_for_cleanup(tmp_path):
    ws = tmp_path / "clone"
    ws.mkdir()
    argv = build_run_argv(runtime="podman", image=DEFAULT_WORKER_IMAGE, workspace=ws,
                          inner_argv=["claude"], name="oxfaz-oxpz-a")
    assert argv[argv.index("--name") + 1] == "oxfaz-oxpz-a"


@pytest.mark.asyncio
async def test_image_exists_uses_inspect_not_podman_only_exists(monkeypatch):
    import oxison.engine.container as c
    seen = {}

    async def fake_run(binary, args, *, timeout=60.0):
        seen["binary"] = binary
        seen["args"] = args
        return 0, ""

    monkeypatch.setattr(c, "_run_capture", fake_run)
    assert await c.image_exists("docker", "img:latest") is True
    # `image inspect` works on BOTH docker and podman; `image exists` is podman-only
    assert seen["args"] == ["image", "inspect", "img:latest"]


@pytest.mark.asyncio
async def test_launch_container_requires_api_key(tmp_path):
    from oxison.engine.container import launch_worker_container
    from oxison.engine.engconfig import EngineConfig
    out = await launch_worker_container(
        tmp_path / "repo", task_identifier="oxpz-a", task_title="t", rationale="",
        acceptance=["x"], files_hint=[], engine_config=EngineConfig(sandbox_layer="container"),
        api_key=None, model=None, runtime="podman", image="img",
        clone_root=tmp_path / "c", log_path=tmp_path / "l" / "x.log",
    )
    assert out.adapter_failure and "token auth" in (out.error or "")


@pytest.mark.asyncio
async def test_dispatch_routes_to_container_layer(tmp_path, monkeypatch):
    import oxison.engine.dispatch as d
    from oxison.engine.engconfig import EngineConfig
    captured = {}

    async def fake_container(repo, **kw):
        captured.update(kw)
        from oxison.engine.dispatch import DispatchOutcome
        return DispatchOutcome(ok=True, branch=kw["task_identifier"], worktree_path="x",
                               changed_files=["a.py"])

    # make routing resolve a runtime + delegate to the container path
    monkeypatch.setattr("oxison.engine.container.resolve_container_runtime",
                        lambda configured=None: "/usr/bin/podman")
    monkeypatch.setattr("oxison.engine.container.launch_worker_container", fake_container)
    out = await d.launch_worker(
        tmp_path, task_identifier="oxpz-a", task_title="t", rationale="", acceptance=["x"],
        files_hint=[], engine_config=EngineConfig(sandbox_enabled=True, sandbox_layer="container"),
        auth_mode="bare", api_key="sk-test", model=None,
        worktree_root=tmp_path / "oxison-build" / "worktrees", log_path=tmp_path / "l.log",
    )
    assert out.ok and captured["runtime"] == "/usr/bin/podman"
    assert captured["image"]  # the configured worker image was passed


def test_run_argv_mounts_srt_settings_read_only(tmp_path):
    # #14: when an srt settings file is provided, it is bind-mounted READ-ONLY at
    # the in-container path so the worker can't tamper with its own egress policy.
    ws = tmp_path / "clone"
    ws.mkdir()
    settings = tmp_path / "x.srt.json"
    settings.write_text("{}", encoding="utf-8")
    argv = build_run_argv(
        runtime="/usr/bin/podman", image=DEFAULT_WORKER_IMAGE, workspace=ws,
        inner_argv=["srt", "--settings", "/srt/settings.json", "claude", "-p", "hi"],
        srt_settings_host=settings,
    )
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert f"{settings.resolve()}:/srt/settings.json:ro" in mounts
    # without it, no /srt mount (back-compat)
    argv2 = build_run_argv(runtime="podman", image="img", workspace=ws,
                           inner_argv=["claude"])
    assert not any("/srt/settings.json" in a for a in argv2)


class _FakeProc:
    pid = 999_999_999  # nonexistent → os.getpgid raises ProcessLookupError → pgid=None
    returncode = 0

    async def wait(self):
        return 0


def _mock_container_io(monkeypatch, captured):
    import oxison.engine.container as c

    async def fake_capture(binary, args, *, timeout=60.0):
        return 0, "basesha123"

    async def fake_prepare(repo, clone_dir, branch):
        return True, ""

    async def fake_remove(runtime, name):
        return None

    async def fake_changed(clone_dir, base_sha):
        return ["a.py"]

    async def fake_exec(*argv, **kw):
        captured["argv"] = list(argv)
        return _FakeProc()

    monkeypatch.setattr(c, "_run_capture", fake_capture)
    monkeypatch.setattr(c, "prepare_clone", fake_prepare)
    monkeypatch.setattr(c, "remove_container", fake_remove)
    monkeypatch.setattr(c, "changed_files", fake_changed)
    monkeypatch.setattr(c, "extract_cost_from_log", lambda p: 0.0)
    monkeypatch.setattr(c.asyncio, "create_subprocess_exec", fake_exec)


@pytest.mark.asyncio
async def test_container_wraps_worker_with_srt_on_linux(tmp_path, monkeypatch):
    # #14: on Linux the worker runs under srt inside the container, with the
    # settings bind-mounted read-only — egress is narrowed to the allowlist.
    import oxison.engine.container as c
    from oxison.engine.engconfig import EngineConfig

    monkeypatch.setattr(c.platform, "system", lambda: "Linux")
    captured = {}
    _mock_container_io(monkeypatch, captured)

    out = await c.launch_worker_container(
        tmp_path / "repo", task_identifier="oxpz-a", task_title="t", rationale="",
        acceptance=["x"], files_hint=[], engine_config=EngineConfig(sandbox_layer="container"),
        api_key="sk-test", model=None, runtime="/usr/bin/podman", image="img",
        clone_root=tmp_path / "c", log_path=tmp_path / "l" / "x.log",
    )
    assert out.ok
    argv = captured["argv"]
    # the inner command is srt-wrapped, settings at the read-only mount
    assert "srt" in argv and "/srt/settings.json" in argv
    assert any(a.endswith(":/srt/settings.json:ro") for a in argv)
    # the host settings file was actually written
    assert (tmp_path / "l" / "oxpz-a.container.srt.json").is_file()


@pytest.mark.asyncio
async def test_container_skips_srt_on_macos_with_warning(tmp_path, monkeypatch, capsys):
    # #14: the macOS podman VM can't nest the srt sandbox, so the wrap is skipped
    # (no regression) with a loud warning; egress stays at the podman default.
    import oxison.engine.container as c
    from oxison.engine.engconfig import EngineConfig

    monkeypatch.setattr(c.platform, "system", lambda: "Darwin")
    # pass the $HOME-mount check so we reach the srt-skip branch
    monkeypatch.setattr(c.Path, "home", classmethod(lambda cls: tmp_path))
    captured = {}
    _mock_container_io(monkeypatch, captured)

    out = await c.launch_worker_container(
        tmp_path / "repo", task_identifier="oxpz-b", task_title="t", rationale="",
        acceptance=["x"], files_hint=[], engine_config=EngineConfig(sandbox_layer="container"),
        api_key="sk-test", model=None, runtime="/usr/bin/podman", image="img",
        clone_root=tmp_path / "c", log_path=tmp_path / "l" / "x.log",
    )
    assert out.ok
    argv = captured["argv"]
    assert "srt" not in argv and not any("/srt/settings.json" in a for a in argv)
    assert "narrowed on macOS" in capsys.readouterr().err
