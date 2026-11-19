"""Regression: `quartz auth` / `mcp` / `acp` must see ~/.quartz/.env.

The subcommands are dispatched before the argument parser runs, so .env loading
used to happen too late for `quartz auth login google` to see its credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import agent.cli as cli


def _prepare_home(tmp_path: Path, monkeypatch: Any) -> Path:
    home = tmp_path / "home"
    (home / ".quartz").mkdir(parents=True)
    (home / ".quartz" / ".env").write_text(
        "QUARTZ_TEST_MARKER_FROM_HOME_ENV=loaded\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("QUARTZ_TEST_MARKER_FROM_HOME_ENV", raising=False)
    # Avoid picking up a real project .env while load_dotenv() searches upward.
    monkeypatch.chdir(tmp_path)
    return home


@pytest.mark.parametrize("subcommand", ["auth", "mcp"])
def test_subcommands_load_env_before_dispatch(
    tmp_path: Path, monkeypatch: Any, subcommand: str
) -> None:
    import os

    _prepare_home(tmp_path, monkeypatch)

    seen: dict[str, str | None] = {}

    def _record(argv: list[str]) -> None:
        seen["marker"] = os.environ.get("QUARTZ_TEST_MARKER_FROM_HOME_ENV")

    monkeypatch.setattr(cli, "_handle_auth_cli", _record, raising=False)
    monkeypatch.setattr(cli, "_handle_mcp_cli", _record, raising=False)
    monkeypatch.setattr(cli.sys, "argv", ["quartz", subcommand])

    cli.main()

    assert seen["marker"] == "loaded"


def test_real_environment_wins_over_env_file(tmp_path: Path, monkeypatch: Any) -> None:
    import os

    _prepare_home(tmp_path, monkeypatch)
    monkeypatch.setenv("QUARTZ_TEST_MARKER_FROM_HOME_ENV", "from-shell")

    cli._load_env_files()

    assert os.environ["QUARTZ_TEST_MARKER_FROM_HOME_ENV"] == "from-shell"
