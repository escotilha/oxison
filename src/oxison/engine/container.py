"""Sandbox Layer 2 — run the Oxfaz build worker inside a rootless container.

Layer 1 (srt) confines the worker with an OS-level allowlist on the host. Layer 2
goes further: the worker (`claude -p`, write tools, bypassPermissions) runs
INSIDE a rootless container whose ONLY bind-mount is the task's workspace, so the
host filesystem — ``~/.ssh``, the main repo, every credential — is physically
absent, not merely denied. The container's mount + network namespaces are the
boundary.

Two consequences shape this module:

* **Self-contained git.** A linked git worktree's ``.git`` points back into the
  host repo, which is not mounted. So the container path uses a standalone
  **clone** (its ``.git`` lives inside the mounted dir), and the worker commits
  there self-contained; the host reads the diff from the clone afterwards.
* **Bare-mode auth.** The macOS Keychain / OAuth store is unreachable from a
  Linux container, so the worker authenticates with an ``ANTHROPIC_API_KEY``
  injected as a run-time env var (never baked into the image).

The argv/command builders here are pure and unit-tested; the live ``podman
build``/``podman run`` calls are exercised by the integration spike.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from oxison.dispatch import generate_session_id

from .dispatch import (
    DEFAULT_WORKER_TIMEOUT_S,
    DispatchOutcome,
    build_worker_prompt,
    redact_secrets,
    worker_log_secrets,
)
from .engconfig import EngineConfig
from .gitutil import changed_files, extract_cost_from_log
from .invoke import ToolSet, build_argv, build_env, kill_process_group
from .sandbox import (
    DEFAULT_SANDBOX_DOMAINS,
    build_srt_settings,
    srt_wrap,
    write_srt_settings,
)

#: Container-internal paths (the worker's view): the clone is bind-mounted at
#: /work, and the srt settings file is bind-mounted read-only at this path.
_CONTAINER_WORK = Path("/work")
_CONTAINER_HOME = Path("/home/worker")
_CONTAINER_SRT_SETTINGS = Path("/srt/settings.json")

#: Container runtimes we support, in preference order (rootless first).
_RUNTIMES = ("podman", "docker")

#: Local image tag the worker runs from.
DEFAULT_WORKER_IMAGE = "localhost/oxfaz-worker:latest"

#: Where the Dockerfile lives, relative to the repo root of *oxison itself*.
DOCKERFILE_SUBDIR = "docker/oxfaz-worker"

_API_KEY_ENV = "ANTHROPIC_API_KEY"


def resolve_container_runtime(configured: str | None = None) -> str | None:
    """Absolute path to a container runtime (podman preferred), or None."""
    if configured:
        if os.path.isabs(configured):
            return configured if os.access(configured, os.X_OK) else None
        return shutil.which(configured)
    for name in _RUNTIMES:
        found = shutil.which(name)
        if found:
            return found
    return None


#: Generous default resource ceilings for the worker container (SECURITY-AUDIT.md
#: F6). These are CEILINGS, not reservations: ``--memory`` caps RAM, so a high
#: value never fails to start on a smaller host, and ``--pids-limit`` caps the
#: process/thread count to stop a fork bomb. Both are orders of magnitude past any
#: legitimate worker (a code-writing claude run + its tools), so they catch a
#: runaway (memory exhaustion / fork bomb) without ever biting real use. NOT a
#: ``--cpus`` throttle — that would slow every worker's wall-clock and could push
#: a slow-but-legitimate worker past the 30-min timeout into a spurious failure;
#: CPU starvation is a soft DoS the timeout already bounds. Override per run via
#: ``EngineConfig`` if a host needs different ceilings; pass ``None`` to omit.
DEFAULT_CONTAINER_MEMORY = "4g"
DEFAULT_CONTAINER_PIDS_LIMIT = 2048


def build_run_argv(
    *,
    runtime: str,
    image: str,
    workspace: Path,
    inner_argv: Sequence[str],
    api_key_env: str = _API_KEY_ENV,
    extra_env_names: Sequence[str] = (),
    srt_settings_host: Path | None = None,
    name: str | None = None,
    memory: str | None = DEFAULT_CONTAINER_MEMORY,
    pids_limit: int | None = DEFAULT_CONTAINER_PIDS_LIMIT,
) -> list[str]:
    """Build the ``podman run`` argv that runs the worker in the container.

    Containment knobs:
    - ``--rm`` ephemeral; ``--cap-drop ALL`` + ``--security-opt no-new-privileges``
      drop ambient privilege.
    - ``--memory`` / ``--pids-limit`` (F6) cap RAM + process count at generous
      ceilings so a runaway worker can't exhaust host memory or fork-bomb the box.
      Ceilings, not reservations — a high ``--memory`` never fails to start on a
      smaller host. ``None`` omits the flag.
    - ``workspace -> /work`` (rw) is the worker's only writable mount; nothing
      else from the host is visible.
    - ``srt_settings_host`` (when set) is bind-mounted **read-only** at
      ``/srt/settings.json`` so the in-container srt wrapper can narrow egress to
      the domain allowlist (M1). The inner argv is srt-wrapped by the caller.
    - ``-e <api_key_env>`` forwards the value from the runtime's own env (the key
      is referenced by name, never placed in argv). ``-e NAME`` with no ``=value``
      is a no-op when ``NAME`` is unset, so forwarding the Anthropic key name is
      harmless in provider mode (it is simply absent from the env).
    - ``extra_env_names`` forwards the provider overlay (``ANTHROPIC_BASE_URL`` +
      ``ANTHROPIC_AUTH_TOKEN`` + knobs) the same way — by name, values stay in env.
    """
    argv = [runtime, "run", "--rm"]
    if name:
        # A deterministic name so a timed-out run (where --rm does NOT fire,
        # because the client was killed) can be force-removed afterwards.
        argv += ["--name", name]
    argv += [
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-v", f"{workspace.resolve()}:/work:rw",
        "-w", "/work",
        "-e", api_key_env,
    ]
    if memory is not None:
        argv += ["--memory", memory]
    if pids_limit is not None:
        argv += ["--pids-limit", str(pids_limit)]
    if srt_settings_host is not None:
        argv += ["-v", f"{srt_settings_host.resolve()}:{_CONTAINER_SRT_SETTINGS}:ro"]
    for env_name in extra_env_names:
        argv += ["-e", env_name]
    argv += [image, *inner_argv]
    return argv


async def _run_capture(binary: str, args: list[str], *, timeout: float = 60.0) -> tuple[int, str]:
    """Run a subcommand capturing combined output (small outputs only)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, f"{binary}: not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, f"{binary} {' '.join(args[:2])} timed out"
    return proc.returncode or 0, (out or b"").decode("utf-8", errors="replace")


