from pathlib import Path

from oxison.sources.base import SourceUnit
from oxison.sources.ingest import IngestOutput, ingest_paths, render_extra_context


def test_ingest_dispatches_by_type_and_collects_ledger(tmp_path: Path):
    md = tmp_path / "notes.md"
    md.write_text("hello world", encoding="utf-8")
    unknown = tmp_path / "thing.xyz"
    unknown.write_text("?", encoding="utf-8")
    out = ingest_paths([md, unknown], ocr_enabled=False, stt_key=None)
    assert isinstance(out, IngestOutput)
    statuses = {r.origin: r.status for r in out.results}
    assert statuses[str(md)] == "ok"
    assert statuses[str(unknown)] == "skipped"   # no adapter
    assert out.unit_count == 1


def test_render_extra_context_groups_by_source():
    units = [
        SourceUnit("alpha body", "pdf", "/x/spec.pdf", "pdf:spec.pdf#p1", {}),
        SourceUnit("slide body", "pptx", "/x/d.pptx", "pptx:d.pptx#slide-1", {}),
    ]
    ctx = render_extra_context(units)
    assert "ADDITIONAL SOURCES" in ctx
    assert "pdf:spec.pdf#p1" in ctx
    assert "alpha body" in ctx
    assert "pptx:d.pptx#slide-1" in ctx


def test_render_extra_context_empty_is_blank():
    assert render_extra_context([]) == ""


def test_ingest_unknown_type_is_skipped_not_error(tmp_path: Path):
    f = tmp_path / "weird.zzz"
    f.write_text("x", encoding="utf-8")
    out = ingest_paths([f], ocr_enabled=False, stt_key=None)
    assert out.results[0].status == "skipped"
    assert "no adapter" in (out.results[0].reason or "")


def test_ingest_needs_ocr_handoff_when_enabled(tmp_path: Path, monkeypatch):
    from oxison.sources import ingest as ingest_mod
    from oxison.sources.base import SourceResult, SourceUnit

    f = tmp_path / "scanned.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    # PdfAdapter reports needs_ocr; OcrAdapter returns a real unit.
    monkeypatch.setattr(
        ingest_mod.PdfAdapter, "extract",
        lambda self, path: SourceResult.skip("pdf", str(path), reason="needs_ocr"),
    )
    monkeypatch.setattr(
        ingest_mod.OcrAdapter, "extract",
        lambda self, path: SourceResult.ok("pdf", str(path), units=[
            SourceUnit("ocr text", "pdf", str(path), f"pdf:{path.name}#p1", {"via": "ocr"})
        ]),
    )

    # ocr_enabled=True → handoff fires, unit comes from OCR
    out_on = ingest_paths([f], ocr_enabled=True, stt_key=None)
    assert out_on.results[0].status == "ok"
    assert out_on.unit_count == 1
    assert out_on.units[0].metadata.get("via") == "ocr"

    # ocr_enabled=False → no handoff, stays skipped as needs_ocr
    out_off = ingest_paths([f], ocr_enabled=False, stt_key=None)
    assert out_off.results[0].status == "skipped"
    assert out_off.results[0].reason == "needs_ocr"


def test_ingest_routes_recording_and_skips_without_key(tmp_path: Path):
    f = tmp_path / "meeting.mp3"
    f.write_bytes(b"fake audio")
    out = ingest_paths([f], ocr_enabled=False, stt_key=None)
    assert out.results[0].source_type == "recording"
    assert out.results[0].status == "skipped"
    assert "key" in (out.results[0].reason or "").lower()


def test_ingest_missing_add_file_is_skipped_not_crash(tmp_path):
    missing = tmp_path / "does_not_exist.md"   # never created
    out = ingest_paths([missing], ocr_enabled=False, stt_key=None)
    assert out.results[0].status == "skipped"
    assert "extraction failed" in (out.results[0].reason or "")


def test_ingest_recording_stub_is_skipped_not_crash(tmp_path):
    # A recording WITH a key but the real _transcribe is a NotImplementedError
    # stub — must be isolated to a skip, not crash the run.
    f = tmp_path / "demo.mp4"
    f.write_bytes(b"fake")
    out = ingest_paths([f], ocr_enabled=False, stt_key="sk-test")
    assert out.results[0].status == "skipped"
    assert "extraction failed" in (out.results[0].reason or "")
