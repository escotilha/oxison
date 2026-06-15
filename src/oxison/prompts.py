"""Prompt builders — identity-baked, read-only by construction.

Every prompt opens with the same identity block: a positive statement
of what the worker is (a read-only analyst returning markdown) and an
explicit negative statement of what it must never do (modify files,
write anything). Per skill-authoring practice, the negative callout is
what actually prevents scope drift — positive guidance alone gets
overridden.

These are pure functions returning strings; no I/O, trivially testable.
Phase 1 covers comprehension (single-pass, per-slice, synthesis).
Phase 2 extends this module with the artifact prompts.
"""

from __future__ import annotations

IDENTITY = (
    "You are oxison's read-only repository analyst.\n"
    "Target repository: {root}\n"
    "\n"
    "HARD CONSTRAINTS (these override any instinct to be helpful by editing):\n"
    "- You have READ-ONLY tools only (Read, Glob, Grep). You have NO shell and "
    "NO write tools, so you cannot modify, create, move, delete, or execute "
    "anything — read files, glob the tree, and grep for patterns to understand "
    "the code.\n"
    "- Your entire job is to READ and UNDERSTAND, then RETURN markdown text. "
    "oxison collects your returned text and writes the files itself — you never "
    "write output anywhere.\n"
)


def _identity(root: str) -> str:
    return IDENTITY.format(root=root)


def _extra_block(extra_context: str) -> str:
    """Render the additional-sources block, or empty string if none."""
    if not extra_context.strip():
        return ""
    return f"\n{extra_context}\n"


def single_pass_prompt(*, root: str, repo_map_context: str, extra_context: str = "") -> str:
    """Comprehend the whole repo in one pass (small repos)."""
    return (
        f"{_identity(root)}\n"
        "Below is a deterministic map of the repository (languages, "
        "dependencies, entry points, structure). Use it as your starting "
        "point, then read the actual code to verify and deepen your "
        "understanding.\n\n"
        f"=== REPOSITORY MAP ===\n{repo_map_context}\n=== END MAP ===\n"
        f"{_extra_block(extra_context)}"
        "\n"
        "Produce a thorough comprehension of this codebase. Cover:\n"
        "1. What the software does (its purpose and core capabilities).\n"
        "2. Who it is for (intended users / audience).\n"
        "3. Architecture: main components, how they fit together, data flow.\n"
        "4. Key modules/files and their responsibilities.\n"
        "5. External dependencies and services it relies on.\n"
        "6. How it is run / entry points.\n\n"
        "Read the most important files before answering. Be concrete and cite "
        "file paths. Return your comprehension as structured markdown."
    )


def slice_prompt(
    *, root: str, repo_map_context: str, slice_dir: str, extra_context: str = ""
) -> str:
    """Comprehend one top-level directory (map-reduce, large repos)."""
    return (
        f"{_identity(root)}\n"
        "This is a LARGE repository being analyzed in slices. Focus ONLY on "
        f"the top-level directory: ./{slice_dir}/\n\n"
        "Repository-wide map for context (do not analyze the whole repo — only "
        f"your slice):\n\n=== REPOSITORY MAP ===\n{repo_map_context}\n=== END MAP ===\n"
        f"{_extra_block(extra_context)}"
        "\n"
        f"Read the important files under ./{slice_dir}/ and summarize:\n"
        "1. What this part of the codebase is responsible for.\n"
        "2. Its key modules/files and what they do.\n"
        "3. How it connects to the rest of the system (imports, APIs, data).\n"
        "4. Any external dependencies or services used here.\n\n"
        f"Cite file paths under ./{slice_dir}/. Return structured markdown."
    )


def synthesis_prompt(
    *, root: str, repo_map_context: str, slice_summaries: str, extra_context: str = ""
) -> str:
    """Merge per-slice summaries into a whole-repo comprehension."""
    return (
        f"{_identity(root)}\n"
        "Per-directory summaries of this repository have already been produced "
        "(below). Synthesize them — plus the repository map — into a single "
        "coherent understanding of the whole system. You may read a few "
        "cross-cutting files (entry points, top-level config) to connect the "
        "pieces, but do not re-analyze every directory.\n\n"
        f"=== REPOSITORY MAP ===\n{repo_map_context}\n=== END MAP ===\n"
        f"{_extra_block(extra_context)}"
        "\n"
        f"=== PER-DIRECTORY SUMMARIES ===\n{slice_summaries}\n=== END SUMMARIES ===\n\n"
        "Produce a unified comprehension covering: what the software does, who "
        "it is for, overall architecture and data flow, how the major parts "
        "interact, external dependencies/services, and how it is run. Return "
        "structured markdown."
    )


