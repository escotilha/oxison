"""Word-document adapter — paragraph text via python-docx."""
from __future__ import annotations

from pathlib import Path

from .base import AdapterAvailability, SourceResult, SourceUnit


class DocxAdapter:
    name = "docx"

    def detect(self, path: Path) -> bool:
        return path.suffix.lower() == ".docx"

    def available(self) -> AdapterAvailability:
        try:
            import docx  # noqa: F401
        except ImportError:
            return AdapterAvailability(
                available=False, reason="python-docx not installed (pip install 'oxi-son[docx]')"
            )
        return AdapterAvailability(available=True)

    def extract(self, path: Path) -> SourceResult:
        avail = self.available()
        if not avail.available:
            return SourceResult.skip(self.name, str(path), reason=avail.reason or "unavailable")
        from docx import Document

        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if not text.strip():
            return SourceResult.skip(self.name, str(path), reason="no paragraph text")
        unit = SourceUnit(
            text=text,
            source_type=self.name,
            origin_path=str(path),
            locator=f"docx:{path.name}",
            metadata={"chars": len(text)},
        )
        return SourceResult.ok(self.name, str(path), units=[unit])
