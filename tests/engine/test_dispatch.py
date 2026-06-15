"""Tests for the dispatch pure helpers (prompt + porcelain parsing)."""

from __future__ import annotations

from oxison.engine.dispatch import build_worker_prompt, parse_changed_files


def test_prompt_encodes_acceptance_and_constraints():
    p = build_worker_prompt(
        "Add cloud sync",
        rationale="users want cross-device todos",
        acceptance=["todos persist across devices", "a sync test passes"],
        files_hint=["src/sync.py"],
        repo_name="linkshort",
    )
    assert "Add cloud sync" in p
    assert "todos persist across devices" in p
    assert "src/sync.py" in p
    assert "linkshort" in p
    # The worker must be told not to touch protected paths.
    assert "oxison-build/" in p
    assert "CI config" in p or ".env" in p


def test_prompt_handles_no_acceptance_or_hints():
    p = build_worker_prompt("X", rationale="", acceptance=[], files_hint=[], repo_name="r")
    assert "(none specified)" in p
    assert "use your judgment" in p


def test_parse_porcelain_modified_and_untracked():
    porcelain = " M src/a.py\n?? src/new.py\nA  src/added.py\n"
    assert parse_changed_files(porcelain) == ["src/a.py", "src/new.py", "src/added.py"]


def test_parse_porcelain_rename():
    porcelain = "R  src/old.py -> src/new.py\n"
    assert parse_changed_files(porcelain) == ["src/new.py"]


def test_parse_porcelain_quoted_path():
    porcelain = ' M "src/has space.py"\n'
    assert parse_changed_files(porcelain) == ["src/has space.py"]


def test_parse_porcelain_empty():
    assert parse_changed_files("") == []
