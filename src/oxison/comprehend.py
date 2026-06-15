"""Comprehension orchestrator — single-pass or map-reduce.

Turns a deterministic ``RepoMap`` into a ``Comprehension`` object by
driving read-only ``claude -p`` workers. Small repos get one pass;
repos whose estimated token surface exceeds the chunk threshold are
sliced by top-level directory, each slice comprehended concurrently
(bounded by ``cfg.max_concurrency``), then merged by a synthesis pass.

Every worker is launched with ``READ_ONLY_TOOLS`` — the safety
invariant — and ``cwd`` set to the target repo so Read/Glob/Grep
resolve against it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .config import READ_ONLY_TOOLS, RunConfig
from .dispatch import InvokeResult, invoke
from .prompts import single_pass_prompt, slice_prompt, synthesis_prompt
from .repomap import RepoMap, estimate_tokens, top_level_dirs

#: Per-worker wall-clock timeout (seconds). Comprehension reads files;
#: 20 min is generous for a single slice.
COMPREHEND_TIMEOUT_S = 1200.0


class ComprehensionError(RuntimeError):
    """A comprehension worker failed (no retry — surfaced to the caller)."""


@dataclass
class SliceSummary:
    directory: str
    text: str
    cost_usd: float


@dataclass
class Comprehension:
    text: str
    slices: list[SliceSummary] = field(default_factory=list)
    total_cost_usd: float = 0.0
    chunked: bool = False


def _require_ok(result: InvokeResult, what: str) -> None:
    if not result.ok:
        raise ComprehensionError(f"{what} failed: {result.error or 'unknown error'}")
    if not result.text.strip():
        raise ComprehensionError(f"{what} returned empty output")


async def _comprehend_single(
    cfg: RunConfig, repo_map: RepoMap, *, extra_context: str = ""
) -> Comprehension:
    prompt = single_pass_prompt(
        root=repo_map.root,
        repo_map_context=repo_map.to_context(),
        extra_context=extra_context,
    )
    result = await invoke(
        prompt,
        cfg=cfg,
        allowed_tools=READ_ONLY_TOOLS,
        cwd=cfg.target,
        timeout_s=COMPREHEND_TIMEOUT_S,
    )
    _require_ok(result, "comprehension")
    return Comprehension(text=result.text, total_cost_usd=result.cost_usd, chunked=False)


async def _comprehend_slice(
    cfg: RunConfig, repo_map: RepoMap, directory: str, *, extra_context: str = ""
) -> SliceSummary:
    prompt = slice_prompt(
        root=repo_map.root,
        repo_map_context=repo_map.to_context(),
        slice_dir=directory,
        extra_context=extra_context,
    )
    result = await invoke(
        prompt,
        cfg=cfg,
        allowed_tools=READ_ONLY_TOOLS,
        cwd=cfg.target,
        timeout_s=COMPREHEND_TIMEOUT_S,
    )
    _require_ok(result, f"slice '{directory}'")
    return SliceSummary(directory=directory, text=result.text, cost_usd=result.cost_usd)


async def _comprehend_mapreduce(
    cfg: RunConfig, repo_map: RepoMap, *, extra_context: str = ""
) -> Comprehension:
    dirs = top_level_dirs(cfg.target)
    if not dirs:
        # No sub-directories to slice — fall back to single pass.
        return await _comprehend_single(cfg, repo_map, extra_context=extra_context)

    sem = asyncio.Semaphore(cfg.max_concurrency)

    async def bounded(directory: str) -> SliceSummary:
        async with sem:
            return await _comprehend_slice(cfg, repo_map, directory, extra_context=extra_context)

    slices = await asyncio.gather(*(bounded(d) for d in dirs))
    slice_cost = sum(s.cost_usd for s in slices)

    joined = "\n\n".join(f"## ./{s.directory}/\n{s.text}" for s in slices)
    synth_prompt = synthesis_prompt(
        root=repo_map.root,
        repo_map_context=repo_map.to_context(),
        slice_summaries=joined,
        extra_context=extra_context,
    )
    synth = await invoke(
        synth_prompt,
        cfg=cfg,
        allowed_tools=READ_ONLY_TOOLS,
        cwd=cfg.target,
        timeout_s=COMPREHEND_TIMEOUT_S,
    )
    _require_ok(synth, "synthesis")
    return Comprehension(
        text=synth.text,
        slices=list(slices),
        total_cost_usd=slice_cost + synth.cost_usd,
        chunked=True,
    )


async def comprehend(
    cfg: RunConfig, repo_map: RepoMap, *, extra_context: str = ""
) -> Comprehension:
    """Comprehend the repo, choosing single-pass vs map-reduce by size."""
    est = estimate_tokens(repo_map)
    if est <= cfg.chunk_threshold:
        return await _comprehend_single(cfg, repo_map, extra_context=extra_context)
    return await _comprehend_mapreduce(cfg, repo_map, extra_context=extra_context)


__all__ = [
    "COMPREHEND_TIMEOUT_S",
    "Comprehension",
    "ComprehensionError",
    "SliceSummary",
    "comprehend",
]
