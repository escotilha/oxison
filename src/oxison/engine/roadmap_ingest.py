"""Ingest an Oxipensa ``roadmap.json`` into the engine taskstore.

This is the Oxipensa->Oxfaz seam: it reads the schema-1.0 roadmap contract
Oxipensa emits and persists each task as a ``planned`` row. Dedup is by the
roadmap's deterministic ``identifier`` (the taskstore's UNIQUE constraint), so
re-ingesting an updated roadmap adds only the genuinely-new tasks and leaves
in-flight/done work untouched — the property the deterministic-id design exists
to provide.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .taskstore import TaskStore

ROADMAP_JSON_FILENAME = "roadmap.json"

#: A task identifier becomes BOTH a host path component (``clone_root/<id>`` and
#: ``worktrees/<id>``) and a container ``--name`` (``oxfaz-<id>``). A roadmap.json
#: can be hand-authored or third-party, so an unconstrained identifier is a
#: path-traversal vector (``../../etc/x`` would escape the build dir on the host)
#: and could feed invalid chars to ``podman --name``. The canonical planner emits
#: ``oxpz-<10 hex>``; this pattern admits that plus any readable slug while
#: rejecting separators, whitespace, and other unsafe chars. ``..`` is screened
#: separately because ``.`` is otherwise legal (e.g. ``v1.2``).
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_MAX_IDENTIFIER_LEN = 128


def is_safe_identifier(identifier: str) -> bool:
    """True if ``identifier`` is safe to use as a path component + container name.

    Fail-closed: rejects path separators, ``..`` traversal, leading dot/dash,
    whitespace, and anything over :data:`_MAX_IDENTIFIER_LEN`.
    """
    return (
        0 < len(identifier) <= _MAX_IDENTIFIER_LEN
        and ".." not in identifier
        and _SAFE_IDENTIFIER.match(identifier) is not None
    )


class RoadmapIngestError(ValueError):
    """The roadmap.json is missing or malformed."""


@dataclass
class IngestResult:
    added: int
    skipped: int
    identifiers: list[str]


def load_roadmap(path: Path) -> dict[str, Any]:
    """Load a roadmap.json from a file or a directory containing one."""
    target = path
    if target.is_dir():
        target = target / ROADMAP_JSON_FILENAME
    if not target.is_file():
        raise RoadmapIngestError(f"no roadmap.json found at {path}")
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoadmapIngestError(f"could not read {target}: {exc}") from exc
    if not isinstance(data, dict) or "tasks" not in data:
        raise RoadmapIngestError(f"{target} is not an Oxipensa roadmap.json (no tasks)")
    # Pin the contract major version — a future schema 2.x must fail loudly here
    # rather than be ingested as if it were 1.x.
    major = str(data.get("schema_version", "")).split(".")[0]
    if major and major != "1":
        raise RoadmapIngestError(
            f"unsupported roadmap.json schema_version "
            f"{data.get('schema_version')!r} (this oxison supports 1.x)"
        )
    return data


def ingest_roadmap(store: TaskStore, roadmap: dict[str, Any]) -> IngestResult:
    """Persist each roadmap task as a planned row; dedup by identifier."""
    raw_tasks = roadmap.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raise RoadmapIngestError("roadmap 'tasks' is not a list")
    added = 0
    skipped = 0
    identifiers: list[str] = []
    for t in raw_tasks:
        if not isinstance(t, dict):
            skipped += 1
            continue
        identifier = str(t.get("identifier", "")).strip()
        title = str(t.get("title", "")).strip()
        if not identifier or not title:
            skipped += 1
            continue
        # Fail-closed: an identifier that would traverse the host build dir or
        # break ``podman --name`` is dropped, not dispatched (see comment above
        # _SAFE_IDENTIFIER). Normal planner-emitted ids always pass.
        if not is_safe_identifier(identifier):
            skipped += 1
            continue
        rid = store.add_task(
            identifier,
            title,
            priority=int(t["priority"]) if isinstance(t.get("priority"), int) else 3,
            kind=str(t.get("kind", "")),
            rationale=str(t.get("rationale", "")),
            acceptance=[str(a) for a in t.get("acceptance", []) if isinstance(a, str)],
            depends_on=[str(d) for d in t.get("depends_on", []) if isinstance(d, str)],
            # The planner's files_hint seeds the task's lock set (refined later).
            files_touched=[str(f) for f in t.get("files_hint", []) if isinstance(f, str)],
        )
        if rid is None:
            skipped += 1
        else:
            added += 1
            identifiers.append(identifier)
    return IngestResult(added=added, skipped=skipped, identifiers=identifiers)


__all__ = [
    "ROADMAP_JSON_FILENAME",
    "IngestResult",
    "RoadmapIngestError",
    "ingest_roadmap",
    "is_safe_identifier",
    "load_roadmap",
]
