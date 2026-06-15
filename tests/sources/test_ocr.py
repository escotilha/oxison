from pathlib import Path

from oxison.sources.ocr import OcrAdapter


def test_ocr_detect(tmp_path: Path):
    assert OcrAdapter().detect(tmp_path / "scanned.pdf")
    assert OcrAdapter().detect(tmp_path / "scan.png")
    assert not OcrAdapter().detect(tmp_path / "notes.md")


def test_ocr_degrades_when_library_absent(tmp_path: Path, monkeypatch):
    f = tmp_path / "scanned.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        OcrAdapter, "_load_ocr_service",
        lambda self: (_ for _ in ()).throw(ImportError("no doc_ext")),
    )
    res = OcrAdapter().extract(f)
    assert res.status == "skipped"
    assert "document_extraction" in (res.reason or "")


def test_ocr_maps_result_to_units(tmp_path: Path, monkeypatch):
    f = tmp_path / "scanned.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    class _FakeBlock:
        def __init__(self, page): self.page = page

    class _FakeResult:
        text = "page1 text\fpage2 text"
        blocks = [_FakeBlock(1), _FakeBlock(2)]
        confidence = 0.88
        language = "pt"
        page_count = 2

    class _FakeService:
        async def process_document(self, content, **kwargs):
            return _FakeResult()

    monkeypatch.setattr(OcrAdapter, "_load_ocr_service", lambda self: _FakeService())
    res = OcrAdapter().extract(f)
    assert res.status == "ok"
    assert res.unit_count == 2
    assert res.units[0].locator == "pdf:scanned.pdf#p1"
    assert res.units[1].locator == "pdf:scanned.pdf#p2"
    assert res.units[0].metadata["confidence"] == 0.88
    assert res.units[0].metadata["language"] == "pt"


def test_ocr_skips_when_no_text(tmp_path: Path, monkeypatch):
    f = tmp_path / "scanned.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    class _EmptyResult:
        text = "   \f  "
        blocks = []
        confidence = 0.0
        language = ""
        page_count = 0

    class _EmptyService:
        async def process_document(self, content, **kwargs):
            return _EmptyResult()

    monkeypatch.setattr(OcrAdapter, "_load_ocr_service", lambda self: _EmptyService())
    res = OcrAdapter().extract(f)
    assert res.status == "skipped"
    assert res.reason == "ocr produced no text"


def test_ocr_extract_works_inside_running_loop(tmp_path, monkeypatch):
    import asyncio

    f = tmp_path / "scanned.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    class _FakeResult:
        text = "page1\fpage2"
        blocks = []
        confidence = 0.9
        language = "en"
        page_count = 2

    class _FakeService:
        async def process_document(self, content, **kwargs):
            return _FakeResult()

    monkeypatch.setattr(OcrAdapter, "_load_ocr_service", lambda self: _FakeService())

    async def driver():
        # Calling the sync extract() from within a running loop is exactly
        # what the pipeline does (sync ingest_paths inside async run_pipeline).
        return OcrAdapter().extract(f)

    res = asyncio.run(driver())
    assert res.status == "ok"            # NOT just "no exception" — must produce units
    assert res.unit_count == 2
    assert res.units[0].locator == "pdf:scanned.pdf#p1"
