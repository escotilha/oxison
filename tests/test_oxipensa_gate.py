"""Tests for the Oxipensa plan-gate."""

from __future__ import annotations

from oxison.oxipensa_gate import (
    DEFAULT_RELEVANCE_MIN_SCORE,
    filter_by_relevance,
    gate_roadmap,
)
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


# ---------------------------------------------------------------------------
# filter_by_relevance — the plan-boundary relevance gate.
# ---------------------------------------------------------------------------


def _task(ident, relevance, *, depends_on=()):
    return RoadmapTask(
        identifier=ident, title=ident, kind="feature", priority=1, rationale="",
        acceptance=["x"], depends_on=list(depends_on), relevance=relevance,
    )


def _relevance_doc(tasks):
    return RoadmapDoc(schema_version="1.0", generated_at="t", source=_src(),
                      summary="", open_questions=[], tasks=tasks)


def test_filter_drops_below_floor():
    doc = _relevance_doc([_task("a", 0.9), _task("b", 0.1)])
    filtered, pruned = filter_by_relevance(doc)
    assert [t.identifier for t in filtered.tasks] == ["a"]
    assert [t.identifier for t in pruned] == ["b"]


def test_filter_preserves_order():
    doc = _relevance_doc([_task("a", 0.9), _task("b", 0.1), _task("c", 0.8)])
    filtered, _ = filter_by_relevance(doc)
    assert [t.identifier for t in filtered.tasks] == ["a", "c"]


def test_filter_transitive_keep_saves_depended_on_task():
    # 'b' is below the floor but a kept 'a' depends on it -> 'b' survives so the
    # filtered doc never carries a dangling dependency the gate would reject.
    doc = _relevance_doc([_task("a", 0.9, depends_on=["b"]), _task("b", 0.1)])
    filtered, pruned = filter_by_relevance(doc)
    assert {t.identifier for t in filtered.tasks} == {"a", "b"}
    assert pruned == []


def test_filter_transitive_keep_is_recursive():
    # a (kept) -> b (low) -> c (low): both b and c are pulled back in.
    doc = _relevance_doc([
        _task("a", 0.9, depends_on=["b"]),
        _task("b", 0.1, depends_on=["c"]),
        _task("c", 0.05),
    ])
    filtered, pruned = filter_by_relevance(doc)
    assert {t.identifier for t in filtered.tasks} == {"a", "b", "c"}
    assert pruned == []


def test_filter_noop_when_all_default_relevance():
    tasks = [_task("a", 1.0), _task("b", 1.0)]
    doc = _relevance_doc(tasks)
    filtered, pruned = filter_by_relevance(doc)
    # Identity no-op: same object back, empty pruned list.
    assert filtered is doc
    assert pruned == []


def test_filter_optout_when_min_score_non_positive():
    doc = _relevance_doc([_task("a", 0.0), _task("b", 0.0)])
    filtered, pruned = filter_by_relevance(doc, min_score=0.0)
    assert filtered is doc
    assert pruned == []


def test_filter_floor_is_inclusive():
    # A task exactly at the floor is kept (>= , not >).
    doc = _relevance_doc([_task("a", DEFAULT_RELEVANCE_MIN_SCORE)])
    filtered, pruned = filter_by_relevance(doc)
    assert [t.identifier for t in filtered.tasks] == ["a"]
    assert pruned == []


def test_filtered_doc_still_passes_the_gate():
    # The end-to-end invariant: pruning never produces a gate-rejectable roadmap.
    raw = {
        "tasks": [
            {"title": "Core", "kind": "feature", "priority": 1,
             "acceptance": ["works"], "relevance": 0.9},
            {"title": "Gold plating", "kind": "feature", "priority": 2,
             "acceptance": ["works"], "relevance": 0.05},
        ],
    }
    doc = _doc(raw)
    filtered, pruned = filter_by_relevance(doc)
    assert len(pruned) == 1 and pruned[0].title == "Gold plating"
    assert gate_roadmap(filtered).ok