__all__ = [
    "IDENTITY",
    "GREENFIELD_IDENTITY",
    "single_pass_prompt",
    "slice_prompt",
    "synthesis_prompt",
    "greenfield_comprehension_prompt",
    "product_prompt",
    "greenfield_product_prompt",
    "manual_prompt",
    "stack_prompt",
    "roadmap_analysis_prompt",
    "security_prompt",
    "roadmap_plan_prompt",
]


# ---------------------------------------------------------------------------
# Greenfield (Oxideia) — start from a brief + non-repo sources, NO codebase.
# These mirror the repo prompts but omit the repository map and reframe the
# task as "the product to be BUILT" rather than "what the code does".
# ---------------------------------------------------------------------------

GREENFIELD_IDENTITY = (
    "You are oxison's product analyst working in GREENFIELD mode.\n"
    "\n"
    "There is NO existing codebase. You are working purely from a written brief\n"
    "and the supporting source materials provided below (decks, documents,\n"
    "recordings, fetched web pages). Reason ONLY from that provided context.\n"
    "\n"
    "HARD CONSTRAINTS:\n"
    "- Do NOT attempt to read files, glob, or grep — there is no repository to\n"
    "  inspect; everything you have is in the context below.\n"
    "- Your job is to THINK and RETURN markdown text. oxison writes the files\n"
    "  itself — you never write output anywhere.\n"
    "- Describe the product to be BUILT. Never claim anything is already\n"
    "  implemented or 'built' — nothing exists yet.\n"
)


def greenfield_comprehension_prompt(*, extra_context: str = "") -> str:
    """Synthesize a brief + non-repo sources into a product understanding."""
    return (
        f"{GREENFIELD_IDENTITY}\n"
        "Synthesize the brief and supporting sources below into a clear,\n"
        "structured understanding of the PROPOSED product (nothing is built yet)."
        f"{_extra_block(extra_context)}"
        "\n"
        "Cover:\n"
        "1. What the product is meant to be (the vision, in a few sentences).\n"
        "2. The problem it solves and who it is for (target users).\n"
        "3. Core capabilities implied by the brief and sources.\n"
        "4. Scope and explicit non-goals, where the inputs suggest them.\n"
        "5. Constraints and assumptions (tech, platform, integrations) if stated.\n"
        "6. Open questions — gaps the brief leaves that a builder would need "
        "resolved.\n\n"
        "Cite sources by their locator (e.g. brief:idea, web:host, "
        "pptx:deck.pptx#slide-4). Be concrete; do not invent requirements the "
        "inputs don't support. Return your understanding as structured markdown."
    )


def greenfield_product_prompt(*, comprehension: str, extra_context: str = "") -> str:
    """PRODUCT.md for a product to be built (no existing code)."""
    return (
        f"{GREENFIELD_IDENTITY}\n"
        "Using the comprehension and sources below, write a PRODUCT document for "
        "the product to be built — a product vision/spec a stakeholder reads "
        "first.\n\n"
        f"=== COMPREHENSION ===\n{comprehension}\n=== END COMPREHENSION ===\n"
        f"{_extra_block(extra_context)}"
        "\n"
        "Cover, with clear markdown sections:\n"
        "- Overview: what the product is, in two or three sentences.\n"
        "- Problem it solves and who it is for (target users / use cases).\n"
        "- Proposed core features and capabilities.\n"
        "- The intended user-facing mental model / UX.\n"
        "- Explicit non-goals and key assumptions.\n\n"
        "Frame everything as the product to BUILD, not as existing software. Do "
        "not invent requirements the inputs don't support; note open questions. "
        f"{_RETURN_BODY}"
    )


# ---------------------------------------------------------------------------
# Phase 2 — artifact prompts. Each returns ONLY the markdown body of one
# document; oxison writes the file itself.
# ---------------------------------------------------------------------------

_RETURN_BODY = (
    "Return ONLY the markdown body of the document — no preamble, no "
    "'Here is...', no surrounding commentary, no code fences around the "
    "whole thing. Start directly with a top-level '# ' heading."
)


