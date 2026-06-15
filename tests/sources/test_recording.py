from pathlib import Path

from oxison.sources.recording import RecordingAdapter


def test_recording_detect(tmp_path: Path):
    a = RecordingAdapter(stt_key=None)
    assert a.detect(tmp_path / "demo.mp4")
    assert a.detect(tmp_path / "call.m4a")
    assert not a.detect(tmp_path / "x.pdf")


def test_recording_degrades_without_key(tmp_path: Path):
    f = tmp_path / "demo.mp4"
    f.write_bytes(b"fake")
    res = RecordingAdapter(stt_key=None).extract(f)
    assert res.status == "skipped"
    assert "key" in (res.reason or "").lower()


def test_recording_transcribes_with_key(tmp_path: Path, monkeypatch):
    f = tmp_path / "demo.mp4"
    f.write_bytes(b"fake")
    a = RecordingAdapter(stt_key="sk-test", stt_provider="deepgram")
    monkeypatch.setattr(
        a, "_transcribe",
        lambda path: [
            {"start": "00:00:00", "text": "intro to the product"},
            {"start": "00:12:30", "text": "the roadmap section"},
        ],
    )
    res = a.extract(f)
    assert res.status == "ok"
    assert res.unit_count == 2
    assert res.units[0].locator == "rec:demo.mp4#00:00:00"
    assert res.units[1].locator == "rec:demo.mp4#00:12:30"
    assert "roadmap" in res.units[1].text
    assert res.units[0].metadata["provider"] == "deepgram"


def test_recording_skips_empty_transcript(tmp_path: Path, monkeypatch):
    f = tmp_path / "demo.mp4"
    f.write_bytes(b"fake")
    a = RecordingAdapter(stt_key="sk-test")
    monkeypatch.setattr(a, "_transcribe", lambda path: [{"start": "00:00:00", "text": "   "}])
    res = a.extract(f)
    assert res.status == "skipped"
    assert res.reason == "empty transcript"
