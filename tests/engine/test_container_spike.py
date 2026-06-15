"""Layer-2 container spike — self-contained clone + real host-isolation check.

The clone test needs only git (runs in CI). The containment test runs the real
worker image and is skipped unless podman + the image are present (so it runs on
a dev box with Layer 2 set up, skipped in CI like the OCR/srt spikes). Both
file-check, never trusting output.

macOS note: a workspace only mounts into the podman VM if it lives under a shared
host path ($HOME), so the containment test uses a $HOME-rooted temp dir, not the
default pytest tmp (which is under /var/folders and is NOT shared into the VM).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from oxison.engine.container import (
    DEFAULT_WORKER_IMAGE,
    build_run_argv,
    prepare_clone,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


# -- clone model (git only — CI-runnable) --------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
@pytest.mark.asyncio
async def test_prepare_clone_is_self_contained(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("v1")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    dest = tmp_path / "clone"
    ok, msg = await prepare_clone(repo, dest, "feat/x")
    assert ok, msg
    # the clone's object store is real files INSIDE dest (no hardlink to the host
    # repo) — so it survives being the only thing mounted into a container.
    assert (dest / ".git" / "objects").is_dir()
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    # it is on the requested branch and has the source history
    branch = subprocess.run(["git", "-C", str(dest), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "feat/x"
    assert (dest / "f.txt").read_text() == "v1"


# -- real container isolation (needs podman + the worker image) -----------

def _image_present() -> bool:
    if not shutil.which("podman"):
        return False
    r = subprocess.run(["podman", "image", "exists", DEFAULT_WORKER_IMAGE], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _image_present(), reason="podman + worker image required")
def test_container_isolates_host_filesystem():
    # workspace under $HOME so it mounts into the podman VM (macOS constraint)
    ws = Path(tempfile.mkdtemp(prefix="oxi-l2-test-", dir=Path.home()))
    try:
        (ws / "marker.txt").write_text("in-work")
        probe = (
            "test -e /Users && echo HOST_USERS_VISIBLE || echo users_absent; "
            "ls ~/.ssh >/dev/null 2>&1 && echo SSH_VISIBLE || echo ssh_absent; "
            "echo x > /work/w && echo work_ok || echo work_fail; "
            "echo x > /etc/p 2>/dev/null && echo ETC_LEAK || echo etc_blocked"
        )
        argv = build_run_argv(
            runtime="podman", image=DEFAULT_WORKER_IMAGE, workspace=ws,
            inner_argv=["sh", "-c", probe],
        )
        out = subprocess.run(argv, capture_output=True, text=True, timeout=120).stdout
        # the host filesystem is physically absent; only /work is writable
        assert "users_absent" in out and "HOST_USERS_VISIBLE" not in out
        assert "ssh_absent" in out and "SSH_VISIBLE" not in out
        assert "work_ok" in out
        assert "etc_blocked" in out and "ETC_LEAK" not in out
        # and the worker actually wrote into the mounted workspace
        assert (ws / "w").exists()
    finally:
        shutil.rmtree(ws, ignore_errors=True)
