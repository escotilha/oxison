"""Source adapters for Oxicome multi-source ingestion."""
from __future__ import annotations

from .base import AdapterAvailability, SourceAdapter, SourceResult, SourceUnit
from .ingest import IngestOutput, ingest_paths, render_extra_context

__all__ = [
    "AdapterAvailability",
    "IngestOutput",
    "SourceAdapter",
    "SourceResult",
    "SourceUnit",
    "ingest_paths",
    "render_extra_context",
]
