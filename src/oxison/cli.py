"""oxison command-line entrypoint (argparse, zero CLI deps).

Commands:
    oxison run <repo>     comprehend a repo and write product docs
    oxison version        print version + banner

The ``run`` command wires the full pipeline. Through Phase 0 it runs
preflight + config resolution + manifest creation and prints a plan
summary (no AI). Later phases extend ``cmd_run`` to execute the
comprehension and generation stages.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    DEFAULT_CHUNK_THRESHOLD,
    ConfigError,
    RunConfig,
    build_run_config,
)
from .manifest import RunManifest
from .preflight import PreflightError, preflight

BANNER = r"""
   ____  _  _  ____  ___   __  __ _
  /  _ \( \/ )(_  _)/ __) /  \(  ( \
 (  ( ) ))  (  _)(_ \__ \(  O )    /
  \_)(_/(_/\_)(____)(___/ \__/\_)__)   oxison v{version}
  point it at a repo - get the product docs back
""".strip("\n")


def _now_iso() -> str:
    """UTC timestamp, stamped once at the CLI boundary."""
    return datetime.now(UTC).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oxison",
        description="Comprehend a local repo and write PRODUCT/MANUAL/STACK docs.",
    )
    parser.add_argument(
        "--version", action="version", version=f"oxison {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="comprehend a repo and write product docs")
    run_p.add_argument("target", help="path to the local repository to comprehend")
    run_p.add_argument(
        "--output-dir",
        default=None,
        help="where to write artifacts (default: ./oxison-output)",
    )
    run_p.add_argument(
        "--bare",
        action="store_true",
        help="use --bare auth (ANTHROPIC_API_KEY) instead of your Claude Code login",
    )
    run_p.add_argument("--api-key", default=None, help="API key for bare mode")
    run_p.add_argument("--model", default=None, help="override the Claude model")
    run_p.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help="hard dollar cap passed to every claude call",
    )
    run_p.add_argument(
        "--chunk-threshold",
        type=int,
        default=DEFAULT_CHUNK_THRESHOLD,
        help=f"map-reduce above this est. token count (default: {DEFAULT_CHUNK_THRESHOLD})",
    )
    run_p.add_argument(
        "--max-concurrency",
        type=int,
        default=4,
        help="max concurrent claude workers (default: 4)",
    )
    run_p.add_argument(
        "--resume",
        action="store_true",
        help="skip pipeline steps already marked done in the manifest",
    )
    run_p.add_argument(
        "--add", action="append", default=[], metavar="PATH",
        help="add a non-repo source (PDF/pptx/docx/md/recording); repeatable",
    )
    run_p.add_argument(
        "--sources", default=None, metavar="DIR",
        help="ingest every supported file in a directory (auto-detect)",
    )
    run_p.add_argument(
        "--ocr", action="store_true",
        help="enable scanned-PDF OCR (lazy-imports an optional document_extraction package)",
    )
    run_p.add_argument(
        "--stt-key", default=None, help="cloud STT API key (enables recording ingest)",
    )
    run_p.add_argument(
        "--stt-provider", default="openai", help="STT provider (default: openai)",
    )
    run_p.set_defaults(func=cmd_run)

    plan_p = sub.add_parser(
        "plan",
        help="Oxipensa: turn a comprehension.json into a roadmap.json + ROADMAP.md",
    )
    plan_p.add_argument(
        "comprehension",
        help="path to a comprehension.json (or an oxison-output dir containing one)",
    )
    plan_p.add_argument(
        "--output-dir",
        default=None,
        help="where to write roadmap.json + ROADMAP.md (default: next to the comprehension)",
    )
    plan_p.add_argument(
        "--repo",
        default=None,
        help="optional repo to ground the planner in (read-only); default: no repo",
    )
    plan_p.add_argument(
        "--answers-file",
        default=None,
        help="optional text file of user guidance to refine the roadmap",
    )
    plan_p.add_argument(
        "--max-tasks",
        type=int,
        default=40,
        help="plan-gate scope fence: reject a roadmap with more tasks (default: 40)",
    )
    plan_p.add_argument(
        "--bare",
        action="store_true",
        help="use --bare auth (ANTHROPIC_API_KEY) instead of your Claude Code login",
    )
    plan_p.add_argument("--api-key", default=None, help="API key for bare mode")
    plan_p.add_argument("--model", default=None, help="override the Claude model")
    plan_p.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help="hard dollar cap passed to the planner call",
    )
    plan_p.set_defaults(func=cmd_plan)

    ideate_p = sub.add_parser(
        "ideate",
        help="Oxideia: start from a brief + non-repo inputs (no repo) → "
             "comprehension + ROADMAP",
    )
    ideate_p.add_argument("--brief", default=None, help="the project idea, as text")
    ideate_p.add_argument(
        "--brief-file", default=None, help="read the project idea from a text file"
    )
    ideate_p.add_argument(
        "--add", action="append", default=[], metavar="PATH",
        help="add a non-repo source (PDF/pptx/docx/md/recording); repeatable",
    )
    ideate_p.add_argument(
        "--sources", default=None, metavar="DIR",
        help="ingest every supported file in a directory (auto-detect)",
    )
    ideate_p.add_argument(
        "--url", action="append", default=[], metavar="URL",
        help="fetch a website link as a source; repeatable",
    )
    ideate_p.add_argument(
        "--ocr", action="store_true",
        help="enable scanned-PDF OCR (lazy-imports an optional document_extraction package)",
    )
    ideate_p.add_argument(
        "--stt-key", default=None, help="cloud STT API key (enables recording ingest)",
    )
    ideate_p.add_argument(
        "--stt-provider", default="openai", help="STT provider (default: openai)",
    )
    ideate_p.add_argument(
        "--output-dir", default=None,
        help="where to write artifacts (default: ./oxison-output)",
    )
    ideate_p.add_argument(
        "--answers-file", default=None,
        help="optional text file of guidance to refine the roadmap (re-run to iterate)",
    )
    ideate_p.add_argument(
        "--max-tasks", type=int, default=40,
        help="plan-gate scope fence: reject a roadmap with more tasks (default: 40)",
    )
    ideate_p.add_argument(
        "--bare", action="store_true",
        help="use --bare auth (ANTHROPIC_API_KEY) instead of your Claude Code login",
    )
    ideate_p.add_argument("--api-key", default=None, help="API key for bare mode")
    ideate_p.add_argument("--model", default=None, help="override the Claude model")
    ideate_p.add_argument(
        "--max-budget-usd", type=float, default=None,
        help="hard dollar cap passed to every claude call",
    )
    ideate_p.set_defaults(func=cmd_ideate)

    build_p = sub.add_parser(
        "build",
        help="Oxfaz: ingest a roadmap.json and run the autonomous build loop",
    )
    build_p.add_argument(
        "roadmap",
        help="path to a roadmap.json (or a dir containing one) from `oxison plan`",
    )
    build_p.add_argument(
        "--repo", required=True,
        help="the git repository to build in (workers run in isolated worktrees)",
    )
    build_p.add_argument(
        "--dry-run", action="store_true",
        help="ingest the roadmap and show the plan; spawn NO build workers",
    )
    build_p.add_argument("--max-ticks", type=int, default=None,
                         help="hard ceiling on loop ticks (LP1 guardrail)")
    build_p.add_argument("--budget-ceiling-usd", type=float, default=None,
                         help="run-level cost ceiling (LP3 guardrail; unset = inactive)")
    build_p.add_argument("--max-workers", type=int, default=1,
                         help="tasks dispatched per tick (default: 1)")
    build_p.add_argument("--no-progress-ticks", type=int, default=5,
                         help="halt after N ticks with no task advancing (LP2 guardrail)")
    build_p.add_argument("--worker-budget-usd", type=float, default=5.0,
                         help="per-worker hard cost cap / timed-out floor (default: 5.0)")
    build_p.add_argument("--no-sandbox", action="store_true",
                         help="DANGER: run build workers WITHOUT the sandbox "
                              "(only on repos you fully trust)")
    build_p.add_argument("--sandbox-layer", choices=("srt", "container"), default="srt",
                         help="sandbox when enabled: srt (Layer 1 host allowlist, default) or "
                              "container (Layer 2 rootless container — needs a runtime + the "
                              "worker image + an API key)")
    build_p.add_argument("--integrate", action="store_true",
                         help="merge each graded branch into the repo's current branch "
                              "as it passes — composes the roadmap into ONE product on "
                              "main (forces --max-workers 1). Default: per-branch, no merge.")
    build_p.add_argument("--bare", action="store_true",
                         help="use --bare auth (ANTHROPIC_API_KEY) instead of your login")
    build_p.add_argument("--api-key", default=None, help="API key for bare mode")
    build_p.add_argument("--model", default=None, help="override the Claude model")
    build_p.set_defaults(func=cmd_build)

    ver_p = sub.add_parser("version", help="print version + banner")
    ver_p.set_defaults(func=cmd_version)

    return parser


def cmd_version(_args: argparse.Namespace) -> int:
    print(BANNER.format(version=__version__))
    return 0


def _print_plan_summary(cfg: RunConfig, manifest: RunManifest, claude_version: str) -> None:
    print(BANNER.format(version=__version__))
    print()
    print(f"  target        : {cfg.target}")
    print(f"  output        : {cfg.output_dir}")
    print(f"  auth mode     : {cfg.auth_mode}")
    print(f"  model         : {cfg.model or '(claude default)'}")
    budget = f"${cfg.max_budget_usd:.2f}" if cfg.max_budget_usd else "(none)"
    print(f"  budget cap    : {budget}")
    print(f"  chunk thresh  : {cfg.chunk_threshold:,} est. tokens")
    print(f"  concurrency   : {cfg.max_concurrency}")
    print(f"  target is git : {cfg.target_is_git}")
    print(f"  claude CLI    : {claude_version}")
    print(f"  run id        : {manifest.run_id}")
    print(f"  manifest      : {manifest.path}")


def cmd_run(args: argparse.Namespace) -> int:
    extra = list(args.add)
    if args.sources:
        sdir = Path(args.sources).expanduser().resolve()
        if sdir.is_dir():
            extra += [str(p) for p in sorted(sdir.iterdir()) if p.is_file()]
        else:
            print(f"oxison: --sources: {args.sources!r} is not a directory, skipping")
    try:
        cfg = build_run_config(
            target=args.target,
            output_dir=args.output_dir,
            bare=args.bare,
            api_key=args.api_key,
            model=args.model,
            max_budget_usd=args.max_budget_usd,
            chunk_threshold=args.chunk_threshold,
            max_concurrency=args.max_concurrency,
            resume=args.resume,
            extra_sources=extra,
            ocr_enabled=args.ocr,
            stt_key=args.stt_key,
            stt_provider=args.stt_provider,
        )
    except ConfigError as exc:
        print(f"oxison: config error: {exc}")
        return 2

    try:
        pre = preflight(cfg)
    except PreflightError as exc:
        print(f"oxison: preflight failed: {exc}")
        return 3

    manifest = RunManifest.load_or_create(
        cfg.output_dir, target=str(cfg.target), started_at=_now_iso()
    )

    _print_plan_summary(cfg, manifest, pre.claude_version)
    print()

    # The pipeline runner is resolved dynamically: it is an optional
    # forward dependency (built in Phase 1+). Resolving it via importlib
    # keeps this module decoupled from the engine modules.
    runner = _load_pipeline_runner()
    if runner is None:
        print("  (engine not built yet — scaffold: config + preflight OK)")
        return 0
    return asyncio.run(runner(cfg, manifest))


def cmd_plan(args: argparse.Namespace) -> int:
    """Oxipensa: comprehension.json -> roadmap.json + ROADMAP.md."""
    from .oxipensa import (
        ROADMAP_JSON_FILENAME,
        ROADMAP_MD_FILENAME,
        PlanError,
        load_comprehension,
    )
    from .oxipensa import plan as run_plan
    from .roadmap_doc import render_roadmap_md

    comp_arg = Path(args.comprehension).expanduser().resolve()
    default_out = comp_arg if comp_arg.is_dir() else comp_arg.parent
    # The planner's cwd: an optional repo to ground in, else the output dir
    # (the worker is read-only and needs a valid, existing directory).
    cwd_target = args.repo if args.repo else str(default_out)
    output_dir = args.output_dir if args.output_dir else str(default_out)

    user_guidance = ""
    if args.answers_file:
        gpath = Path(args.answers_file).expanduser()
        if not gpath.is_file():
            print(f"oxison: --answers-file not found: {args.answers_file}")
            return 2
        user_guidance = gpath.read_text(encoding="utf-8", errors="replace")

    try:
        comprehension = load_comprehension(comp_arg)
        cfg = build_run_config(
            target=cwd_target,
            output_dir=output_dir,
            bare=args.bare,
            api_key=args.api_key,
            model=args.model,
            max_budget_usd=args.max_budget_usd,
            chunk_threshold=DEFAULT_CHUNK_THRESHOLD,
            max_concurrency=1,
            resume=False,
        )
    except (ConfigError, PlanError) as exc:
        print(f"oxison: {exc}")
        return 2

    try:
        pre = preflight(cfg)
    except PreflightError as exc:
        print(f"oxison: preflight failed: {exc}")
        return 3

    print(BANNER.format(version=__version__))
    print()
    print("  oxipensa (planner)")
    print(f"  comprehension : {comp_arg}")
    print(f"  ground repo   : {args.repo or '(none — plan from comprehension)'}")
    print(f"  output        : {cfg.output_dir}")
    print(f"  model         : {cfg.model or '(claude default)'}")
    print(f"  claude CLI    : {pre.claude_version}")
    print()
    print("→ planning roadmap (read-only worker, self-correcting gate)...")

    try:
        result = asyncio.run(
            run_plan(
                cfg,
                comprehension,
                generated_at=_now_iso(),
                user_guidance=user_guidance,
                max_tasks=args.max_tasks,
            )
        )
    except PlanError as exc:
        print(f"oxison: planning failed: {exc}")
        return 5

    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        rj_path = cfg.output_dir / ROADMAP_JSON_FILENAME
        rm_path = cfg.output_dir / ROADMAP_MD_FILENAME
        rj_path.write_text(result.doc.to_json(), encoding="utf-8")
        rm_path.write_text(render_roadmap_md(result.doc), encoding="utf-8")
    except OSError as exc:
        # The planner run already succeeded (and cost money) — fail cleanly
        # rather than throw away the result with a traceback.
        print(f"oxison: could not write roadmap artifacts: {exc}")
        return 4

    note = "" if result.attempts == 1 else f" (after {result.attempts} attempts)"
    print(f"  ✓ {len(result.doc.tasks)} tasks planned{note} — ${result.cost_usd:.4f}")
    print(f"  ✓ {ROADMAP_JSON_FILENAME}")
    print(f"  ✓ {ROADMAP_MD_FILENAME}")
    print()
    print(f"✓ roadmap in {cfg.output_dir}")
    return 0


def cmd_ideate(args: argparse.Namespace) -> int:
    """Oxideia: greenfield — brief + non-repo inputs → comprehension + ROADMAP."""
    from .config import build_greenfield_config
    from .pipeline import greenfield_pipeline

    if args.brief and args.brief_file:
        print("oxison: pass either --brief or --brief-file, not both")
        return 2
    brief = args.brief
    if args.brief_file:
        bpath = Path(args.brief_file).expanduser()
        if not bpath.is_file():
            print(f"oxison: --brief-file not found: {args.brief_file}")
            return 2
        brief = bpath.read_text(encoding="utf-8", errors="replace").strip()

    extra = list(args.add)
    if args.sources:
        sdir = Path(args.sources).expanduser().resolve()
        if sdir.is_dir():
            extra += [str(p) for p in sorted(sdir.iterdir()) if p.is_file()]
        else:
            print(f"oxison: --sources: {args.sources!r} is not a directory, skipping")

    if not (brief or extra or args.url):
        print(
            "oxison: ideate needs at least one input — pass --brief/--brief-file, "
            "--add, --sources, or --url"
        )
        return 2

    user_guidance = ""
    if args.answers_file:
        gpath = Path(args.answers_file).expanduser()
        if not gpath.is_file():
            print(f"oxison: --answers-file not found: {args.answers_file}")
            return 2
        user_guidance = gpath.read_text(encoding="utf-8", errors="replace")

    try:
        cfg = build_greenfield_config(
            output_dir=args.output_dir,
            bare=args.bare,
            api_key=args.api_key,
            model=args.model,
            max_budget_usd=args.max_budget_usd,
            brief=brief,
            urls=list(args.url),
            extra_sources=extra,
            ocr_enabled=args.ocr,
            stt_key=args.stt_key,
            stt_provider=args.stt_provider,
        )
    except ConfigError as exc:
        print(f"oxison: config error: {exc}")
        return 2

    try:
        pre = preflight(cfg)
    except PreflightError as exc:
        print(f"oxison: preflight failed: {exc}")
        return 3

    print(BANNER.format(version=__version__))
    print()
    print("  oxideia (greenfield)")
    print(
        f"  inputs        : brief={'yes' if brief else 'no'}, "
        f"sources={len(extra)}, urls={len(args.url)}"
    )
    print(f"  output        : {cfg.output_dir}")
    print(f"  auth mode     : {cfg.auth_mode}")
    print(f"  model         : {cfg.model or '(claude default)'}")
    print(f"  claude CLI    : {pre.claude_version}")
    print()

    return asyncio.run(
        greenfield_pipeline(cfg, user_guidance=user_guidance, max_tasks=args.max_tasks)
    )


def cmd_build(args: argparse.Namespace) -> int:
    """Oxfaz: ingest a roadmap.json and run the autonomous build loop."""
    from .engine.dispatch import DispatchOutcome, launch_worker
    from .engine.engconfig import EngineConfig
    from .engine.gates import grade_diff
    from .engine.loop import LoopOptions, run_build_loop
    from .engine.roadmap_ingest import (
        RoadmapIngestError,
        ingest_roadmap,
        load_roadmap,
    )
    from .engine.sandbox import resolve_srt_binary
    from .engine.taskstore import Task, TaskStore

    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"oxison: --repo is not a git repository: {repo}")
        return 2

    try:
        roadmap = load_roadmap(Path(args.roadmap).expanduser().resolve())
    except RoadmapIngestError as exc:
        print(f"oxison: {exc}")
        return 2

    # --integrate composes the roadmap into one product on main: each graded
    # branch is git-merged as it passes. It requires sequential dispatch (so each
    # task branches from the accumulated main), so it forces --max-workers 1.
    integrator = None
    if args.integrate:
        import subprocess

        from .engine.integrate import make_integrator

        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            print(
                "oxison: --integrate needs a clean working tree on the repo's main "
                "branch (uncommitted changes would block the fast-forward merge).\n"
                "  commit or stash them, then re-run."
            )
            return 2
        if args.max_workers != 1:
            print(f"  note: --integrate forces --max-workers 1 (was {args.max_workers}); "
                  "parallel integration is not yet supported.")
        args.max_workers = 1
        integrator = make_integrator(repo)

    store = TaskStore.open(repo)
    ingest = ingest_roadmap(store, roadmap)

    print(BANNER.format(version=__version__))
    print()
    print("  oxfaz (autonomous builder)")
    print(f"  repo          : {repo}")
    print(f"  roadmap       : {len(roadmap.get('tasks', []))} tasks "
          f"({ingest.added} new, {ingest.skipped} already known)")
    counts = store.status_counts()
    print(f"  taskstore     : {counts}")

    if args.dry_run:
        print("\n  DRY RUN — no workers spawned. Planned tasks:")
        for t in store.find_next_planned(limit=1000, redispatch_cap=args.no_progress_ticks + 999):
            print(f"   · [{t.priority}] {t.identifier}  {t.title}  ({t.kind})")
        print("\n✓ dry run complete")
        return 0

    # Real build: validate auth + claude, then run the loop with live workers.
    try:
        cfg = build_run_config(
            target=str(repo), output_dir=str(repo / "oxison-build"),
            bare=args.bare, api_key=args.api_key, model=args.model,
            max_budget_usd=args.worker_budget_usd,
            chunk_threshold=DEFAULT_CHUNK_THRESHOLD, max_concurrency=args.max_workers,
            resume=False,
        )
    except ConfigError as exc:
        print(f"oxison: config error: {exc}")
        return 2
    try:
        pre = preflight(cfg)
    except PreflightError as exc:
        print(f"oxison: preflight failed: {exc}")
        return 3

    engine_config = EngineConfig(
        worker_max_budget_usd=args.worker_budget_usd,
        sandbox_enabled=not args.no_sandbox,
        sandbox_layer=args.sandbox_layer,
    )
    # Preflight the sandbox: fail BEFORE the loop (not one tick in) if a
    # prerequisite is missing. If disabled, warn loudly on stderr — never silent.
    if not engine_config.sandbox_enabled:
        print(
            "oxison: WARNING — --no-sandbox: build workers run UNSANDBOXED with full "
            "filesystem + network access. Use only on trusted repos.",
            file=sys.stderr,
        )
    elif engine_config.sandbox_layer == "container":
        from .engine.container import image_exists, resolve_container_runtime
        runtime = resolve_container_runtime(engine_config.container_runtime)
        if runtime is None:
            print("oxison: container sandbox needs a runtime — install podman (or docker).")
            return 3
        if not asyncio.run(image_exists(runtime, engine_config.worker_image)):
            print(
                f"oxison: container sandbox image {engine_config.worker_image!r} not found.\n"
                f"  build it:  {runtime} build -t {engine_config.worker_image} "
                "docker/oxfaz-worker"
            )
            return 3
        if not cfg.api_key:
            print(
                "oxison: container sandbox needs an API key (bare-mode auth — the host "
                "Keychain isn't reachable inside the container).\n"
                "  set ANTHROPIC_API_KEY / OXISON_API_KEY, or pass --api-key."
            )
            return 3
    elif resolve_srt_binary(engine_config.srt_binary) is None:
        print(
            "oxison: build sandbox enabled but the srt runtime is not installed.\n"
            "  install:  npm i -g @anthropic-ai/sandbox-runtime\n"
            "  or run with --no-sandbox (ONLY on repos you fully trust)."
        )
        return 3
    wt_root = repo / "oxison-build" / "worktrees"
    log_root = repo / "oxison-build" / "logs"

    async def dispatcher(task: Task, branch: str) -> DispatchOutcome:
        return await launch_worker(
            repo, task_identifier=task.identifier, task_title=task.title,
            rationale=task.rationale, acceptance=task.acceptance,
            files_hint=task.files_touched, engine_config=engine_config,
            auth_mode=cfg.auth_mode, api_key=cfg.api_key, model=cfg.model,
            worktree_root=wt_root, log_path=log_root / f"{task.identifier}.log",
        )

    def grader(outcome: DispatchOutcome) -> Any:
        return grade_diff(outcome.changed_files, protected_paths=engine_config.protected_paths)

    options = LoopOptions(
        branch_prefix=engine_config.branch_prefix, max_workers=args.max_workers,
        max_ticks=args.max_ticks, budget_ceiling_usd=args.budget_ceiling_usd,
        no_progress_ticks=args.no_progress_ticks,
        redispatch_cap=engine_config.redispatch_cap,
        worker_budget_floor=engine_config.worker_max_budget_usd,
    )

    print(f"  claude CLI    : {pre.claude_version}")
    if not engine_config.sandbox_enabled:
        sandbox_status = "OFF (--no-sandbox)"
    elif engine_config.sandbox_layer == "container":
        sandbox_status = f"container (Layer 2, {engine_config.worker_image})"
    else:
        sandbox_status = "srt (Layer 1, filesystem + egress confined)"
    print(f"  sandbox       : {sandbox_status}")
    if integrator is not None:
        print("  integrate     : ON — each graded branch is merged into "
              f"{repo.name}'s current branch (main accumulates)")
    print("\n→ BUILD MODE — workers WRITE code in isolated worktrees under "
          "oxison-build/worktrees/\n")

    summary = asyncio.run(
        run_build_loop(store, options=options, dispatcher=dispatcher,
                       grader=grader, now_fn=_now_iso, now_epoch_fn=time.time,
                       integrator=integrator)
    )

    print(f"\n✓ build loop halted: {summary.halt_reason}")
    print(f"  ticks={summary.ticks} dispatched={summary.dispatched} "
          f"merged={summary.merged} failed={summary.failed} "
          f"integrated={summary.integrated} spent=${summary.spent_usd:.4f}")
    print(f"  taskstore: {store.status_counts()}")
    if integrator is not None and summary.integrated:
        print(f"  ✓ main now holds {summary.integrated} integrated task(s)")
    return 0


def _load_pipeline_runner() -> Callable[[RunConfig, RunManifest], Coroutine[Any, Any, int]] | None:
    """Return ``oxison.pipeline.run_pipeline`` if available, else None."""
    try:
        module = importlib.import_module("oxison.pipeline")
    except ImportError:
        return None
    runner: Callable[[RunConfig, RunManifest], Coroutine[Any, Any, int]] = module.run_pipeline
    return runner


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        print(BANNER.format(version=__version__))
        print()
        parser.print_help()
        return 0
    func = args.func
    result: int = func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
