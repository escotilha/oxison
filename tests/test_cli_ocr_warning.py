"""Tests for the `--ocr` privilege warning (SECURITY-AUDIT.md F3).

The OCR adapter runs third-party `document_extraction` code in the main process
before any sandbox. `_warn_if_ocr` makes that trust boundary explicit at enable
time. These assert it fires (to stderr) exactly when `--ocr` is set.
"""
from __future__ import annotations

import types

import oxison.cli as cli


def _args(**kw):
    base = {"ocr": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_warns_to_stderr_when_ocr_enabled(capsys):
    cli._warn_if_ocr(_args(ocr=True))
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "--ocr" in err
    assert "full privileges" in err


def test_silent_when_ocr_disabled(capsys):
    cli._warn_if_ocr(_args(ocr=False))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_silent_when_ocr_attr_absent(capsys):
    # Defensive: a Namespace without `ocr` (getattr default False) must not warn.
    cli._warn_if_ocr(types.SimpleNamespace())
    assert capsys.readouterr().err == ""
