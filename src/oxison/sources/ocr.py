"""Scanned-PDF / image OCR adapter — opt-in (--ocr).

Lazy-imports an optional, unpublished ``document_extraction`` package. The
heavy PaddleOCR stack is NEVER an oxison dependency: if the import fails,
the input is skipped-with-reason and the run continues on other sources.

Expected interface (top-level ``document_extraction``, installed into the
environment — never resolved from the current working directory or a target
repo; see ``_load_ocr_service`` for why):
    get_ocr_service() -> OCRService
    async OCRService.process_document(content: bytes, ...) -> OCRResult
    OCRResult{text, blocks[{page,...}], confidence, language, page_count}
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from typing import Any

from .base import AdapterAvailability, SourceResult, SourceUnit

_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}

#: ``sys.path`` entries that resolve relative to the invocation — the current
#: working directory (``""`` / ``"."``) and the dir of the launching script.
#: A target repo or ``--sources`` dir oxison ingests is attacker-controlled, so
#: a ``document_extraction`` module found via any of these would be hijacked
#: code running in the unsandboxed host process. We strip them before import.
_UNTRUSTED_PATH_ENTRIES = {"", "."}


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
        """Import the optional ``document_extraction`` package safely.

        Security (SECURITY-AUDIT F3): the previous ``from src.document_extraction
        import ...`` resolved ``src`` through ``sys.path``, whose first entries
        point at the invocation dir / CWD. Because oxison deliberately ingests
        files from untrusted target repos, a planted ``src/document_extraction``
        (or top-level ``document_extraction``) under the CWD or a ``--sources``
        dir would be imported and executed in the unsandboxed host process —
        arbitrary RCE from a file plus the ``--ocr`` flag, before any AI worker,
        sandbox, or budget gate runs.

        Hardening (fail-closed, no global state mutation):

        1. Resolve the module's spec via ``PathFinder.find_spec`` against an
           explicitly-built *trusted* path list — ``sys.path`` with the
           CWD-relative entries (``""``, ``"."``, anything resolving to CWD or a
           subdir of it) removed. This never mutates the process-global
           ``sys.path``, so there is no restore window a concurrent import could
           race (the previous strip/restore approach had a latent TOCTOU; ingest
           is sequential today, but find_spec closes the window structurally).
        2. Require a real on-disk origin under a *trusted* location. A spec with
           no ``origin`` (namespace package / exotic loader) is rejected — the
           check fails CLOSED, not open.

        A ``document_extraction`` legitimately installed into site-packages
        resolves and loads; a planted one is invisible (not on the trusted path)
        or rejected (origin under CWD). Any failure raises ``ImportError`` so
        callers degrade to skip-with-reason.
        """
        cwd = Path.cwd().resolve()

        def _is_untrusted(entry: str) -> bool:
            if entry in _UNTRUSTED_PATH_ENTRIES:
                return True
            try:
                resolved = Path(entry or ".").resolve()
            except (OSError, ValueError):
                return True  # un-resolvable entry: treat as untrusted (fail closed)
            return resolved == cwd or cwd in resolved.parents

        trusted_path = [p for p in sys.path if not _is_untrusted(p)]

        spec = importlib.machinery.PathFinder.find_spec(
            "document_extraction", path=trusted_path
        )
        if spec is None or spec.origin is None:
            raise ImportError(
                "document_extraction not importable from a trusted location "
                "(OCR stack absent, or only resolvable from the working "
                "directory — refusing to load untrusted code)"
            )

        # Defense in depth: confirm the resolved origin is not under CWD even if
        # a trusted path entry somehow pointed back into it. The CWD anchor is a
        # proxy for "interpreter-supplied invocation-relative sys.path entries" —
        # the actual untrusted locations (the target repo, --sources dirs) are
        # never added to sys.path, so they can't reach the import machinery here.
        try:
            origin = Path(spec.origin).resolve()
        except (OSError, ValueError) as exc:
            raise ImportError(
                f"could not verify document_extraction origin: {exc}"
            ) from exc
        if origin == cwd or cwd in origin.parents:
            raise ImportError(
                f"refusing to load document_extraction from the working "
                f"directory ({origin}); install it into the environment"
            )

        if spec.loader is None:  # find_spec with a real origin always has one
            raise ImportError("document_extraction spec has no loader")
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # import-time error in the (trusted) module
            raise ImportError(
                f"document_extraction failed to import: {exc}"
            ) from exc

        return module.get_ocr_service()

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