def product_prompt(
    *, root: str, comprehension: str, repo_map_context: str, extra_context: str = ""
) -> str:
    """PRODUCT.md — what it is, who it's for, what it does."""
    return (
        f"{_identity(root)}\n"
        "Using the comprehension and map below, write a PRODUCT document for "
        "this repository — the kind of overview a new stakeholder reads first.\n\n"
        f"=== COMPREHENSION ===\n{comprehension}\n=== END COMPREHENSION ===\n\n"
        f"=== REPOSITORY MAP ===\n{repo_map_context}\n=== END MAP ===\n"
        f"{_extra_block(extra_context)}"
        "\n"
        "Cover, with clear markdown sections:\n"
        "- Overview: what the product is, in two or three sentences.\n"
        "- Problem it solves and who it is for (target users / use cases).\n"
        "- Core features and capabilities.\n"
        "- How it works at a high level (the user-facing mental model).\n"
        "- Notable design decisions or constraints, if evident.\n\n"
        "Be accurate to the actual code — do not invent features that aren't "
        f"there. If something is unclear, say so. {_RETURN_BODY}"
    )


def manual_prompt(
    *, root: str, comprehension: str, repo_map_context: str, extra_context: str = ""
) -> str:
    """MANUAL.md — install, run, use."""
    return (
        f"{_identity(root)}\n"
        "Using the comprehension and map below, write a USER MANUAL for this "
        "repository — practical, task-oriented how-to documentation.\n\n"
        f"=== COMPREHENSION ===\n{comprehension}\n=== END COMPREHENSION ===\n\n"
        f"=== REPOSITORY MAP ===\n{repo_map_context}\n=== END MAP ===\n"
        f"{_extra_block(extra_context)}"
        "\n"
        "Cover, with clear markdown sections:\n"
        "- Prerequisites (language/runtime versions, accounts, services).\n"
        "- Installation (the actual commands, derived from the manifests).\n"
        "- Configuration (env vars / config files the code actually reads).\n"
        "- Usage: the main commands/entry points with concrete examples.\n"
        "- Common workflows or recipes, if the code supports them.\n"
        "- Troubleshooting notes, if any are evident.\n\n"
        "Ground every command in what the repo actually defines (entry points, "
        f"scripts, manifests). Do not invent flags or commands. {_RETURN_BODY}"
    )


def stack_prompt(
    *, root: str, comprehension: str, repo_map_context: str, extra_context: str = ""
) -> str:
    """STACK.md — languages, deps+versions, infra, services.

    The repo map carries the authoritative dependency list; the prompt
    instructs the model to ground the stack doc in it rather than guess.
    """
    return (
        f"{_identity(root)}\n"
        "Using the comprehension and the deterministic map below, write a "
        "TECH STACK document for this repository.\n\n"
        f"=== COMPREHENSION ===\n{comprehension}\n=== END COMPREHENSION ===\n\n"
        f"=== REPOSITORY MAP (authoritative for dependencies) ===\n"
        f"{repo_map_context}\n=== END MAP ===\n"
        f"{_extra_block(extra_context)}"
        "\n"
        "Cover, with clear markdown sections:\n"
        "- Languages and their roles (use the language histogram from the map).\n"
        "- Frameworks and key libraries (derive from the dependency manifests "
        "in the map — these are authoritative; do not invent dependencies "
        "that aren't listed).\n"
        "- Runtime / build tooling.\n"
        "- Infrastructure and external services (Docker, CI, databases, APIs) "
        "from the services hints and code.\n"
        "- Versions where the manifests specify them.\n\n"
        "The dependency manifests in the map are the source of truth — if a "
        "library isn't in the map or the code, do not list it. "
        f"{_RETURN_BODY}"
    )


# ---------------------------------------------------------------------------
# Phase 3 — roadmap-or-security branch.
# ---------------------------------------------------------------------------


