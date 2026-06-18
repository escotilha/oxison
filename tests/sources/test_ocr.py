import sys
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


def test_ocr_does_not_import_document_extraction_planted_in_cwd(tmp_path, monkeypatch):
    """SECURITY-AUDIT F3: a malicious document_extraction planted in the working
    directory (a target repo / --sources dir oxison ingests) must NOT be imported
    into the unsandboxed host process. Pre-fix, ``from src.document_extraction``
    resolved via sys.path[0] (CWD) and would execute attacker code at import.
    """
    # Plant BOTH attack shapes the old code was vulnerable to:
    #   src/document_extraction.py  (the old `from src.document_extraction`)
    #   document_extraction.py      (a top-level name found via CWD on sys.path)
    marker = tmp_path / "PWNED"
    payload = (
        "from pathlib import Path\n"
        f"Path(r{str(marker)!r}).write_text('executed')\n"
        "def get_ocr_service():\n"
        "    raise RuntimeError('should never be called')\n"
    )
    src_pkg = tmp_path / "src"
    src_pkg.mkdir()
    (src_pkg / "__init__.py").write_text("")
    (src_pkg / "document_extraction.py").write_text(payload)
    (tmp_path / "document_extraction.py").write_text(payload)

    # Simulate oxison running with the malicious repo as CWD, with CWD on path
    # (the realistic worst case: `python -m oxison` / a launcher that prepends ""
    # and the cwd itself as an absolute entry).
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.syspath_prepend("")
    # Ensure no previously-imported copy short-circuits the import machinery.
    monkeypatch.delitem(sys.modules, "document_extraction", raising=False)

    avail = OcrAdapter().available()

    assert not avail.available, "planted document_extraction must not be loaded"
    assert not marker.exists(), "attacker payload executed — RCE not contained!"


def test_ocr_does_not_import_document_extraction_in_cwd_subdir(tmp_path, monkeypatch):
    """The one layer-1 survivor class the reviewer flagged: a planted module in a
    SUBDIRECTORY of CWD that is itself on sys.path. The origin-under-CWD check
    (not just the CWD-entry strip) must reject it.
    """
    marker = tmp_path / "PWNED"
    sub = tmp_path / "vendor"
    sub.mkdir()
    (sub / "document_extraction.py").write_text(
        "from pathlib import Path\n"
        f"Path(r{str(marker)!r}).write_text('executed')\n"
        "def get_ocr_service():\n    raise RuntimeError('nope')\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(sub))  # subdir of CWD, absolute, on path
    monkeypatch.delitem(sys.modules, "document_extraction", raising=False)

    avail = OcrAdapter().available()
    assert not avail.available, "module under a CWD subdir must be rejected"
    assert not marker.exists(), "attacker payload executed from CWD subdir!"


def test_ocr_rejects_namespace_package_without_origin(tmp_path, monkeypatch):
    """Fail-closed: a document_extraction with no real on-disk origin (namespace
    package / exotic loader) must be REJECTED, not accepted. Pre-hardening the
    origin check was guarded by `if origin is not None`, failing open.
    """
    # A namespace package (dir with NO __init__.py) on a trusted path resolves to
    # a spec with origin=None.
    trusted = tmp_path / "site"
    (trusted / "document_extraction").mkdir(parents=True)  # no __init__.py
    # Run from a DIFFERENT cwd so `trusted` is not stripped as CWD-relative.
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.syspath_prepend(str(trusted))
    monkeypatch.delitem(sys.modules, "document_extraction", raising=False)

    avail = OcrAdapter().available()
    assert not avail.available, "namespace package (no origin) must fail closed"
    assert "trusted location" in (avail.reason or "")


def test_ocr_loads_document_extraction_from_trusted_location(tmp_path, monkeypatch):
    """The legitimate case must still work: a real document_extraction installed
    in a trusted location (NOT the CWD) loads and its get_ocr_service is called.
    """
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "document_extraction.py").write_text(
        "class _Svc:\n"
        "    pass\n"
        "def get_ocr_service():\n"
        "    return _Svc()\n"
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)            # cwd is elsewhere; `site` is trusted
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, "document_extraction", raising=False)

    avail = OcrAdapter().available()
    assert avail.available, f"legit install should load, got: {avail.reason!r}"
