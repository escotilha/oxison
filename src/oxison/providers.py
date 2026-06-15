"""Named model providers — Anthropic-compatible endpoints oxison can target.

oxison drives ``claude -p``, which speaks the Anthropic Messages API. Several
non-Anthropic models expose an **Anthropic-compatible** endpoint, so oxison can
use them by pointing the child ``claude`` at a different base URL + auth token
(``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_AUTH_TOKEN``) and passing the provider's
model id via ``--model``.

The worker env builder (``dispatch.build_env``) **deliberately strips inherited
``ANTHROPIC_*`` vars** — a secrets boundary, and a guard against the parent env
silently overriding per-call settings (see ``dispatch.py`` docstring item 4). So
a provider is never picked up from the ambient env; it is selected explicitly via
``--provider`` and oxison **constructs** the overlay here, passing it through the
one documented channel (``build_env(..., extra=...)``). This keeps the boundary
intact: only vars oxison built from an explicit choice ever reach the child.

Adding a provider is one ``Provider(...)`` entry below.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ProviderError(ValueError):
    """Raised for an unknown provider name. Subclass of ``ValueError`` so the
    CLI's existing ``ConfigError``/``ValueError`` handling surfaces it cleanly."""


@dataclass(frozen=True)
class Provider:
    """An Anthropic-compatible model backend.

    ``extra_env`` and ``sandbox_domains`` are tuples-of-pairs / tuples (not
    dicts) so the dataclass stays hashable + frozen-safe.
    """

    name: str
    base_url: str
    #: Env vars read (in order) for the provider key when ``--api-key`` is absent.
    token_envs: tuple[str, ...]
    #: Model id used when the user doesn't pass ``--model``.
    default_model: str
    #: Extra child-env knobs the provider's docs recommend (key, value) pairs.
    extra_env: tuple[tuple[str, str], ...] = ()
    #: Egress hosts a sandboxed *build* worker must reach (merged into the srt
    #: allowlist). The Phase-1 read-only flows are unsandboxed and ignore this.
    sandbox_domains: tuple[str, ...] = ()


#: Kimi K2 (Moonshot). Native Anthropic-compatible endpoint; the docs use
#: ``ANTHROPIC_AUTH_TOKEN`` and set the two knobs below. ``platform.moonshot.ai``
#: now 301s to ``platform.kimi.ai`` — same provider.
KIMI = Provider(
    name="kimi",
    base_url="https://api.moonshot.ai/anthropic",
    token_envs=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
    default_model="kimi-k2.7-code",
    extra_env=(
        ("ENABLE_TOOL_SEARCH", "false"),
        ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "262144"),
    ),
    sandbox_domains=("api.moonshot.ai",),
)

#: xAI Grok. Native Anthropic-compatible ``/v1/messages`` endpoint at
#: ``api.x.ai`` (Bearer auth). Default is the stable flagship ``grok-4.3``; the
#: agentic-coding model ``grok-build-0.1`` is available via ``--model``.
GROK = Provider(
    name="grok",
    base_url="https://api.x.ai",
    token_envs=("XAI_API_KEY", "GROK_API_KEY"),
    default_model="grok-4.3",
    extra_env=(),
    sandbox_domains=("api.x.ai",),
)

PROVIDERS: dict[str, Provider] = {p.name: p for p in (KIMI, GROK)}


def provider_names() -> list[str]:
    """Sorted known provider names (for argparse ``choices`` + error messages)."""
    return sorted(PROVIDERS)


def resolve_provider(name: str | None) -> Provider | None:
    """Look up a provider by name. ``None`` in → ``None`` out (no provider).

    Raises ``ProviderError`` (a ``ValueError``) on an unknown name.
    """
    if name is None:
        return None
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ProviderError(
            f"unknown provider {name!r}; known providers: {', '.join(provider_names())}"
        ) from None


def resolve_provider_token(
    provider: Provider,
    explicit_key: str | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the provider key. Precedence: ``--api-key`` > the provider's
    ``token_envs`` (in order). Returns ``None`` if nothing is set."""
    if explicit_key:
        return explicit_key
    e = env if env is not None else os.environ
    for var in provider.token_envs:
        value = e.get(var)
        if value:
            return value
    return None


def provider_child_env(provider: Provider, token: str) -> dict[str, str]:
    """The child-env overlay that routes ``claude`` to this provider:
    ``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_AUTH_TOKEN`` + the provider's extra knobs."""
    env = {
        "ANTHROPIC_BASE_URL": provider.base_url,
        "ANTHROPIC_AUTH_TOKEN": token,
    }
    env.update(provider.extra_env)
    return env


__all__ = [
    "GROK",
    "KIMI",
    "PROVIDERS",
    "Provider",
    "ProviderError",
    "provider_child_env",
    "provider_names",
    "resolve_provider",
    "resolve_provider_token",
]
