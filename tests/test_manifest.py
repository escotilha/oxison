from __future__ import annotations

from pathlib import Path

from oxison.manifest import STEP_NAMES, RunManifest


def test_create_and_roundtrip(tmp_path: Path) -> None:
    m = RunManifest.load_or_create(tmp_path, target="/repo", started_at="2026-01-01T00:00:00Z")
    assert m.path.exists()
    assert m.run_id
    assert set(m.steps) == set(STEP_NAMES)
    assert all(r.status == "pending" for r in m.steps.values())

    # Reload picks up the same run_id (resume semantics).
    again = RunManifest.load_or_create(tmp_path, target="/repo", started_at="ignored")
    assert again.run_id == m.run_id


def test_mark_and_is_complete(tmp_path: Path) -> None:
    m = RunManifest.load_or_create(tmp_path, target="/repo", started_at="t")
    assert not m.is_complete("map")
    m.mark("map", "done", cost_usd=0.12, artifact="MAP")
    assert m.is_complete("map")
    assert m.steps["map"].cost_usd == 0.12

    reloaded = RunManifest.load_or_create(tmp_path, target="/repo", started_at="t")
    assert reloaded.is_complete("map")
    assert reloaded.steps["map"].artifact == "MAP"


def test_total_cost(tmp_path: Path) -> None:
    m = RunManifest.load_or_create(tmp_path, target="/repo", started_at="t")
    m.mark("map", "done", cost_usd=0.10)
    m.mark("comprehend", "done", cost_usd=0.25)
    assert m.total_cost_usd() == 0.35


def test_corrupt_manifest_recreates(tmp_path: Path) -> None:
    (tmp_path / ".oxison-run.json").write_text("{not json")
    m = RunManifest.load_or_create(tmp_path, target="/repo", started_at="t")
    assert m.run_id  # recovered with a fresh manifest, no crash


def test_unknown_step_raises(tmp_path: Path) -> None:
    m = RunManifest.load_or_create(tmp_path, target="/repo", started_at="t")
    try:
        m.mark("nope", "done")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown step")


def test_mark_clears_stale_error_on_successful_rerun(tmp_path: Path) -> None:
    # A step that failed (error recorded) then succeeds must not keep the stale
    # error alongside status "done" (the bug: error field was never cleared).
    m = RunManifest.load_or_create(tmp_path, target="/repo", started_at="t")
    step = STEP_NAMES[0]
    m.mark(step, "failed", error="claude exited 1")
    assert m.steps[step].error == "claude exited 1"
    m.mark(step, "done", cost_usd=1.0)
    assert m.steps[step].status == "done"
    assert m.steps[step].error is None
    # persists across reload
    again = RunManifest.load_or_create(tmp_path, target="/repo", started_at="t")
    assert again.steps[step].error is None


def test_mark_failed_preserves_error_when_no_new_one(tmp_path: Path) -> None:
    m = RunManifest.load_or_create(tmp_path, target="/repo", started_at="t")
    step = STEP_NAMES[0]
    m.mark(step, "failed", error="boom")
    m.mark(step, "failed")  # re-failed without a new error string
    assert m.steps[step].error == "boom"  # error kept while status is failed
