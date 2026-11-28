"""Tests for the built-in env_info() REPL helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from quartz.kernel.helpers import BUILTIN_HELPERS, env_info, format_helpers_for_prompt, load_helpers


def test_env_info_registered_and_formatted(tmp_path: Path) -> None:
    assert "env_info" in BUILTIN_HELPERS
    loaded = load_helpers(cwd=tmp_path)
    assert "env_info" in loaded.functions
    prompt_block = format_helpers_for_prompt(loaded)
    assert "env_info(" in prompt_block


def test_env_info_reports_venvs_manifests_and_tools(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_pytest = venv_bin / "pytest"
    fake_pytest.write_text("#!/bin/sh\n")
    fake_pytest.chmod(0o755)

    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (tmp_path / "package.json").write_text("{}")

    out = env_info("nonexistent_tool_xyz", cwd=tmp_path)
    assert str(tmp_path.resolve()) in out
    assert "python:" in out.lower()
    assert ".venv" in out
    assert "pytest" in out
    assert "pyproject.toml" in out
    assert "package.json" in out
    assert "nonexistent_tool_xyz: not found" in out


def test_env_info_empty_dir(tmp_path: Path) -> None:
    out = env_info(cwd=tmp_path)
    assert str(tmp_path.resolve()) in out
    assert "venvs: none" in out
    assert "manifests: none" in out


def test_env_info_missing_cwd_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        env_info(cwd=missing)
