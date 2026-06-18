from pathlib import Path

from oxison.sources.base import SourceResult, SourceUnit
from oxison.sources.ingest import (
    LOW_RELEVANCE_ANNOTATE,
    MAX_SOURCE_FILE_BYTES,
    IngestOutput,
    _safe_extract,
    domain_terms_from_repomap,
    ingest_paths,
    render_extra_context,
    source_relevance,
)


class _CountingAdapter:
    """A fake adapter that records whether extract() was reached."""
    name = "fake"

    def __init__(self):
        self.extract_called = False

    def extract(self, path):
        self.extract_called = True
        return SourceResult.ok(self.name, str(path), units=[])


def test_safe_extract_skips_oversized_file_before_parsing(tmp_path: Path, monkeypatch):
    # SECURITY-AUDIT.md F7: a file over the size cap is skipped BEFORE the parser
    # runs, so a malicious huge document can't DoS the in-process parser.
    big = tmp_path / "huge.pdf"
    big.write_bytes(b"%PDF-1.4\n")  # tiny on disk; we fake the reported size
    monkeypatch.setattr(
        "oxison.sources.ingest.Path.stat",
        lambda self: type("S", (), {"st_size": MAX_SOURCE_FILE_BYTES + 1})(),
    )
    adapter = _CountingAdapter()
    res = _safe_extract(adapter, big)
    assert res.status == "skipped"
    assert "too large" in (res.reason or "")
    assert adapter.extract_called is False  # parser never touched the file


def test_safe_extract_allows_normal_file(tmp_path: Path):
    # Regression guard: a normal-sized file still reaches the parser.
    f = tmp_path / "notes.pdf"
    f.write_bytes(b"%PDF-1.4\nsmall\n")
    adapter = _CountingAdapter()
    res = _safe_extract(adapter, f)
    assert res.status == "ok"
    assert adapter.extract_called is True


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


# ---------------------------------------------------------------------------
# Source domain-relevance gate (abstain-safe, repo mode only).
# ---------------------------------------------------------------------------


def _unit(text: str, locator: str = "src:x") -> SourceUnit:
    return SourceUnit(text, "brief", "(x)", locator, {})


def _build_repo_map_for(tmp_path: Path):
    """A tiny on-disk repo whose top-level vocabulary is 'widget' / 'sprocket'.

    Both terms are top-level entries so they land in the repo map's ``tree``
    (the domain-term source); a deeply-nested file would not be captured.
    """
    from oxison.repomap import build_repo_map

    (tmp_path / "widget").mkdir()
    (tmp_path / "widget" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "sprocket").mkdir()
    (tmp_path / "sprocket" / "core.py").write_text("y = 1\n", encoding="utf-8")
    return build_repo_map(tmp_path)


def test_render_without_domain_terms_is_unchanged():
    # Backward compat: the default (domain_terms=None) reproduces the original
    # rendering byte-for-byte — greenfield and old callers are unaffected.
    units = [_unit("hello world", "a:1"), _unit("foo bar", "b:2")]
    expected = "\n".join([
        "=== ADDITIONAL SOURCES ===",
        "(extracted by oxison from non-repo inputs; cite by locator)",
        "\n--- [a:1] ---\nhello world",
        "\n--- [b:2] ---\nfoo bar",
        "\n=== END ADDITIONAL SOURCES ===",
    ])
    assert render_extra_context(units) == expected


def test_source_relevance_empty_domain_returns_one():
    # No domain terms (greenfield) -> can't judge -> don't penalize.
    assert source_relevance("anything at all", frozenset()) == 1.0


def test_source_relevance_discriminates_on_topic_from_off(tmp_path: Path):
    terms = domain_terms_from_repomap(_build_repo_map_for(tmp_path))
    assert "widget" in terms and "sprocket" in terms
    on = source_relevance("the widget and the sprocket spin together", terms)
    off = source_relevance("a recipe for chocolate chip cookies and milk", terms)
    assert off == 0.0
    assert on > off
    assert off < LOW_RELEVANCE_ANNOTATE   # off-topic is flag-worthy


def test_render_annotates_low_relevance_but_keeps_it(tmp_path: Path):
    terms = domain_terms_from_repomap(_build_repo_map_for(tmp_path))
    rendered = render_extra_context(
        [_unit("cooking recipes and gardening tips", "off:1"),
         _unit("widget sprocket assembly notes", "on:1")],
        domain_terms=terms,
    )
    # off-topic kept but flagged; on-topic present and NOT flagged.
    assert "[off:1]" in rendered and "low domain relevance" in rendered
    assert "[on:1] ---" in rendered


def test_render_optin_min_score_drops_off_topic(tmp_path: Path):
    terms = domain_terms_from_repomap(_build_repo_map_for(tmp_path))
    rendered = render_extra_context(
        [_unit("cooking recipes and gardening tips", "off:1"),
         _unit("widget sprocket assembly notes", "on:1")],
        domain_terms=terms,
        min_score=0.1,
    )
    assert "[off:1]" not in rendered   # dropped (opt-in)
    assert "[on:1]" in rendered        # kept
