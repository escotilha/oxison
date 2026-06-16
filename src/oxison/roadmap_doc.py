"""roadmap.json — the Oxipensa->Oxfaz contract (schema 1.0).

Oxipensa consumes Oxicome's ``comprehension.json`` and emits this: a
deterministically-keyed, lockable, provenance-tagged task list that Oxfaz
(the autonomous builder) consumes. The envelope is schema-versioned so Oxfaz
can pin a version and the contract can evolve without breaking the consumer.

Two deliberate design choices make the contract robust:

* **oxison assigns the identifier, not the model.** ``identifier`` is a
  deterministic function of ``(kind, normalized title)`` — so re-running
  Oxipensa over the same comprehension yields the same keys, and Oxfaz dedups
  by ``identifier`` (the build-engine taskstore's UNIQUE constraint).
* **dependencies are authored by title, resolved to identifiers here.** The
  model references prerequisites by their human-readable title (which it can
  produce reliably); :func:`build_roadmap_doc` rewrites those titles to the
  computed identifiers. An unresolvable title is left as-is so the plan-gate
  flags it as a dangling dependency rather than silently dropping it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

#: The task kinds Oxfaz understands. The gate rejects anything else.
ALLOWED_KINDS: tuple[str, ...] = (
    "feature",
    "fix",
    "chore",
    "docs",
    "infra",
    "refactor",
    "test",
)

#: Coarse effort buckets. The gate normalizes unknown values to "M".
ALLOWED_EFFORT: tuple[str, ...] = ("S", "M", "L")

_ID_PREFIX = "oxpz-"


def _normalize_title(title: str) -> str:
    return " ".join(title.lower().split())


def deterministic_identifier(kind: str, title: str) -> str:
    """A stable ``oxpz-<hash>`` key derived from ``(kind, title)``.

    Stable across runs and independent of task ordering, so Oxfaz can dedup a
    re-planned roadmap by identifier.
    """
    basis = f"{kind.strip().lower()}|{_normalize_title(title)}"
    # Not a security hash — just a stable content key for deduplication.
    digest = hashlib.sha1(basis.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{_ID_PREFIX}{digest}"


@dataclass
class RoadmapTask:
    """One buildable unit of work, with provenance and acceptance criteria."""

    identifier: str
    title: str
    kind: str
    priority: int
    rationale: str
    evidence: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    estimated_effort: str = "M"
    files_hint: list[str] = field(default_factory=list)
    #: How directly this task serves the product's stated core intent, in
    #: ``[0, 1]`` — the planner's self-assessment (1.0 = core; low = speculative
    #: / gold-plating). The plan-boundary relevance filter
    #: (:func:`oxison.oxipensa_gate.filter_by_relevance`) prunes tasks below a
    #: floor so a roadmap doesn't carry speculative work into the build. Absent
    #: in the model output -> defaults to ``1.0`` (don't penalize, like the
    #: memory subsystem's "unknown recency -> 1.0" rule), so an older planner
    #: that doesn't emit it is unaffected.
    relevance: float = 1.0


@dataclass
class RoadmapDoc:
    """The full roadmap.json envelope (schema 1.0)."""

    schema_version: str
    generated_at: str
    source: dict[str, Any]
    summary: str
    open_questions: list[str]
    tasks: list[RoadmapTask]

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "generated_at": self.generated_at,
                "source": self.source,
                "summary": self.summary,
                "open_questions": self.open_questions,
                "tasks": [asdict(t) for t in self.tasks],
            },
            indent=2,
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Defensive coercion of raw model JSON -> typed dataclasses. The gate enforces
# *semantics* (non-empty title, valid kind, acceptance present, ...); these
# helpers only normalize *shape* so a missing/typo'd field becomes a clean
# empty value the gate can then report on, never a crash.
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):  # bool is an int subclass — treat as missing
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _as_float_01(value: Any, default: float) -> float:
    """Coerce to a float clamped to ``[0, 1]``; missing/invalid -> ``default``.

    A defensive shape-normalizer (same role as :func:`_as_int`): a typo'd or
    out-of-range ``relevance`` becomes a clean in-range value the relevance
    filter can act on, never a crash.
    """
    if isinstance(value, bool):  # bool is an int subclass — treat as missing
        return default
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return default
    else:
        return default
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _coerce_task(raw: Any) -> RoadmapTask | None:
    """Coerce one raw task dict into a ``RoadmapTask`` (identifier computed).

    ``depends_on`` is captured as authored titles here; titles are resolved to
    identifiers in :func:`build_roadmap_doc` once every task is known.
    """
    if not isinstance(raw, dict):
        return None
    title = _as_str(raw.get("title"))
    kind = _as_str(raw.get("kind")).lower()
    effort = _as_str(raw.get("estimated_effort")).upper() or "M"
    if effort not in ALLOWED_EFFORT:
        effort = "M"
    # identifier is computed from (kind, title); an empty title still produces
    # a deterministic-but-degenerate id, which the gate then rejects on title.
    return RoadmapTask(
        identifier=deterministic_identifier(kind, title),
        title=title,
        kind=kind,
        priority=_as_int(raw.get("priority"), 3),
        rationale=_as_str(raw.get("rationale")),
        evidence=_as_str_list(raw.get("evidence")),
        acceptance=_as_str_list(raw.get("acceptance")),
        depends_on=_as_str_list(raw.get("depends_on")),  # titles, for now
        estimated_effort=effort,
        files_hint=_as_str_list(raw.get("files_hint")),
        relevance=_as_float_01(raw.get("relevance"), 1.0),
    )


def build_roadmap_doc(
    *,
    raw: dict[str, Any],
    source: dict[str, Any],
    generated_at: str,
) -> RoadmapDoc:
    """Build a typed ``RoadmapDoc`` from raw model JSON.

    Computes deterministic identifiers and resolves ``depends_on`` titles to
    those identifiers. Output is shape-valid; semantic validation is the
    plan-gate's job (:mod:`oxison.oxipensa_gate`).
    """
    raw_tasks = raw.get("tasks")
    coerced = [
        t for t in (_coerce_task(rt) for rt in raw_tasks) if t is not None
    ] if isinstance(raw_tasks, list) else []

    # Map normalized title -> identifier for dependency resolution. On a title
    # collision the later task wins the map; the gate's duplicate-identifier
    # check surfaces the collision either way.
    title_to_id = {_normalize_title(t.title): t.identifier for t in coerced if t.title}
    for task in coerced:
        task.depends_on = [
            title_to_id.get(_normalize_title(dep), dep) for dep in task.depends_on
        ]

    return RoadmapDoc(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        source=source,
        summary=_as_str(raw.get("summary")),
        open_questions=_as_str_list(raw.get("open_questions")),
        tasks=coerced,
    )


def render_roadmap_md(doc: RoadmapDoc) -> str:
    """Render a deterministic ROADMAP.md from the roadmap.json envelope.

    Tasks are ordered by ``(priority, identifier)`` so the markdown is a stable
    function of the JSON — prose can never drift from the structured contract.
    """
    lines: list[str] = ["# Roadmap", ""]
    src_what = _as_str(doc.source.get("product_what"))
    src_gen = _as_str(doc.source.get("generated_at"))
    meta = "Generated by oxison · Oxipensa"
    if src_gen:
        meta += f" · from comprehension.json ({src_gen})"
    lines.append(f"> {meta}")
    if src_what:
        lines.append(">")
        lines.append(f"> **Product:** {src_what}")
    lines.append("")
    if doc.summary:
        lines.extend([doc.summary, ""])

    if doc.open_questions:
        lines.append("## Open questions")
        lines.append("")
        lines.extend(f"- {q}" for q in doc.open_questions)
        lines.append("")

    # depends_on holds identifiers (the machine contract); render them as the
    # human-readable task titles so ROADMAP.md is legible.
    id_to_title = {t.identifier: t.title for t in doc.tasks}
    ordered = sorted(doc.tasks, key=lambda t: (t.priority, t.identifier))
    lines.append(f"## Tasks ({len(ordered)})")
    lines.append("")
    for idx, task in enumerate(ordered, start=1):
        header = (
            f"### {idx}. {task.title or '(untitled)'}  "
            f"`{task.identifier}`"
        )
        lines.append(header)
        lines.append("")
        tags = f"priority {task.priority} · {task.kind or '?'} · effort {task.estimated_effort}"
        # Surface a below-default relevance so a kept-but-marginal task is
        # visible in the human roadmap (default 1.0 renders unchanged).
        if task.relevance < 1.0:
            tags += f" · relevance {task.relevance:.2f}"
        lines.append(f"_{tags}_")
        lines.append("")
        if task.rationale:
            lines.extend([task.rationale, ""])
        if task.acceptance:
            lines.append("**Acceptance:**")
            lines.append("")
            lines.extend(f"- {a}" for a in task.acceptance)
            lines.append("")
        if task.evidence:
            lines.append(f"**Evidence:** {', '.join(task.evidence)}")
            lines.append("")
        if task.depends_on:
            dep_titles = [id_to_title.get(d, d) for d in task.depends_on]
            lines.append(f"**Depends on:** {', '.join(dep_titles)}")
            lines.append("")
        if task.files_hint:
            lines.append(f"**Files (hint):** {', '.join(task.files_hint)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "ALLOWED_EFFORT",
    "ALLOWED_KINDS",
    "SCHEMA_VERSION",
    "RoadmapDoc",
    "RoadmapTask",
    "build_roadmap_doc",
    "deterministic_identifier",
    "render_roadmap_md",
]
