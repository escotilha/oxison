"""Curated ``CLAUDE_CONFIG_DIR`` for worker skill invocation (Layer-1 + token auth).

The build worker can invoke a *curated* generic skill subset via the ``Skill``
tool. To guarantee it sees ONLY that subset — never the operator's full skill
library that ``~/.claude/skills`` exposes (which may include project-specific
skills) — the worker is pointed at a dedicated, per-task ``CLAUDE_CONFIG_DIR``
whose ``skills/`` holds only the curated names.

Why this is gated to token auth (``--api-key``/``--provider``): under token auth
the worker authenticates from the env, so the curated dir needs **no credential
material** — it is a fresh, isolated config home (Claude writes its own
``.claude.json`` there) plus the curated skill symlinks. Relocating
``CLAUDE_CONFIG_DIR`` under host OAuth instead would break auth (the login creds
live in the real config dir), and mirroring them in is unsafe. The token-auth
gate is what keeps this mechanism clean.

Per-task (not shared) so concurrent workers never race on the dir's
``.claude.json`` scratch.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def build_curated_config_dir(
    dest: Path, *, source_skills_dir: Path, skill_names: Sequence[str]
) -> Path:
    """Create/refresh ``dest`` as a ``CLAUDE_CONFIG_DIR`` exposing ONLY ``skill_names``.

    ``source_skills_dir`` is the operator's skills directory (e.g.
    ``~/.claude/skills``). Only the named skills that actually exist there are
    symlinked into ``dest/skills/``; every other skill the operator has is never
    linked, so the worker cannot see or invoke it. The symlink targets are
    resolved (the source is itself often a symlink), so a worker reading the
    skill still reaches the real files. Returns ``dest``.
    """
    skills = dest / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    # Refresh: drop any prior links so a name removed from the curated set (or a
    # stale dir reused across runs) never lingers as an invokable skill.
    for existing in skills.iterdir():
        if existing.is_symlink() or existing.exists():
            existing.unlink()
    for name in skill_names:
        src = (source_skills_dir / name).resolve()
        if not src.exists():
            continue  # a curated name not present in this setup is simply skipped
        (skills / name).symlink_to(src)
    return dest


__all__ = ["build_curated_config_dir"]
