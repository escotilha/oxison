"""Preflight checks — fail fast, before any tokens are spent.

oxison's whole value proposition is "clone and point at a repo." That
only works if the environment is ready. Preflight verifies the
``claude`` CLI is installed and (in OAuth mode) that credentials are
present, raising an actionable ``PreflightError`` *before* the pipeline
starts — never discovering a missing binary three AI calls deep.

No AI is invoked here. The cheapest real auth check (a 1-turn ping) is
deferred to the first comprehension call in Phase 1 so preflight stays
free.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .config import RunConfig


class PreflightError(RuntimeError):
    """A required precondition for running oxison is not met."""


@dataclass(frozen=True)
class PreflightResult:
    claude_path: str
    claude_version: str


def _run_version(binary: str) -> str:
    """Return ``claude --version`` output, or raise PreflightError."""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - covered via which() guard
        raise PreflightError(f"'{binary}' not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PreflightError(f"'{binary} --version' timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise PreflightError(
            f"'{binary} --version' exited {exc.returncode}: {exc.stderr.strip()}"
        ) from exc
    return proc.stdout.strip()


def check_claude_cli(binary: str = "claude") -> PreflightResult:
    """Verify the Claude Code CLI is installed and runnable."""
    path = shutil.which(binary)
    if path is None:
        raise PreflightError(
            f"the Claude Code CLI ('{binary}') is not installed or not on PATH.\n"
            "Install it from https://claude.com/claude-code and run "
            "`claude` once to sign in."
        )
    version = _run_version(path)
    return PreflightResult(claude_path=path, claude_version=version)


def preflight(cfg: RunConfig, *, binary: str = "claude") -> PreflightResult:
    """Run all preflight checks for a given run configuration.

    - Always: the ``claude`` CLI exists and reports a version.
    - Bare mode: token auth must be present — either an Anthropic ``api_key`` or
      a provider overlay (``provider_env`` carries ``ANTHROPIC_AUTH_TOKEN``).
      Already validated in ``build_run_config``; re-checked here defensively.
    - OAuth mode: we rely on the CLI's own credential store; a live
      auth probe is deferred to the first AI call to avoid spending
      tokens in preflight.
    """
    result = check_claude_cli(binary)
    if cfg.auth_mode == "bare" and not cfg.api_key and not cfg.provider_env:
        raise PreflightError(
            "bare mode selected but no API key resolved — set OXISON_API_KEY "
            "or ANTHROPIC_API_KEY, or drop --bare."
        )
    return result


__all__ = ["PreflightError", "PreflightResult", "check_claude_cli", "preflight"]
