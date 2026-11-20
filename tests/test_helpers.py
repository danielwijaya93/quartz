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
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=False,
        headless=True,
    )
    result = _handle_slash_command(rt, "/helpers")
    assert result is False


def test_custom_helper_can_compose_builtin_helpers(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("first line\nsecond line\n", encoding="utf-8")

    project_dir = tmp_path / ".quartz"
    project_dir.mkdir(parents=True)
    project_file = project_dir / "helpers.py"
    project_file.write_text(
        "def read_first_line(p):\n"
        "    \"\"\"Read first line using builtin read helper.\"\"\"\n"
        "    return read(p, 1, 1)\n",
        encoding="utf-8",
    )

    loaded = load_helpers(cwd=tmp_path, global_helpers_path=Path("/nonexistent"))
    assert "read_first_line" in loaded.functions
    res = loaded.functions["read_first_line"](str(sample_file))
    assert "1: first line" in res


def test_custom_helper_can_call_bash_in_kernel(tmp_path: Path) -> None:
    project_dir = tmp_path / ".quartz"
    project_dir.mkdir(parents=True)
    project_file = project_dir / "helpers.py"
    project_file.write_text(
        "async def custom_pwd():\n"
        "    \"\"\"Run pwd using bash helper.\"\"\"\n"
        "    h = bash('pwd')\n"
        "    res = await h\n"
        "    return res.output.strip()\n",
        encoding="utf-8",
    )

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell("await custom_pwd()")
        assert res.ok
        assert str(tmp_path.resolve()) == res.result.strip("'\"")
    finally:
        kernel.close()


def test_edit_and_write_atomic_and_crlf_safe(tmp_path: Path) -> None:
    from agent.helpers import edit, write

    # Test atomic write
    f = tmp_path / "crlf_test.txt"
    write(str(f), "hello\r\nworld\r\n")
    assert f.read_bytes() == b"hello\r\nworld\r\n"

    # Test edit with LF old/new string adapting to CRLF file
    edit(str(f), "world", "quartz")
    raw = f.read_bytes()
    assert b"hello\r\nquartz\r\n" == raw
    assert b"\r\n" in raw
    assert raw.count(b"\r\n") == 2

    # Test multiline edit with LF in arguments preserves CRLF in file
    edit(str(f), "hello\nquartz", "greeting\ngalaxy")
    raw2 = f.read_bytes()
    assert b"greeting\r\ngalaxy\r\n" == raw2
    assert b"\n" not in raw2.replace(b"\r\n", b"")

def test_rg_fixed_literal_search(tmp_path: Path) -> None:
    from unittest.mock import patch
    from agent.helpers import rg

    test_file = tmp_path / "sample.txt"
    test_file.write_text("prefix.middle\nprefix1middle\n[tag] item\ntag item\n", encoding="utf-8")

    # 1. Ripgrep branch (fixed=True should match dot literally)
    out_fixed = rg("prefix.middle", str(tmp_path), fixed=True)
    assert "prefix.middle" in out_fixed
    assert "prefix1middle" not in out_fixed

    # Regex search without fixed=True matches both
    out_regex = rg("prefix.middle", str(tmp_path), fixed=False)
    assert "prefix.middle" in out_regex
    assert "prefix1middle" in out_regex

    # Bracket literal search
    out_bracket = rg("[tag]", str(tmp_path), fixed=True)
    assert "[tag] item" in out_bracket
    assert "tag item" not in out_bracket

    # 2. Fallback Python re branch
    with patch("shutil.which", return_value=None):
        fb_fixed = rg("prefix.middle", str(tmp_path), fixed=True)
        assert "prefix.middle" in fb_fixed
        assert "prefix1middle" not in fb_fixed

        fb_regex = rg("prefix.middle", str(tmp_path), fixed=False)
        assert "prefix.middle" in fb_regex
        assert "prefix1middle" in fb_regex

        fb_bracket = rg("[tag]", str(tmp_path), fixed=True)
        assert "[tag] item" in fb_bracket
        assert "tag item" not in fb_bracket


def test_format_helpers_for_prompt_includes_fixed_option() -> None:
    loaded = load_helpers(global_helpers_path=Path("/nonexistent/helpers.py"))
    prompt = format_helpers_for_prompt(loaded)
    assert "rg(pattern, path='.', glob=None, max_hits=100, fixed=False)" in prompt
    assert "fixed=True for literal string search" in prompt

def test_prompt_rules_include_chaining_and_helper_prioritization() -> None:
    from agent.prompt import PROMPT_RULES

    assert "Batch and chain operations generously into one cell" in PROMPT_RULES
    assert "Prioritize available REPL helpers and primitives" in PROMPT_RULES


def test_write_and_edit_py_check_missing_import(tmp_path: Path) -> None:
    from agent.helpers import edit, write

    py_file = tmp_path / "mod.py"
    out_write = write(str(py_file), "def f():\n    return json.dumps({'a': 1})\n")
    assert "[python check]" in out_write
    assert "undefined name 'json'" in out_write
    assert py_file.exists()

    out_edit = edit(str(py_file), "return json.dumps({'a': 1})", "return Path('.').name")
    assert "[python check]" in out_edit
    assert "undefined name 'Path'" in out_edit
    assert "Path('.').name" in py_file.read_text(encoding="utf-8")


def test_write_and_edit_py_check_valid_and_edge_cases(tmp_path: Path) -> None:
    from agent.helpers import _check_python_source, write

    valid_src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "import os\n\n"
        "if TYPE_CHECKING:\n"
        "    from missing_mod import ForwardRefType\n\n"
        "class Box:\n"
        "    class_attr = 10\n"
        "    items = [x * 2 for x in range(3)]\n"
        "    def method(self, x: ForwardRefType) -> ForwardRefType:\n"
        "        return helper_fn(x)\n\n"
        "def helper_fn(val):\n"
        "    return os.path.basename(str(val))\n"
    )
    valid_file = tmp_path / "valid.py"
    out = write(str(valid_file), valid_src)
    assert "[python check]" not in out

    star_file = tmp_path / "star.py"
    out_star = write(str(star_file), "from math import *\nx = sin(1)\n")
    assert "[python check]" not in out_star

    # Real agent/*.py files must produce zero false positives
    repo_root = Path(__file__).resolve().parent.parent
    for py_path in sorted((repo_root / "agent").glob("*.py")):
        warnings = _check_python_source(py_path, py_path.read_text(encoding="utf-8"))
        assert warnings == [], f"Unexpected warnings for {py_path}: {warnings}"


