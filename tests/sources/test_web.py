"""WebAdapter tests — all offline (the _fetch network call is mocked)."""
from __future__ import annotations

import socket
from collections.abc import Callable

import pytest

from oxison.sources.web import WebAdapter


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: any real socket use in these tests is a failure."""
    def _blocked(*_a: object, **_k: object) -> None:
        raise AssertionError("network access in a WebAdapter test")
    monkeypatch.setattr(socket, "socket", _blocked)


def _fake_fetch(
    body: str, content_type: str = "text/html", status: int = 200
) -> Callable[[WebAdapter, str], dict[str, object]]:
    def _f(self: WebAdapter, url: str) -> dict[str, object]:
        return {"status": status, "body": body, "content_type": content_type}
    return _f


def test_detect_accepts_http_https() -> None:
    a = WebAdapter()
    assert a.detect("http://example.com")
    assert a.detect("https://example.com/page")


def test_detect_rejects_non_http() -> None:
    a = WebAdapter()
    assert not a.detect("file:///etc/passwd")
    assert not a.detect("ftp://example.com")
    assert not a.detect("/local/path/file.md")
    assert not a.detect("example.com")  # no scheme


def test_extract_html_returns_one_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (
        "<html><head><title>My Title</title>"
        "<style>.x{color:red}</style></head>"
        "<body><script>evil()</script>"
        "<h1>Hello</h1><p>World body text.</p></body></html>"
    )
    monkeypatch.setattr(WebAdapter, "_fetch", _fake_fetch(html))
    res = WebAdapter().extract("https://example.com/post")
    assert res.status == "ok"
    assert len(res.units) == 1
    u = res.units[0]
    assert u.source_type == "web"
    assert u.locator == "web:example.com"
    assert u.metadata["url"] == "https://example.com/post"
    assert u.metadata["title"] == "My Title"
    # visible text kept; script/style dropped
    assert "Hello" in u.text and "World body text." in u.text
    assert "evil()" not in u.text and "color:red" not in u.text


def test_extract_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WebAdapter, "_fetch", _fake_fetch("raw notes", "text/plain"))
    res = WebAdapter().extract("https://example.com/notes.txt")
    assert res.status == "ok"
    assert res.units[0].text == "raw notes"


def test_extract_blank_html_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WebAdapter, "_fetch", _fake_fetch("<html><body></body></html>"))
    res = WebAdapter().extract("https://example.com/empty")
    assert res.status == "skipped"
    assert "no extractable text" in (res.reason or "")


def test_extract_non_text_content_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WebAdapter, "_fetch", _fake_fetch("%PDF...", "application/pdf"))
    res = WebAdapter().extract("https://example.com/file.pdf")
    assert res.status == "skipped"
    assert "content-type" in (res.reason or "")


def test_extract_bad_scheme_skips_without_fetch() -> None:
    # No _fetch mock — must skip on scheme before any network attempt.
    res = WebAdapter().extract("file:///etc/passwd")
    assert res.status == "skipped"
    assert "scheme" in (res.reason or "")
