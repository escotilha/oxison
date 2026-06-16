"""Direct tests for engine/gitutil.py.

These import from the canonical ``gitutil`` module (not the ``dispatch`` re-export
shim), so a regression in gitutil that still passes through the shim is caught
(CAND-4).
"""

from __future__ import annotations

from oxison.engine.gitutil import extract_cost_from_log, parse_changed_files


def test_parse_changed_files_modified_and_untracked():
    out = parse_changed_files(" M src/a.py\n?? src/b.py\n")
    assert out == ["src/a.py", "src/b.py"]


def test_parse_changed_files_rename_takes_new_path():
    assert parse_changed_files("R  old.py -> new.py\n") == ["new.py"]


def test_parse_changed_files_strips_quotes():
    assert parse_changed_files('?? "weird name.py"\n') == ["weird name.py"]


def test_parse_changed_files_empty():
    assert parse_changed_files("") == []


# extract_cost_from_log is on the C3 budget-floor critical path (a timed-out
# worker that reports no cost is charged the cap floor); test it in isolation.

def test_extract_cost_from_result_event(tmp_path):
    log = tmp_path / "w.log"
    log.write_text(
        '{"type":"system","subtype":"init"}\n'
        '{"type":"result","subtype":"success","total_cost_usd":0.0734}\n',
        encoding="utf-8",
    )
    assert extract_cost_from_log(log) == 0.0734


def test_extract_cost_no_result_event_is_zero(tmp_path):
    log = tmp_path / "w.log"
    log.write_text('{"type":"assistant"}\n{"type":"system"}\n', encoding="utf-8")
    assert extract_cost_from_log(log) == 0.0


def test_extract_cost_missing_file_is_zero(tmp_path):
    assert extract_cost_from_log(tmp_path / "nope.log") == 0.0


def test_extract_cost_tolerates_malformed_trailing_line(tmp_path):
    # A SIGKILL'd worker can leave a truncated final JSON line — must not crash.
    log = tmp_path / "w.log"
    log.write_text(
        '{"type":"result","total_cost_usd":1.5}\n{"type":"assist',
        encoding="utf-8",
    )
    assert extract_cost_from_log(log) == 1.5
