"""Tests for the Oxipensa orchestrator (claude -p mocked, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import oxison.oxipensa as oxipensa
from oxison.config import READ_ONLY_TOOLS, RunConfig
from oxison.dispatch import InvokeResult
from oxison.oxipensa import PlanError, load_comprehension, plan


def _cfg(tmp_path: Path) -> RunConfig:
    return RunConfig(
        target=tmp_path,
        output_dir=tmp_path,
        auth_mode="oauth",
        api_key=None,
        model=None,
        max_budget_usd=None,
        chunk_threshold=100_000,
        max_concurrency=1,
        resume=False,
        target_is_git=False,
    )


def _comprehension() -> dict:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-06-14T00:00:00Z",
        "sources": [{"type": "git", "origin": "/repo", "status": "ok", "units": 10}],
        "product": {"what": "A todo app"},
        "state": {},
        "stack": {},
        "open_questions": ["Should it sync to the cloud?"],
        "comprehension_markdown": "# Todo app\nA small CLI todo app.",
    }


class _FakeInvoker:
    """Returns canned (ok, text) responses in order; records calls."""

    def __init__(self, responses: list[tuple[bool, str]]):
        self._responses = responses
        self.calls: list[dict] = []

    async def __call__(self, prompt: str, **kwargs) -> InvokeResult:
        self.calls.append({"prompt": prompt, **kwargs})
        ok, text = self._responses[len(self.calls) - 1]
        return InvokeResult(ok=ok, text=text, cost_usd=0.01, exit_code=0 if ok else 1,
                            error=None if ok else "boom")


def _good_roadmap_json() -> str:
    return json.dumps(
        {
            "summary": "Ship sync",
            "open_questions": [],
            "tasks": [
                {"title": "Add cloud sync", "kind": "feature", "priority": 1,
                 "acceptance": ["todos persist across devices"], "evidence": ["git:src/store.py"]},
            ],
        }
    )


@pytest.mark.asyncio
async def test_happy_path(tmp_path, monkeypatch):
    fake = _FakeInvoker([(True, _good_roadmap_json())])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(_cfg(tmp_path), _comprehension(), generated_at="t")
    assert result.attempts == 1
    assert result.cost_usd == pytest.approx(0.01)
    assert len(result.doc.tasks) == 1
    assert result.doc.tasks[0].title == "Add cloud sync"
    assert result.doc.tasks[0].identifier.startswith("oxpz-")


@pytest.mark.asyncio
async def test_worker_is_read_only(tmp_path, monkeypatch):
    fake = _FakeInvoker([(True, _good_roadmap_json())])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    await plan(_cfg(tmp_path), _comprehension(), generated_at="t")
    assert fake.calls[0]["allowed_tools"] == READ_ONLY_TOOLS
    assert "Edit" not in fake.calls[0]["allowed_tools"]
    assert "Write" not in fake.calls[0]["allowed_tools"]


@pytest.mark.asyncio
async def test_self_correction_recovers(tmp_path, monkeypatch):
    # Attempt 1: a task with no acceptance (gate fails). Attempt 2: valid.
    bad = json.dumps({"tasks": [{"title": "x", "kind": "fix", "priority": 1}]})
    fake = _FakeInvoker([(True, bad), (True, _good_roadmap_json())])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(_cfg(tmp_path), _comprehension(), generated_at="t")
    assert result.attempts == 2
    assert result.cost_usd == pytest.approx(0.02)
    # The retry prompt carried the gate's feedback.
    assert "acceptance" in fake.calls[1]["prompt"]


@pytest.mark.asyncio
async def test_gate_failure_after_all_attempts_raises(tmp_path, monkeypatch):
    bad = json.dumps({"tasks": [{"title": "x", "kind": "fix", "priority": 1}]})
    fake = _FakeInvoker([(True, bad), (True, bad)])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    with pytest.raises(PlanError, match="plan-gate"):
        await plan(_cfg(tmp_path), _comprehension(), generated_at="t")


@pytest.mark.asyncio
async def test_invalid_json_then_raises(tmp_path, monkeypatch):
    fake = _FakeInvoker([(True, "not json"), (True, "still not json")])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    with pytest.raises(PlanError):
        await plan(_cfg(tmp_path), _comprehension(), generated_at="t")


@pytest.mark.asyncio
async def test_worker_failure_raises_immediately(tmp_path, monkeypatch):
    fake = _FakeInvoker([(False, "")])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    with pytest.raises(PlanError, match="planner worker failed"):
        await plan(_cfg(tmp_path), _comprehension(), generated_at="t")
    assert len(fake.calls) == 1  # no retry on a process failure


@pytest.mark.asyncio
async def test_fenced_json_is_parsed(tmp_path, monkeypatch):
    fenced = "```json\n" + _good_roadmap_json() + "\n```"
    fake = _FakeInvoker([(True, fenced)])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(_cfg(tmp_path), _comprehension(), generated_at="t")
    assert len(result.doc.tasks) == 1


@pytest.mark.asyncio
async def test_product_name_falls_back_to_h1(tmp_path, monkeypatch):
    # v1 Oxicome leaves product empty; the H1 of the prose is the product name.
    comp = _comprehension()
    comp["product"] = {}
    comp["comprehension_markdown"] = "# linkshort\nA URL shortener."
    fake = _FakeInvoker([(True, _good_roadmap_json())])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(_cfg(tmp_path), comp, generated_at="t")
    assert result.doc.source["product_what"] == "linkshort"


@pytest.mark.asyncio
async def test_open_questions_merged_from_comprehension(tmp_path, monkeypatch):
    comp = _comprehension()  # open_questions: ["Should it sync to the cloud?"]
    roadmap = json.loads(_good_roadmap_json())
    roadmap["open_questions"] = ["What auth model?"]
    fake = _FakeInvoker([(True, json.dumps(roadmap))])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(_cfg(tmp_path), comp, generated_at="t")
    q = result.doc.open_questions
    assert "What auth model?" in q
    assert "Should it sync to the cloud?" in q  # carried from comprehension
    assert q.index("What auth model?") < q.index("Should it sync to the cloud?")


@pytest.mark.asyncio
async def test_open_questions_deduped(tmp_path, monkeypatch):
    comp = _comprehension()
    roadmap = json.loads(_good_roadmap_json())
    roadmap["open_questions"] = ["Should it sync to the cloud?"]  # same as comp
    fake = _FakeInvoker([(True, json.dumps(roadmap))])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(_cfg(tmp_path), comp, generated_at="t")
    assert result.doc.open_questions.count("Should it sync to the cloud?") == 1


def _mixed_relevance_roadmap_json() -> str:
    # One core task and one speculative low-relevance task.
    return json.dumps(
        {
            "summary": "Ship core, defer fluff",
            "open_questions": [],
            "tasks": [
                {"title": "Add cloud sync", "kind": "feature", "priority": 1,
                 "acceptance": ["todos persist across devices"], "relevance": 0.95},
                {"title": "Add holiday theme", "kind": "feature", "priority": 5,
                 "acceptance": ["a festive theme is selectable"], "relevance": 0.05},
            ],
        }
    )


@pytest.mark.asyncio
async def test_plan_prunes_low_relevance_task(tmp_path, monkeypatch):
    fake = _FakeInvoker([(True, _mixed_relevance_roadmap_json())])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(_cfg(tmp_path), _comprehension(), generated_at="t")
    titles = [t.title for t in result.doc.tasks]
    assert titles == ["Add cloud sync"]            # core kept
    assert [t.title for t in result.pruned] == ["Add holiday theme"]  # fluff pruned


@pytest.mark.asyncio
async def test_plan_pruned_empty_when_nothing_below_floor(tmp_path, monkeypatch):
    # The default roadmap omits relevance -> all default to 1.0 -> nothing pruned.
    fake = _FakeInvoker([(True, _good_roadmap_json())])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(_cfg(tmp_path), _comprehension(), generated_at="t")
    assert result.pruned == []


@pytest.mark.asyncio
async def test_plan_relevance_optout_keeps_all(tmp_path, monkeypatch):
    fake = _FakeInvoker([(True, _mixed_relevance_roadmap_json())])
    monkeypatch.setattr(oxipensa, "invoke", fake)
    result = await plan(
        _cfg(tmp_path), _comprehension(), generated_at="t", relevance_min_score=0.0
    )
    assert {t.title for t in result.doc.tasks} == {"Add cloud sync", "Add holiday theme"}
    assert result.pruned == []


def test_load_comprehension_from_file(tmp_path):
    p = tmp_path / "comprehension.json"
    p.write_text(json.dumps(_comprehension()), encoding="utf-8")
    data = load_comprehension(p)
    assert data["product"]["what"] == "A todo app"


def test_load_comprehension_from_dir(tmp_path):
    (tmp_path / "comprehension.json").write_text(json.dumps(_comprehension()), encoding="utf-8")
    data = load_comprehension(tmp_path)
    assert "comprehension_markdown" in data


def test_load_comprehension_missing_raises(tmp_path):
    with pytest.raises(PlanError, match="no comprehension.json"):
        load_comprehension(tmp_path)


def test_load_comprehension_not_a_comprehension_raises(tmp_path):
    p = tmp_path / "comprehension.json"
    p.write_text(json.dumps({"unrelated": True}), encoding="utf-8")
    with pytest.raises(PlanError, match="does not look like"):
        load_comprehension(p)
