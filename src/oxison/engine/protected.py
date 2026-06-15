"""Segment-anchored protected-path matcher — one source, two consumers.

This is the single home for "does this path touch a protected location?"
(H3). Both the plan-gate (``planner.gate``, Phase 4, checking a plan's
self-reported ``files_touched``) and the grader (``gates.grade``, Phase 5,
checking the actual diff) import **this same** ``is_protected`` symbol. A
diff touching a protected path must fail the grader even when the plan's
declaration was clean (C1) — so the matcher cannot live inside either
consumer; it is a shared leaf.

**Segment-anchored, not ``str.startswith``.** Naive prefix matching has two
failure modes this matcher avoids:

* False positives — a ``.env`` rule must not match ``envoy.py`` or
  ``preview.env`` or ``my_env.py``. It matches the path segment ``.env``
  (and dotted children like ``.env.production``), not any string starting
  with those characters.
* False negatives (monorepo bypass) — an ``alembic/`` rule must catch a
  nested ``apps/api/alembic/versions/072.py``, which a root-anchored
  ``startswith("alembic/")`` would miss.

A rule ending in ``/`` matches a *directory* anywhere in the path. A rule
without ``/`` matches a *file/segment* by exact name or as a dotted parent
(``.env`` matches ``.env`` and ``.env.production`` but not ``preview.env``).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath


def _normalize(path: str) -> PurePosixPath:
    """Normalize to forward-slash segments, dropping a leading ``./``.

    Uses ``removeprefix`` (not ``lstrip``) so a dotfile keeps its leading
    dot — ``lstrip("./")`` would strip the ``.`` of ``./.env`` down to
    ``env`` because it strips a character *set*, not a prefix.
    """
    cleaned = path.strip().removeprefix("./")
    return PurePosixPath(cleaned)


def _matches_rule(parts: tuple[str, ...], rule: str) -> bool:
    rule = rule.strip()
    if not rule:
        return False

    if rule.endswith("/"):
        # Directory rule: the rule's segments must appear as a contiguous run
        # of *directory* segments anywhere in the path — anchored at segment
        # boundaries, so "alembic/" hits apps/api/alembic/versions/072.py.
        # The run must end before the final segment (the filename), i.e. the
        # matched directory has at least one path segment beneath it.
        dir_parts = tuple(p for p in rule.rstrip("/").split("/") if p)
        if not dir_parts:
            return False
        n = len(dir_parts)
        last_dir_index = len(parts) - 1  # exclusive of the filename segment
        return any(
            parts[i : i + n] == dir_parts for i in range(0, last_dir_index - n + 1)
        )

    # File/segment rule. Match any segment that is exactly the rule, or a
    # dotted child of it (``.env`` -> ``.env.production``). Crucially NOT a
    # segment that merely *ends with* the rule (``preview.env``) or *starts
    # with* it as a different name (``envoy.py`` vs ``.env``).
    return any(seg == rule or seg.startswith(rule + ".") for seg in parts)


def is_protected(path: str, protected: Iterable[str]) -> bool:
    """Return True if ``path`` touches any protected rule.

    ``path`` is a repo-relative path (POSIX or OS-native separators accepted).
    ``protected`` is the iterable of rules (e.g. ``EngineConfig.protected_paths``).

    Fail-safe choices (defense in depth — this is a C1 safety matcher):

    * A path containing a ``..`` segment is treated as protected outright. A
      ``..`` could traverse *into* a protected directory while textually
      dodging a segment rule (``.github/sub/../workflows/x``); since a build
      worker has no legitimate reason to emit a ``..`` path, flag it rather
      than risk a bypass.
    * Matching is **case-insensitive** (``casefold``). On a case-insensitive
      filesystem (macOS, Windows) ``.GITHUB/workflows`` *is* the real
      protected dir; a case-sensitive compare would wave it through.
    """
    parts = _normalize(path.replace("\\", "/")).parts
    if not parts:
        return False
    if ".." in parts:
        return True
    folded_parts = tuple(p.casefold() for p in parts)
    return any(
        _matches_rule(folded_parts, rule.casefold()) for rule in protected
    )


def is_protected_path(path: str, protected: Iterable[str]) -> bool:
    """Like :func:`is_protected`, but also catches a path that names a protected
    *directory itself* (no child segment).

    The segment-anchored directory rule requires a child, so ``.github/workflows``
    or ``oxison-build`` *alone* would slip through :func:`is_protected`. This
    probes with a synthetic child so the bare directory is caught like a file
    under it, without false-positiving normal paths.

    Use this for **declared targets** — the plan-gate's ``files_hint`` and the
    grader's diff paths — so both consumers enforce identical semantics (the C1
    invariant: a plan-clean path that touches a protected dir must still fail the
    grader).
    """
    rules = tuple(protected)  # materialize — is_protected is called twice
    if is_protected(path, rules):
        return True
    return is_protected(path.rstrip("/") + "/__oxison_probe__", rules)


__all__ = ["is_protected", "is_protected_path"]
