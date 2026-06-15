"""PDF adapter — text extraction via pypdf.

A scanned/image PDF (no extractable text layer) is NOT failed: it is
skipped with reason ``needs_ocr`` so the OCR adapter (opt-in, --ocr)
can pick it up. Without --ocr it simply stays skipped.
"""
from __future__ import annotations

from pathlib import Path

from .base import AdapterAvailability, SourceResult, SourceUnit


class PdfAdapter:
    name = "pdf"

    def detect(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def available(self) -> AdapterAvailability:
        try:
            import pypdf  # noqa: F401
        except ImportError:
            return AdapterAvailability(
                available=False, reason="pypdf not installed (pip install 'oxi-son[pdf]')"
            )
        return AdapterAvailability(available=True)

    def _page_texts(self, path: Path) -> list[str]:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [(page.extract_text() or "") for page in reader.pages]

    def extract(self, path: Path) -> SourceResult:
        avail = self.available()
        if not avail.available:
            return SourceResult.skip(self.name, str(path), reason=avail.reason or "unavailable")
        texts = self._page_texts(path)
        units = [
            SourceUnit(
                text=t,
                source_type=self.name,
                origin_path=str(path),
                locator=f"pdf:{path.name}#p{i + 1}",
                metadata={"page": i + 1},
            )
            for i, t in enumerate(texts)
            if t.strip()
        ]
        if not units:
            return SourceResult.skip(self.name, str(path), reason="needs_ocr")
        return SourceResult.ok(self.name, str(path), units=units)
