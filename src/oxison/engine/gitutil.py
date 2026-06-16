"""Shared git + worker-log helpers for the build engine.

Lifted out of ``engine/dispatch.py`` (CTO #18) so ``integrate.py`` and
``container.py`` can use them without reaching across a module boundary into
another module's privates. These are the engine's small, pure-ish git/log
primitives; they are public API of *this* module (no leading underscore) because
they are deliberately shared.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


async def git_cmd(args: list[str], *, cwd: Path) -> tuple[int, str]:
    """Run ``git <args>`` in ``cwd``; return ``(returncode, combined-ish output)``
    (stdout, or stderr when stdout is empty)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=os.fspath(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, (out or err).decode("utf-8", errors="replace")


def parse_changed_files(porcelain: str) -> list[str]:
    """Parse ``git status --porcelain`` output into a list of changed paths.

    Handles renames (``R  old -> new`` → the new path) and quoted paths.
    """
    files: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        if " -> " in rest:  # rename/copy
            rest = rest.split(" -> ", 1)[1]
        files.append(rest.strip().strip('"'))
    return files


async def changed_files(worktree: Path, base_sha: str) -> list[str]:
    """All files the worker changed vs. the worktree's base commit.

    Unions uncommitted changes (``status --porcelain``) with committed ones
    (``diff base..HEAD``). Diffing against the captured *base* SHA — not
    ``HEAD`` — is what lets a worker that **commits** its work still have its
    changes detected (after a commit, ``diff HEAD`` is empty).
    """
    _rc1, porcelain = await git_cmd(["status", "--porcelain"], cwd=worktree)
    files = set(parse_changed_files(porcelain))
    rc2, committed = await git_cmd(["diff", "--name-only", base_sha, "HEAD"], cwd=worktree)
    if rc2 == 0:
        files.update(f for f in committed.splitlines() if f.strip())
    return sorted(files)


def extract_cost_from_log(log_path: Path) -> float:
    """Pull ``total_cost_usd`` from the worker's stream-json ``result`` event."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(evt, dict) and evt.get("type") == "result":
            return float(evt.get("total_cost_usd", 0.0))
    return 0.0


__all__ = ["changed_files", "extract_cost_from_log", "git_cmd", "parse_changed_files"]
