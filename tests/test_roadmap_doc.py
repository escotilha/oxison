"""Tests for the roadmap.json contract — build, identifiers, render."""

from __future__ import annotations

import json

from oxison.roadmap_doc import (
    SCHEMA_VERSION,
    build_roadmap_doc,
    deterministic_identifier,
    render_roadmap_md,
)


def _src():
    return {"schema_version": "1.0", "generated_at": "2026-06-14T00:00:00Z", "product_what": "X"}


def test_identifier_is_deterministic_and_stable():
    a = deterministic_identifier("feature", "Add JWT refresh")
    b = deterministic_identifier("feature", "Add JWT refresh")
    assert a == b
    assert a.startswith("oxpz-")


def test_identifier_normalizes_whitespace_and_case():
    a = deterministic_identifier("feature", "Add  JWT   Refresh")
    b = deterministic_identifier("FEATURE", "add jwt refresh")
    assert a == b


def test_identifier_differs_by_kind_and_title():
    assert deterministic_identifier("feature", "X") != deterministic_identifier("fix", "X")
    assert deterministic_identifier("feature", "X") != deterministic_identifier("feature", "Y")


def test_build_assigns_ids_and_resolves_depends_on_by_title():
    raw = {
        "summary": "do it",
        "open_questions": ["q1"],
        "tasks": [
            {"title": "Build A", "kind": "feature", "priority": 1, "acceptance": ["a done"]},
            {
                "title": "Build B",
                "kind": "feature",
                "priority": 2,
                "acceptance": ["b done"],
                "depends_on": ["Build A"],
            },
        ],
    }
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="2026-06-14T00:00:00Z")
    assert doc.schema_version == SCHEMA_VERSION
    a_id = deterministic_identifier("feature", "Build A")
    assert doc.tasks[0].identifier == a_id
    # B's depends_on title was rewritten to A's computed identifier.
    assert doc.tasks[1].depends_on == [a_id]


def test_unresolvable_dependency_kept_raw():
    raw = {
        "tasks": [
            {
                "title": "B",
                "kind": "feature",
                "priority": 1,
                "acceptance": ["x"],
                "depends_on": ["Nonexistent task"],
            }
        ]
    }
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    assert doc.tasks[0].depends_on == ["Nonexistent task"]


def test_coercion_defaults_missing_fields():
    raw = {"tasks": [{"title": "Only a title"}]}
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    t = doc.tasks[0]
    assert t.priority == 3
    assert t.estimated_effort == "M"
    assert t.kind == ""
    assert t.evidence == [] and t.acceptance == [] and t.depends_on == []


def test_effort_normalized():
    raw = {"tasks": [{"title": "x", "kind": "fix", "estimated_effort": "huge"}]}
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    assert doc.tasks[0].estimated_effort == "M"


def test_to_json_roundtrips_shape():
    raw = {"summary": "s", "tasks": [{"title": "x", "kind": "fix", "acceptance": ["ok"]}]}
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    parsed = json.loads(doc.to_json())
    assert parsed["schema_version"] == "1.0"
    assert parsed["tasks"][0]["title"] == "x"
    assert "identifier" in parsed["tasks"][0]


def test_render_is_deterministic_and_ordered_by_priority():
    raw = {
        "summary": "thesis",
        "tasks": [
            {"title": "Later", "kind": "feature", "priority": 5, "acceptance": ["z"]},
            {"title": "First", "kind": "fix", "priority": 1, "acceptance": ["a"]},
        ],
    }
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    md = render_roadmap_md(doc)
    assert md == render_roadmap_md(doc)  # deterministic
    assert md.index("First") < md.index("Later")  # priority order
    assert "thesis" in md
    assert "**Acceptance:**" in md


def test_render_depends_on_shows_titles_not_ids():
    raw = {
        "tasks": [
            {"title": "Build A", "kind": "feature", "priority": 1, "acceptance": ["a"]},
            {
                "title": "Build B",
                "kind": "feature",
                "priority": 2,
                "acceptance": ["b"],
                "depends_on": ["Build A"],
            },
        ]
    }
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    md = render_roadmap_md(doc)
    assert "**Depends on:** Build A" in md
    # the depends-on line must not leak the opaque identifier
    dep_line = md.split("**Depends on:**")[1].splitlines()[0]
    assert "oxpz-" not in dep_line


# ---------------------------------------------------------------------------
# relevance — the planner self-assessment + its defensive coercion / render.
# ---------------------------------------------------------------------------


def test_relevance_defaults_to_one_when_absent():
    # An older planner that doesn't emit `relevance` must be unaffected (1.0).
    raw = {"tasks": [{"title": "x", "kind": "fix", "acceptance": ["ok"]}]}
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    assert doc.tasks[0].relevance == 1.0


def test_relevance_parsed_and_clamped():
    raw = {
        "tasks": [
            {"title": "a", "kind": "fix", "acceptance": ["x"], "relevance": 0.3},
            {"title": "b", "kind": "fix", "acceptance": ["x"], "relevance": "0.7"},
            {"title": "c", "kind": "fix", "acceptance": ["x"], "relevance": 5},
            {"title": "d", "kind": "fix", "acceptance": ["x"], "relevance": -2},
            {"title": "e", "kind": "fix", "acceptance": ["x"], "relevance": "nonsense"},
            {"title": "f", "kind": "fix", "acceptance": ["x"], "relevance": True},
        ]
    }
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    rel = {t.title: t.relevance for t in doc.tasks}
    assert rel["a"] == 0.3
    assert rel["b"] == 0.7        # numeric string parsed
    assert rel["c"] == 1.0        # clamped down
    assert rel["d"] == 0.0        # clamped up
    assert rel["e"] == 1.0        # invalid string -> default
    assert rel["f"] == 1.0        # bool treated as missing -> default


def test_render_surfaces_below_default_relevance_only():
    raw = {
        "tasks": [
            {"title": "Core", "kind": "feature", "priority": 1,
             "acceptance": ["a"], "relevance": 1.0},
            {"title": "Marginal", "kind": "feature", "priority": 2,
             "acceptance": ["b"], "relevance": 0.4},
        ]
    }
    doc = build_roadmap_doc(raw=raw, source=_src(), generated_at="t")
    md = render_roadmap_md(doc)
    assert "relevance 0.40" in md          # the kept-but-marginal task is tagged
    assert "relevance 1.00" not in md      # a default-relevance task is not
