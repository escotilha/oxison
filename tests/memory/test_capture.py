"""Tests for the write path — the mechanical verify-before-store grader gate."""

from __future__ import annotations

from oxison.engine.dispatch import DispatchOutcome
from oxison.engine.gates import GradeVerdict
from oxison.engine.taskstore import Task
from oxison.memory.capture import capture_from_outcome, components_from_files
from oxison.memory.config import TIER_EPISODIC, TIER_PROCEDURAL
from oxison.memory.store import MemoryStore

NOW = "2026-06-15T00:00:00Z"


def _task(**kw):
    base = {"id": 1, "identifier": "oxpz-a", "title": "Add login", "status": "merged",
            "priority": 1, "kind": "feature", "acceptance": ["login works"]}
    base.update(kw)
    return Task(**base)


def _outcome(**kw):
    base = {"ok": True, "branch": "feat/oxison-oxpz-a", "worktree_path": "/wt",
            "changed_files": ["src/auth/login.py", "src/auth/session.py"]}
    base.update(kw)
    return DispatchOutcome(**base)


def test_adapter_failure_is_not_stored(tmp_path):
    s = MemoryStore.open(tmp_path)
    key = capture_from_outcome(
        s, task=_task(), outcome=_outcome(ok=False, adapter_failure=True),
        verdict=GradeVerdict(ok=False, reason="n/a"), scope="r", now=NOW, merged=False,
    )
    assert key is None
    assert s.all_records() == []  # an engine outage is not a lesson


def test_timeout_is_not_stored(tmp_path):
    s = MemoryStore.open(tmp_path)
    key = capture_from_outcome(
        s, task=_task(), outcome=_outcome(ok=False, timed_out=True),
        verdict=GradeVerdict(ok=False, reason="timeout"), scope="r", now=NOW, merged=False,
    )
    assert key is None and s.all_records() == []


def test_verified_success_stores_procedural(tmp_path):
    s = MemoryStore.open(tmp_path)
    key = capture_from_outcome(
        s, task=_task(), outcome=_outcome(),
        verdict=GradeVerdict(ok=True, reason="clean"), scope="r", now=NOW, merged=True,
    )
    assert key is not None
    rec = s.get(key)
    assert rec.tier == TIER_PROCEDURAL and rec.verified is True
    assert rec.purpose == "Add login"
    assert rec.anchors == ["src/auth"]  # structural component, not the files


def test_grader_clean_but_not_merged_is_not_stored(tmp_path):
    s = MemoryStore.open(tmp_path)
    key = capture_from_outcome(
        s, task=_task(), outcome=_outcome(),
        verdict=GradeVerdict(ok=True, reason="clean"), scope="r", now=NOW, merged=False,
    )
    assert key is None  # wait for the merge signal before promoting to memory


def test_grader_rejection_stores_episodic_antipattern(tmp_path):
    s = MemoryStore.open(tmp_path)
    key = capture_from_outcome(
        s, task=_task(), outcome=_outcome(ok=False),
        verdict=GradeVerdict(ok=False, reason="diff touches protected path(s): .github/workflows/"),
        scope="r", now=NOW, merged=False,
    )
    assert key is not None
    rec = s.get(key)
    assert rec.tier == TIER_EPISODIC and rec.verified is True
    assert "Anti-pattern" in rec.truth


def test_components_are_structural_not_files():
    comps = components_from_files(["src/auth/login.py", "src/auth/session.py", "README.md"])
    assert "src/auth" in comps        # the dir, the structural anchor
    assert "README.md" in comps       # a top-level file maps to itself
    assert "src/auth/login.py" not in comps  # never the run-specific filename


def test_stored_truth_has_no_raw_filenames(tmp_path):
    s = MemoryStore.open(tmp_path)
    key = capture_from_outcome(
        s, task=_task(), outcome=_outcome(),
        verdict=GradeVerdict(ok=True, reason="clean"), scope="r", now=NOW, merged=True,
    )
    truth = s.get(key).truth
    # structural anchors only — the diff, filenames, and line numbers are dropped
    assert "login.py" not in truth and "session.py" not in truth
