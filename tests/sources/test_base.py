from oxison.sources.base import AdapterAvailability, SourceResult, SourceUnit


def test_source_unit_carries_provenance():
    u = SourceUnit(
        text="hello",
        source_type="pdf",
        origin_path="/x/spec.pdf",
        locator="pdf:spec.pdf#p1",
        metadata={"confidence": 0.9},
    )
    assert u.source_type == "pdf"
    assert u.locator == "pdf:spec.pdf#p1"
    assert u.metadata["confidence"] == 0.9


def test_source_result_ok_and_skip():
    ok = SourceResult.ok("pdf", "/x/spec.pdf", units=[
        SourceUnit("t", "pdf", "/x/spec.pdf", "pdf:spec.pdf#p1", {})
    ])
    assert ok.status == "ok"
    assert ok.unit_count == 1
    skip = SourceResult.skip("recording", "/x/a.mp4", reason="no STT key")
    assert skip.status == "skipped"
    assert skip.reason == "no STT key"
    assert skip.unit_count == 0


def test_availability():
    a = AdapterAvailability(available=False, reason="pypdf not installed")
    assert not a.available
    assert "pypdf" in (a.reason or "")


def test_mutable_defaults_are_not_shared():
    a = SourceUnit("a", "pdf", "/x", "pdf:x#p1")
    b = SourceUnit("b", "pdf", "/y", "pdf:y#p1")
    a.metadata["k"] = 1
    assert b.metadata == {}          # SourceUnit.metadata default_factory isolation
    r1 = SourceResult("pdf", "/x", "ok")
    r2 = SourceResult("pdf", "/y", "ok")
    r1.units.append(SourceUnit("u", "pdf", "/x", "pdf:x#p1"))
    assert r2.units == []            # SourceResult.units default_factory isolation
