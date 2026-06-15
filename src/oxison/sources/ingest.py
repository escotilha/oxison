"""Ingestion orchestrator: detect -> dispatch -> collect -> render.

Selects the right adapter per input path, runs it, accumulates a
per-source status ledger, and renders the collected units into the
``extra_context`` text block injected into comprehension/generation
prompts. Inputs with no matching adapter are recorded skipped (never
an error). This is the single entry point the pipeline calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .base import SourceAdapter, SourceResult, SourceUnit
from .docs import DocsAdapter
from .docx import DocxAdapter
from .ocr import OcrAdapter
from .pdf import PdfAdapter
from .pptx import PptxAdapter
from .recording import RecordingAdapter
from .web import WebAdapter


@dataclass
class IngestOutput:
    results: list[SourceResult] = field(default_factory=list)

    @property
    def units(self) -> list[SourceUnit]:
        return [u for r in self.results for u in r.units]

    @property
    def unit_count(self) -> int:
        return len(self.units)


def _static_adapters() -> list[SourceAdapter]:
    # PDF text first; OCR is consulted separately (only when enabled).
    return [DocsAdapter(), PdfAdapter(), PptxAdapter(), DocxAdapter()]


def _safe_extract(adapter: SourceAdapter, path: Path) -> SourceResult:
    """Run adapter.extract, converting any unexpected error into a skip.

    Honors the orchestrator contract: a bad input is recorded skipped,
    never raised. Adapters already return skips for *expected* conditions
    (missing dep, no key, needs_ocr); this is the net for *unexpected*
    errors (a missing --add file, the recording stub, an adapter bug).
    """
    try:
        return adapter.extract(path)
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all net at the orchestrator boundary
        return SourceResult.skip(
            getattr(adapter, "name", "unknown"), str(path),
            reason=f"extraction failed: {type(exc).__name__}: {exc}",
        )


def ingest_paths(
    paths: list[Path],
    *,
    ocr_enabled: bool,
    stt_key: str | None,
    stt_provider: str = "openai",
) -> IngestOutput:
    adapters = _static_adapters()
    ocr = OcrAdapter()
    recording = RecordingAdapter(stt_key=stt_key, stt_provider=stt_provider)
    out = IngestOutput()
    for path in paths:
        if recording.detect(path):
            out.results.append(_safe_extract(recording, path))
            continue
        handled = False
        for adapter in adapters:
            if adapter.detect(path):
                res = _safe_extract(adapter, path)
                # A text-PDF that needs OCR: retry via OCR only when enabled.
                if res.status == "skipped" and res.reason == "needs_ocr" and ocr_enabled:
                    res = _safe_extract(ocr, path)
                out.results.append(res)
                handled = True
                break
        if not handled:
            out.results.append(
                SourceResult.skip("unknown", str(path), reason="no adapter for this file type")
            )
    return out


def brief_unit(text: str) -> SourceUnit:
    """Wrap a plain-text project brief as a first-class, citable source unit."""
    return SourceUnit(
        text=text,
        source_type="brief",
        origin_path="(brief)",
        locator="brief:idea",
        metadata={},
    )


def ingest_urls(urls: list[str]) -> IngestOutput:
    """Fetch user-provided URLs via the web adapter, collecting a status ledger.

    Parallel to ``ingest_paths`` but for URLs (not file paths), so it does not
    touch the path-based adapter dispatch. A fetch error is recorded skipped,
    never raised — same orchestrator contract as ``ingest_paths``.
    """
    out = IngestOutput()
    if not urls:
        return out
    adapter = WebAdapter()
    avail = adapter.available()
    for url in urls:
        if not avail.available:
            out.results.append(
                SourceResult.skip("web", url, reason=avail.reason or "unavailable")
            )
            continue
        try:
            out.results.append(adapter.extract(url))
        except Exception as exc:  # noqa: BLE001 — orchestrator-boundary net (matches _safe_extract)
            out.results.append(
                SourceResult.skip("web", url, reason=f"fetch failed: {type(exc).__name__}: {exc}")
            )
    return out


def render_extra_context(units: list[SourceUnit]) -> str:
    if not units:
        return ""
    lines = [
        "=== ADDITIONAL SOURCES ===",
        "(extracted by oxison from non-repo inputs; cite by locator)",
    ]
    for u in units:
        lines.append(f"\n--- [{u.locator}] ---\n{u.text}")
    lines.append("\n=== END ADDITIONAL SOURCES ===")
    return "\n".join(lines)
