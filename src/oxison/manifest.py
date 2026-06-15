"""Resumable, flat-JSON run manifest.

One run writes one ``<output_dir>/.oxison-run.json``. Each pipeline
step records its status, cost, and the artifact it produced. A re-run
with ``--resume`` reads this file and skips steps already marked
``done``. Writes are atomic (temp file + ``os.replace``) so a crash
mid-write never corrupts an existing manifest.

This is deliberately a flat JSON file, not a SQLite DB — oxison is a
single bounded pipeline, not a task queue. (oxi-core uses SQLite
because it coordinates many concurrent tasks; oxison does not.)
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

StepStatus = Literal["pending", "running", "done", "failed"]

#: The ordered pipeline steps oxison records. ``branch`` is the Phase 3
#: roadmap-or-security follow-on (recorded even pre-Phase-3 as pending).
STEP_NAMES: tuple[str, ...] = (
    "map",
    "ingest",
    "comprehend",
    "product",
    "manual",
    "stack",
    "comprehension_json",
    "branch",
)

MANIFEST_FILENAME = ".oxison-run.json"


@dataclass
class StepRecord:
    """Status of a single pipeline step."""

    status: StepStatus = "pending"
    cost_usd: float = 0.0
    artifact: str | None = None
    error: str | None = None


@dataclass
class RunManifest:
    """In-memory view of a run's progress, backed by a JSON file."""

    path: Path
    run_id: str
    target: str
    started_at: str
    steps: dict[str, StepRecord] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def load_or_create(cls, output_dir: Path, *, target: str, started_at: str) -> RunManifest:
        """Load an existing manifest from ``output_dir`` or create a fresh one.

        ``started_at`` is supplied by the caller (oxison never calls
        ``datetime.now()`` deep in a library function — the timestamp is
        stamped once at the CLI boundary and threaded in).
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / MANIFEST_FILENAME
        if path.exists():
            existing = cls._from_json(path)
            if existing is not None:
                return existing
        manifest = cls(
            path=path,
            run_id=str(uuid.uuid4()),
            target=target,
            started_at=started_at,
            steps={name: StepRecord() for name in STEP_NAMES},
        )
        manifest.save()
        return manifest

    @classmethod
    def _from_json(cls, path: Path) -> RunManifest | None:
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        steps = {
            name: StepRecord(**data.get("steps", {}).get(name, {}))
            for name in STEP_NAMES
        }
        return cls(
            path=path,
            run_id=data.get("run_id", str(uuid.uuid4())),
            target=data.get("target", ""),
            started_at=data.get("started_at", ""),
            steps=steps,
        )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def mark(
        self,
        step: str,
        status: StepStatus,
        *,
        cost_usd: float | None = None,
        artifact: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update one step's record and persist atomically."""
        if step not in self.steps:
            raise KeyError(f"unknown step: {step}")
        rec = self.steps[step]
        rec.status = status
        if cost_usd is not None:
            rec.cost_usd = cost_usd
        if artifact is not None:
            rec.artifact = artifact
        if error is not None:
            rec.error = error
        elif status != "failed":
            # A fresh non-failed attempt (running/done) clears a prior failure's
            # error — otherwise a step that failed then succeeded keeps a stale,
            # misleading error string alongside status "done".
            rec.error = None
        self.save()

    def is_complete(self, step: str) -> bool:
        """True if ``step`` is already done (used by ``--resume``)."""
        return self.steps.get(step, StepRecord()).status == "done"

    def total_cost_usd(self) -> float:
        return round(sum(r.cost_usd for r in self.steps.values()), 6)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self) -> None:
        """Write the manifest atomically (temp file + ``os.replace``)."""
        payload = {
            "run_id": self.run_id,
            "target": self.target,
            "started_at": self.started_at,
            "steps": {name: asdict(rec) for name, rec in self.steps.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


__all__ = [
    "MANIFEST_FILENAME",
    "STEP_NAMES",
    "RunManifest",
    "StepRecord",
    "StepStatus",
]
