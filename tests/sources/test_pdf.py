from pathlib import Path

import pytest

from oxison.sources.pdf import PdfAdapter

pypdf = pytest.importorskip("pypdf")


def _make_blank_pdf(path: Path) -> None:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with open(path, "wb") as fh:
        w.write(fh)


def test_pdf_detect(tmp_path: Path):
    a = PdfAdapter()
    assert a.detect(tmp_path / "spec.pdf")
    assert not a.detect(tmp_path / "notes.md")


def test_pdf_blank_page_flags_needs_ocr(tmp_path: Path):
    f = tmp_path / "scanned.pdf"
    _make_blank_pdf(f)
    res = PdfAdapter().extract(f)
    assert res.status == "skipped"
    assert res.reason == "needs_ocr"


def test_pdf_extracts_text_when_present(tmp_path: Path, monkeypatch):
    f = tmp_path / "spec.pdf"
    _make_blank_pdf(f)
    monkeypatch.setattr(
        PdfAdapter, "_page_texts", lambda self, path: ["page one text", "page two text"]
    )
    res = PdfAdapter().extract(f)
    assert res.status == "ok"
    assert res.unit_count == 2
    assert res.units[0].locator == "pdf:spec.pdf#p1"
    assert res.units[1].locator == "pdf:spec.pdf#p2"
    assert "page two" in res.units[1].text


def test_pdf_degrades_when_pypdf_absent(tmp_path: Path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("forced: pypdf absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    res = PdfAdapter().extract(f)
    assert res.status == "skipped"
    assert "pypdf not installed" in (res.reason or "")
