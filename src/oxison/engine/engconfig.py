"""The engine constant surface — ``EngineConfig``.

This is *not* a behavioral module; it is the externalized set of constants
every engine module reads (first-principles §9). Its single invariant: no
module hardcodes a constant — every number/path/label is a field here with
a safe, generic, project-agnostic default.

Two defaults carry load-bearing rationale documented inline:

* ``worker_max_budget_usd`` is **non-None** and rejects ``None`` on the build
  path. Phase-1's CLI lets ``--max-budget-usd`` default to ``None``; the
  engine must not. The loop's budget ceiling (LP3) charges this cap as a
  *floor* to a timed-out worker — because ``dispatch._extract_cost`` returns
  ``0.0`` for a worker that was killed before emitting a ``result`` event. A
  ``None`` cap degenerates that floor back to ``$0.00`` and reopens the C3
  hole. This is the per-worker cap (always set), distinct from the run-level
  ``budget_ceiling_usd`` (allowed to be unset — LP1/LP2 backstop it).

* ``pre_push_test_command`` defaults to ``None`` meaning "discover the host
  project's own" — never a hardcoded ``ruff``/``pytest``, which would be a
  project-specific assumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Segment-anchored protected-path rules (consumed via ``protected.is_protected``).
# Each entry matches by path *segment*, never by ``str.startswith`` (see
# ``protected.py``). Generic and project-agnostic — no project-specific paths.
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    ".github/workflows/",
    ".env",  # also matches .env.local, .env.production (segment + dot-prefix rule)
    ".git/",
    ".ssh/",
    ".gnupg/",
    ".aws/",
    ".gcp/",
    "oxison-build/",  # the engine's own state — a plan must never touch it
    # dependency lockfiles
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "Cargo.lock",
    "uv.lock",  # oxison itself uses uv + ships this lockfile
    "go.sum",
    "Gemfile.lock",
    "Pipfile.lock",
    "composer.lock",
    # CI / pipeline definitions (a tampered pipeline is a supply-chain vector).
    # File rules match by exact segment name; directory rules end in "/".
    ".gitlab-ci.yml",
    ".circleci/",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "dependabot.yml",  # .github/dependabot.yml — matched by filename segment
)


@dataclass(frozen=True)
class EngineConfig:
    """Frozen constant surface for the build engine.

    Every field has a generic default; nothing here is project-specific.
    Construct with overrides per run; never mutate (frozen).
    """

    # --- dispatch / concurrency ---
    max_workers: int = 3
    redispatch_cap: int = 3
    """Max times a single task may be re-dispatched before being failed."""

    # --- locks ---
    lock_ttl_seconds: int = 4 * 60 * 60  # 4h
    orphan_min_age_seconds: int = 5 * 60  # 5min

    # --- safety: protected paths / risky dirs ---
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS
    risky_dirs: tuple[str, ...] = ()
    """Dirs that route a task to a human tier instead of auto-dispatch.

    Empty by default — opt-in only.
    """

    # --- planner / plan-gate ---
    plan_file_count_cap: int = 8
    """Reject a plan whose ``files_touched`` exceeds this — scope fence."""

    # --- grader ---
    grader_diff_size_cap: int = 1500
    """Reject a graded diff larger than this many changed lines — a scope fence so
    a runaway worker can't land a massive diff. Threaded into the grader via
    cmd_build's grader closure (#36)."""

    # --- branch / worker env contract ---
    branch_prefix: str = "feat/oxison-"
    # RESERVED, deliberately NOT injected (#36): the worker prompt instructs the
    # worker to never read env vars (credential safety), so injecting these would
    # be inert and contradictory. They name a future task-context-via-env contract
    # for if that constraint is ever reconciled — kept so the names stay stable.
    env_task_id: str = "OXISON_TASK_ID"
    env_branch: str = "OXISON_BRANCH"
    env_worktree: str = "OXISON_WORKTREE"

    # --- worker bounding (C3 / M1) ---
    worker_max_budget_usd: float = 5.0
    """Per-worker hard cap. MUST be non-None (see module docstring / C3).

    Charged as a floor to a timed-out worker by the loop's budget ceiling.
    A ``None`` here reopens the C3 hole, so the build path rejects it.
    """
    worker_timeout_seconds: int = 30 * 60  # 30min wall-clock per worker (M1)

    # --- worker sandbox ---
    sandbox_enabled: bool = True
    """Master on/off for the worker sandbox.

    On by default — the worker runs ``claude -p`` with write tools under
    ``bypassPermissions``, so unsandboxed it can write/exec outside its
    worktree. ``oxison build --no-sandbox`` flips this off for trusted local runs.
    """
    sandbox_layer: str = "srt"
    """Which sandbox when enabled: ``"srt"`` (Layer 1 — host OS allowlist, the
    default) or ``"container"`` (Layer 2 — the worker runs inside a rootless
    container whose only mount is its workspace, so the host filesystem is
    physically absent). Layer 2 needs a container runtime + the worker image +
    an API key (bare-mode auth; the Keychain isn't reachable in the VM)."""
    container_runtime: str | None = None
    """Container runtime for Layer 2. ``None`` = discover (podman, then docker)."""
    worker_image: str = "localhost/oxfaz-worker:latest"
    """Image the Layer-2 worker runs from (built from ``docker/oxfaz-worker``)."""
    sandbox_allowed_domains: tuple[str, ...] = ()
    """Network egress allowlist for a sandboxed worker. Empty here means "use
    ``sandbox.DEFAULT_SANDBOX_DOMAINS``" (api.anthropic.com + registries + git
    host); set to override. No TLS inspection, so keep it tight."""
    sandbox_extra_write_paths: tuple[str, ...] = ()
    """Extra absolute paths a worker may write (e.g. a shared build cache).
    Added on top of the worktree + scoped ``.git`` + ``~/.claude`` defaults."""
    srt_binary: str | None = None
    """srt executable. ``None`` = discover on PATH at preflight; set to pin."""

    # --- worker skills (Layer-1 srt + token auth only) ---
    worker_skills: bool = False
    """Let the build worker invoke a *curated* generic skill subset via the
    ``Skill`` tool. Gated at dispatch to TOKEN auth (``--api-key``/``--provider``)
    so the curated config dir needs no credential mirroring; under host OAuth the
    feature is off. Layer-1 (srt) only — container workers don't see host skills.
    Default off."""
    worker_skill_names: tuple[str, ...] = (
        "first-principles", "review-changes", "verify", "test-and-fix", "cto",
    )
    """The curated generic subset a worker may invoke. The worker sees ONLY these
    (via a dedicated CLAUDE_CONFIG_DIR), never the operator's full skill library —
    so project-specific skills are never exposed. A name absent from the host is
    silently skipped."""

    # --- loop guardrails (the three net-new aborts) ---
    no_progress_ticks: int = 5
    """LP2: halt after N consecutive ticks with no task advancing."""
    max_ticks: int | None = None
    """LP1: hard iteration cap; ``None`` = rely on no-progress + completion."""
    budget_ceiling_usd: float | None = None
    """LP3: run-level cost ceiling. ``None`` is allowed — distinct from the
    always-set per-worker cap above; LP1/LP2 backstop an unset ceiling."""

    # --- pre-push test gate (regression guard) ---
    pre_push_test_command: str | None = None
    """Host project's own test command. ``None`` = the regression guard is off;
    never a hardcoded ``ruff``/``pytest`` (that would be project-specific). When
    set, the engine runs it under the same srt sandbox as the worker — once on a
    baseline worktree, then on each graded worktree — and rejects a change that
    turns a passing suite red (see :mod:`engine.regression`)."""
    regression_timeout_seconds: int = 600
    """Wall-clock ceiling for a single regression test run. A run that exceeds
    it is killed (process-group) and treated as red, so a hanging suite can't
    stall the build loop."""

    # internal: extra env vars to whitelist into the worker child env
    extra_env_whitelist: tuple[str, ...] = field(default=())

    # internal: provider child-env overlay (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN
    # + knobs) when `oxison build --provider <name>` is used. Tuple-of-pairs to keep
    # the frozen dataclass hashable; empty = Anthropic (default) auth. Carried into
    # both Layer-1 (srt) and Layer-2 (container) worker envs via build_env(extra=…).
    provider_env: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        # C3: the per-worker cap must be a positive number, never None/0.
        # The field is typed ``float``, but a caller can still pass ``None``
        # at runtime (Python does not enforce annotations) — so the guard is
        # a real runtime check, not dead code. ``frozen=True`` blocks normal
        # assignment, so we validate by reading.
        cap = self.worker_max_budget_usd
        # Reject None, non-numeric, bool (a subclass of int — True would slip
        # through as 1.0), nan (all nan comparisons are False, so ``cap <= 0``
        # would not catch it — ``not (cap > 0)`` does), and inf (a meaningless
        # "floor" charge). ``isinstance(cap, bool)`` is checked first because
        # bool passes ``isinstance(_, int)``.
        if (
            isinstance(cap, bool)
            or not isinstance(cap, (int, float))
            or not (cap > 0)
            or cap == float("inf")
        ):
            raise ValueError(
                "worker_max_budget_usd must be a finite positive float (got "
                f"{cap!r}); None/zero/nan/inf degenerates the LP3 budget floor "
                "and reopens the C3 hole."
            )
