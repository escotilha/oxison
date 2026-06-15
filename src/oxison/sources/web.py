"""URL / web-page adapter — fetch a user-provided link, extract readable text.

This is oxison's second deliberate off-host egress point (after the
recording/STT adapter): it issues an HTTP GET to a URL the *user* supplied.
There is NO model-initiated fetching — the read-only AI workers still only get
``Read,Glob,Grep``; this fetch happens in oxison's deterministic ingest stage,
before any AI call. The HTTP call is isolated in ``_fetch`` so it can be mocked
in tests (no network).

Stdlib only (``urllib`` + ``html.parser``) — no extra dependency. Hardening:
http/https only, a wall-clock timeout, and response-size + extracted-char caps.
URLs are user-provided, so no IP allowlist in v1 (tracked as a follow-up).
"""
from __future__ import annotations

import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

from .base import AdapterAvailability, SourceResult, SourceUnit

_MAX_BYTES = 5_000_000      # cap the download (5 MB)
_MAX_CHARS = 200_000        # cap the extracted text fed to the model
_TIMEOUT_S = 20.0
_UA = "oxison/0.1 (+https://github.com/escotilha/oxison)"
_SKIP_TAGS = {"script", "style", "head", "noscript", "template", "svg", "nav", "footer"}


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style/chrome; capture <title>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._parts.append(text)

    def text(self) -> str:
        return "\n".join(self._parts)


class WebAdapter:
    name = "web"

    def __init__(self, *, timeout_s: float = _TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    def detect(self, url: str) -> bool:
        return urlparse(url).scheme in ("http", "https")

    def available(self) -> AdapterAvailability:
        # Stdlib only — always available.
        return AdapterAvailability(available=True)

    def _fetch(self, url: str) -> dict[str, object]:
        """GET the URL; return {status, body, content_type}. Isolated for mocking."""
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        resp = urllib.request.urlopen(req, timeout=self.timeout_s)  # nosec B310
        with resp:
            content_type = resp.headers.get_content_type()
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(_MAX_BYTES)
            status = int(getattr(resp, "status", 0) or 0)
        body = raw.decode(charset, errors="replace")
        return {"status": status, "body": body, "content_type": content_type}

    def extract(self, url: str) -> SourceResult:
        if not self.detect(url):
            return SourceResult.skip(
                self.name, url, reason="unsupported URL scheme (http/https only)"
            )
        fetched = self._fetch(url)
        content_type = str(fetched.get("content_type") or "")
        body = str(fetched.get("body") or "")
        if content_type == "text/html" or content_type == "application/xhtml+xml":
            parser = _TextExtractor()
            parser.feed(body)
            text = parser.text()
            title = parser.title
        elif content_type.startswith("text/"):
            text = body
            title = ""
        else:
            return SourceResult.skip(
                self.name, url, reason=f"unsupported content-type: {content_type}"
            )
        text = text.strip()[:_MAX_CHARS]
        if not text:
            return SourceResult.skip(self.name, url, reason="no extractable text")
        host = urlparse(url).hostname or url
        unit = SourceUnit(
            text=text,
            source_type=self.name,
            origin_path=url,
            locator=f"web:{host}",
            metadata={"url": url, "status": fetched.get("status"), "title": title},
        )
        return SourceResult.ok(self.name, url, units=[unit])
