"""Pipeline runner — orchestrates the oxison stages.

Stage order: map (deterministic, free) -> ingest (deterministic, free) ->
comprehend (AI, read-only) -> generate PRODUCT/MANUAL/STACK (AI, read-only) ->
comprehension_json (deterministic, free) -> branch (roadmap-or-security).
oxison owns every write; all output lands under ``cfg.output_dir``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .branch import BranchError, run_branch
from .comprehend import ComprehensionError, comprehend
from .comprehension_doc import build_comprehension_doc
from .config import RunConfig
from .generate import ARTIFACTS, GenerationError, generate
from .manifest import RunManifest
from .oxipensa_gate import DEFAULT_MAX_TASKS
from .repomap import build_repo_map, estimate_tokens
from .sources.base import SourceResult, SourceUnit
from .sources.ingest import brief_unit, ingest_paths, ingest_urls, render_extra_context

COMPREHENSION_FILENAME = "COMPREHENSION.md"
COMPREHENSION_JSON_FILENAME = "comprehension.json"
REPOMAP_FILENAME = "repomap.json"


def _write(output_dir: Path, name: str, content: str) -> Path:
    """oxison-owned atomic-ish write of an artifact into the output dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / name
    path.write_text(content, encoding="utf-8")
    return path


async def run_pipeline(cfg: RunConfig, manifest: RunManifest) -> int:
    """Run map + comprehend. Returns a process exit code."""
    # --- Stage: map (deterministic, free, always rebuilt) ---
    print("→ mapping repository (deterministic, no AI)...")
    repo_map = build_repo_map(cfg.target)
    map_path = _write(cfg.output_dir, REPOMAP_FILENAME, repo_map.to_json())
    manifest.mark("map", "done", cost_usd=0.0, artifact=str(map_path))
    est = estimate_tokens(repo_map)
    mode = "single-pass" if est <= cfg.chunk_threshold else "map-reduce"
    print(f"  {repo_map.total_files} files, ~{est:,} est. tokens → {mode}")

    # --- Stage: ingest extra sources (deterministic extraction, read-only) ---
    extra_context = ""
    ingest_results: list[SourceResult] = []
    manifest.mark("ingest", "running")
    if cfg.extra_sources:
        print(f"→ ingesting {len(cfg.extra_sources)} extra source(s)...")
        ing = ingest_paths(
            [Path(p) for p in cfg.extra_sources],
            ocr_enabled=cfg.ocr_enabled,
            stt_key=cfg.stt_key,
            stt_provider=cfg.stt_provider,
        )
        ingest_results = ing.results
        extra_context = render_extra_context(ing.units)
        for r in ing.results:
            flag = "✓" if r.status == "ok" else "·"
            note = "" if r.status == "ok" else f" (skipped: {r.reason})"
            print(f"  {flag} {r.source_type}: {r.origin}{note}")
    manifest.mark("ingest", "done", cost_usd=0.0)

    # --- Stage: comprehend (AI, read-only) ---
    comp_path = cfg.output_dir / COMPREHENSION_FILENAME
    if cfg.resume and manifest.is_complete("comprehend") and comp_path.exists():
        print("→ comprehension: cached (--resume), skipping")
        comprehension_text = comp_path.read_text(encoding="utf-8")
    else:
        print(f"→ comprehending ({mode}, read-only workers)...")
        manifest.mark("comprehend", "running")
        try:
            comp = await comprehend(cfg, repo_map, extra_context=extra_context)
        except ComprehensionError as exc:
            # `exc` already reads "comprehension failed: …" — don't double-prefix.
            manifest.mark("comprehend", "failed", error=str(exc))
            print(f"oxison: {exc}")
            return 4
        _write(cfg.output_dir, COMPREHENSION_FILENAME, comp.text)
        manifest.mark(
            "comprehend", "done", cost_usd=comp.total_cost_usd, artifact=str(comp_path)
        )
        comprehension_text = comp.text
        slices_note = f" ({len(comp.slices)} slices)" if comp.chunked else ""
        print(f"  comprehension done{slices_note} — ${comp.total_cost_usd:.4f}")

    # --- Stage: generate PRODUCT / MANUAL / STACK (AI, read-only) ---
    def _cached(step: str) -> bool:
        return (
            cfg.resume
            and manifest.is_complete(step)
            and (cfg.output_dir / ARTIFACTS[step]).exists()
        )

    pending = [step for step in ARTIFACTS if not _cached(step)]
    if not pending:
        print("→ artifacts: all cached (--resume), skipping")
    else:
        skipped = [s for s in ARTIFACTS if s not in pending]
        if skipped:
            print(f"→ artifacts: {', '.join(skipped)} cached; generating {', '.join(pending)}...")
        else:
            print(f"→ generating artifacts ({', '.join(pending)}, read-only workers)...")
        for step in pending:
            manifest.mark(step, "running")
        try:
            artifacts = await generate(
                cfg, comprehension_text, repo_map, steps=pending, extra_context=extra_context
            )
        except GenerationError as exc:
            for step in pending:
                if manifest.steps[step].status == "running":
                    manifest.mark(step, "failed", error=str(exc))
            print(f"oxison: artifact generation failed: {exc}")
            return 5
        for art in artifacts:
            manifest.mark(art.step, "done", cost_usd=art.cost_usd, artifact=str(art.path))
            print(f"  {art.filename} — ${art.cost_usd:.4f}")

    # --- Stage: comprehension.json (the Oxipensa contract) ---
    ledger = [SourceResult.ok("git", str(cfg.target), units=[]), *ingest_results]
    doc = build_comprehension_doc(
        comprehension_text=comprehension_text,
        source_results=ledger,
        generated_at=datetime.now(UTC).isoformat(),
    )
    cj_path = _write(cfg.output_dir, COMPREHENSION_JSON_FILENAME, doc.to_json())
    manifest.mark("comprehension_json", "done", cost_usd=0.0, artifact=str(cj_path))
    print(f"  ✓ {COMPREHENSION_JSON_FILENAME}")

    # --- Stage: branch — roadmap analysis OR security scan (AI, read-only) ---
    branch_done = (
        cfg.resume
        and manifest.is_complete("branch")
        and manifest.steps["branch"].artifact is not None
        and Path(manifest.steps["branch"].artifact).exists()
    )
    if branch_done:
        print("→ branch: cached (--resume), skipping")
    else:
        print("→ roadmap-or-security branch (read-only)...")
        manifest.mark("branch", "running")
        try:
            branch = await run_branch(cfg, repo_map, comprehension_text)
        except BranchError as exc:
            manifest.mark("branch", "failed", error=str(exc))
            print(f"oxison: branch stage failed: {exc}")
            return 6
        manifest.mark("branch", "done", cost_usd=branch.cost_usd, artifact=str(branch.path))
        if branch.kind == "roadmap":
            enrich = (
                f" (+{branch.structured_item_count} oxi-parsed items)"
                if branch.structured_item_count
                else ""
            )
            print(
                f"  roadmap found ({branch.roadmap_source}) → "
                f"{branch.filename}{enrich} — ${branch.cost_usd:.4f}"
            )
        else:
            print(f"  no roadmap → security scan → {branch.filename} — ${branch.cost_usd:.4f}")
            print("  tip: add a ROADMAP.md so oxison can analyze planned work next time.")

    print()
    print(f"✓ artifacts in {cfg.output_dir}")
    for step, filename in ARTIFACTS.items():
        rec = manifest.steps[step]
        flag = "✓" if rec.status == "done" else "·"
        print(f"  {flag} {filename}")
    branch_rec = manifest.steps["branch"]
    if branch_rec.status == "done" and branch_rec.artifact:
        print(f"  ✓ {Path(branch_rec.artifact).name}")
    print(f"  total cost: ${manifest.total_cost_usd():.4f}")
    return 0


