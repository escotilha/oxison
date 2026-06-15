"""comprehension.json — the Oxicome->Oxipensa contract (schema 1.0).

v1 guarantees a valid, schema-versioned envelope with the provenance
ledger (machine-built from ingest results) plus the comprehension
markdown. The richer product/state/gaps structure is reserved in the
schema and populated by a later task; consumers pin ``schema_version``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .sources.base import SourceResult

SCHEMA_VERSION = "1.0"


@dataclass
class ComprehensionDoc:
    schema_version: str
    generated_at: str
    sources: list[dict[str, Any]]
    comprehension_markdown: str
    product: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    stack: dict[str, Any] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "generated_at": self.generated_at,
                "sources": self.sources,
                "product": self.product,
                "state": self.state,
                "stack": self.stack,
                "open_questions": self.open_questions,
                "comprehension_markdown": self.comprehension_markdown,
            },
            indent=2,
            ensure_ascii=False,
        )


def _ledger_entry(r: SourceResult) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": r.source_type,
        "origin": r.origin,
        "status": r.status,
        "units": r.unit_count,
    }
    if r.reason:
        entry["reason"] = r.reason
    return entry


def build_comprehension_doc(
    *,
    comprehension_text: str,
    source_results: list[SourceResult],
    generated_at: str,
) -> ComprehensionDoc:
    return ComprehensionDoc(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        sources=[_ledger_entry(r) for r in source_results],
        comprehension_markdown=comprehension_text,
    )