def roadmap_analysis_prompt(
    *,
    root: str,
    comprehension: str,
    roadmap_text: str,
    roadmap_path: str,
    structured_items: str | None = None,
) -> str:
    """ROADMAP-ANALYSIS.md — analyze the repo's existing roadmap.

    ``structured_items`` is an optional pre-parsed item list (oxi-core
    enrichment) included as extra structure when available.
    """
    enrichment = (
        f"\n=== STRUCTURED ITEMS (pre-parsed) ===\n{structured_items}\n"
        "=== END STRUCTURED ITEMS ===\n"
        if structured_items
        else ""
    )
    return (
        f"{_identity(root)}\n"
        f"This repository has a roadmap at `{roadmap_path}`. Using your "
        "understanding of the codebase (comprehension below) and the roadmap "
        "contents, analyze the roadmap.\n\n"
        f"=== COMPREHENSION ===\n{comprehension}\n=== END COMPREHENSION ===\n\n"
        f"=== ROADMAP ({roadmap_path}) ===\n{roadmap_text}\n=== END ROADMAP ==="
        f"{enrichment}\n\n"
        "Produce a ROADMAP ANALYSIS covering, with clear markdown sections:\n"
        "- Summary: what the roadmap plans, in a few sentences.\n"
        "- Planned items/themes, grouped sensibly (by priority/tier/area).\n"
        "- Feasibility: for the major items, how they map onto the existing "
        "code — what already exists vs. what's greenfield.\n"
        "- Dependencies and suggested sequencing between items.\n"
        "- Risks, gaps, or ambiguities in the roadmap.\n"
        "- A recommended next 1–3 items to tackle, with rationale.\n\n"
        "Ground your analysis in the actual code where relevant (cite paths). "
        f"{_RETURN_BODY}"
    )


def security_prompt(*, root: str, comprehension: str, repo_map_context: str) -> str:
    """SECURITY-NOTES.md — lightweight, read-only security surface scan."""
    return (
        f"{_identity(root)}\n"
        "This repository has no roadmap, so perform a LIGHTWEIGHT security "
        "review of the codebase. This is a best-effort surface scan, NOT a "
        "full SAST audit — be clear about that limitation in your output.\n\n"
        f"=== COMPREHENSION ===\n{comprehension}\n=== END COMPREHENSION ===\n\n"
        f"=== REPOSITORY MAP ===\n{repo_map_context}\n=== END MAP ===\n\n"
        "Read the security-relevant files and report, with clear markdown "
        "sections and a severity tag (Low/Medium/High) on each finding:\n"
        "- Secrets / credentials committed or hardcoded (scan configs, .env, "
        "source).\n"
        "- Obvious misconfigurations (permissive CORS, debug enabled, exposed "
        "endpoints, missing auth).\n"
        "- Dependency risk surface (notably outdated or known-risky libraries "
        "from the manifests).\n"
        "- Input-handling risks (injection, unsafe deserialization, command "
        "execution) visible in the code.\n\n"
        "Begin with a one-line scope disclaimer that this is a lightweight, "
        "non-exhaustive scan. If you find nothing notable, say so plainly "
        "rather than inventing issues. End with a short note recommending the "
        "user add a roadmap so oxison can analyze planned work instead. "
        f"{_RETURN_BODY}"
    )


# ---------------------------------------------------------------------------
# Oxipensa — the planner. Consumes Oxicome's comprehension.json and returns a
# single JSON roadmap object (the Oxipensa->Oxfaz contract). Unlike the
# read-from-repo prompts above, this reasons over the *structured
# comprehension* and returns JSON, not markdown.
# ---------------------------------------------------------------------------

_PLANNER_IDENTITY = (
    "You are Oxipensa, oxison's read-only planning analyst.\n"
    "\n"
    "Your job: turn a structured comprehension of a software project (what it\n"
    "is and where it's at) into a prioritized, buildable roadmap that an\n"
    "autonomous build engine (Oxfaz) will execute task by task.\n"
    "\n"
    "HARD CONSTRAINTS (these override any instinct to be helpful by editing):\n"
    "- You have READ-ONLY tools only (Read, Glob, Grep) — no shell, no write\n"
    "  tools. You cannot modify, create, move, delete, or execute anything; you\n"
    "  only think and RETURN JSON.\n"
    "- Ground every task in the comprehension below. Do NOT invent work that the\n"
    "  comprehension gives no evidence for. Prefer closing the gap between what\n"
    "  the project PROMISES (decks/specs/planned state) and what it has BUILT.\n"
)