def test_write_py_check_syntax_error_and_non_py_skip(tmp_path: Path) -> None:
    from agent.helpers import write

    bad_py = tmp_path / "bad.py"
    out_bad = write(str(bad_py), "def broken(:\n    pass\n")
    assert "[python check]" in out_bad
    assert "SyntaxError" in out_bad
    assert bad_py.exists()

    txt_file = tmp_path / "notes.txt"
    out_txt = write(str(txt_file), "undefined_name_here()\ndef broken(:\n")
    assert "[python check]" not in out_txt


def test_system_prompt_mentions_stdlib_auto_import_and_explicit_py_imports(tmp_path: Path) -> None:
    prompt = build_system_prompt(tmp_path, global_helpers_path=Path("/nonexistent"))
    assert "auto-imported" in prompt
    assert "explicit imports are still required" in prompt


def test_edit_preserves_mode_and_write_respects_umask(tmp_path: Path) -> None:
    import os
    import stat
    from agent.helpers import edit, write

    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(script, 0o755)

    edit(str(script), "echo hi", "echo bye")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755

    prev_umask = os.umask(0o022)
    try:
        new_file = tmp_path / "plain.txt"
        write(str(new_file), "hello\n")
        assert stat.S_IMODE(new_file.stat().st_mode) == 0o644
    finally:
        os.umask(prev_umask)


def test_rg_leading_dash_patterns(tmp_path: Path) -> None:
    from agent.helpers import rg

    target = tmp_path / "flags.txt"
    target.write_text("--verbose flag\n-n line\n", encoding="utf-8")

    out_fixed = rg("--verbose", str(tmp_path), fixed=True)
    assert "--verbose flag" in out_fixed

    out_regex = rg("-n", str(tmp_path), fixed=False)
    assert "-n line" in out_regex


