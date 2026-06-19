"""Tests for the curated CLAUDE_CONFIG_DIR builder (worker skill scoping)."""
from __future__ import annotations

from pathlib import Path

from oxison.engine.skillscope import build_curated_config_dir


def _make_source(tmp_path: Path) -> Path:
    """A fake ~/.claude/skills with a mix of generic + project-specific skills."""
    src = tmp_path / "source-skills"
    for name in ("cto", "verify", "first-principles", "contably-ci-rescue", "qa-conta"):
        (src / name).mkdir(parents=True)
        (src / name / "SKILL.md").write_text(f"# {name}\n")
    return src


def test_curated_dir_exposes_only_named_skills(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    dest = build_curated_config_dir(
        tmp_path / "cfg",
        source_skills_dir=src,
        skill_names=("cto", "verify", "first-principles"),
    )
    exposed = {p.name for p in (dest / "skills").iterdir()}
    assert exposed == {"cto", "verify", "first-principles"}
    # The project-specific skills the operator has are NEVER linked in.
    assert "contably-ci-rescue" not in exposed
    assert "qa-conta" not in exposed


def test_curated_links_resolve_to_real_skill(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    dest = build_curated_config_dir(
        tmp_path / "cfg", source_skills_dir=src, skill_names=("cto",)
    )
    link = dest / "skills" / "cto"
    assert link.is_symlink()
    assert (link / "SKILL.md").read_text() == "# cto\n"  # reaches the real file


def test_absent_curated_name_is_skipped(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    dest = build_curated_config_dir(
        tmp_path / "cfg",
        source_skills_dir=src,
        skill_names=("cto", "does-not-exist"),
    )
    exposed = {p.name for p in (dest / "skills").iterdir()}
    assert exposed == {"cto"}  # missing name silently skipped, no crash


def test_refresh_drops_stale_links(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    cfg = tmp_path / "cfg"
    build_curated_config_dir(cfg, source_skills_dir=src, skill_names=("cto", "verify"))
    # Re-run with a narrower set — the dropped skill must not linger as invokable.
    build_curated_config_dir(cfg, source_skills_dir=src, skill_names=("cto",))
    exposed = {p.name for p in (cfg / "skills").iterdir()}
    assert exposed == {"cto"}
