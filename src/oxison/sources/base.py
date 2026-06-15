"""Source-adapter contract: normalized, provenance-tagged extraction.

Every source type (pdf, pptx, docx, recording, ...) is an adapter that
turns one input into ``SourceUnit``s. Adapters READ their inputs and
never modify them — oxison's read-only invariant extends here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class SourceUnit:
    """One normalized chunk of extracted text + its provenance."""

    text: str
    source_type: str          # "git" | "pdf" | "pptx" | "docx" | "recording"
    origin_path: str          # the input file/dir this came from
    locator: str              # "pdf:spec.pdf#p3" | "pptx:deck.pptx#slide-4" | ...
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceResult:
    """Per-source outcome: ok (with units) or skipped (with reason)."""

    source_type: str
    origin: str
    status: str               # "ok" | "skipped"
    units: list[SourceUnit] = field(default_factory=list)
    reason: str | None = None

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @classmethod
    def ok(cls, source_type: str, origin: str, *, units: list[SourceUnit]) -> SourceResult:
        return cls(source_type=source_type, origin=origin, status="ok", units=units)

    @classmethod
    def skip(cls, source_type: str, origin: str, *, reason: str) -> SourceResult:
        return cls(source_type=source_type, origin=origin, status="skipped", reason=reason)


@dataclass
class AdapterAvailability:
    """Whether an adapter's dependencies are importable right now."""

    available: bool
    reason: str | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    name: str

    def detect(self, path: Path) -> bool: ...
    def available(self) -> AdapterAvailability: ...
    def extract(self, path: Path) -> SourceResult: ...
