"""Tests for ingesting an Oxipensa roadmap.json into the taskstore."""

from __future__ import annotations

import json

import pytest

from oxison.engine.roadmap_ingest import (
    RoadmapIngestError,
    ingest_roadmap,
    load_roadmap,
)
from oxison.engine.taskstore import TaskStore


def _roadmap():
    return {
        "schema_version": "1.0",
        "tasks": [
            {"identifier": "oxpz-a", "title": "A", "kind": "feature", "priority": 1,
             "acceptance": ["a works"], "depends_on": [], "files_hint": ["src/a.py"]},
            {"identifier": "oxpz-b", "title": "B", "kind": "fix", "priority": 2,
             "acceptance": ["b works"], "depends_on": ["oxpz-a"], "files_hint": []},
        ],
    }


def test_ingest_adds_tasks(tmp_path):
    s = TaskStore.open(tmp_path)
    res = ingest_roadmap(s, _roadmap())
    assert res.added == 2 and res.skipped == 0
    a = s.get_task("oxpz-a")
    assert a is not None and a.priority == 1
    assert a.files_touched == ["src/a.py"]  # files_hint seeds the lock set


def test_ingest_dedups_on_reingest(tmp_path):
    s = TaskStore.open(tmp_path)
    ingest_roadmap(s, _roadmap())
    res2 = ingest_roadmap(s, _roadmap())  # same roadmap again
    assert res2.added == 0 and res2.skipped == 2
    assert len(s.all_tasks()) == 2


def test_ingest_skips_malformed_tasks(tmp_path):
    s = TaskStore.open(tmp_path)
    rm = {"tasks": [{"identifier": "", "title": "no id"}, "not a dict",
                    {"identifier": "ok", "title": "fine", "acceptance": ["x"]}]}
    res = ingest_roadmap(s, rm)
    assert res.added == 1 and res.skipped == 2


def test_load_roadmap_from_dir(tmp_path):
    (tmp_path / "roadmap.json").write_text(json.dumps(_roadmap()), encoding="utf-8")
    data = load_roadmap(tmp_path)
    assert len(data["tasks"]) == 2


def test_load_roadmap_missing(tmp_path):
    with pytest.raises(RoadmapIngestError, match="no roadmap.json"):
        load_roadmap(tmp_path)


def test_load_roadmap_not_a_roadmap(tmp_path):
    (tmp_path / "roadmap.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    with pytest.raises(RoadmapIngestError, match="no tasks"):
        load_roadmap(tmp_path / "roadmap.json")


def test_is_safe_identifier_accepts_planner_ids_and_slugs():
    from oxison.engine.roadmap_ingest import is_safe_identifier
    for ok in ["oxpz-1a2b3c4d5e", "oxpz-a", "feature_x", "v1.2", "A1", "task-01"]:
        assert is_safe_identifier(ok), ok


def test_is_safe_identifier_rejects_traversal_and_unsafe_chars():
    from oxison.engine.roadmap_ingest import is_safe_identifier
    for bad in [
        "../../etc/passwd",      # path traversal
        "a/b",                   # separator
        "..",                    # parent
        "x..y",                  # embedded ..
        ".hidden",               # leading dot
        "-flag",                 # leading dash (could read as a CLI flag)
        "has space",             # whitespace
        "a;b",                   # shell-ish metachar
        "",                      # empty
        "x" * 129,               # over length cap
    ]:
        assert not is_safe_identifier(bad), bad


def test_ingest_drops_unsafe_identifier_keeps_safe(tmp_path):
    s = TaskStore.open(tmp_path)
    rm = {"schema_version": "1.0", "tasks": [
        {"identifier": "../../escape", "title": "evil", "acceptance": ["x"]},
        {"identifier": "oxpz-safe", "title": "good", "acceptance": ["x"]},
    ]}
    res = ingest_roadmap(s, rm)
    assert res.added == 1 and res.skipped == 1
    assert s.get_task("oxpz-safe") is not None
    assert s.get_task("../../escape") is None
