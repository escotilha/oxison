from pathlib import Path

from oxison.sources.docs import DocsAdapter


def test_docs_adapter_detects_md_txt(tmp_path: Path):
    a = DocsAdapter()
    assert a.detect(tmp_path / "notes.md")
    assert a.detect(tmp_path / "readme.txt")
    assert a.detect(tmp_path / "doc.rst")
    assert not a.detect(tmp_path / "deck.pptx")


def test_docs_adapter_extracts_text(tmp_path: Path):
    f = tmp_path / "notes.md"
    f.write_text("# Title\n\nbody text", encoding="utf-8")
    res = DocsAdapter().extract(f)
    assert res.status == "ok"
    assert res.unit_count == 1
    u = res.units[0]
    assert u.source_type == "docs"
    assert "body text" in u.text
    assert u.locator == f"docs:{f.name}"


def test_docs_adapter_is_read_only(tmp_path: Path):
    f = tmp_path / "notes.md"
    f.write_text("x", encoding="utf-8")
    before = f.read_bytes()
    DocsAdapter().extract(f)
    assert f.read_bytes() == before


def test_docs_adapter_skips_empty_file(tmp_path: Path):
    f = tmp_path / "blank.md"
    f.write_text("   \n\t ", encoding="utf-8")   # whitespace-only
    res = DocsAdapter().extract(f)
    assert res.status == "skipped"
    assert res.reason == "empty file"
    assert res.unit_count == 0
