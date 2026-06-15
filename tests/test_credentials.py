"""Tests for the provider-key store (``oxison.credentials``).

The OS-keychain / secret-tool backends are **mocked** — tests never touch the
real keychain. The file backend is exercised for real under a tmp config dir.
"""

from __future__ import annotations

import stat
import types

import pytest

from oxison import credentials as cred


@pytest.fixture
def file_backend(tmp_path, monkeypatch):
    """Force the portable file backend: no keychain, no secret-tool, tmp config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(cred, "_kc_available", lambda: False)
    monkeypatch.setattr(cred, "_st_available", lambda: False)
    return tmp_path


def test_detect_backend_file_when_no_os_store(file_backend):
    assert cred.detect_backend() == "file"


def test_file_roundtrip(file_backend):
    assert cred.get_saved_key("grok") is None
    assert cred.set_saved_key("grok", "xai-secret-1234") == "file"
    assert cred.get_saved_key("grok") == "xai-secret-1234"
    present, backend = cred.saved_key_status("grok")
    assert present and backend == "file"
    assert cred.delete_saved_key("grok") is True
    assert cred.get_saved_key("grok") is None
    assert cred.delete_saved_key("grok") is False  # idempotent


def test_file_is_0600_and_dir_0700(file_backend):
    cred.set_saved_key("kimi", "sk-abcd")
    path = cred._config_path()
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_status_carries_no_key_material(file_backend):
    cred.set_saved_key("grok", "xai-supersecret-wxyz")
    status = cred.saved_key_status("grok")
    assert status == (True, "file")
    # the status tuple contains no part of the key (not even a last-4)
    assert not any("supersecret" in str(x) or "wxyz" in str(x) for x in status)


def test_set_empty_key_rejected(file_backend):
    with pytest.raises(cred.CredentialError):
        cred.set_saved_key("grok", "   ")


def test_corrupt_file_is_fail_soft(file_backend):
    path = cred._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{", encoding="utf-8")
    assert cred.get_saved_key("grok") is None  # fail-soft, never raises


def test_keychain_backend_mocked(tmp_path, monkeypatch):
    """Keychain path with a mocked ``security`` CLI — no real keychain touched."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(cred, "_kc_available", lambda: True)
    monkeypatch.setattr(cred, "_st_available", lambda: False)
    store: dict[str, str] = {}

    def fake_run(argv, **kw):
        if argv[:2] == ["security", "add-generic-password"]:
            store[argv[argv.index("-s") + 1]] = argv[argv.index("-w") + 1]
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["security", "find-generic-password"]:
            svc = argv[argv.index("-s") + 1]
            if svc in store:
                return types.SimpleNamespace(returncode=0, stdout=store[svc] + "\n", stderr="")
            return types.SimpleNamespace(returncode=44, stdout="", stderr="not found")
        if argv[:2] == ["security", "delete-generic-password"]:
            existed = store.pop(argv[argv.index("-s") + 1], None) is not None
            return types.SimpleNamespace(returncode=0 if existed else 44, stdout="", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(cred.subprocess, "run", fake_run)

    assert cred.detect_backend() == "keychain"
    assert cred.set_saved_key("grok", "xai-kc-1234") == "keychain"
    assert cred.get_saved_key("grok") == "xai-kc-1234"
    present, backend = cred.saved_key_status("grok")
    assert present and backend == "keychain"
    assert cred.delete_saved_key("grok") is True
    assert cred.get_saved_key("grok") is None


def test_keychain_write_failure_falls_back_to_file(tmp_path, monkeypatch):
    """If the OS keystore write fails, the key still saves to the file backend."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(cred, "_kc_available", lambda: True)
    monkeypatch.setattr(cred, "_st_available", lambda: False)
    monkeypatch.setattr(cred, "_kc_set", lambda provider, key: False)  # write fails

    assert cred.set_saved_key("grok", "xai-fallback-9999") == "file"
    # _kc_get returns None (mock store empty) so the file value resolves.
    monkeypatch.setattr(cred, "_kc_get", lambda provider: None)
    assert cred.get_saved_key("grok") == "xai-fallback-9999"
