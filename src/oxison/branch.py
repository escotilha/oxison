"""Roadmap-or-security branch — the follow-on stage.

If the repo ships a roadmap, oxison analyzes it (ROADMAP-ANALYSIS.md).
Otherwise it runs a lightweight, read-only security surface scan
(SECURITY-NOTES.md) and recommends adding a roadmap.

oxi-core integration is **opportunistic enrichment only**: its
``parse_roadmap`` is oxi-format-specific (``## Tier N`` + ``**ID · …**``)
and oxi-core is not a public dependency, so it is lazily imported and
used to add structure *when present*. The primary roadmap analysis is a
``claude -p`` pass that works on any roadmap format. Everything here is
read-only; oxison owns the writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import READ_ONLY_TOOLS, RunConfig
from .dispatch import invoke
from .mdutil import strip_preamble
from .prompts import roadmap_analysis_prompt, security_prompt
from .repomap import RepoMap

BRANCH_TIMEOUT_S = 900.0

ROADMAP_ANALYSIS_FILENAME = "ROADMAP-ANALYSIS.md"
SECURITY_NOTES_FILENAME = "SECURITY-NOTES.md"

#: Roadmap filenames oxison looks for, in priority order.
ROADMAP_CANDIDATES: tuple[str, ...] = (
    "ROADMAP.md",
    "roadmap.md",
    "docs/ROADMAP.md",
    "docs/roadmap.md",
    "BACKLOG.md",
    "backlog.md",
    "TODO.md",
)

#: Cap roadmap text fed to the model (chars) so a huge backlog can't blow
#: the prompt. The analysis still sees the whole structure for normal
#: roadmaps; pathological ones are truncated with a note.
_ROADMAP_TEXT_CAP = 24_000


class BranchError(RuntimeError):
    """The branch stage's AI worker failed (no retry — surfaced)."""


@dataclass
class BranchResult:
    kind: Literal["roadmap", "security"]
    filename: str
    path: Path
    cost_usd: float
    roadmap_source: str | None = None
    structured_item_count: int = 0


def detect_roadmap(target: Path) -> Path | None:
    """Return the first roadmap file found in the target, or None."""
    for candidate in ROADMAP_CANDIDATES:
        path = target / candidate
        if path.is_file():
            return path
    return None


def _try_oxi_parse(text: str) -> list[str] | None:
    """Opportunistically parse the roadmap with oxi-core, if importable.

    Returns a list of ``"identifier · title"`` strings on success, or
    None if oxi-core is absent, the roadmap isn't in oxi format, or it
    parses to zero items. Never raises — enrichment is best-effort.
    """
    try:
        from oxi_core.planner import parse_roadmap  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        items = parse_roadmap(text)
    except Exception:  # noqa: BLE001 - enrichment must never break the run
        return None
    if not items:
        return None
    return [f"{it.identifier} · {it.title}" for it in items]


def _write(output_dir: Path, filename: str, content: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_text(strip_preamble(content), encoding="utf-8")
    return path


async def _analyze_roadmap(
    cfg: RunConfig, repo_map: RepoMap, comprehension: str, roadmap_path: Path
) -> BranchResult:
    raw = roadmap_path.read_text(encoding="utf-8", errors="replace")
    text = raw[:_ROADMAP_TEXT_CAP]
    if len(raw) > _ROADMAP_TEXT_CAP:
        text += "\n\n[... roadmap truncated by oxison ...]"
    rel = str(roadmap_path.relative_to(cfg.target))

    structured = _try_oxi_parse(raw)
    structured_block = "\n".join(f"- {s}" for s in structured) if structured else None

    prompt = roadmap_analysis_prompt(
        root=repo_map.root,
        comprehension=comprehension,
        roadmap_text=text,
        roadmap_path=rel,
        structured_items=structured_block,
    )
    result = await invoke(
        prompt,
        cfg=cfg,
        allowed_tools=READ_ONLY_TOOLS,
        cwd=cfg.target,
        timeout_s=BRANCH_TIMEOUT_S,
    )
    if not result.ok:
        raise BranchError(f"roadmap analysis failed: {result.error or 'unknown'}")
    if not result.text.strip():
        raise BranchError("roadmap analysis returned empty output")
    path = _write(cfg.output_dir, ROADMAP_ANALYSIS_FILENAME, result.text)
    return BranchResult(
        kind="roadmap",
        filename=ROADMAP_ANALYSIS_FILENAME,
        path=path,
        cost_usd=result.cost_usd,
        roadmap_source=rel,
        structured_item_count=len(structured) if structured else 0,
    )


async def _security_scan(
    cfg: RunConfig, repo_map: RepoMap, comprehension: str
) -> BranchResult:
    prompt = security_prompt(
        root=repo_map.root,
        comprehension=comprehension,
        repo_map_context=repo_map.to_context(),
    )
    result = await invoke(
        prompt,
        cfg=cfg,
        allowed_tools=READ_ONLY_TOOLS,
        cwd=cfg.target,
        timeout_s=BRANCH_TIMEOUT_S,
    )
    if not result.ok:
        raise BranchError(f"security scan failed: {result.error or 'unknown'}")
    if not result.text.strip():
        raise BranchError("security scan returned empty output")
    path = _write(cfg.output_dir, SECURITY_NOTES_FILENAME, result.text)
    return BranchResult(
        kind="security",
        filename=SECURITY_NOTES_FILENAME,
        path=path,
        cost_usd=result.cost_usd,
    )


async def run_branch(
    cfg: RunConfig, repo_map: RepoMap, comprehension: str
) -> BranchResult:
    """Take the roadmap arm if a roadmap exists, else the security arm."""
    roadmap_path = detect_roadmap(cfg.target)
    if roadmap_path is not None:
        return await _analyze_roadmap(cfg, repo_map, comprehension, roadmap_path)
    return await _security_scan(cfg, repo_map, comprehension)


__all__ = [
    "BRANCH_TIMEOUT_S",
    "ROADMAP_ANALYSIS_FILENAME",
    "ROADMAP_CANDIDATES",
    "SECURITY_NOTES_FILENAME",
    "BranchError",
    "BranchResult",
    "detect_roadmap",
    "run_branch",
]
