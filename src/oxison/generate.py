"""Artifact generators — PRODUCT / MANUAL / STACK.

Each document is produced by one read-only ``claude -p`` worker that
returns *only* the markdown body; oxison writes the file itself into the
output directory. This keeps every worker read-only (the #1 invariant)
and keeps all file-writing authority in one place.

The three generators run concurrently (bounded by ``cfg.max_concurrency``).
The STACK doc is grounded in the deterministic repo map so dependency
lists come from the manifests, not the model's imagination.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .config import READ_ONLY_TOOLS, RunConfig
from .dispatch import invoke
from .mdutil import strip_preamble
from .prompts import (
    greenfield_product_prompt,
    manual_prompt,
    product_prompt,
    stack_prompt,
)
from .repomap import RepoMap

#: Per-document wall-clock timeout (seconds).
GENERATE_TIMEOUT_S = 900.0

#: Manifest step name -> output filename.
ARTIFACTS: dict[str, str] = {
    "product": "PRODUCT.md",
    "manual": "MANUAL.md",
    "stack": "STACK.md",
}


class GenerationError(RuntimeError):
    """An artifact generation worker failed (no retry — surfaced)."""


@dataclass
class GeneratedArtifact:
    step: str
    filename: str
    path: Path
    cost_usd: float


def _prompt_for(
    step: str,
    *,
    root: str,
    comprehension: str,
    repo_map_context: str,
    extra_context: str = "",
    mode: str = "repo",
) -> str:
    if mode == "greenfield":
        # Greenfield generates only PRODUCT (no MANUAL/STACK — nothing built yet)
        # and omits the repo map.
        return greenfield_product_prompt(
            comprehension=comprehension, extra_context=extra_context
        )
    builders = {
        "product": product_prompt,
        "manual": manual_prompt,
        "stack": stack_prompt,
    }
    return builders[step](
        root=root,
        comprehension=comprehension,
        repo_map_context=repo_map_context,
        extra_context=extra_context,
    )


async def _generate_one(
    cfg: RunConfig,
    step: str,
    *,
    comprehension: str,
    repo_map: RepoMap,
    extra_context: str = "",
    mode: str = "repo",
) -> tuple[str, str, float]:
    """Produce one artifact's markdown via a read-only worker.

    Returns ``(step, markdown, cost)``. oxison (the caller) owns the
    actual file write — this function never writes.
    """
    prompt = _prompt_for(
        step,
        root=repo_map.root,
        comprehension=comprehension,
        repo_map_context=repo_map.to_context(),
        extra_context=extra_context,
        mode=mode,
    )
    result = await invoke(
        prompt,
        cfg=cfg,
        allowed_tools=READ_ONLY_TOOLS,
        cwd=cfg.target,
        timeout_s=GENERATE_TIMEOUT_S,
    )
    if not result.ok:
        raise GenerationError(f"{step} generation failed: {result.error or 'unknown'}")
    if not result.text.strip():
        raise GenerationError(f"{step} generation returned empty output")
    return step, result.text, result.cost_usd


def _write_artifact(output_dir: Path, filename: str, content: str) -> Path:
    """oxison-owned write of one artifact into the output dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_text(strip_preamble(content), encoding="utf-8")
    return path


async def generate(
    cfg: RunConfig,
    comprehension: str,
    repo_map: RepoMap,
    *,
    steps: list[str] | None = None,
    extra_context: str = "",
    mode: str = "repo",
) -> list[GeneratedArtifact]:
    """Generate the requested artifacts concurrently; oxison writes them.

    ``steps`` defaults to all of product/manual/stack. Used by ``--resume``
    to regenerate only the steps not yet complete. ``mode="greenfield"`` uses
    the greenfield product prompt (and should be called with ``steps=["product"]``).
    """
    todo = steps if steps is not None else list(ARTIFACTS.keys())
    sem = asyncio.Semaphore(cfg.max_concurrency)

    async def bounded(step: str) -> tuple[str, str, float]:
        async with sem:
            return await _generate_one(
                cfg,
                step,
                comprehension=comprehension,
                repo_map=repo_map,
                extra_context=extra_context,
                mode=mode,
            )

    results = await asyncio.gather(*(bounded(s) for s in todo))

    artifacts: list[GeneratedArtifact] = []
    for step, markdown, cost in results:
        filename = ARTIFACTS[step]
        path = _write_artifact(cfg.output_dir, filename, markdown)
        artifacts.append(
            GeneratedArtifact(step=step, filename=filename, path=path, cost_usd=cost)
        )
    return artifacts


__all__ = [
    "ARTIFACTS",
    "GENERATE_TIMEOUT_S",
    "GeneratedArtifact",
    "GenerationError",
    "generate",
]
