"""ingest_urls + brief_unit tests — offline."""
from __future__ import annotations

import socket

import pytest

from oxison.sources.ingest import brief_unit, ingest_urls, render_extra_context
from oxison.sources.web import WebAdapter


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_a: object, **_k: object) -> None:
        raise AssertionError("network access in an ingest_urls test")
    monkeypatch.setattr(socket, "socket", _blocked)


def test_brief_unit_shape() -> None:
    u = brief_unit("build a todo app")
    assert u.source_type == "brief"
    assert u.locator == "brief:idea"
    assert u.text == "build a todo app"


def test_ingest_urls_empty_is_noop() -> None:
    out = ingest_urls([])
    assert out.results == []
    assert out.units == []


def test_ingest_urls_collects_units(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fetch(self: WebAdapter, url: str) -> dict[str, object]:
        return {"status": 200, "body": f"<p>page for {url}</p>", "content_type": "text/html"}
    monkeypatch.setattr(WebAdapter, "_fetch", _fetch)
    out = ingest_urls(["https://a.com", "https://b.com/x"])
    assert len(out.results) == 2
    assert all(r.status == "ok" for r in out.results)
    locators = {u.locator for u in out.units}
    assert locators == {"web:a.com", "web:b.com"}


def test_ingest_urls_fetch_error_becomes_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(self: WebAdapter, url: str) -> dict[str, object]:
        raise OSError("connection refused")
    monkeypatch.setattr(WebAdapter, "_fetch", _boom)
    out = ingest_urls(["https://down.example"])
    assert len(out.results) == 1
    assert out.results[0].status == "skipped"
    assert "fetch failed" in (out.results[0].reason or "")


def test_brief_unit_renders_into_extra_context() -> None:
    ctx = render_extra_context([brief_unit("an idea")])
    assert "brief:idea" in ctx
    assert "an idea" in ctx
