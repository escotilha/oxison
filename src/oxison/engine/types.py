"""Small shared engine value types.

Lives here (not in ``dispatch.py``) so a consumer that only needs the plain
``DispatchOutcome`` dataclass — e.g. ``integrate.py`` and its tests — doesn't pull
the whole dispatch module (subprocess + container machinery) into scope (CTO L3).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DispatchOutcome:
    """Result of one write-worker run."""

    ok: bool
    branch: str
    worktree_path: str
    changed_files: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    timed_out: bool = False
    adapter_failure: bool = False
    error: str | None = None
    log_path: str | None = None


__all__ = ["DispatchOutcome"]
