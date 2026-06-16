"""Direct tests for engine/gitutil.py.

These import from the canonical ``gitutil`` module (not the ``dispatch`` re-export
shim), so a regression in gitutil that still passes through the shim is caught
(CAND-4).
"""

from __future__ import annotations

from oxison.engine.gitutil import parse_changed_files


def test_parse_changed_files_modified_and_untracked():
    out = parse_changed_files(" M src/a.py\n?? src/b.py\n")
    assert out == ["src/a.py", "src/b.py"]


def test_parse_changed_files_rename_takes_new_path():
    assert parse_changed_files("R  old.py -> new.py\n") == ["new.py"]


def test_parse_changed_files_strips_quotes():
    assert parse_changed_files('?? "weird name.py"\n') == ["weird name.py"]


def test_parse_changed_files_empty():
    assert parse_changed_files("") == []
