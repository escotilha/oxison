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
import contextlib
import getpass
import importlib
import os
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
from .credentials import (
    CredentialError,
    delete_saved_key,
    detect_backend,
    get_saved_key,
    saved_key_status,
    set_saved_key,
)
from .manifest import RunManifest
from .preflight import PreflightError, preflight
from .providers import Provider, provider_names, resolve_provider, resolve_provider_token

BANNER = r"""
   ____  _  _  ____  ___   __  __ _
  /  _ \( \/ )(_  _)/ __) /  \(  ( \
 (  ( ) ))  (  _)(_ \__ \(  O )    /
  \_)(_/(_/\_)(____)(___/ \__/\_)__)   oxison v{version}
  point it at a repo - get the product docs back
""".strip("\n")

# Help text for the --api-key flag, shared across subparsers. The DANGER note
# steers users to the env var (SECURITY-AUDIT.md F2): a flag value is visible in
# `ps`/argv for the parent process lifetime; an env var is not.
_API_KEY_HELP = (
    "API key for bare mode. DANGER: visible in ps/argv for the process "
    "lifetime; prefer the OXISON_API_KEY / ANTHROPIC_API_KEY env var"
)


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
    run_p.add_argument("--api-key", default=None, help=_API_KEY_HELP)
    run_p.add_argument("--model", default=None, help="override the Claude model")
    run_p.add_argument(
        "--provider", default=None, choices=provider_names(),
        help="run via a non-Anthropic provider (Anthropic-compatible endpoint): "
             "%(choices)s. Reads the provider key from its env var or --api-key; "
             "defaults the model to the provider's, override with --model.",
    )
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
        "--relevance-min-score",
        type=float,
        default=0.25,
        help="prune tasks the planner self-scored below this relevance floor "
             "(0.0-1.0, default: 0.25); pass 0 to keep every task",
    )
    plan_p.add_argument(
        "--bare",
        action="store_true",
        help="use --bare auth (ANTHROPIC_API_KEY) instead of your Claude Code login",
    )
    plan_p.add_argument("--api-key", default=None, help=_API_KEY_HELP)
    plan_p.add_argument("--model", default=None, help="override the Claude model")
    plan_p.add_argument(
        "--provider", default=None, choices=provider_names(),
        help="run via a non-Anthropic provider (Anthropic-compatible endpoint): "
             "%(choices)s. Reads the provider key from its env var or --api-key; "
             "defaults the model to the provider's, override with --model.",
    )
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
        "--relevance-min-score", type=float, default=0.25,
        help="prune tasks the planner self-scored below this relevance floor "
             "(0.0-1.0, default: 0.25); pass 0 to keep every task",
    )
    ideate_p.add_argument(
        "--bare", action="store_true",
        help="use --bare auth (ANTHROPIC_API_KEY) instead of your Claude Code login",
    )
    ideate_p.add_argument("--api-key", default=None, help=_API_KEY_HELP)
    ideate_p.add_argument("--model", default=None, help="override the Claude model")
    ideate_p.add_argument(
        "--provider", default=None, choices=provider_names(),
        help="run via a non-Anthropic provider (Anthropic-compatible endpoint): "
             "%(choices)s. Reads the provider key from its env var or --api-key; "
             "defaults the model to the provider's, override with --model.",
    )
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
        "--scaffold", action="store_true",
        help="greenfield: if --repo is empty/absent, git-init it with an initial "
             "commit first, so the build loop can implement a from-scratch roadmap "
             "(e.g. one produced by `oxison ideate`) into a fresh repo. Refuses a "
             "non-empty non-git dir.",
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
    build_p.add_argument(
        "--test-cmd", default=None,
        help="REGRESSION GUARD (opt-in): the project's own test command (e.g. "
             "'pytest -q' or 'npm test'). When set, the engine runs it under the "
             "same build sandbox on a baseline worktree and on each graded change, "
             "rejecting a change that turns a passing suite red. Off by default. "
             "Not supported with --sandbox-layer container.")
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
    build_p.add_argument("--protected-branches", default="main,master",
                         help="comma-separated branches --integrate must never advance "
                              "in place (it redirects onto an integration branch instead). "
                              "Default: main,master. Set e.g. 'main,develop,trunk'.")
    build_p.add_argument("--worker-skills", action="store_true",
                         help="let build workers invoke a curated generic skill subset "
                              "(cto, review-changes, verify, test-and-fix, first-principles) "
                              "via a scoped CLAUDE_CONFIG_DIR. Layer-1 (srt) + token auth only "
                              "(--api-key/--provider); off under host OAuth.")
    build_p.add_argument("--critic", action="store_true",
                         help="add an AI critic: after the deterministic grader passes, a "
                              "read-only review judges each diff against the task's acceptance "
                              "criteria and can reject it. One extra AI call per graded task.")
    build_p.add_argument("--bare", action="store_true",
                         help="use --bare auth (ANTHROPIC_API_KEY) instead of your login")
    build_p.add_argument("--api-key", default=None, help=_API_KEY_HELP)
    build_p.add_argument("--model", default=None, help="override the Claude model")
    build_p.add_argument(
        "--provider", default=None, choices=provider_names(),
        help="build via a non-Anthropic provider (Anthropic-compatible endpoint): "
             "%(choices)s. Reads the provider key from its env var or --api-key; "
             "defaults the model to the provider's, override with --model. "
             "Sandboxed build workers auto-allow the provider's API host.",
    )
    build_p.add_argument(
        "--no-memory", action="store_true",
        help="disable cross-run memory (default: on — capture grader-verified "
             "outcomes to oxison-build/memory.db and inject relevant priors into "
             "workers; scoped to this repo)",
    )
    build_p.set_defaults(func=cmd_build)

    auth_p = sub.add_parser(
        "auth", help="manage saved provider API keys (OS keychain, file fallback)"
    )
    auth_sub = auth_p.add_subparsers(dest="auth_cmd")
    auth_set = auth_sub.add_parser(
        "set", help="save a provider key (prompts hidden unless --api-key is given)"
    )
    auth_set.add_argument("provider", choices=provider_names())
    auth_set.add_argument(
        "--api-key", default=None,
        help="the key to save (omit to be prompted; hidden input)",
    )
    auth_set.set_defaults(func=cmd_auth_set)
    auth_status = auth_sub.add_parser(
        "status", help="show which provider keys are saved / detected in the env"
    )
    auth_status.set_defaults(func=cmd_auth_status)
    auth_rm = auth_sub.add_parser("rm", help="delete a saved provider key")
    auth_rm.add_argument("provider", choices=provider_names())
    auth_rm.set_defaults(func=cmd_auth_rm)
    # `oxison auth` with no subcommand → show status
    auth_p.set_defaults(func=cmd_auth_status)

    ver_p = sub.add_parser("version", help="print version + banner")
    ver_p.set_defaults(func=cmd_version)

    return parser


