"""Scanned-PDF / image OCR adapter — opt-in (--ocr).

Lazy-imports an optional, unpublished ``document_extraction`` package. The
heavy PaddleOCR stack is NEVER an oxison dependency: if the import fails,
the input is skipped-with-reason and the run continues on other sources.

Expected interface (``src.document_extraction``):
    get_ocr_service() -> OCRService
    async OCRService.process_document(content: bytes, ...) -> OCRResult
    OCRResult{text, blocks[{page,...}], confidence, language, page_count}
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path
from typing import Any

from .base import AdapterAvailability, SourceResult, SourceUnit

_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


class OcrAdapter:
    name = "ocr"

    def detect(self, path: Path) -> bool:
        return path.suffix.lower() in _EXTS

    def available(self) -> AdapterAvailability:
        try:
            self._load_ocr_service()
        except ImportError as exc:
            return AdapterAvailability(available=False, reason=str(exc))
        return AdapterAvailability(available=True)

    def _load_ocr_service(self) -> Any:
        from src.document_extraction import (  # type: ignore[import-not-found]  # optional external pkg
            get_ocr_service,
        )
        return get_ocr_service()

    def extract(self, path: Path) -> SourceResult:
        try:
            service = self._load_ocr_service()
        except ImportError:
            return SourceResult.skip(
                self.name, str(path),
                reason="document_extraction not importable (OCR stack absent)",
            )
        # process_document is async, but extract() is sync and is called from
        # within the pipeline's already-running event loop (via the sync
        # ingest_paths). asyncio.run() cannot run inside a running loop, so we
        # drive the coroutine in a worker thread that has no loop of its own —
        # one code path that works whether or not the caller's thread has a
        # running loop. (The coroutine is created inside the thunk so it's
        # constructed on the thread that runs it.)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            result = ex.submit(
                lambda: asyncio.run(service.process_document(path.read_bytes()))
            ).result()
        pages = result.text.split("\f") if result.text else []
        units: list[SourceUnit] = []
        for i, page_text in enumerate(pages, start=1):
            if not page_text.strip():
                continue
            units.append(
                SourceUnit(
                    text=page_text,
                    source_type="pdf",
                    origin_path=str(path),
                    locator=f"pdf:{path.name}#p{i}",
                    metadata={
                        "page": i,
                        "confidence": getattr(result, "confidence", None),
                        "language": getattr(result, "language", None),
                        "via": "ocr",
                    },
                )
            )
        if not units:
            return SourceResult.skip(self.name, str(path), reason="ocr produced no text")
        return SourceResult.ok(self.name, str(path), units=units)
