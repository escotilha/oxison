"""Plain-text/markdown adapter — stdlib only, no extraction deps."""
from __future__ import annotations

from pathlib import Path

from .base import AdapterAvailability, SourceResult, SourceUnit

_EXTS = {".md", ".txt", ".markdown", ".rst"}


class DocsAdapter:
    name = "docs"

    def detect(self, path: Path) -> bool:
        return path.suffix.lower() in _EXTS

    def available(self) -> AdapterAvailability:
        return AdapterAvailability(available=True)

    def extract(self, path: Path) -> SourceResult:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return SourceResult.skip(self.name, str(path), reason="empty file")
        unit = SourceUnit(
            text=text,
            source_type=self.name,
            origin_path=str(path),
            locator=f"docs:{path.name}",
            metadata={"chars": len(text)},
        )
        return SourceResult.ok(self.name, str(path), units=[unit])
