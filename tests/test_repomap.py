from __future__ import annotations

from pathlib import Path

from oxison.repomap import build_repo_map, estimate_tokens, top_level_dirs


def _make_fixture(root: Path) -> None:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "node_modules" / "junk").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["requests>=2.0", "PyYAML"]\n'
        '[project.scripts]\ndemo = "pkg.cli:main"\n',
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        '{"dependencies": {"react": "^18"}, "devDependencies": {"vitest": "^1"}}',
        encoding="utf-8",
    )
    (root / "src" / "pkg" / "__main__.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "src" / "pkg" / "core.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (root / "tests" / "test_x.py").write_text("def test(): pass\n", encoding="utf-8")
    (root / "node_modules" / "junk" / "huge.py").write_text("# vendored\n" * 100, encoding="utf-8")
    (root / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (root / ".env.example").write_text("DATABASE_URL=\nAPI_KEY=\n", encoding="utf-8")


def test_build_repo_map_basic(tmp_path: Path) -> None:
    _make_fixture(tmp_path)
    rm = build_repo_map(tmp_path)

    assert "Python" in rm.languages
    assert rm.languages["Python"].files >= 3  # __main__, core, test_x (not vendored)
    # node_modules must be skipped — its vendored .py is not counted.
    assert rm.languages["Python"].files == 3

    paths = {m.path for m in rm.manifests}
    assert "pyproject.toml" in paths
    assert "package.json" in paths

    py_manifest = next(m for m in rm.manifests if m.path == "pyproject.toml")
    assert "requests" in py_manifest.dependencies
    assert "PyYAML" in py_manifest.dependencies

    js_manifest = next(m for m in rm.manifests if m.path == "package.json")
    assert "react" in js_manifest.dependencies
    assert "vitest" in js_manifest.dependencies

    assert any("__main__.py" in e for e in rm.entry_points)
    assert "script:demo" in rm.entry_points
    assert "Docker" in rm.services
    assert any("env keys" in s for s in rm.services)


def test_top_level_dirs_skips_vendor(tmp_path: Path) -> None:
    _make_fixture(tmp_path)
    dirs = top_level_dirs(tmp_path)
    assert "src" in dirs
    assert "tests" in dirs
    assert "node_modules" not in dirs


def test_estimate_tokens_monotonic(tmp_path: Path) -> None:
    _make_fixture(tmp_path)
    rm = build_repo_map(tmp_path)
    assert estimate_tokens(rm) > 0


def test_to_json_roundtrips(tmp_path: Path) -> None:
    import json

    _make_fixture(tmp_path)
    rm = build_repo_map(tmp_path)
    data = json.loads(rm.to_json())
    assert data["root"] == str(tmp_path.resolve())
    assert "languages" in data and "manifests" in data