def _prompt_and_maybe_save(prov: Provider) -> str | None:
    """Interactively prompt for a provider key (hidden) and offer to save it.

    Returns the entered key (saved or not) or None if the user gave nothing.
    Only call this on an interactive terminal — the caller gates on isatty.
    """
    print(f"  no {prov.token_envs[0]} found for provider '{prov.name}'.")
    try:
        key = getpass.getpass(f"  Paste your {prov.name} API key (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not key:
        return None
    try:
        ans = input("  Save it for next time? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        ans = "n"
    if ans in ("", "y", "yes"):
        try:
            backend = set_saved_key(prov.name, key)
            print(f"  ✓ saved to {backend} — future runs won't ask")
        except CredentialError as exc:
            print(f"  ! could not save ({exc}); using the key for this run only")
    return key


def _resolve_provider_key(args: argparse.Namespace) -> str | None:
    """Resolve the key to hand the config builder as ``api_key=``.

    No provider selected → return the plain ``--api-key`` (existing behavior is
    byte-for-byte unchanged). Provider selected → funnel: ``--api-key`` > env var
    > saved keystore > interactive prompt (TTY only). Returns None when nothing
    resolves and we can't prompt (headless), so the config builder raises its
    clear "requires an API key" error instead of hanging on a prompt.
    """
    provider_name = getattr(args, "provider", None)
    explicit = getattr(args, "api_key", None)
    if not provider_name:
        return explicit
    prov = resolve_provider(provider_name)
    if prov is None:  # argparse choices already validated; defensive
        return explicit
    key = resolve_provider_token(prov, explicit, env=None)  # --api-key > env
    if key:
        return key
    key = get_saved_key(prov.name)  # saved keystore
    if key:
        return key
    if sys.stdin.isatty():  # interactive prompt + optional save
        return _prompt_and_maybe_save(prov)
    return None  # headless: let the builder raise the clear error


def _warn_if_ocr(args: argparse.Namespace) -> None:
    """Warn (once, to stderr) when ``--ocr`` is enabled.

    The OCR adapter lazy-imports an optional ``document_extraction`` package and
    runs its ``process_document`` on user files **in the main oxison process** —
    before any sandbox or AI stage. That code executes with the invoking user's
    full privileges, so the user must trust whatever provides that import. This
    is a deliberate opt-in (the import is never an oxison dependency); the warning
    just makes the trust boundary explicit at enable time.
    """
    if getattr(args, "ocr", False):
        print(
            "oxison: WARNING: --ocr executes third-party document_extraction code "
            "with your full privileges, in the main process before any sandbox. "
            "Only enable it with an OCR stack you trust.",
            file=sys.stderr,
        )


def cmd_version(_args: argparse.Namespace) -> int:
    print(BANNER.format(version=__version__))
    return 0


def cmd_auth_set(args: argparse.Namespace) -> int:
    prov = resolve_provider(args.provider)
    if prov is None:  # unreachable: argparse choices validates the name
        print(f"oxison: unknown provider {args.provider!r}")
        return 2
    if args.api_key:
        key = args.api_key.strip()
    else:
        try:
            key = getpass.getpass(f"Paste your {prov.name} API key (hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 2
    if not key:
        print("oxison: no key provided")
        return 2
    try:
        backend = set_saved_key(prov.name, key)
    except CredentialError as exc:
        print(f"oxison: {exc}")
        return 1
    print(f"✓ saved {prov.name} key to {backend}")
    return 0


def cmd_auth_status(_args: argparse.Namespace) -> int:
    print(f"credential backend: {detect_backend()}")
    for name in provider_names():
        prov = resolve_provider(name)
        if prov is None:  # unreachable: provider_names() yields known names
            continue
        # saved_key_status carries no key-derived data, so nothing here can echo
        # any part of the secret (CodeQL py/clear-text-logging).
        present, backend = saved_key_status(name)
        env_var = next((v for v in prov.token_envs if os.environ.get(v)), None)
        saved = f"saved ✓ ({backend})" if present else "not saved"
        env_note = f"; env {env_var} set" if env_var else ""
        print(f"  {name:6} {saved}{env_note}")
    return 0


def cmd_auth_rm(args: argparse.Namespace) -> int:
    if delete_saved_key(args.provider):
        print(f"✓ removed saved {args.provider} key")
    else:
        print(f"no saved {args.provider} key to remove")
    return 0


def _print_plan_summary(cfg: RunConfig, manifest: RunManifest, claude_version: str) -> None:
    print(BANNER.format(version=__version__))
    print()
    print(f"  target        : {cfg.target}")
    print(f"  output        : {cfg.output_dir}")
    print(f"  auth mode     : {cfg.auth_mode}")
    if cfg.provider:
        print(f"  provider      : {cfg.provider}")
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
        _warn_if_ocr(args)
        cfg = build_run_config(
            target=args.target,
            output_dir=args.output_dir,
            bare=args.bare,
            api_key=_resolve_provider_key(args),
            model=args.model,
            max_budget_usd=args.max_budget_usd,
            chunk_threshold=args.chunk_threshold,
            max_concurrency=args.max_concurrency,
            resume=args.resume,
            provider=args.provider,
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
            api_key=_resolve_provider_key(args),
            model=args.model,
            max_budget_usd=args.max_budget_usd,
            chunk_threshold=DEFAULT_CHUNK_THRESHOLD,
            max_concurrency=1,
            resume=False,
            provider=args.provider,
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
    if cfg.provider:
        print(f"  provider      : {cfg.provider}")
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
                relevance_min_score=args.relevance_min_score,
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
    if result.pruned:
        print(
            f"  ⤵ {len(result.pruned)} low-relevance task(s) pruned "
            "(speculative / off-target):"
        )
        for task in result.pruned:
            print(f"      · {task.title}  (relevance {task.relevance:.2f})")
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
        _warn_if_ocr(args)
        cfg = build_greenfield_config(
            output_dir=args.output_dir,
            bare=args.bare,
            api_key=_resolve_provider_key(args),
            model=args.model,
            max_budget_usd=args.max_budget_usd,
            brief=brief,
            urls=list(args.url),
            provider=args.provider,
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
    if cfg.provider:
        print(f"  provider      : {cfg.provider}")
    print(f"  model         : {cfg.model or '(claude default)'}")
    print(f"  claude CLI    : {pre.claude_version}")
    print()

    rc = asyncio.run(
        greenfield_pipeline(
            cfg,
            user_guidance=user_guidance,
            max_tasks=args.max_tasks,
            relevance_min_score=args.relevance_min_score,
        )
    )
    if rc == 0:
        from .oxipensa import ROADMAP_JSON_FILENAME
        roadmap_path = cfg.output_dir / ROADMAP_JSON_FILENAME
        if roadmap_path.is_file():
            print("\n→ scaffold a fresh repo and build this roadmap:")
            print(f"    oxison build {roadmap_path} --repo ./<new-dir> --scaffold")
    return rc


def _scaffold_repo(repo: Path) -> int:
    """git-init a fresh repo at ``repo`` with one initial commit so ``oxison build``
    has a base to implement a greenfield roadmap into (the build loop needs a
    HEAD to branch workers from). Refuses a non-empty, non-git directory — it will
    only scaffold an empty or not-yet-existing path, never over existing files."""
    import subprocess

    if repo.exists() and any(repo.iterdir()):
        print(f"oxison: --scaffold target is non-empty and not a git repo: {repo}\n"
              "  point --repo at an empty or new directory (won't scaffold over files).")
        return 2
    repo.mkdir(parents=True, exist_ok=True)

    def _git(*a: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True, check=False)

    if _git("init", "-q").returncode != 0:
        print(f"oxison: --scaffold: git init failed in {repo}")
        return 3
    (repo / "README.md").write_text(
        f"# {repo.name}\n\nScaffolded by `oxison build --scaffold`; the build loop "
        "implements the roadmap into this repo.\n", encoding="utf-8")
    _git("add", "-A")
    # Inline identity so the initial commit succeeds even where the host git has
    # no user.name/email configured (a fresh CI/container).
    commit = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=oxison",
         "-c", "user.email=oxison@localhost", "commit", "-qm", "scaffold: initial commit"],
        capture_output=True, text=True, check=False,
    )
    if commit.returncode != 0:
        print(f"oxison: --scaffold: initial commit failed: {commit.stderr.strip()[:200]}")
        return 3
    print(f"  scaffolded a fresh git repo at {repo} (initial commit)")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Oxfaz: ingest a roadmap.json and run the autonomous build loop."""
    from .engine.dispatch import DispatchOutcome, launch_worker
    from .engine.engconfig import EngineConfig
    from .engine.gates import GradeVerdict, grade_diff
    from .engine.loop import LoopOptions, run_build_loop
    from .engine.roadmap_ingest import (
        RoadmapIngestError,
        ingest_roadmap,
        load_roadmap,
    )
    from .engine.sandbox import resolve_srt_binary
    from .engine.taskstore import Task, TaskStore
    from .memory import (
        MemoryConfig,
        MemoryStore,
        build_memory_block,
        capture_from_outcome,
        memory_query_for_task,
    )

    repo = Path(args.repo).expanduser().resolve()
    if args.scaffold and not (repo / ".git").exists():
        scaffold_rc = _scaffold_repo(repo)
        if scaffold_rc != 0:
            return scaffold_rc
    if not (repo / ".git").exists():
        hint = " (pass --scaffold to git-init a fresh one for a greenfield build)"
        print(f"oxison: --repo is not a git repository: {repo}{hint}")
        return 2

    try:
        roadmap = load_roadmap(Path(args.roadmap).expanduser().resolve())
    except RoadmapIngestError as exc:
        print(f"oxison: {exc}")
        return 2

    # Protected-path gate for direct-build (SECURITY-AUDIT.md F5). The planner
    # path runs the full plan-gate before a roadmap is ever written; a hand-crafted
    # or tampered roadmap.json fed straight to `oxison build` skipped it, so a task
    # could declare a protected files_hint (a lockfile, CI config, .git/) and spend
    # worker budget before the grader's post-diff backstop caught the write. Reject
    # at ingest time instead — fail early, before any dispatch. The grader
    # (grade_diff, same is_protected_path matcher) remains the authoritative
    # backstop on the actual diff; this only narrows the window. We check the raw
    # files_hint with the identical coercion ingest_roadmap uses, so the gate sees
    # exactly the paths that would be persisted.
    from .engine.protected import is_protected_path

    protected = EngineConfig().protected_paths
    protected_hits: list[str] = []
    for t in roadmap.get("tasks", []):
        if not isinstance(t, dict):
            continue
        ident = t.get("identifier") or t.get("title") or "?"
        for fpath in t.get("files_hint", []):
            if isinstance(fpath, str) and is_protected_path(fpath, protected):
                protected_hits.append(f"{ident}: {fpath}")
    if protected_hits:
        print(
            "oxison: roadmap rejected — tasks target protected paths "
            "(lockfiles / CI / .git etc.); refusing to dispatch:"
        )
        for hit in protected_hits:
            print(f"   · {hit}")
        return 2

    # --integrate composes the roadmap into one product as each graded branch is
    # git-merged. It requires sequential dispatch (so each task branches from the
    # accumulated tip), so it forces --max-workers 1. To honour "never write main
    # directly", when the repo sits on a protected branch (main/master) the loop
    # composes onto a dedicated integration branch and leaves the live branch
    # untouched for the user to merge; the original branch is restored at the end.
    integrator = None
    integration_target: str | None = None      # branch we compose onto, if redirected
    restore_branch: str | None = None          # branch to switch back to at the end
    restore_failed = False                      # set if the end-of-run restore fails
    if args.integrate:
        import subprocess

        from .engine.integrate import (
            INTEGRATION_BRANCH,
            current_branch,
            ensure_integration_branch,
            make_integrator,
        )

        # Branches --integrate must never advance in place (configurable; the
        # backstop in integrate_branch is armed with the same set).
        protected_branches = frozenset(
            b.strip() for b in args.protected_branches.split(",") if b.strip()
        )

        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            print(
                "oxison: --integrate needs a clean working tree (uncommitted changes "
                "would block the fast-forward merge).\n"
                "  commit or stash them, then re-run."
            )
            return 2
        if args.max_workers != 1:
            print(f"  note: --integrate forces --max-workers 1 (was {args.max_workers}); "
                  "parallel integration is not yet supported.")
        args.max_workers = 1
        # Redirect off a protected branch onto a dedicated one (never advance main
        # in place). Skipped on --dry-run (no side effects). Detached HEAD fails
        # early rather than once-per-task. On a non-protected branch, integrate
        # onto it as before.
        if not args.dry_run:
            live = asyncio.run(current_branch(repo))
            if live is None:
                print("oxison: --integrate needs a checked-out branch (the repo is in "
                      "detached HEAD). Check out a branch, then re-run.")
                return 2
            if live in protected_branches:
                ok, msg = asyncio.run(
                    ensure_integration_branch(
                        repo, base_branch=live, integration_branch=INTEGRATION_BRANCH
                    )
                )
                if not ok:
                    print(f"oxison: {msg}")
                    return 3
                integration_target = INTEGRATION_BRANCH
                restore_branch = live
        integrator = make_integrator(repo, protected_branches=protected_branches)

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
            bare=args.bare, api_key=_resolve_provider_key(args), model=args.model,
            max_budget_usd=args.worker_budget_usd,
            chunk_threshold=DEFAULT_CHUNK_THRESHOLD, max_concurrency=args.max_workers,
            resume=False, provider=args.provider,
        )
    except ConfigError as exc:
        print(f"oxison: config error: {exc}")
        return 2
    try:
        pre = preflight(cfg)
    except PreflightError as exc:
        print(f"oxison: preflight failed: {exc}")
        return 3

    # Provider mode: carry the auth overlay into the worker env, and widen the
    # sandbox egress allowlist so a sandboxed worker can reach the provider's
    # API host (otherwise Layer-1 srt blocks it and the build fails by default).
    prov = resolve_provider(args.provider)
    sandbox_domains: tuple[str, ...] = ()
    if prov is not None and prov.sandbox_domains:
        from .engine.sandbox import DEFAULT_SANDBOX_DOMAINS
        sandbox_domains = DEFAULT_SANDBOX_DOMAINS + prov.sandbox_domains

    # Regression guard (opt-in via --test-cmd). The container layer has a
    # different workspace model (a clone, not the worktree), so the guard's
    # srt-on-worktree mechanism doesn't apply there — warn + disable rather than
    # silently mislead. In --no-sandbox mode the guard runs the command bare.
    test_cmd = args.test_cmd
    if test_cmd and not args.no_sandbox and args.sandbox_layer == "container":
        print("oxison: --test-cmd (regression guard) is not supported with "
              "--sandbox-layer container; it is ignored for this run.", file=sys.stderr)
        test_cmd = None

    engine_config = EngineConfig(
        worker_max_budget_usd=args.worker_budget_usd,
        sandbox_enabled=not args.no_sandbox,
        sandbox_layer=args.sandbox_layer,
        provider_env=cfg.provider_env,
        sandbox_allowed_domains=sandbox_domains,
        worker_skills=args.worker_skills,
        pre_push_test_command=test_cmd,
    )
    # Worker-skills is enforced at dispatch (token auth + Layer-1 only); surface
    # the gates here so an operator who asked for it isn't silently ignored.
    if args.worker_skills:
        token_auth = bool(cfg.api_key) or bool(cfg.provider_env)
        if not token_auth:
            print("oxison: --worker-skills needs token auth (--api-key or --provider); "
                  "it is OFF under host OAuth login.", file=sys.stderr)
        elif engine_config.sandbox_layer == "container":
            print("oxison: --worker-skills is Layer-1 (srt) only — a container worker "
                  "can't see host skills, so it is ignored under --sandbox-layer container.",
                  file=sys.stderr)
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
        if not cfg.api_key and not cfg.provider_env:
            print(
                "oxison: container sandbox needs token auth (bare-mode — the host "
                "Keychain isn't reachable inside the container).\n"
                "  set ANTHROPIC_API_KEY / OXISON_API_KEY, pass --api-key, or "
                "select a provider (e.g. --provider kimi) with its key set."
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

    # Regression guard — constructed only when --test-cmd is set. Establishes a
    # baseline test run once, then gates each graded worktree on green→red. See
    # engine/regression.py. Cleaned up in the teardown finally below.
    regression_verifier = None
    if engine_config.pre_push_test_command:
        from .engine.regression import RegressionVerifier
        regression_verifier = RegressionVerifier(
            repo=repo, engine_config=engine_config,
            work_dir=repo / "oxison-build" / "regression",
        )

    # Cross-run memory (default on; --no-memory disables). Scope = repo name so
    # priors never cross between projects. Keyword-only (no embedder dependency);
    # abstains below MemoryConfig.abstain_min_score so a weak match injects nothing.
    mem_store = None if args.no_memory else MemoryStore.open(repo)
    mem_config = MemoryConfig()

    async def dispatcher(task: Task, branch: str) -> DispatchOutcome:
        memory_block = ""
        if mem_store is not None:
            memory_block = build_memory_block(
                mem_store, query=memory_query_for_task(task), scope=repo.name,
                now=_now_iso(), config=mem_config, task_kind=task.kind,
            )
        return await launch_worker(
            repo, task_identifier=task.identifier, task_title=task.title,
            rationale=task.rationale, acceptance=task.acceptance,
            files_hint=task.files_touched, engine_config=engine_config,
            auth_mode=cfg.auth_mode, api_key=cfg.api_key, model=cfg.model,
            worktree_root=wt_root, log_path=log_root / f"{task.identifier}.log",
            memory_block=memory_block,
        )

    async def grader(outcome: DispatchOutcome) -> Any:
        base = grade_diff(
            outcome.changed_files,
            protected_paths=engine_config.protected_paths,
            diff_size_cap=engine_config.grader_diff_size_cap,
        )
        # A structural rejection (protected path / oversized / empty) short-circuits:
        # no point running the suite on a diff we're already rejecting. With no
        # --test-cmd, this returns exactly the old structural verdict.
        if not base.ok or regression_verifier is None:
            return base
        return await regression_verifier.check(outcome)

    def recorder(
        task: Task, outcome: DispatchOutcome, verdict: GradeVerdict, merged: bool
    ) -> None:
        # Grader-gated capture (capture_from_outcome decides storable-or-not).
        if mem_store is None:
            return
        capture_from_outcome(
            mem_store, task=task, outcome=outcome, verdict=verdict,
            scope=repo.name, now=_now_iso(), merged=merged, config=mem_config,
        )

    options = LoopOptions(
        branch_prefix=engine_config.branch_prefix, max_workers=args.max_workers,
        max_ticks=args.max_ticks, budget_ceiling_usd=args.budget_ceiling_usd,
        no_progress_ticks=args.no_progress_ticks,
        redispatch_cap=engine_config.redispatch_cap,
        worker_budget_floor=engine_config.worker_max_budget_usd,
    )

    print(f"  claude CLI    : {pre.claude_version}")
    if cfg.provider:
        print(f"  provider      : {cfg.provider} (model: {cfg.model})")
    if not engine_config.sandbox_enabled:
        sandbox_status = "OFF (--no-sandbox)"
    elif engine_config.sandbox_layer == "container":
        sandbox_status = f"container (Layer 2, {engine_config.worker_image})"
    else:
        sandbox_status = "srt (Layer 1, filesystem + egress confined)"
    print(f"  sandbox       : {sandbox_status}")
    if mem_store is not None:
        print(f"  memory        : on ({len(mem_store.live_in_scope(repo.name))} in scope)")
    else:
        print("  memory        : off (--no-memory)")
    if regression_verifier is not None:
        print(f"  regression    : on (test-cmd: {engine_config.pre_push_test_command!r})")
    if integrator is not None:
        if integration_target is not None:
            print(f"  integrate     : ON — composing onto {integration_target!r}; "
                  f"protected {restore_branch!r} is left untouched (merge it when ready)")
        else:
            print("  integrate     : ON — each graded branch is fast-forwarded onto "
                  "the current branch")
    # AI critic (opt-in): a read-only review gate after the deterministic grader.
    critic = None
    if args.critic:
        from .engine.critic import make_critic
        critic = make_critic(cfg)
        print("  critic        : ON — each graded diff is AI-reviewed against its "
              "acceptance criteria before acceptance")
    print("\n→ BUILD MODE — workers WRITE code in isolated worktrees under "
          "oxison-build/worktrees/\n")

    try:
        summary = asyncio.run(
            run_build_loop(store, options=options, dispatcher=dispatcher,
                           grader=grader, now_fn=_now_iso, now_epoch_fn=time.time,
                           integrator=integrator,
                           recorder=recorder if mem_store is not None else None,
                           critic=critic)
        )
    finally:
        if regression_verifier is not None:
            # Remove the baseline worktree (fail-soft — never mask a loop error).
            with contextlib.suppress(Exception):
                asyncio.run(regression_verifier.cleanup())
        if mem_store is not None:
            mem_store.close()
        # Restore the user's original branch; the composed work stays on the
        # integration branch for them to merge. Best-effort — never mask a loop error.
        if restore_branch is not None:
            import subprocess
            rc = subprocess.run(
                ["git", "-C", str(repo), "checkout", restore_branch],
                capture_output=True, text=True, check=False,
            )
            if rc.returncode != 0:
                restore_failed = True
                print(f"  note: could not restore branch {restore_branch!r} (you are on "
                      f"{integration_target!r}): {rc.stderr.strip()[:160]}", file=sys.stderr)

    print(f"\n✓ build loop halted: {summary.halt_reason}")
    print(f"  ticks={summary.ticks} dispatched={summary.dispatched} "
          f"merged={summary.merged} failed={summary.failed} "
          f"integrated={summary.integrated} spent=${summary.spent_usd:.4f}")
    print(f"  taskstore: {store.status_counts()}")
    if integrator is not None and summary.integrated:
        if integration_target is not None and not restore_failed:
            print(f"  ✓ {integration_target!r} holds {summary.integrated} integrated "
                  f"task(s) — review, then merge into {restore_branch!r}:")
            print(f"      git merge {integration_target}")
        elif integration_target is not None:
            # Restore failed — the user is on the integration branch, not
            # restore_branch, so don't tell them to "merge into" it from there.
            print(f"  ✓ {integration_target!r} holds {summary.integrated} integrated "
                  f"task(s) (you are on it; the restore to {restore_branch!r} failed above).")
        else:
            print(f"  ✓ the current branch now holds {summary.integrated} integrated task(s)")
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
