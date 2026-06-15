"""Small markdown post-processing helpers (oxison-owned, deterministic)."""

from __future__ import annotations


def strip_preamble(markdown: str) -> str:
    """Drop any chatty preamble before the first markdown heading.

    Workers are told to return only the body starting with ``# ``, but a
    model occasionally prepends a sentence ("Now I have enough info...").
    If a heading appears within the first few non-empty lines, drop
    everything before it. Otherwise leave the text untouched (trimmed).
    """
    lines = markdown.splitlines()
    seen_nonblank = 0
    for i, line in enumerate(lines):
        if line.strip():
            seen_nonblank += 1
        if line.lstrip().startswith("#"):
            return "\n".join(lines[i:]).strip() + "\n"
        if seen_nonblank >= 6:
            break
    return markdown.strip() + "\n"


__all__ = ["strip_preamble"]