async def image_exists(runtime: str, image: str) -> bool:
    # `image inspect` works on BOTH podman and docker; `image exists` is
    # podman-only (docker would always 'fail' → Layer 2 unusable on docker).
    rc, _ = await _run_capture(runtime, ["image", "inspect", image])
    return rc == 0


async def remove_container(runtime: str, name: str) -> None:
    """Force-remove a container by name (idempotent — ignores 'no such')."""
    await _run_capture(runtime, ["rm", "-f", name], timeout=30.0)


async def build_image(runtime: str, dockerfile_dir: Path, image: str) -> tuple[int, str]:
    """Build the worker image from ``dockerfile_dir/Dockerfile``."""
    return await _run_capture(
        runtime, ["build", "-t", image, os.fspath(dockerfile_dir)], timeout=1200.0,
    )


async def prepare_clone(repo: Path, dest: Path, branch: str) -> tuple[bool, str]:
    """Create a self-contained clone of ``repo`` at ``dest`` on ``branch``.

    ``--no-hardlinks`` so the clone's object store is real files inside ``dest``
    (a hardlink to the host repo would break once only ``dest`` is mounted).
    """
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, msg = await _run_capture(
        "git", ["clone", "--no-hardlinks", "--quiet", os.fspath(repo), os.fspath(dest)],
    )
    if rc != 0:
        return False, f"git clone failed: {msg.strip()[:300]}"
    rc, msg = await _run_capture("git", ["-C", os.fspath(dest), "checkout", "-q", "-b", branch])
    if rc != 0:
        return False, f"git checkout -b failed: {msg.strip()[:300]}"
    return True, ""


