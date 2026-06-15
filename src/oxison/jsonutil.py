"""Extract a single JSON object from model output (oxison-owned, deterministic).

A ``claude -p`` worker asked to "return JSON" reliably returns the right
*content* but occasionally wraps it: a ```json code fence, a "Here is the
plan:" preamble, or a trailing note. This helper pulls the first balanced
top-level JSON object out of such text so the caller gets a clean ``dict``.

It is intentionally narrow — it finds one object (``{ ... }``), not arrays or
scalars — because every oxison AI-JSON contract (the Oxipensa roadmap, future
gate verdicts) is a single envelope object. Pure function, no I/O, trivially
testable.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


class JsonExtractError(ValueError):
    """No parseable JSON object could be extracted from the text."""


def _strip_fences(text: str) -> str:
    """Remove a surrounding ```json ... ``` (or bare ```) fence if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # Drop the opening fence line (``` or ```json) and the closing fence.
    newline = stripped.find("\n")
    if newline == -1:
        return stripped
    body = stripped[newline + 1 :]
    end = body.rfind("```")
    if end != -1:
        body = body[:end]
    return body.strip()


def _balanced_object_at(text: str, start: int) -> str | None:
    """Return the brace-balanced ``{...}`` span beginning at ``text[start]``.

    ``text[start]`` must be ``{``. Tracks string state so a brace inside a JSON
    string literal does not affect depth, and respects backslash escapes.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _iter_balanced_objects(text: str) -> Iterator[str]:
    """Yield each brace-balanced ``{...}`` span at every ``{`` in ``text``.

    Yielding *every* candidate (not just the first) is what lets the extractor
    skip a brace-containing preamble — e.g. a model that echoes ``{root}`` or
    ``{id}`` placeholders before the real JSON object.
    """
    i = text.find("{")
    while i != -1:
        span = _balanced_object_at(text, i)
        if span is not None:
            yield span
        i = text.find("{", i + 1)


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object found in ``text``.

    Tolerates a surrounding code fence and chatty preamble/postamble. Raises
    :class:`JsonExtractError` when no object can be parsed — callers surface
    that as a hard failure (a malformed contract is never written silently).
    """
    if not text or not text.strip():
        raise JsonExtractError("empty model output")

    candidates = [_strip_fences(text), text]
    for candidate in candidates:
        # Try the whole candidate first (the common, clean case)...
        try:
            whole = json.loads(candidate)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, dict):
            return whole
        # ...then try each balanced object in turn, skipping any (e.g. a
        # brace-containing preamble) that isn't itself a valid JSON object.
        for span in _iter_balanced_objects(candidate):
            try:
                parsed = json.loads(span)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise JsonExtractError("no JSON object found in model output")


__all__ = ["JsonExtractError", "extract_json_object"]
