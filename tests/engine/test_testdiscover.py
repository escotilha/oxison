"""Tests for best-effort test-command discovery (engine/testdiscover.py)."""

from __future__ import annotations

import json

from oxison.engine.testdiscover import discover_test_command


def test_none_for_empty_repo(tmp_path):
    assert discover_test_command(tmp_path) is None


def test_npm_test(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert discover_test_command(tmp_path) == "npm test"


def test_npm_init_placeholder_ignored(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
    )
    assert discover_test_command(tmp_path) is None


def test_pytest_from_pyproject_tool_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n")
    assert discover_test_command(tmp_path) == "pytest"


def test_pytest_from_dependency_mention(tmp_path):
    (tmp_path / "requirements-dev.txt").write_text("pytest>=8\n")
    assert discover_test_command(tmp_path) == "pytest"


def test_pytest_not_detected_from_project_name_only(tmp_path):
    # A project NAMED "pytest-*" with no actual pytest dep/config must not match.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pytest-clone-detector"\nversion = "0.1"\n'
    )
    assert discover_test_command(tmp_path) is None


def test_pytest_from_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    assert discover_test_command(tmp_path) == "pytest"


def test_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
    assert discover_test_command(tmp_path) == "cargo test"


def test_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert discover_test_command(tmp_path) == "go test ./..."


def test_makefile_test_target(tmp_path):
    (tmp_path / "Makefile").write_text("build:\n\tgo build\ntest:\n\tgo test ./...\n")
    assert discover_test_command(tmp_path) == "make test"


def test_makefile_takes_precedence_over_npm(tmp_path):
    # An author-defined `make test` is the most authoritative signal.
    (tmp_path / "Makefile").write_text("test:\n\tnpm test\n")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert discover_test_command(tmp_path) == "make test"


def test_phony_only_is_not_a_target(tmp_path):
    # ".PHONY: test" with no real `test:` rule must not be detected as a target.
    (tmp_path / "Makefile").write_text(".PHONY: test\n")
    assert discover_test_command(tmp_path) is None


def test_malformed_manifests_degrade_to_none(tmp_path):
    (tmp_path / "package.json").write_text("{ not valid json ")
    (tmp_path / "pyproject.toml").write_text("this = is = not = toml")
    assert discover_test_command(tmp_path) is None
