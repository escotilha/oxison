"""AI-critic tests — verdict parsing + the make_critic closure (invoke faked)."""
from __future__ import annotations

import types

import pytest

from oxison.dispatch import InvokeResult
from oxison.engine.critic import _parse_verdict, make_critic


def test_parse_verdict_pass_fail_ambiguous():
    assert _parse_verdict("notes\nVERDICT: PASS")[0] is True
    failed, reason = _parse_verdict("analysis\nVERDICT: FAIL — endpoint returns 500")
    assert failed is False and "endpoint returns 500" in reason
    assert _parse_verdict("I think it's fine, no clear verdict")[0] is None


def _fake_invoke(result: InvokeResult):
    async def _inv(*_a, **_k):
        return result
    return _inv


def _task():
    return types.SimpleNamespace(title="add endpoint", acceptance=["GET /x returns 200"])


def _outcome(tmp_path):
    return types.SimpleNamespace(changed_files=["api.py"], worktree_path=str(tmp_path))


@pytest.mark.asyncio
async def test_critic_rejects_on_explicit_fail(monkeypatch, tmp_path):
    monkeypatch.setattr("oxison.engine.critic.invoke", _fake_invoke(
        InvokeResult(ok=True, text="reviewed\nVERDICT: FAIL — acceptance unmet",
                     cost_usd=0.03, exit_code=0)))
    v = await make_critic(types.SimpleNamespace())(_task(), _outcome(tmp_path))
    assert not v.ok and v.failure_class == "critic"
    assert "acceptance unmet" in v.reason and v.cost_usd == 0.03


@pytest.mark.asyncio
async def test_critic_accepts_on_pass(monkeypatch, tmp_path):
    monkeypatch.setattr("oxison.engine.critic.invoke", _fake_invoke(
        InvokeResult(ok=True, text="looks correct\nVERDICT: PASS", cost_usd=0.02, exit_code=0)))
    v = await make_critic(types.SimpleNamespace())(_task(), _outcome(tmp_path))
    assert v.ok and v.cost_usd == 0.02


@pytest.mark.asyncio
async def test_critic_fails_open_on_infra_error(monkeypatch, tmp_path):
    # An invoke failure (timeout/engine) must NOT reject good-by-grader work.
    monkeypatch.setattr("oxison.engine.critic.invoke", _fake_invoke(
        InvokeResult(ok=False, text="", cost_usd=0.0, exit_code=1, error="timeout")))
    v = await make_critic(types.SimpleNamespace())(_task(), _outcome(tmp_path))
    assert v.ok and "skipped" in v.reason  # defers to the grader's pass


@pytest.mark.asyncio
async def test_critic_ambiguous_defers_to_pass(monkeypatch, tmp_path):
    monkeypatch.setattr("oxison.engine.critic.invoke", _fake_invoke(
        InvokeResult(ok=True, text="It seems fine overall.", cost_usd=0.01, exit_code=0)))
    v = await make_critic(types.SimpleNamespace())(_task(), _outcome(tmp_path))
    assert v.ok  # no clear verdict → defer to grader's pass, not a rejection
