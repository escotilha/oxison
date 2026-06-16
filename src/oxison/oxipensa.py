"""Oxipensa — the planner stage. comprehension.json -> roadmap.json.

Consumes Oxicome's ``comprehension.json`` (the frozen schema-1.0 contract) and
drives a read-only ``claude -p`` worker to reason over the project's structured
state + prose comprehension, returning a prioritized, gated roadmap.

The stage is a **self-correcting loop** (a propose -> plan_gate -> revise
cycle): the planner proposes a roadmap, the
deterministic plan-gate validates it, and on rejection the gate's violations
are fed back into a single corrective pass. A roadmap that still fails the gate
is never written — oxison surfaces a hard error rather than emit a broken
contract. oxison owns every write; the worker only reads and returns JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import READ_ONLY_TOOLS, RunConfig
from .dispatch import invoke
from .jsonutil import JsonExtractError, extract_json_object
from .oxipensa_gate import (
    DEFAULT_MAX_TASKS,
    DEFAULT_RELEVANCE_MIN_SCORE,
    filter_by_relevance,
    gate_roadmap,
)
from .prompts import roadmap_plan_prompt
from .roadmap_doc import RoadmapDoc, RoadmapTask, build_roadmap_doc

#: Per-attempt wall-clock timeout. Planning reads a comprehension and reasons;
#: 15 min is generous for one pass.
OXIPENSA_TIMEOUT_S = 900.0

#: Planner attempts: one proposal + one gate-feedback correction.
MAX_ATTEMPTS = 2

ROADMAP_JSON_FILENAME = "roadmap.json"
ROADMAP_MD_FILENAME = "ROADMAP.md"
COMPREHENSION_JSON_FILENAME = "comprehension.json"


class PlanError(RuntimeError):
    """Planning failed — bad worker, unparseable output, or gate rejection."""


@dataclass
class PlanResult:
    """A gated roadmap plus the cost and attempt count it took to produce."""

    doc: RoadmapDoc
    cost_usd: float
    attempts: int
    #: Tasks dropped by the plan-boundary relevance filter (below the floor and
    #: not transitively needed by a kept task). Empty when nothing was pruned.
    #: The CLI/pipeline surface this so a dropped task is visible, not silent.
    pruned: list[RoadmapTask] = field(default_factory=list)


def load_comprehension(path: Path) -> dict[str, Any]:
    """Load a ``comprehension.json`` from a file or a directory containing one.

    Raises :class:`PlanError` (not a bare ``OSError``/``JSONDecodeError``) so
    the CLI prints one actionable message.
    """
    target = path
    if target.is_dir():
        target = target / COMPREHENSION_JSON_FILENAME
    if not target.is_file():
        raise PlanError(f"no comprehension.json found at {path}")
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"could not read {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError(f"{target} is not a JSON object")
    if "comprehension_markdown" not in data and "comprehension" not in data:
        raise PlanError(
            f"{target} does not look like an Oxicome comprehension.json "
            "(missing comprehension_markdown)"
        )
    # Pin the contract major version — a future schema 2.x must fail loudly at
    # the seam rather than be silently misread as 1.x downstream.
    major = str(data.get("schema_version", "")).split(".")[0]
    if major and major != "1":
        raise PlanError(
            f"unsupported comprehension.json schema_version "
            f"{data.get('schema_version')!r} (this oxison supports 1.x)"
        )
    return data


def _first_heading(markdown: str) -> str:
    """The text of the first top-level (``# ``) markdown heading, or ""."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _product_name(comprehension: dict[str, Any]) -> str:
    """The product name: the comprehension's ``product.what`` if present, else
    the first H1 of the prose comprehension.

    Oxicome v1 emits an empty ``product`` (only ``comprehension_markdown`` is
    populated), so the heading fallback keeps Oxipensa useful on today's real
    comprehension.json — not just the future structured shape.
    """
    product = comprehension.get("product")
    if isinstance(product, dict):
        what = product.get("what")
        if isinstance(what, str) and what.strip():
            return what.strip()
    markdown = comprehension.get("comprehension_markdown") or comprehension.get(
        "comprehension", ""
    )
    return _first_heading(markdown) if isinstance(markdown, str) else ""


def _structured_state(comprehension: dict[str, Any]) -> str:
    return json.dumps(
        {
            "product": comprehension.get("product", {}),
            "state": comprehension.get("state", {}),
            "stack": comprehension.get("stack", {}),
            "sources": comprehension.get("sources", []),
        },
        indent=2,
        ensure_ascii=False,
    )


def _open_questions_list(comprehension: dict[str, Any]) -> list[str]:
    raw = comprehension.get("open_questions", [])
    if not isinstance(raw, list):
        return []
    return [q.strip() for q in raw if isinstance(q, str) and q.strip()]


def _open_questions(comprehension: dict[str, Any]) -> str:
    items = _open_questions_list(comprehension)
    return "\n".join(f"- {q}" for q in items) if items else "(none stated)"


def _merge_open_questions(planner: list[str], carried: list[str]) -> list[str]:
    """Union the planner's open questions with the comprehension's, planner
    first, deduped case-insensitively — so a stated open question (the hook for
    the solicit-input path) is never silently dropped by the planner.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for q in [*planner, *carried]:
        key = " ".join(q.lower().split())
        if key and key not in seen:
            seen.add(key)
            merged.append(q)
    return merged


def _source_provenance(comprehension: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": comprehension.get("schema_version", ""),
        "generated_at": comprehension.get("generated_at", ""),
        "product_what": _product_name(comprehension),
    }


async def plan(
    cfg: RunConfig,
    comprehension: dict[str, Any],
    *,
    generated_at: str,
    user_guidance: str = "",
    max_tasks: int = DEFAULT_MAX_TASKS,
    relevance_min_score: float = DEFAULT_RELEVANCE_MIN_SCORE,
    greenfield: bool = False,
) -> PlanResult:
    """Produce a gated roadmap from a comprehension, self-correcting once.

    ``generated_at`` is stamped at the CLI boundary and threaded in (oxison
    never calls ``datetime.now()`` inside a library function). ``greenfield``
    reframes the plan as an initial from-scratch build (Oxideia).

    ``relevance_min_score`` is the plan-boundary relevance floor: tasks the
    planner self-scored below it (and not transitively needed by a kept task)
    are pruned before the gate runs, so speculative gold-plating never becomes a
    build contract. ``<= 0`` opts out (keep every task). The pruned tasks ride
    back on :attr:`PlanResult.pruned` so the caller can surface them.
    """
    markdown = comprehension.get("comprehension_markdown") or comprehension.get(
        "comprehension", ""
    )
    structured = _structured_state(comprehension)
    questions = _open_questions(comprehension)
    name = _product_name(comprehension)
    source = _source_provenance(comprehension)

    total_cost = 0.0
    prior_errors = ""
    last_problem = "unknown"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = roadmap_plan_prompt(
            product_name=name,
            comprehension_markdown=str(markdown),
            structured_state=structured,
            open_questions=questions,
            user_guidance=user_guidance,
            prior_errors=prior_errors,
            greenfield=greenfield,
        )
        result = await invoke(
            prompt,
            cfg=cfg,
            allowed_tools=READ_ONLY_TOOLS,
            cwd=cfg.target,
            timeout_s=OXIPENSA_TIMEOUT_S,
        )
        total_cost += result.cost_usd
        if not result.ok:
            # A process-level failure (timeout, non-zero exit) won't be fixed by
            # re-prompting — surface it.
            raise PlanError(f"planner worker failed: {result.error or 'unknown error'}")

        try:
            raw = extract_json_object(result.text)
        except JsonExtractError as exc:
            last_problem = f"output was not valid JSON: {exc}"
            prior_errors = (
                f"Your previous output was not valid JSON ({exc}). "
                "Return ONLY a single JSON object, no prose, no code fence."
            )
            continue

        doc = build_roadmap_doc(raw=raw, source=source, generated_at=generated_at)
        # Prune clearly off-target tasks BEFORE the gate. Transitive-keep means
        # the survivor set is dependency-closed, so this never hands the gate a
        # dangling dep — the filtered doc is always at least as gate-acceptable
        # as the unfiltered one.
        doc, pruned = filter_by_relevance(doc, min_score=relevance_min_score)
        gate = gate_roadmap(doc, max_tasks=max_tasks)
        if gate.ok:
            doc.open_questions = _merge_open_questions(
                doc.open_questions, _open_questions_list(comprehension)
            )
            return PlanResult(
                doc=doc, cost_usd=total_cost, attempts=attempt, pruned=pruned
            )
        last_problem = "; ".join(gate.violations)
        prior_errors = gate.feedback()

    raise PlanError(
        f"roadmap failed the plan-gate after {MAX_ATTEMPTS} attempts. "
        f"Last problem: {last_problem}"
    )


__all__ = [
    "COMPREHENSION_JSON_FILENAME",
    "MAX_ATTEMPTS",
    "OXIPENSA_TIMEOUT_S",
    "ROADMAP_JSON_FILENAME",
    "ROADMAP_MD_FILENAME",
    "PlanError",
    "PlanResult",
    "load_comprehension",
    "plan",
]
