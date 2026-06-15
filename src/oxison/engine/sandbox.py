"""srt sandbox policy for the Oxfaz build worker (Layer 1).

The build worker is `claude -p` with write tools under `bypassPermissions`, so
it can write/exec outside its worktree. We wrap it in Anthropic's
`@anthropic-ai/sandbox-runtime` (`srt`), which confines the WHOLE process tree
(`sandbox-exec` on macOS, `bubblewrap` on Linux) with a deny-all-writes default
plus an explicit filesystem + network-egress allowlist. (Claude's built-in
`/sandbox` confines only Bash — not Edit/Write under bypassPermissions — so it
cannot contain this worker; `srt` is the documented answer.)

srt's model (confirmed against the installed package, schema 1.0):

* ``filesystem.allowWrite`` — the ONLY writable paths (locked down).
* ``filesystem.denyRead``   — carve-outs from the otherwise-broad read default
  (the worker can read the repo + ``~/.claude`` it needs; we deny credentials).
* ``network.allowedDomains`` — egress allowlist (no TLS inspection → keep tight).

This module is pure + deterministic (the settings builder is unit-tested); the
actual confinement is exercised by an opt-in integration spike.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

#: The srt executable name (resolved to an absolute path at preflight).
SRT_BINARY = "srt"

#: Network egress a sandboxed worker may reach. No TLS inspection, so keep this
#: tight. Includes package registries + git host so a build can install deps and
#: fetch (owner decision, 2026-06-15). api.anthropic.com is required for the
#: worker's own claude calls.
DEFAULT_SANDBOX_DOMAINS: tuple[str, ...] = (
    "api.anthropic.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "crates.io",
    "static.crates.io",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
)

#: Credential paths (relative to home) the worker must never READ. srt's default
#: read policy is broad, so deny these explicitly (belt-and-suspenders even where
#: srt already blocks well-known secrets).
DEFAULT_CRED_DENY: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".config/gcloud",
    ".config/gh",
    ".netrc",
    ".config/git/credentials",
)


def build_srt_settings(
    *,
    worktree: Path,
    repo: Path,
    task_identifier: str,
    home: Path,
    allowed_domains: tuple[str, ...],
    extra_write_paths: tuple[str, ...] = (),
    tmpdir: str | None = None,
) -> dict[str, Any]:
    """Build the srt settings for one build worker: deny-all writes + a scoped
    allowlist that still lets the worker build, test, and ``git commit``.

    The git allowlist is the load-bearing, non-obvious part: a *linked* worktree
    commits by writing the per-worktree metadata (``.git/worktrees/<task>``) AND
    appending to the shared object/ref/log store in the main ``.git`` — but we
    scope to those subpaths so the worker still cannot rewrite ``.git/config`` or
    install a ``.git/hooks`` script.
    """
    git = repo / ".git"
    tmp = tmpdir if tmpdir is not None else tempfile.gettempdir()
    allow_write = [
        str(worktree.resolve()),
        # Allow the whole .git so every git write a commit needs works — loose
        # refs, the shared object store, per-worktree metadata, AND the various
        # top-level lock files (packed-refs.lock etc.) that a granular allowlist
        # misses. The dangerous bits are carved back out via denyWrite below.
        str(git),
        # Claude's own session/projects state — MUST stay writable AND readable
        # (OAuth creds live under ~/.claude, so it is never in denyRead).
        str(home / ".claude"),
        str(home / ".claude.json"),
        tmp,  # node/claude scratch
        *extra_write_paths,
    ]
    # Deny the parts of .git that turn a write into code execution: a worker
    # must not install a hook or rewrite git config (e.g. core.hooksPath) — those
    # run commands on the NEXT git op, potentially outside the sandbox.
    deny_write = [str(git / "config"), str(git / "hooks")]
    deny_read = [str(home / rel) for rel in DEFAULT_CRED_DENY]
    # srt 1.0 requires all four keys present (denyWrite / deniedDomains too, even
    # when empty) — an incomplete config is rejected and srt falls back to
    # deny-all. We need no extra carve-outs: allowWrite/allowedDomains ARE the
    # allowlists, so the deny lists are empty.
    return {
        "filesystem": {
            "allowWrite": allow_write,
            "denyWrite": deny_write,
            "denyRead": deny_read,
        },
        "network": {
            "allowedDomains": list(allowed_domains),
            "deniedDomains": [],
        },
    }


def write_srt_settings(path: Path, settings: dict[str, Any]) -> None:
    """Write the srt settings JSON (oxison owns this file, in the parent)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def resolve_srt_binary(configured: str | None = None) -> str | None:
    """Absolute path to the srt executable, or None if not found.

    Resolved once (at preflight) and invoked directly — never via ``npx``, which
    would re-hit the npm registry and run *before* the sandbox exists.
    """
    candidate = configured or SRT_BINARY
    if os.path.isabs(candidate):
        return candidate if os.access(candidate, os.X_OK) else None
    return shutil.which(candidate)


def srt_wrap(srt_binary: str, settings_path: Path, inner_argv: list[str]) -> list[str]:
    """Prepend the srt wrapper to an existing ``claude -p`` argv."""
    return [srt_binary, "--settings", str(settings_path), *inner_argv]


__all__ = [
    "DEFAULT_CRED_DENY",
    "DEFAULT_SANDBOX_DOMAINS",
    "SRT_BINARY",
    "build_srt_settings",
    "resolve_srt_binary",
    "srt_wrap",
    "write_srt_settings",
]