async def greenfield_pipeline(
    cfg: RunConfig,
    *,
    user_guidance: str = "",
    max_tasks: int = DEFAULT_MAX_TASKS,
) -> int:
    """Oxideia: start from a brief + non-repo sources (NO repo) → comprehension
    + PRODUCT.md, then an initial ROADMAP. oxison owns every write.

    No manifest/resume (greenfield is one-shot). The worker ``cwd`` is the empty
    staging dir in ``cfg.target``; workers stay read-only.
    """
    from .oxipensa import (
        ROADMAP_JSON_FILENAME,
        ROADMAP_MD_FILENAME,
        PlanError,
    )
    from .oxipensa import plan as run_plan
    from .roadmap_doc import render_roadmap_md

    print("→ Oxideia: greenfield mode — planning from your brief + sources (no repo)")
    repo_map = build_repo_map(cfg.target)  # empty staging dir; repomap.json not written

    # --- ingest: brief + file sources + URLs (all deterministic) ---
    results: list[SourceResult] = []
    units: list[SourceUnit] = []
    if cfg.brief:
        bu = brief_unit(cfg.brief)
        results.append(SourceResult.ok("brief", "brief:idea", units=[bu]))
        units.append(bu)
    if cfg.extra_sources:
        ing = ingest_paths(
            [Path(p) for p in cfg.extra_sources],
            ocr_enabled=cfg.ocr_enabled,
            stt_key=cfg.stt_key,
            stt_provider=cfg.stt_provider,
        )
        results.extend(ing.results)
        units.extend(ing.units)
    if cfg.urls:
        web = ingest_urls(cfg.urls)
        results.extend(web.results)
        units.extend(web.units)
    for r in results:
        flag = "✓" if r.status == "ok" else "·"
        note = "" if r.status == "ok" else f" (skipped: {r.reason})"
        print(f"  {flag} {r.source_type}: {r.origin}{note}")

    extra_context = render_extra_context(units)
    if not extra_context.strip():
        print(
            "oxison: greenfield needs at least one usable input — the brief, "
            "sources, and URLs were all empty or skipped."
        )
        return 4

    # --- comprehend (greenfield, read-only worker) ---
    print("→ comprehending the brief + sources (read-only worker)...")
    try:
        comp = await comprehend(cfg, repo_map, extra_context=extra_context, mode="greenfield")
    except ComprehensionError as exc:
        print(f"oxison: {exc}")
        return 4
    _write(cfg.output_dir, COMPREHENSION_FILENAME, comp.text)
    print(f"  comprehension done — ${comp.total_cost_usd:.4f}")

    # --- comprehension.json (correct ledger: brief + sources + web; NO git) ---
    doc = build_comprehension_doc(
        comprehension_text=comp.text,
        source_results=results,
        generated_at=datetime.now(UTC).isoformat(),
    )
    _write(cfg.output_dir, COMPREHENSION_JSON_FILENAME, doc.to_json())
    print(f"  ✓ {COMPREHENSION_JSON_FILENAME}")

    # --- generate PRODUCT.md only (no MANUAL/STACK — nothing built yet) ---
    print("→ generating PRODUCT.md (read-only worker)...")
    try:
        artifacts = await generate(
            cfg, comp.text, repo_map, steps=["product"],
            extra_context=extra_context, mode="greenfield",
        )
    except GenerationError as exc:
        print(f"oxison: artifact generation failed: {exc}")
        return 5
    product_cost = sum(a.cost_usd for a in artifacts)
    for art in artifacts:
        print(f"  {art.filename} — ${art.cost_usd:.4f}")

    # --- plan: the initial from-scratch roadmap (greenfield framing) ---
    print("→ planning the initial roadmap (read-only worker, self-correcting gate)...")
    try:
        result = await run_plan(
            cfg,
            json.loads(doc.to_json()),
            generated_at=datetime.now(UTC).isoformat(),
            user_guidance=user_guidance,
            max_tasks=max_tasks,
            greenfield=True,
        )
    except PlanError as exc:
        print(f"oxison: planning failed: {exc}")
        return 5
    _write(cfg.output_dir, ROADMAP_JSON_FILENAME, result.doc.to_json())
    _write(cfg.output_dir, ROADMAP_MD_FILENAME, render_roadmap_md(result.doc))
    note = "" if result.attempts == 1 else f" (after {result.attempts} attempts)"
    print(f"  ✓ {len(result.doc.tasks)} tasks planned{note} — ${result.cost_usd:.4f}")

    print()
    print(f"✓ greenfield plan in {cfg.output_dir}")
    print(f"  ✓ {COMPREHENSION_FILENAME}")
    print("  ✓ PRODUCT.md")
    print(f"  ✓ {ROADMAP_MD_FILENAME}")
    total = comp.total_cost_usd + product_cost + result.cost_usd
    print(f"  total cost: ${total:.4f}")
    return 0


__all__ = [
    "COMPREHENSION_FILENAME",
    "COMPREHENSION_JSON_FILENAME",
    "REPOMAP_FILENAME",
    "greenfield_pipeline",
    "run_pipeline",
]