_PLANNER_JSON_CONTRACT = (
    "Return ONLY a single JSON object (no prose, no code fence) of this shape:\n"
    "{\n"
    '  "summary": "one-paragraph thesis: what this roadmap delivers and why",\n'
    '  "open_questions": ["unresolved questions that block confident planning"],\n'
    '  "tasks": [\n'
    "    {\n"
    '      "title": "specific, imperative (e.g. \\"Add JWT refresh-token rotation\\")",\n'
    '      "kind": "one of: feature | fix | chore | docs | infra | refactor | test",\n'
    '      "priority": 1,                       // 1 = do first; higher = later\n'
    '      "rationale": "why this task, why now",\n'
    '      "evidence": ["provenance locators from the comprehension, e.g. '
    'git:src/x.py, pptx:deck.pptx#slide-4"],\n'
    '      "acceptance": ["at least one OBSERVABLE, checkable end-state — '
    'phrase as a condition a fresh process could verify, e.g. '
    "\\\"GET /health returns 200 and the new column exists\\\", NOT \\\"works well\\\""
    '"],\n'
    '      "depends_on": ["the exact TITLE of a prerequisite task in this list"],\n'
    '      "estimated_effort": "S | M | L",\n'
    '      "files_hint": ["likely paths to touch — a hint, never a CI/.env/.git path"]\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "\n"
    "Rules the build engine enforces — follow them or the plan is rejected:\n"
    "- Every task MUST have at least one observable acceptance criterion.\n"
    "- depends_on references another task by its exact title; no cycles.\n"
    "- files_hint must never name a protected path (.github/workflows, .env,\n"
    "  .git/, lockfiles, oxison-build/).\n"
    "- Keep the roadmap focused (a few well-scoped tasks beat dozens of vague\n"
    "  ones). Sequence by dependency and put the highest-leverage work first.\n"
)


def roadmap_plan_prompt(
    *,
    product_name: str,
    comprehension_markdown: str,
    structured_state: str,
    open_questions: str,
    user_guidance: str = "",
    prior_errors: str = "",
    greenfield: bool = False,
) -> str:
    """Build the Oxipensa planner prompt.

    ``structured_state`` is the comprehension's product/state/stack rendered as
    JSON; ``open_questions`` is the comprehension's open-questions list.
    ``user_guidance`` carries optional human input (the "solicit input" path).
    ``prior_errors`` is set on the self-correction retry — the plan-gate's
    violations from the previous attempt, fed back so the planner fixes them.
    ``greenfield`` reframes the plan as an initial from-scratch build (no code
    exists yet) rather than closing the gap on an existing codebase.
    """
    greenfield_block = (
        "\n=== GREENFIELD BUILD (from scratch) ===\n"
        "NOTHING exists yet — there is no codebase. Produce the INITIAL build "
        "plan to create this product from zero. Sequence foundational scaffolding "
        "first (project setup, core data model, primary user flow), then "
        "features. Every task is greenfield; depends_on must express real build "
        "order, and acceptance criteria must be observable even for foundational "
        'tasks (e.g. "the project builds and the dev server serves the landing '
        'page").\n'
        "=== END GREENFIELD BUILD ===\n"
        if greenfield
        else ""
    )
    guidance_block = (
        f"\n=== USER GUIDANCE (incorporate this) ===\n{user_guidance}\n"
        "=== END USER GUIDANCE ===\n"
        if user_guidance.strip()
        else ""
    )
    retry_block = (
        "\n=== YOUR PREVIOUS ATTEMPT WAS REJECTED — FIX THESE AND RETURN A "
        f"CORRECTED ROADMAP ===\n{prior_errors}\n=== END ===\n"
        if prior_errors.strip()
        else ""
    )
    return (
        f"{_PLANNER_IDENTITY}\n"
        f"Project: {product_name or '(unnamed)'}\n"
        f"{greenfield_block}\n"
        "Below is the structured comprehension produced by Oxicome — a unified,\n"
        "provenance-tagged understanding of the project across all its sources.\n\n"
        f"=== STRUCTURED STATE (product / state / stack, JSON) ===\n"
        f"{structured_state}\n=== END STRUCTURED STATE ===\n\n"
        f"=== COMPREHENSION (prose) ===\n{comprehension_markdown}\n"
        "=== END COMPREHENSION ===\n\n"
        f"=== OPEN QUESTIONS (from comprehension) ===\n{open_questions}\n"
        "=== END OPEN QUESTIONS ===\n"
        f"{guidance_block}"
        f"{retry_block}"
        "\n"
        f"{_PLANNER_JSON_CONTRACT}"
    )
