"""Tests for the Oxipensa plan-gate."""

from __future__ import annotations

from oxison.oxipensa_gate import gate_roadmap
from oxison.roadmap_doc import RoadmapDoc, RoadmapTask, build_roadmap_doc


def _src():
    return {"schema_version": "1.0", "generated_at": "t", "product_what": "X"}


def _valid_raw():
    return {
        "summary": "s",
        "tasks": [
            {"title": "Build A", "kind": "feature", "priority": 1, "acceptance": ["A works"]},
            {
                "title": "Build B",
                "kind": "fix",
                "priority": 2,
                "acceptance": ["B works"],
                "depends_on": ["Build A"],
            },
        ],
    }


def _doc(raw):
    return build_roadmap_doc(raw=raw, source=_src(), generated_at="t")


def test_valid_roadmap_passes():
    res = gate_roadmap(_doc(_valid_raw()))
    assert res.ok
    assert res.violations == []


def test_empty_roadmap_fails():
    res = gate_roadmap(_doc({"tasks": []}))
    assert not res.ok
    assert any("no tasks" in v for v in res.violations)


def test_empty_title_fails():
    raw = {"tasks": [{"title": "", "kind": "feature", "priority": 1, "acceptance": ["x"]}]}
    res = gate_roadmap(_doc(raw))
    assert not res.ok
    assert any("empty title" in v for v in res.violations)


def test_invalid_kind_fails():
    raw = {"tasks": [{"title": "x", "kind": "banana", "priority": 1, "acceptance": ["x"]}]}
    res = gate_roadmap(_doc(raw))
    assert any("invalid kind" in v for v in res.violations)


def test_bad_priority_fails():
    raw = {"tasks": [{"title": "x", "kind": "fix", "priority": 0, "acceptance": ["x"]}]}
    res = gate_roadmap(_doc(raw))
    assert any("priority" in v for v in res.violations)


def test_missing_acceptance_fails():
    raw = {"tasks": [{"title": "x", "kind": "fix", "priority": 1}]}
    res = gate_roadmap(_doc(raw))
    assert any("acceptance" in v for v in res.violations)


def test_protected_files_hint_fails():
    raw = {
        "tasks": [
            {
                "title": "x",
                "kind": "infra",
                "priority": 1,
                "acceptance": ["x"],
                "files_hint": [".github/workflows/ci.yml"],
            }
        ]
    }
    res = gate_roadmap(_doc(raw))
    assert any("protected path" in v for v in res.violations)


def test_protected_env_and_lockfile_caught():
    raw = {
        "tasks": [
            {
                "title": "x",
                "kind": "chore",
                "priority": 1,
                "acceptance": ["x"],
                "files_hint": ["apps/api/.env", "pnpm-lock.yaml"],
            }
        ]
    }
    res = gate_roadmap(_doc(raw))
    assert sum("protected path" in v for v in res.violations) == 2


def test_protected_bare_directory_hint_caught():
    # A hint naming the protected directory itself (no child) must still fail.
    raw = {
        "tasks": [
            {
                "title": "x",
                "kind": "infra",
                "priority": 1,
                "acceptance": ["x"],
                "files_hint": [".github/workflows"],
            }
        ]
    }
    res = gate_roadmap(_doc(raw))
    assert any("protected path" in v for v in res.violations)


def test_self_dependency_single_violation():
    # A self-loop reports "depends on itself" but must NOT also report a cycle.
    a = RoadmapTask(identifier="oxpz-a", title="A", kind="fix", priority=1, rationale="",
                    acceptance=["x"], depends_on=["oxpz-a"])
    doc = RoadmapDoc(schema_version="1.0", generated_at="t", source=_src(),
                     summary="", open_questions=[], tasks=[a])
    res = gate_roadmap(doc)
    assert any("depends on itself" in v for v in res.violations)
    assert not any("cycle" in v for v in res.violations)


def test_duplicate_identifier_fails():
    # Two tasks with same kind+title collide on the deterministic identifier.
    raw = {
        "tasks": [
            {"title": "Same", "kind": "fix", "priority": 1, "acceptance": ["x"]},
            {"title": "same", "kind": "FIX", "priority": 2, "acceptance": ["y"]},
        ]
    }
    res = gate_roadmap(_doc(raw))
    assert any("duplicate identifier" in v for v in res.violations)


def test_dangling_dependency_fails():
    raw = {
        "tasks": [
            {
                "title": "B",
                "kind": "fix",
                "priority": 1,
                "acceptance": ["x"],
                "depends_on": ["Ghost"],
            }
        ]
    }
    res = gate_roadmap(_doc(raw))
    assert any("unknown task" in v for v in res.violations)


def test_dependency_cycle_fails():
    # Build a doc by hand so depends_on already holds identifiers forming a cycle.
    a = RoadmapTask(identifier="oxpz-a", title="A", kind="fix", priority=1, rationale="",
                    acceptance=["x"], depends_on=["oxpz-b"])
    b = RoadmapTask(identifier="oxpz-b", title="B", kind="fix", priority=1, rationale="",
                    acceptance=["y"], depends_on=["oxpz-a"])
    doc = RoadmapDoc(
        schema_version="1.0", generated_at="t", source=_src(),
        summary="", open_questions=[], tasks=[a, b],
    )
    res = gate_roadmap(doc)
    assert any("cycle" in v for v in res.violations)


def test_self_dependency_fails():
    a = RoadmapTask(identifier="oxpz-a", title="A", kind="fix", priority=1, rationale="",
                    acceptance=["x"], depends_on=["oxpz-a"])
    doc = RoadmapDoc(schema_version="1.0", generated_at="t", source=_src(),
                     summary="", open_questions=[], tasks=[a])
    res = gate_roadmap(doc)
    assert any("depends on itself" in v for v in res.violations)


def test_too_many_tasks_fails():
    raw = {
        "tasks": [
            {"title": f"T{i}", "kind": "fix", "priority": 1, "acceptance": ["x"]}
            for i in range(5)
        ]
    }
    res = gate_roadmap(_doc(raw), max_tasks=3)
    assert any("too many tasks" in v for v in res.violations)


def test_feedback_is_bulleted():
    res = gate_roadmap(_doc({"tasks": []}))
    assert res.feedback().startswith("- ")
