import json

from oxison.comprehension_doc import build_comprehension_doc
from oxison.sources.base import SourceResult, SourceUnit


def test_build_comprehension_doc_envelope_and_ledger():
    results = [
        SourceResult.ok("pdf", "/x/spec.pdf", units=[
            SourceUnit("t", "pdf", "/x/spec.pdf", "pdf:spec.pdf#p1", {})
        ]),
        SourceResult.skip("recording", "/x/demo.mp4", reason="no STT key"),
    ]
    doc = build_comprehension_doc(
        comprehension_text="# What it is\n...",
        source_results=results,
        generated_at="2026-06-14T00:00:00Z",
    )
    blob = json.loads(doc.to_json())
    assert blob["schema_version"] == "1.0"
    assert blob["generated_at"] == "2026-06-14T00:00:00Z"
    ledger = {s["origin"]: s for s in blob["sources"]}
    assert ledger["/x/spec.pdf"]["status"] == "ok"
    assert ledger["/x/spec.pdf"]["units"] == 1
    assert ledger["/x/demo.mp4"]["status"] == "skipped"
    assert ledger["/x/demo.mp4"]["reason"] == "no STT key"
    assert "comprehension_markdown" in blob
    assert blob["comprehension_markdown"].startswith("# What it is")


def test_to_json_is_valid_and_stable():
    doc = build_comprehension_doc(
        comprehension_text="x", source_results=[], generated_at="2026-06-14T00:00:00Z"
    )
    blob = json.loads(doc.to_json())
    assert blob["sources"] == []
    assert blob["schema_version"] == "1.0"
    # reserved structured fields exist and default empty
    assert blob["product"] == {}
    assert blob["state"] == {}
    assert blob["stack"] == {}
    assert blob["open_questions"] == []


def test_ok_ledger_entry_has_no_reason_key():
    doc = build_comprehension_doc(
        comprehension_text="x",
        source_results=[SourceResult.ok("docs", "/a.md", units=[])],
        generated_at="2026-06-14T00:00:00Z",
    )
    blob = json.loads(doc.to_json())
    entry = blob["sources"][0]
    assert entry["status"] == "ok"
    assert "reason" not in entry   # reason only present on skipped entries


def test_contract_key_sets_are_frozen():
    # The full top-level envelope key set and the ledger-entry key sets are
    # the Oxipensa contract — freeze them so a refactor can't silently drop
    # or rename a key without a test failing.
    doc = build_comprehension_doc(
        comprehension_text="x",
        source_results=[
            SourceResult.ok("pdf", "/a.pdf", units=[
                SourceUnit("t", "pdf", "/a.pdf", "pdf:a.pdf#p1", {})
            ]),
            SourceResult.skip("recording", "/b.mp4", reason="no key"),
        ],
        generated_at="2026-06-14T00:00:00Z",
    )
    blob = json.loads(doc.to_json())
    assert set(blob.keys()) == {
        "schema_version", "generated_at", "sources", "product",
        "state", "stack", "open_questions", "comprehension_markdown",
    }
    ok_entry = next(s for s in blob["sources"] if s["status"] == "ok")
    skip_entry = next(s for s in blob["sources"] if s["status"] == "skipped")
    assert set(ok_entry.keys()) == {"type", "origin", "status", "units"}
    assert set(skip_entry.keys()) == {"type", "origin", "status", "units", "reason"}
