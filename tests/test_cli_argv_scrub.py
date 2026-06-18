"""Tests for --api-key / --stt-key argv scrubbing (SECURITY-AUDIT.md F2).

The key passed on the command line must not linger in `sys.argv` (visible to
ps / /proc/self/cmdline) after parsing, while the parsed Namespace still carries
the real value for downstream consumers.
"""
from __future__ import annotations

import sys

import oxison.cli as cli


def _with_argv(argv, fn):
    saved = sys.argv
    sys.argv = list(argv)
    try:
        fn()
        return list(sys.argv)
    finally:
        sys.argv = saved


def test_scrubs_space_separated_api_key():
    result = _with_argv(
        ["oxison", "run", "repo", "--api-key", "sk-ant-SECRET", "--bare"],
        cli._scrub_api_key_argv,
    )
    assert "sk-ant-SECRET" not in result
    assert "[REDACTED]" in result
    # surrounding args are untouched
    assert result[:3] == ["oxison", "run", "repo"] and result[-1] == "--bare"


def test_scrubs_equals_form():
    result = _with_argv(
        ["oxison", "run", "repo", "--api-key=sk-ant-SECRET"],
        cli._scrub_api_key_argv,
    )
    assert "sk-ant-SECRET" not in " ".join(result)
    assert "--api-key=[REDACTED]" in result


def test_scrubs_stt_key_too():
    result = _with_argv(
        ["oxison", "run", "repo", "--stt-key", "stt-SECRET"],
        cli._scrub_api_key_argv,
    )
    assert "stt-SECRET" not in result
    assert "[REDACTED]" in result


def test_noop_when_no_key_flag():
    argv = ["oxison", "run", "repo", "--bare", "--provider", "kimi"]
    result = _with_argv(argv, cli._scrub_api_key_argv)
    assert result == argv  # unchanged


def test_parsed_args_keep_the_real_key():
    # The behavior guard: scrubbing argv must NOT lose the key for consumers —
    # the value lives on the parsed Namespace, which is the source of truth.
    saved = sys.argv
    sys.argv = ["oxison", "run", "repo", "--api-key", "sk-ant-REAL", "--bare"]
    try:
        args = cli.build_parser().parse_args(sys.argv[1:])
        cli._scrub_api_key_argv()
        assert args.api_key == "sk-ant-REAL"  # parsed value intact
        assert "sk-ant-REAL" not in sys.argv   # argv scrubbed
    finally:
        sys.argv = saved
