"""Cross-stage / cross-cutting tests added from the whole-platform audit.

These exercise the seams the per-stage suites missed: the Oxicome→Oxipensa→Oxfaz
contract chain, schema-version pinning at both seams, and the C1 invariant that
the SAME protected path is rejected by BOTH the plan-gate and the grader.
"""

from __future__ import annotations

import json

import pytest

from oxison.comprehension_doc import build_comprehension_doc
from oxison.engine.engconfig import EngineConfig
from oxison.engine.gates import grade_diff
from oxison.engine.roadmap_ingest import (
    RoadmapIngestError,
    ingest_roadmap,
    load_roadmap,
)
from oxison.engine.taskstore import STATUS_PLANNED, TaskStore
from oxison.oxipensa import PlanError, load_comprehension
from oxison.oxipensa_gate import gate_roadmap
from oxison.roadmap_doc import RoadmapDoc, RoadmapTask, build_roadmap_doc
from oxison.sources.base import SourceResult

PROTECTED = EngineConfig().protected_paths


# -- the full contract chain: comprehension.json -> roadmap.json -> taskstore --

def test_comprehension_to_roadmap_to_taskstore_seam(tmp_path):
    # Stage 1 (Oxicome) emits comprehension.json.
    comp = build_comprehension_doc(
        comprehension_text="# TodoApp\nA CLI todo tool.",
        source_results=[SourceResult.ok("git", str(tmp_path), units=[])],
        generated_at="2026-06-14T00:00:00Z",
    )
    comp_path = tmp_path / "comprehension.json"
    comp_path.write_text(comp.to_json(), encoding="utf-8")

    # Stage 2 (Oxipensa) loads it — the contract key survives the round-trip.
    loaded = load_comprehension(comp_path)
    assert "comprehension_markdown" in loaded
    assert loaded["schema_version"] == "1.0"

    # Stage 2 emits roadmap.json (a realistic one built from the contract).
    raw = {"summary": "ship multiply", "tasks": [
        {"title": "Add multiply", "kind": "feature", "priority": 1,
         "acceptance": ["multiply(a,b) returns a*b"], "files_hint": ["calc.py"]}]}
    doc = build_roadmap_doc(
        raw=raw,
        source={"schema_version": "1.0", "generated_at": "t", "product_what": "TodoApp"},
        generated_at="t",
    )
    rm_path = tmp_path / "roadmap.json"
    rm_path.write_text(doc.to_json(), encoding="utf-8")

    # Stage 3 (Oxfaz) loads + ingests it — the chain holds end to end.
    store = TaskStore.open(tmp_path)
    res = ingest_roadmap(store, load_roadmap(rm_path))
    assert res.added == 1
    task = store.get_task(doc.tasks[0].identifier)
    assert task is not None
    assert task.status == STATUS_PLANNED
    assert task.acceptance == ["multiply(a,b) returns a*b"]
    assert task.files_touched == ["calc.py"]  # files_hint seeded the lock set


# -- schema-version pinning at both seams --------------------------------

def test_load_comprehension_rejects_future_schema(tmp_path):
    p = tmp_path / "comprehension.json"
    p.write_text(json.dumps({"schema_version": "2.0", "comprehension_markdown": "# x"}),
                 encoding="utf-8")
    with pytest.raises(PlanError, match="unsupported"):
        load_comprehension(p)


def test_load_comprehension_accepts_1x(tmp_path):
    p = tmp_path / "comprehension.json"
    p.write_text(json.dumps({"schema_version": "1.3", "comprehension_markdown": "# x"}),
                 encoding="utf-8")
    assert load_comprehension(p)["schema_version"] == "1.3"  # minor bumps are fine


def test_load_roadmap_rejects_future_schema(tmp_path):
    p = tmp_path / "roadmap.json"
    p.write_text(json.dumps({"schema_version": "2.0", "tasks": []}), encoding="utf-8")
    with pytest.raises(RoadmapIngestError, match="unsupported"):
        load_roadmap(p)


# -- C1: the same protected path is rejected by BOTH gate and grader -----

def _doc_with_hint(path: str) -> RoadmapDoc:
    task = RoadmapTask(identifier="oxpz-x", title="X", kind="infra", priority=1,
                       rationale="", acceptance=["x"], files_hint=[path])
    return RoadmapDoc(schema_version="1.0", generated_at="t",
                      source={"schema_version": "1.0", "generated_at": "t", "product_what": "X"},
                      summary="", open_questions=[], tasks=[task])


def test_C1_protected_path_rejected_by_both_gate_and_grader():
    path = ".github/workflows/ci.yml"
    # grader (acts on the real diff)
    verdict = grade_diff([path], protected_paths=PROTECTED)
    assert not verdict.ok and path in verdict.protected_hits
    # plan-gate (acts on the declared files_hint)
    res = gate_roadmap(_doc_with_hint(path))
    assert any("protected path" in v for v in res.violations)


def test_C1_bare_protected_directory_rejected_by_both():
    # The bare-directory case the grader previously missed — now symmetric.
    for path in ["oxison-build", ".github/workflows", ".git"]:
        assert not grade_diff([path], protected_paths=PROTECTED).ok, path
        violations = gate_roadmap(_doc_with_hint(path)).violations
        assert any("protected path" in v for v in violations), path


@pytest.mark.parametrize("path", [
    "uv.lock",              # oxison's own lockfile — the headline gap
    "go.sum",
    "Gemfile.lock",
    "Pipfile.lock",
    "composer.lock",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    ".github/dependabot.yml",
])
def test_C1_lockfile_and_ci_paths_rejected_by_both(path):
    # A prompt-injected (or hand-crafted direct-build) worker must not be able
    # to tamper with a dependency lockfile or CI pipeline. Both the grader and
    # the plan-gate reject these, same as any other protected path (C1 parity).
    assert not grade_diff([path], protected_paths=PROTECTED).ok, path
    violations = gate_roadmap(_doc_with_hint(path)).violations
    assert any("protected path" in v for v in violations), path
