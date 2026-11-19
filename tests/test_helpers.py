"""Tests for pluggable REPL helpers, dynamic loading, and system prompt integration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.commands import _CLIRuntime, _handle_slash_command
from agent.helpers import (
    PROTECTED_NAMES,
    format_helpers_for_prompt,
    load_helpers,
)
from agent.kernel_client import KernelClient
from agent.prompt import build_system_prompt
from agent.session import Session


def test_builtin_helpers_loaded_by_default() -> None:
    loaded = load_helpers(global_helpers_path=Path("/nonexistent/helpers.py"))
    assert "ls" in loaded.functions
    assert "find" in loaded.functions
    assert "rg" in loaded.functions
    assert "read" in loaded.functions
    assert "edit" in loaded.functions
    assert "write" in loaded.functions
    assert "view_image" in loaded.functions

    for name in ("ls", "read", "write"):
        info = loaded.infos[name]
        assert info.source == "builtin"
        assert callable(info.func)
        assert info.doc


def test_load_global_helpers(tmp_path: Path) -> None:
    global_file = tmp_path / "global_helpers.py"
    global_file.write_text(
        "def custom_calc(a: int, b: int) -> int:\n"
        "    \"\"\"Add two numbers together.\"\"\"\n"
        "    return a + b\n",
        encoding="utf-8",
    )

    loaded = load_helpers(global_helpers_path=global_file)
    assert "custom_calc" in loaded.functions
    info = loaded.infos["custom_calc"]
    assert info.source == "global"
    assert "(a: int, b: int) -> int" in info.signature
    assert info.doc == "Add two numbers together."
    assert loaded.functions["custom_calc"](2, 3) == 5


def test_load_project_helpers_and_override(tmp_path: Path) -> None:
    project_dir = tmp_path / ".quartz"
    project_dir.mkdir(parents=True)
    project_file = project_dir / "helpers.py"
    project_file.write_text(
        "def project_helper():\n"
        "    \"\"\"Project-specific utility.\"\"\"\n"
        "    return 'project-ok'\n"
        "def ls(path='.'):\n"
        "    \"\"\"Overridden ls helper.\"\"\"\n"
        "    return ['custom_listing']\n",
        encoding="utf-8",
    )

    loaded = load_helpers(cwd=tmp_path, global_helpers_path=Path("/nonexistent"))
    assert "project_helper" in loaded.functions
    assert loaded.infos["project_helper"].source == "project"

    # Verify override
    assert loaded.infos["ls"].source == "project"
    assert loaded.functions["ls"]() == ["custom_listing"]


def test_protected_primitives_cannot_be_overridden(tmp_path: Path) -> None:
    project_dir = tmp_path / ".quartz"
    project_dir.mkdir(parents=True)
    project_file = project_dir / "helpers.py"
    project_file.write_text(
        "def bash(cmd):\n"
        "    return 'hacked'\n"
        "def mcp():\n"
        "    return 'hacked'\n"
        "def valid_fn():\n"
        "    return 123\n",
        encoding="utf-8",
    )

    loaded = load_helpers(cwd=tmp_path, global_helpers_path=Path("/nonexistent"))
    assert "valid_fn" in loaded.functions
    # bash and mcp should NOT be in loaded.functions (they are protected kernel primitives)
    assert "bash" not in loaded.functions
    assert "mcp" not in loaded.functions

    assert len(loaded.warnings) == 2
    assert any("Cannot override protected primitive 'bash'" in w for w in loaded.warnings)
    assert any("Cannot override protected primitive 'mcp'" in w for w in loaded.warnings)


def test_syntax_error_in_helpers_handled_gracefully(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_helpers.py"
    bad_file.write_text("def broken(:\n", encoding="utf-8")

    loaded = load_helpers(global_helpers_path=bad_file)
    # Builtins should still load despite error
    assert "read" in loaded.functions
    assert len(loaded.warnings) == 1
    assert "Failed to load helpers" in loaded.warnings[0]


def test_format_helpers_for_prompt_includes_custom(tmp_path: Path) -> None:
    project_dir = tmp_path / ".quartz"
    project_dir.mkdir(parents=True)
    project_file = project_dir / "helpers.py"
    project_file.write_text(
        "def deploy_db(env: str) -> bool:\n"
        "    \"\"\"Migrate database for target environment.\"\"\"\n"
        "    return True\n",
        encoding="utf-8",
    )

    prompt = build_system_prompt(tmp_path, global_helpers_path=Path("/nonexistent"))
    assert "## REPL Functions" in prompt
    assert "Custom user helpers:" in prompt
    assert "`deploy_db(env: str) -> bool`: Migrate database for target environment. (project)" in prompt


def test_kernel_executes_custom_project_helper(tmp_path: Path) -> None:
    project_dir = tmp_path / ".quartz"
    project_dir.mkdir(parents=True)
    project_file = project_dir / "helpers.py"
    project_file.write_text(
        "def multiply(x: int, y: int) -> int:\n"
        "    \"\"\"Multiply two numbers.\"\"\"\n"
        "    return x * y\n",
        encoding="utf-8",
    )

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        # Check cell execution
        res = kernel.exec_cell("multiply(6, 7)")
        assert res.ok
        assert res.result == "42"

        # Check list_helpers IPC
        helpers_data = kernel.list_helpers()
        names = [h["name"] for h in helpers_data["helpers"]]
        assert "multiply" in names
        mult_info = next(h for h in helpers_data["helpers"] if h["name"] == "multiply")
        assert mult_info["source"] == "project"
        assert mult_info["doc"] == "Multiply two numbers."
    finally:
        kernel.close()


def test_slash_command_helpers_execution(tmp_path: Path) -> None:
    from agent.loop import TurnUsage
    session = Session.create(tmp_path)
    rt = _CLIRuntime(
        cwd=tmp_path,
        kernel=None,  # type: ignore[arg-type]
        client=SimpleNamespace(),
        provider_name="openai",
        model="gpt-5-mini",
        effort="medium",
        system_prompt="",
        active_session=session,
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128000,
        compact_reserve_tokens=16384,
        keep_recent_messages=10,
        verbose=False,
        plain_output=False,
        headless=True,
    )
    result = _handle_slash_command(rt, "/helpers")
    assert result is False
