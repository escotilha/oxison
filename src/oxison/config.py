"""Run configuration + target-repo resolution.

A ``RunConfig`` is the single immutable description of one oxison run:
where the target repo is, where artifacts go, how to authenticate, and
the cost/concurrency knobs. The CLI builds it once and threads it
through every stage; nothing downstream reads ``argparse.Namespace``
or the environment directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .providers import (
    ProviderError,
    provider_child_env,
    resolve_provider,
    resolve_provider_token,
)

AuthMode = Literal["oauth", "bare"]

#: Read-only tool set every comprehension/generation worker is limited
#: to. This constant is the mechanical expression of oxison's #1 safety
#: invariant — it must never contain a write- or exec-capable tool.
#: ``Bash`` is deliberately EXCLUDED: under ``--permission-mode
#: bypassPermissions`` a shell is a full write/exec primitive (``echo > f``,
#: ``rm``, ``curl | sh``), so it belongs to the write tier, not here. With
#: only ``Read``/``Glob``/``Grep`` a read-only worker is *structurally*
#: incapable of mutating the target repo. A unit test asserts the exclusion.
READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")

#: Default chunk threshold (estimated tokens). Above this, the repo is
#: sliced by top-level directory and map-reduced.
DEFAULT_CHUNK_THRESHOLD = 100_000

#: Default output directory name, created under the current working dir.
DEFAULT_OUTPUT_DIRNAME = "oxison-output"


class ConfigError(ValueError):
    """Raised when a target path or auth configuration is invalid."""


@dataclass(frozen=True)
class RunConfig:
    """Immutable, fully-resolved configuration for one oxison run."""

    target: Path
    output_dir: Path
    auth_mode: AuthMode
    api_key: str | None
    model: str | None
    max_budget_usd: float | None
    chunk_threshold: int
    max_concurrency: int
    resume: bool
    target_is_git: bool
    extra_sources: list[str] = field(default_factory=list)
    ocr_enabled: bool = False
    stt_key: str | None = None
    stt_provider: str = "openai"
    #: Greenfield (ideate) mode only: user-provided website links to fetch, and
    #: the plain-text project brief. Empty/None for the repo-based run/plan flows.
    urls: list[str] = field(default_factory=list)
    brief: str | None = None
    #: Selected non-Anthropic provider name (e.g. "kimi", "grok") or None for
    #: Anthropic. Display-only; the auth/routing lives in ``provider_env``.
    provider: str | None = None
    #: Provider child-env overlay (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN +
    #: knobs) as a tuple-of-pairs (frozen-safe). Empty = Anthropic auth. Passed
    #: into ``dispatch.build_env(extra=…)`` for every worker. Carries the token.
    provider_env: tuple[tuple[str, str], ...] = ()


def resolve_target(raw: str) -> Path:
    """Resolve a target-repo argument to an absolute, validated directory.

    Raises ``ConfigError`` (not a bare ``OSError``) so the CLI can print
    one actionable message rather than a traceback.
    """
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"target path does not exist: {path}")
    if not path.is_dir():
        raise ConfigError(f"target path is not a directory: {path}")
    return path


def resolve_staging(output_dir: Path) -> Path:
    """Create and return an empty staging dir to use as the worker ``cwd`` in
    greenfield mode (there is no repo).

    Workers are launched read-only (``READ_ONLY_TOOLS``), so an existing empty
    directory grants no write capability — it only gives ``claude -p`` a valid
    ``cwd`` to resolve Read/Glob/Grep against (which find nothing, as intended;
    the greenfield prompts tell the worker to reason from context, not files).
    """
    staging = (output_dir / ".oxison-staging").resolve()
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def resolve_api_key(explicit: str | None, env: dict[str, str] | None = None) -> str | None:
    """Resolve the API key from the explicit flag, then env vars.

    Precedence: ``--api-key`` > ``OXISON_API_KEY`` > ``ANTHROPIC_API_KEY``.
    """
    if explicit:
        return explicit
    e = env if env is not None else dict(os.environ)
    return e.get("OXISON_API_KEY") or e.get("ANTHROPIC_API_KEY") or None


def resolve_auth_mode(*, bare: bool, api_key: str | None) -> AuthMode:
    """Decide the auth mode.

    Default is OAuth (use the host's existing Claude Code login).
    ``--bare`` forces bare mode; supplying an API key also implies it,
    since a key is only meaningful in bare mode.
    """
    if bare or api_key:
        return "bare"
    return "oauth"


def _provider_overrides(
    provider_name: str | None,
    *,
    api_key: str | None,
    model: str | None,
    env: dict[str, str] | None,
) -> tuple[str | None, tuple[tuple[str, str], ...], str | None]:
    """Resolve a ``--provider`` selection into ``(name, overlay, model)``.

    Returns ``(None, (), model)`` when no provider is selected — Anthropic auth,
    unchanged. Otherwise resolves the provider key (precedence: ``--api-key`` >
    the provider's env vars), builds the child-env overlay, and defaults the
    model to the provider's default when ``--model`` was not given. Raises
    ``ConfigError`` on an unknown provider or a missing key. The caller forces
    ``auth_mode="bare"`` (token auth, not the host OAuth login) when a provider
    is set — the overlay carries ``ANTHROPIC_AUTH_TOKEN``, not ``ANTHROPIC_API_KEY``.
    """
    try:
        prov = resolve_provider(provider_name)
    except ProviderError as exc:
        raise ConfigError(str(exc)) from exc
    if prov is None:
        return None, (), model
    token = resolve_provider_token(prov, api_key, env=env)
    if not token:
        raise ConfigError(
            f"--provider {prov.name} requires an API key — set "
            f"{' or '.join(prov.token_envs)}, or pass --api-key."
        )
    overlay = tuple(provider_child_env(prov, token).items())
    return prov.name, overlay, (model or prov.default_model)


def build_run_config(
    *,
    target: str,
    output_dir: str | None,
    bare: bool,
    api_key: str | None,
    model: str | None,
    max_budget_usd: float | None,
    chunk_threshold: int,
    max_concurrency: int,
    resume: bool,
    provider: str | None = None,
    extra_sources: list[str] | None = None,
    ocr_enabled: bool = False,
    stt_key: str | None = None,
    stt_provider: str = "openai",
    env: dict[str, str] | None = None,
) -> RunConfig:
    """Assemble a validated ``RunConfig`` from raw CLI inputs."""
    resolved_target = resolve_target(target)
    prov_name, provider_env, model = _provider_overrides(
        provider, api_key=api_key, model=model, env=env
    )
    if prov_name is not None:
        # Provider mode: token auth via the overlay, never the host OAuth login.
        auth_mode: AuthMode = "bare"
        resolved_key: str | None = None
    else:
        resolved_key = resolve_api_key(api_key, env=env)
        auth_mode = resolve_auth_mode(bare=bare, api_key=resolved_key)
        if auth_mode == "bare" and not resolved_key:
            raise ConfigError(
                "bare mode requires an API key — set OXISON_API_KEY or "
                "ANTHROPIC_API_KEY, or drop --bare to use your Claude Code login."
            )
    out = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else Path.cwd() / DEFAULT_OUTPUT_DIRNAME
    )
    if max_concurrency < 1:
        raise ConfigError("--max-concurrency must be >= 1")
    if chunk_threshold < 1:
        raise ConfigError("--chunk-threshold must be >= 1")
    return RunConfig(
        target=resolved_target,
        output_dir=out,
        auth_mode=auth_mode,
        api_key=resolved_key if auth_mode == "bare" else None,
        model=model,
        max_budget_usd=max_budget_usd,
        chunk_threshold=chunk_threshold,
        max_concurrency=max_concurrency,
        resume=resume,
        target_is_git=(resolved_target / ".git").exists(),
        extra_sources=list(extra_sources or []),
        ocr_enabled=ocr_enabled,
        stt_key=stt_key,
        stt_provider=stt_provider,
        provider=prov_name,
        provider_env=provider_env,
    )


def build_greenfield_config(
    *,
    output_dir: str | None,
    bare: bool,
    api_key: str | None,
    model: str | None,
    max_budget_usd: float | None,
    brief: str | None,
    urls: list[str] | None = None,
    provider: str | None = None,
    extra_sources: list[str] | None = None,
    ocr_enabled: bool = False,
    stt_key: str | None = None,
    stt_provider: str = "openai",
    env: dict[str, str] | None = None,
) -> RunConfig:
    """Assemble a ``RunConfig`` for greenfield (ideate) mode — no repo target.

    Mirrors ``build_run_config`` but resolves an empty *staging* dir as the
    worker ``cwd`` instead of an existing repo, and carries the brief + URLs.
    Always single-pass, single-worker, no resume.
    """
    prov_name, provider_env, model = _provider_overrides(
        provider, api_key=api_key, model=model, env=env
    )
    if prov_name is not None:
        auth_mode: AuthMode = "bare"
        resolved_key: str | None = None
    else:
        resolved_key = resolve_api_key(api_key, env=env)
        auth_mode = resolve_auth_mode(bare=bare, api_key=resolved_key)
        if auth_mode == "bare" and not resolved_key:
            raise ConfigError(
                "bare mode requires an API key — set OXISON_API_KEY or "
                "ANTHROPIC_API_KEY, or drop --bare to use your Claude Code login."
            )
    out = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else Path.cwd() / DEFAULT_OUTPUT_DIRNAME
    )
    staging = resolve_staging(out)
    return RunConfig(
        target=staging,
        output_dir=out,
        auth_mode=auth_mode,
        api_key=resolved_key if auth_mode == "bare" else None,
        model=model,
        max_budget_usd=max_budget_usd,
        chunk_threshold=DEFAULT_CHUNK_THRESHOLD,
        max_concurrency=1,
        resume=False,
        target_is_git=False,
        extra_sources=list(extra_sources or []),
        ocr_enabled=ocr_enabled,
        stt_key=stt_key,
        stt_provider=stt_provider,
        urls=list(urls or []),
        brief=brief,
        provider=prov_name,
        provider_env=provider_env,
    )


__all__ = [
    "DEFAULT_CHUNK_THRESHOLD",
    "DEFAULT_OUTPUT_DIRNAME",
    "READ_ONLY_TOOLS",
    "AuthMode",
    "ConfigError",
    "RunConfig",
    "build_greenfield_config",
    "build_run_config",
    "resolve_api_key",
    "resolve_auth_mode",
    "resolve_staging",
    "resolve_target",
]
