"""Ingestion orchestrator: detect -> dispatch -> collect -> render.

Selects the right adapter per input path, runs it, accumulates a
per-source status ledger, and renders the collected units into the
``extra_context`` text block injected into comprehension/generation
prompts. Inputs with no matching adapter are recorded skipped (never
an error). This is the single entry point the pipeline calls.

When grounding against an existing repo (not greenfield), the renderer can
score each source's *domain relevance* — a coarse, deterministic lexical
overlap between the source text and the repo's own vocabulary (its name,
languages, dependencies, top-level layout). The gate is **abstain-safe**: by
default it *annotates* a low-relevance source ("weight this less") rather than
dropping it, so an off-topic input can't silently bloat the comprehension while
an explicitly-added source is never silently discarded. This is the ingest-side
twin of the plan-boundary relevance filter (:func:`oxison.oxipensa_gate.filter_by_relevance`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..repomap import RepoMap
from .base import SourceAdapter, SourceResult, SourceUnit
from .docs import DocsAdapter
from .docx import DocxAdapter
from .ocr import OcrAdapter
from .pdf import PdfAdapter
from .pptx import PptxAdapter
from .recording import RecordingAdapter
from .web import WebAdapter

#: Distinct domain-term matches at which a source is "clearly on-topic"
#: (relevance saturates to 1.0). Small + forgiving on purpose: the domain
#: vocabulary a deterministic repo-map can extract (project name, top-level
#: layout, deps) is coarse — for a ``src/``-layout repo it's mostly scaffolding
#: filenames plus the product name — so the gate's job is only to catch a source
#: that shares *essentially none* of that vocabulary, not to rank on-topic ones.
RELEVANCE_FULL_AT = 4

#: A source scoring below this is *annotated* as low domain relevance. Set so
#: the gate fires only on ~zero vocabulary overlap (a single domain-term match
#: lands at ``1/4 = 0.25`` and is NOT flagged): deliberately abstain-safe, since
#: a false alarm on a genuinely-relevant source erodes trust in the hint, and
#: the renderer never drops by default anyway. A source that doesn't mention the
#: repo's name *or* any of its structure/deps is the off-topic case worth a flag.
LOW_RELEVANCE_ANNOTATE = 0.2

#: Generic tokens that carry no domain signal — stripped from both the repo
#: vocabulary and the source text before overlap so "src"/"test"/"json" don't
#: inflate relevance.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "src", "lib", "test", "tests", "main", "app", "index", "the", "and",
        "for", "with", "from", "this", "that", "are", "was", "use", "used",
        "json", "yaml", "toml", "md", "txt", "doc", "docs", "readme", "code",
        "file", "files", "dir", "core", "util", "utils", "config", "build",
        "python", "javascript", "typescript", "java", "shell", "markdown",
        "http", "https", "www", "com", "org",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class IngestOutput:
    results: list[SourceResult] = field(default_factory=list)

    @property
    def units(self) -> list[SourceUnit]:
        return [u for r in self.results for u in r.units]

    @property
    def unit_count(self) -> int:
        return len(self.units)


def _static_adapters() -> list[SourceAdapter]:
    # PDF text first; OCR is consulted separately (only when enabled).
    return [DocsAdapter(), PdfAdapter(), PptxAdapter(), DocxAdapter()]


#: Skip a source file larger than this before handing it to a parser
#: (SECURITY-AUDIT.md F7). The document parsers (pypdf/pptx/docx) run in the main
#: process and have a history of size-correlated DoS (zip bombs, quadratic alloc,
#: xref loops). A generous 64 MiB ceiling is far above any real doc a user feeds
#: in while bounding a malicious one before the parser touches it. This is a
#: pre-emptive guard, not a parse limit — a small-but-pathological file still
#: relies on the catch-all net below (a parse timeout is a deferred follow-up).
MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024


def _safe_extract(adapter: SourceAdapter, path: Path) -> SourceResult:
    """Run adapter.extract, converting any unexpected error into a skip.

    Honors the orchestrator contract: a bad input is recorded skipped,
    never raised. Adapters already return skips for *expected* conditions
    (missing dep, no key, needs_ocr); this is the net for *unexpected*
    errors (a missing --add file, the recording stub, an adapter bug).

    Oversized files are skipped *before* extraction (F7) so a malicious huge
    document can't DoS the in-process parser.
    """
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    if size is not None and size > MAX_SOURCE_FILE_BYTES:
        return SourceResult.skip(
            getattr(adapter, "name", "unknown"), str(path),
            reason=f"file too large: {size} bytes > {MAX_SOURCE_FILE_BYTES} cap",
        )
    try:
        return adapter.extract(path)
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all net at the orchestrator boundary
        return SourceResult.skip(
            getattr(adapter, "name", "unknown"), str(path),
            reason=f"extraction failed: {type(exc).__name__}: {exc}",
        )


def ingest_paths(
    paths: list[Path],
    *,
    ocr_enabled: bool,
    stt_key: str | None,
    stt_provider: str = "openai",
) -> IngestOutput:
    adapters = _static_adapters()
    ocr = OcrAdapter()
    recording = RecordingAdapter(stt_key=stt_key, stt_provider=stt_provider)
    out = IngestOutput()
    for path in paths:
        if recording.detect(path):
            out.results.append(_safe_extract(recording, path))
            continue
        handled = False
        for adapter in adapters:
            if adapter.detect(path):
                res = _safe_extract(adapter, path)
                # A text-PDF that needs OCR: retry via OCR only when enabled.
                if res.status == "skipped" and res.reason == "needs_ocr" and ocr_enabled:
                    res = _safe_extract(ocr, path)
                out.results.append(res)
                handled = True
                break
        if not handled:
            out.results.append(
                SourceResult.skip("unknown", str(path), reason="no adapter for this file type")
            )
    return out


def brief_unit(text: str) -> SourceUnit:
    """Wrap a plain-text project brief as a first-class, citable source unit."""
    return SourceUnit(
        text=text,
        source_type="brief",
        origin_path="(brief)",
        locator="brief:idea",
        metadata={},
    )


def ingest_urls(urls: list[str]) -> IngestOutput:
    """Fetch user-provided URLs via the web adapter, collecting a status ledger.

    Parallel to ``ingest_paths`` but for URLs (not file paths), so it does not
    touch the path-based adapter dispatch. A fetch error is recorded skipped,
    never raised — same orchestrator contract as ``ingest_paths``.
    """
    out = IngestOutput()
    if not urls:
        return out
    adapter = WebAdapter()
    avail = adapter.available()
    for url in urls:
        if not avail.available:
            out.results.append(
                SourceResult.skip("web", url, reason=avail.reason or "unavailable")
            )
            continue
        try:
            out.results.append(adapter.extract(url))
        except Exception as exc:  # noqa: BLE001 — orchestrator-boundary net (matches _safe_extract)
            out.results.append(
                SourceResult.skip("web", url, reason=f"fetch failed: {type(exc).__name__}: {exc}")
            )
    return out


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens (len >= 3, non-stopword) — the overlap alphabet."""
    return {
        t for t in _TOKEN_RE.findall(text.lower())
        if len(t) >= 3 and t not in _STOPWORDS
    }