async def launch_worker_container(
    repo: Path,
    *,
    task_identifier: str,
    task_title: str,
    rationale: str,
    acceptance: list[str],
    files_hint: list[str],
    engine_config: EngineConfig,
    api_key: str | None,
    model: str | None,
    runtime: str,
    image: str,
    clone_root: Path,
    log_path: Path,
    timeout_s: float = DEFAULT_WORKER_TIMEOUT_S,
    memory_block: str = "",
) -> DispatchOutcome:
    """Run one build worker inside a container; return the outcome.

    The worker builds + commits in a self-contained clone (mounted at /work); the
    host reads the diff from that clone afterwards. Token auth is required (bare
    mode — no host Keychain in the VM): either ``api_key`` (Anthropic) or a
    provider overlay on ``engine_config.provider_env`` (e.g. ``--provider kimi``).
    """
    branch = f"{engine_config.branch_prefix}{task_identifier}"
    clone_dir = clone_root / task_identifier
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not api_key and not engine_config.provider_env:
        return DispatchOutcome(
            ok=False, branch=branch, worktree_path=str(clone_dir), adapter_failure=True,
            error="container sandbox requires token auth (bare mode) — set "
            "ANTHROPIC_API_KEY / OXISON_API_KEY or pass --api-key, "
            "or select a provider (e.g. --provider kimi) with its key set",
        )

    # macOS: a path only mounts into the podman VM if it's under a shared host
    # dir ($HOME). A repo outside $HOME mounts an EMPTY /work and the failure
    # looks like "worker produced no changes" — fail fast with a clear message.
    if platform.system() == "Darwin" and Path.home() not in clone_dir.resolve().parents:
        return DispatchOutcome(
            ok=False, branch=branch, worktree_path=str(clone_dir), adapter_failure=True,
            error=f"on macOS the repo must live under $HOME ({Path.home()}) to mount "
            f"into the podman VM; {clone_dir} is not — move the repo under your home dir",
        )

    rc_base, base_out = await _run_capture("git", ["-C", os.fspath(repo), "rev-parse", "HEAD"])
    base_sha = base_out.strip() if rc_base == 0 else "HEAD"

    ok, msg = await prepare_clone(repo, clone_dir, branch)
    if not ok:
        return DispatchOutcome(
            ok=False, branch=branch, worktree_path=str(clone_dir),
            adapter_failure=True, error=msg,
        )

    prompt = build_worker_prompt(
        task_title, rationale=rationale, acceptance=acceptance,
        files_hint=files_hint, repo_name=repo.name, memory_block=memory_block,
    )
    inner_argv = build_argv(
        prompt, tool_set=ToolSet.FULL_WRITE, auth_mode="bare", model=model,
        max_budget_usd=engine_config.worker_max_budget_usd,
        session_id=generate_session_id(),
    )

    # M1 — narrow container egress to the domain allowlist by running the worker
    # under srt *inside* the container (its filesystem boundary is already the
    # mount; this adds the network axis srt enforces on Layer-1). srt needs
    # bubblewrap, which on the macOS podman VM can't nest a bind-mount of the
    # `-v` volume (a VM-overlay limitation, not present on a native Linux host
    # where Layer-2 deploys) — so we skip the wrap there to avoid breaking the
    # container path, keeping today's behavior with a loud warning.
    srt_settings_host: Path | None = None
    if platform.system() == "Darwin":
        print(
            "oxison: WARNING — container egress is NOT narrowed on macOS (the "
            "podman VM can't nest the srt sandbox); egress stays at the podman "
            "default. Run Layer-2 on Linux for egress control, or use "
            "--sandbox-layer srt.",
            file=sys.stderr,
        )
    else:
        domains = engine_config.sandbox_allowed_domains or DEFAULT_SANDBOX_DOMAINS
        settings = build_srt_settings(
            worktree=_CONTAINER_WORK, repo=_CONTAINER_WORK,
            task_identifier=task_identifier, home=_CONTAINER_HOME,
            # /tmp is the *container's* scratch dir (an allowWrite path inside the
            # sandbox), not a host temp file — S108/B108 don't apply here.
            allowed_domains=domains, tmpdir="/tmp",  # noqa: S108  # nosec B108
        )
        srt_settings_host = log_path.parent / f"{task_identifier}.container.srt.json"
        write_srt_settings(srt_settings_host, settings)
        # srt runs from PATH inside the image; settings are at the read-only mount.
        inner_argv = srt_wrap("srt", _CONTAINER_SRT_SETTINGS, list(inner_argv))

    container_name = f"oxfaz-{task_identifier}"
    # Remove any stale container from a prior attempt (deterministic name).
    await remove_container(runtime, container_name)
    argv = build_run_argv(
        runtime=runtime, image=image, workspace=clone_dir,
        inner_argv=inner_argv, name=container_name,
        srt_settings_host=srt_settings_host,
        # In provider mode, forward the overlay var names so the in-VM worker
        # gets ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (+ knobs) too.
        extra_env_names=[k for k, _ in engine_config.provider_env],
    )
    # api_key -> ANTHROPIC_API_KEY (Anthropic) or None (provider mode); the
    # provider overlay supplies ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN for `-e`.
    env = build_env(api_key=api_key, extra=dict(engine_config.provider_env))

    timed_out = False
    try:
        with open(log_path, "wb") as logf:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=os.fspath(clone_dir), env=env,
                stdout=logf, stderr=logf, stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                pgid: int | None = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                kill_process_group(proc, pgid, signal.SIGTERM)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                if proc.returncode is None:
                    kill_process_group(proc, pgid, signal.SIGKILL)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except FileNotFoundError as exc:
        return DispatchOutcome(
            ok=False, branch=branch, worktree_path=str(clone_dir),
            adapter_failure=True, error=f"container runtime not found: {exc}",
            log_path=str(log_path),
        )
    finally:
        # --rm does NOT fire when the client is SIGKILL'd, so the container can
        # outlive a timeout (holding the clone + the API key in its env). Force-
        # remove it by name unconditionally (idempotent on a clean --rm exit).
        await remove_container(runtime, container_name)
        # Redact any credential the worker surfaced into its log (M6/CWE-532) on
        # EVERY exit path — in finally so an unexpected exception can't leave the
        # key in the persisted log.
        redact_secrets(log_path, worker_log_secrets(api_key, engine_config))

    exit_code = proc.returncode
    changed = await changed_files(clone_dir, base_sha)
    cost = engine_config.worker_max_budget_usd if timed_out else extract_cost_from_log(log_path)
    ok_run = (not timed_out) and exit_code == 0 and bool(changed)
    error = None
    if timed_out:
        error = f"container worker timed out after {timeout_s:.0f}s"
    elif exit_code != 0:
        error = f"container worker exited {exit_code}"
    elif not changed:
        error = "container worker produced no changes"
    return DispatchOutcome(
        ok=ok_run, branch=branch, worktree_path=str(clone_dir), changed_files=changed,
        cost_usd=cost, timed_out=timed_out, error=error, log_path=str(log_path),
    )


__all__ = [
    "DEFAULT_WORKER_IMAGE",
    "DOCKERFILE_SUBDIR",
    "build_image",
    "build_run_argv",
    "image_exists",
    "launch_worker_container",
    "prepare_clone",
    "remove_container",
    "resolve_container_runtime",
]
