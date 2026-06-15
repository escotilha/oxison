"""Persistent provider-key store — keychain-first, file-fallback.

Makes `--provider` keys seamless: a key saved once is found on every later run,
so the user neither re-exports an env var nor passes ``--api-key`` each time.

Three backends, tried in priority order:

1. **macOS Keychain** via the ``security`` CLI (the platform default on Darwin).
2. **Linux libsecret** via ``secret-tool`` (when it's on ``PATH``).
3. **0600 JSON file** at ``$XDG_CONFIG_HOME/oxison/credentials`` (else
   ``~/.config/oxison/credentials``) — the portable fallback.

Design rules:

- **Fail-soft.** A missing tool, absent entry, or backend error degrades to
  "no key" / "not saved" — it never raises into the caller. The key store is a
  convenience layer; a failure just falls through to the next resolution step
  (env var, ``--api-key``, or the clear "no key" error).
- **Never log the key.** Callers display only the last 4 chars (``saved_key_status``).
- **No secret in argv on read.** ``security find-generic-password -w`` and
  ``secret-tool lookup`` print to stdout; only the macOS *write* path passes the
  key as an argument (``security … -w <key>``) — a brief, one-time, local exposure
  on the user's own machine, the same pattern the platform documents.
- **Zero new dependencies** — stdlib ``subprocess`` (argv form, like the rest of
  oxison) + a JSON file.

This module runs only in the *parent* oxison process to resolve a key. The key
then flows into the provider overlay exactly like ``--api-key`` (see
``providers.provider_child_env`` / ``dispatch.build_env``) — the worker/sandbox
boundary is unchanged.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import subprocess  # noqa: S404 — argv-form only, fixed binaries, no shell
from pathlib import Path

#: Keychain/secret-tool service name per provider, e.g. ``oxison-grok``.
_SERVICE_PREFIX = "oxison-"

_TIMEOUT_S = 10.0


def _service(provider: str) -> str:
    return f"{_SERVICE_PREFIX}{provider}"


def _account() -> str:
    """Keychain/secret-tool account — the login user (best-effort)."""
    try:
        return getpass.getuser()
    except Exception:
        return "oxison"


def _config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "oxison" / "credentials"


def last4(key: str) -> str:
    """Last 4 chars of a key, for non-leaking status display."""
    return key[-4:] if len(key) >= 4 else "?"


# --- macOS Keychain (security) -------------------------------------------------

def _kc_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("security") is not None


def _kc_get(provider: str) -> str | None:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _service(provider),
             "-a", _account(), "-w"],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    key = proc.stdout.strip()
    return key or None


def _kc_set(provider: str, key: str) -> bool:
    try:
        # -U updates an existing item instead of erroring on duplicate.
        proc = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", _service(provider),
             "-a", _account(), "-w", key],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _kc_del(provider: str) -> bool:
    try:
        proc = subprocess.run(
            ["security", "delete-generic-password", "-s", _service(provider),
             "-a", _account()],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# --- Linux libsecret (secret-tool) --------------------------------------------

def _st_available() -> bool:
    return platform.system() == "Linux" and shutil.which("secret-tool") is not None


def _st_get(provider: str) -> str | None:
    try:
        proc = subprocess.run(
            ["secret-tool", "lookup", "service", _service(provider),
             "account", _account()],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    key = proc.stdout.strip()
    return key or None


def _st_set(provider: str, key: str) -> bool:
    try:
        # `store` reads the secret from stdin — no secret in argv.
        proc = subprocess.run(
            ["secret-tool", "store", "--label", f"oxison {provider} API key",
             "service", _service(provider), "account", _account()],
            input=key, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _st_del(provider: str) -> bool:
    try:
        proc = subprocess.run(
            ["secret-tool", "clear", "service", _service(provider),
             "account", _account()],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# --- 0600 JSON file fallback ---------------------------------------------------

def _file_read_all() -> dict[str, dict[str, str]]:
    path = _config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_write_all(data: dict[str, dict[str, str]]) -> bool:
    path = _config_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Open with 0600 from the start so the secret never exists world-readable.
        fd = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.chmod(path, 0o600)  # tighten even if the file pre-existed with other perms
    except OSError:
        return False
    return True


def _file_get(provider: str) -> str | None:
    key = _file_read_all().get(provider, {}).get("api_key")
    return key or None


def _file_set(provider: str, key: str) -> bool:
    data = _file_read_all()
    data.setdefault(provider, {})["api_key"] = key
    return _file_write_all(data)


def _file_del(provider: str) -> bool:
    data = _file_read_all()
    if provider not in data:
        return False
    del data[provider]
    return _file_write_all(data)


# --- public API ----------------------------------------------------------------

def detect_backend() -> str:
    """The preferred *available* backend for writing: keychain | secret-tool | file."""
    if _kc_available():
        return "keychain"
    if _st_available():
        return "secret-tool"
    return "file"


def get_saved_key(provider: str) -> str | None:
    """First saved key found across backends (keychain/secret-tool, then file)."""
    if _kc_available():
        key = _kc_get(provider)
        if key:
            return key
    if _st_available():
        key = _st_get(provider)
        if key:
            return key
    return _file_get(provider)


def set_saved_key(provider: str, key: str) -> str:
    """Save ``key`` for ``provider`` to the preferred backend.

    Returns the backend name on success; raises ``CredentialError`` if the write
    fails or the key is empty.
    """
    key = key.strip()
    if not key:
        raise CredentialError("refusing to save an empty key")
    backend = detect_backend()
    ok = {
        "keychain": _kc_set,
        "secret-tool": _st_set,
        "file": _file_set,
    }[backend](provider, key)
    if not ok:
        # Last resort: if the OS keystore write failed, try the file backend so the
        # user isn't left unable to save at all.
        if backend != "file" and _file_set(provider, key):
            return "file"
        raise CredentialError(f"could not save the key via the {backend} backend")
    return backend


def delete_saved_key(provider: str) -> bool:
    """Remove a saved key from every backend. True if any backend had one."""
    removed = False
    if _kc_available() and _kc_del(provider):
        removed = True
    if _st_available() and _st_del(provider):
        removed = True
    if _file_del(provider):
        removed = True
    return removed


def saved_key_status(provider: str) -> tuple[bool, str | None, str | None]:
    """``(present, backend, last4)`` for ``provider`` — never returns the key itself."""
    if _kc_available():
        key = _kc_get(provider)
        if key:
            return True, "keychain", last4(key)
    if _st_available():
        key = _st_get(provider)
        if key:
            return True, "secret-tool", last4(key)
    key = _file_get(provider)
    if key:
        return True, "file", last4(key)
    return False, None, None


class CredentialError(RuntimeError):
    """A keystore write failed (all backends)."""


__all__ = [
    "CredentialError",
    "delete_saved_key",
    "detect_backend",
    "get_saved_key",
    "last4",
    "saved_key_status",
    "set_saved_key",
]