def domain_terms_from_repomap(repo_map: RepoMap) -> frozenset[str]:
    """Derive the repo's domain vocabulary from its deterministic map.

    Pulls from the signals most indicative of *what the project is about*: its
    root/name, top-level layout, entry-point stems, service hints, and — the
    strongest signal — its dependency names. Greenfield builds (empty staging
    dir) yield few/no terms, which the renderer treats as "can't judge" (every
    source scores 1.0), so the gate is naturally inert without a real repo.
    """
    parts: list[str] = [Path(repo_map.root).name]
    parts.extend(repo_map.languages.keys())
    parts.extend(repo_map.tree)
    parts.extend(repo_map.entry_points)
    parts.extend(repo_map.services)
    for manifest in repo_map.manifests:
        parts.extend(manifest.dependencies)
    return frozenset(_tokens(" ".join(parts)))


def source_relevance(text: str, domain_terms: frozenset[str]) -> float:
    """Coarse lexical relevance of a source to the repo domain, in ``[0, 1]``.

    ``= min(1, distinct_domain_terms_mentioned / RELEVANCE_FULL_AT)``. With no
    domain terms (greenfield), returns ``1.0`` — "can't judge, don't penalize",
    the same default-1.0 convention the plan-gate uses for an absent relevance.
    Deliberately forgiving: it flags the source that mentions almost none of the
    repo's vocabulary, not the merely-secondary one.
    """
    if not domain_terms:
        return 1.0
    overlap = len(domain_terms & _tokens(text))
    return min(1.0, overlap / RELEVANCE_FULL_AT)


def render_extra_context(
    units: list[SourceUnit],
    *,
    domain_terms: frozenset[str] | None = None,
    min_score: float = 0.0,
) -> str:
    """Render collected source units into the prompt's extra-context block.

    ``domain_terms`` (when provided — repo mode) turns on the abstain-safe
    relevance gate: each unit is scored by :func:`source_relevance`, a
    below-``LOW_RELEVANCE_ANNOTATE`` unit gets a visible "low domain relevance"
    marker on its header (so the comprehension worker down-weights it), and a
    unit below ``min_score`` is dropped entirely. ``min_score`` defaults to
    ``0.0`` (annotate-only, never drop) so an explicitly-added source is never
    silently discarded. ``domain_terms=None`` (the default) reproduces the
    original behavior exactly — greenfield and old callers are unaffected.
    """
    if not units:
        return ""
    lines = [
        "=== ADDITIONAL SOURCES ===",
        "(extracted by oxison from non-repo inputs; cite by locator)",
    ]
    for u in units:
        if domain_terms is None:
            lines.append(f"\n--- [{u.locator}] ---\n{u.text}")
            continue
        score = source_relevance(u.text, domain_terms)
        if min_score > 0.0 and score < min_score:
            continue  # opt-in drop of a clearly off-topic source
        marker = (
            f"  ⚠ low domain relevance ({score:.2f}) — weight accordingly"
            if score < LOW_RELEVANCE_ANNOTATE
            else ""
        )
        lines.append(f"\n--- [{u.locator}]{marker} ---\n{u.text}")
    lines.append("\n=== END ADDITIONAL SOURCES ===")
    return "\n".join(lines)
