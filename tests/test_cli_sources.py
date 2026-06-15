from __future__ import annotations

import contextlib

import oxison.cli as cli_mod
from oxison.cli import build_parser


def test_cli_parses_source_flags() -> None:
    p = build_parser()
    ns = p.parse_args([
        "run", "/repo",
        "--add", "a.pdf", "--add", "b.pptx",
        "--ocr", "--stt-key", "sk", "--stt-provider", "deepgram",
    ])
    assert ns.add == ["a.pdf", "b.pptx"]
    assert ns.ocr is True
    assert ns.stt_key == "sk"
    assert ns.stt_provider == "deepgram"


def test_cli_source_flags_default() -> None:
    p = build_parser()
    ns = p.parse_args(["run", "/repo"])
    assert ns.add == []
    assert ns.ocr is False
    assert ns.stt_key is None
    assert ns.stt_provider == "openai"


def test_cmd_run_expands_sources_dir_and_merges_add(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    sdir = tmp_path / "inputs"
    sdir.mkdir()
    (sdir / "b.pdf").write_bytes(b"%PDF")
    (sdir / "a.md").write_text("x", encoding="utf-8")
    (sdir / "sub").mkdir()  # directory inside — must be excluded (files only)

    captured = {}

    def fake_build_run_config(**kw):
        captured.update(kw)
        raise SystemExit(0)  # stop cmd_run right after config build

    monkeypatch.setattr(cli_mod, "build_run_config", fake_build_run_config)

    p = cli_mod.build_parser()
    ns = p.parse_args([
        "run", str(repo), "--add", "extra.docx", "--sources", str(sdir),
    ])
    with contextlib.suppress(SystemExit):
        ns.func(ns)

    es = captured["extra_sources"]
    # --add entries come first, then sorted dir files (files only, no subdir)
    assert es[0] == "extra.docx"
    assert str(sdir / "a.md") in es
    assert str(sdir / "b.pdf") in es
    assert str(sdir / "sub") not in es
    # sorted order within the dir expansion (a.md before b.pdf)
    assert es.index(str(sdir / "a.md")) < es.index(str(sdir / "b.pdf"))


def test_cmd_run_warns_when_sources_not_a_dir(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    not_a_dir = tmp_path / "notes.txt"
    not_a_dir.write_text("x", encoding="utf-8")

    def fake_build_run_config(**kw):
        raise SystemExit(0)

    monkeypatch.setattr(cli_mod, "build_run_config", fake_build_run_config)
    p = cli_mod.build_parser()
    ns = p.parse_args(["run", str(repo), "--sources", str(not_a_dir)])
    with contextlib.suppress(SystemExit):
        ns.func(ns)
    out = capsys.readouterr().out
    assert "not a directory" in out
