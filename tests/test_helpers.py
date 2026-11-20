"""Tests for pluggable REPL helpers, dynamic loading, and system prompt integration."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.commands import _handle_slash_command
from agent.runtime import AgentRuntime as _CLIRuntime
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


def test_helper_file_with_dataclass_loads(tmp_path: Path) -> None:
    """Decorators that resolve annotations via sys.modules need registration first.

    With ``from __future__ import annotations``, ``@dataclass`` looks up the module
    in ``sys.modules`` to resolve string annotations. Helper modules were executed
    without ever being registered, so any helper file containing a dataclass raised
    "'NoneType' object has no attribute '__dict__'" and every helper in that file
    was silently discarded.
    """
    helper_file = tmp_path / "dc_helpers.py"
    helper_file.write_text(
        "from __future__ import annotations\n"
        "\n"
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Limits:\n"
        "    \"\"\"Per-model token limits.\"\"\"\n"
        "    context: int = 100_000\n"
        "    output: int = 8_192\n"
        "\n"
        "\n"
        "def effective_limit(limits: Limits, ratio: float = 0.5) -> int:\n"
        "    \"\"\"Return the usable context window.\"\"\"\n"
        "    return int(limits.context * ratio)\n",
        encoding="utf-8",
    )

    loaded = load_helpers(global_helpers_path=helper_file)
    mod_name = "_quartz_helpers_global_dc_helpers"
    try:
        assert loaded.warnings == []
        assert "effective_limit" in loaded.functions
        # The module is registered while executing, like a real import.
        assert mod_name in sys.modules

        limits_cls = sys.modules[mod_name].Limits
        assert limits_cls(context=200_000, output=8_192).context == 200_000
        assert loaded.functions["effective_limit"](limits_cls(context=200_000)) == 100_000
    finally:
        sys.modules.pop(mod_name, None)


def test_failed_helper_file_leaves_no_module_entry(tmp_path: Path) -> None:
    """A helper file that raises mid-execution must not stay in sys.modules."""
    bad_file = tmp_path / "boom_helpers.py"
    bad_file.write_text(
        "def partial():\n"
        "    return 1\n"
        "raise RuntimeError('boom during import')\n",
        encoding="utf-8",
    )

    loaded = load_helpers(global_helpers_path=bad_file)
    mod_name = "_quartz_helpers_global_boom_helpers"
    try:
        assert "read" in loaded.functions  # builtins survive
        assert loaded.warnings and "boom during import" in loaded.warnings[0]
        assert mod_name not in sys.modules
    finally:
        sys.modules.pop(mod_name, None)


def test_reloading_same_helper_file_replaces_module_entry(tmp_path: Path) -> None:
    """Repeated load_helpers calls (e.g. /helpers) must not leak stale modules.

    The rewritten source deliberately differs in length: CPython invalidates a
    cached .pyc by (mtime rounded to seconds, source size), so a same-size edit
    made within the same second would legitimately reuse the old bytecode.
    """
    helper_file = tmp_path / "reload_helpers.py"
    helper_file.write_text(
        "def version():\n"
        "    \"\"\"Return 1.\"\"\"\n"
        "    return 1\n",
        encoding="utf-8",
    )
    mod_name = "_quartz_helpers_global_reload_helpers"
    try:
        first = load_helpers(global_helpers_path=helper_file)
        assert first.functions["version"]() == 1
        first_mod = sys.modules[mod_name]

        helper_file.write_text(
            "def version():\n"
            "    \"\"\"Return two, from the rewritten helper file.\"\"\"\n"
            "    return 2\n",
            encoding="utf-8",
        )
        second = load_helpers(global_helpers_path=helper_file)
        assert second.functions["version"]() == 2
        assert sys.modules[mod_name] is not first_mod
    finally:
        sys.modules.pop(mod_name, None)


def test_same_stem_global_and_project_helpers_coexist(tmp_path: Path) -> None:
    """Global and project helper files share the stem 'helpers' but not a module name."""
    global_file = tmp_path / "global" / "helpers.py"
    global_file.parent.mkdir()
    global_file.write_text(
        "def from_global():\n"
        "    \"\"\"Global.\"\"\"\n"
        "    return 'global'\n",
        encoding="utf-8",
    )
    project_dir = tmp_path / ".quartz"
    project_dir.mkdir()
    (project_dir / "helpers.py").write_text(
        "def from_project():\n"
        "    \"\"\"Project.\"\"\"\n"
        "    return 'project'\n",
        encoding="utf-8",
    )

    loaded = load_helpers(cwd=tmp_path, global_helpers_path=global_file)
    try:
        assert loaded.functions["from_global"]() == "global"
        assert loaded.functions["from_project"]() == "project"
        assert loaded.warnings == []
    finally:
        sys.modules.pop("_quartz_helpers_global_helpers", None)
        sys.modules.pop("_quartz_helpers_project_helpers", None)


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




def _pycheck(src: str, name: str = "m.py") -> list[str]:
    from agent.pycheck import check_python_source

    return check_python_source(Path("/tmp") / name, src)


def test_pycheck_symtable_scoping_cases() -> None:
    assert _pycheck("def f():\n    return missing_a\n") == [
        "line 2: undefined name 'missing_a' (missing import?)"
    ]
    # class body vs. method scope
    src = "class A:\n    x = 1\n    y = x + 1\n    def m(self):\n        return y\n"
    assert [w for w in _pycheck(src) if "'y'" in w] == ["line 5: undefined name 'y' (missing import?)"]
    # comprehensions (PEP 709), walrus, match, type alias, future annotations
    ok = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from collections import OrderedDict\n"
        "def f(a: OrderedDict, b: Later) -> Later:\n"
        "    vals = [y for y in range(3) if (z := y)]\n"
        "    match a:\n        case {'k': v, **rest}:\n            print(v, rest, z)\n"
        "        case [first, *others] | first:\n            print(first, others)\n"
        "    return vals\n"
    )
    assert _pycheck(ok) == []
    assert _pycheck("type Alias[T] = list[T]\nx: Alias[int] = []\n") == []
    assert _pycheck("def f(:\n") and _pycheck("def f(:\n")[0].startswith("SyntaxError")
    assert _pycheck("x = 1\n", "notes.txt") == []


def test_pycheck_quartz_helpers_allows_builtins(tmp_path: Path) -> None:
    from agent.pycheck import check_python_source

    p = tmp_path / ".quartz" / "helpers.py"
    assert check_python_source(p, "def h():\n    return read('x') + str(Path('.'))\n") == []
    assert check_python_source(tmp_path / "other.py", "def h():\n    return read('x')\n")


def test_atomic_write_never_touches_process_umask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import stat

    from agent.helpers import _atomic_write

    def boom(_mask: int) -> int:
        raise AssertionError("os.umask must not be called")

    monkeypatch.setattr(os, "umask", boom)
    target = tmp_path / "new.txt"
    _atomic_write(target, "hi\n")
    assert stat.S_IMODE(target.stat().st_mode) in (0o644, 0o664, 0o666 & ~0o022, 0o666 & ~0o002)
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_atomic_write_cleans_tmp_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from agent.helpers import _atomic_write

    def bad_replace(*_a: object) -> None:
        raise OSError("nope")

    monkeypatch.setattr(os, "replace", bad_replace)
    with pytest.raises(OSError):
        _atomic_write(tmp_path / "f.txt", "x")
    assert list(tmp_path.iterdir()) == []


def test_ls_find_rg_error_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from agent import helpers

    f = tmp_path / "a.txt"
    f.write_text("hello\n")
    with pytest.raises(FileNotFoundError, match="No such directory"):
        helpers.ls(str(tmp_path / "nope"))
    with pytest.raises(NotADirectoryError):
        helpers.ls(str(f))
    with pytest.raises(NotADirectoryError):
        helpers.find("*", str(f))
    assert helpers.rg("x", str(tmp_path / "nope")).startswith("Path not found")

    # python fallback: invalid regex handled, fixed search works
    monkeypatch.setattr(helpers.shutil, "which", lambda _n: None)
    assert helpers.rg("(", str(tmp_path)).startswith("Invalid regex")
    assert "a.txt:1:hello" in helpers.rg("hello", str(tmp_path))
    assert helpers.rg("(", str(tmp_path), fixed=True) == "(no matches)"

    # external rg timeout
    monkeypatch.setattr(helpers.shutil, "which", lambda _n: "/usr/bin/rg")

    def slow(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired("rg", 15)

    monkeypatch.setattr(helpers.subprocess, "run", slow)
    assert "timed out" in helpers.rg("hello", str(tmp_path))


def test_find_skips_unreadable_and_symlink_dirs(tmp_path: Path) -> None:
    import os

    from agent.helpers import find

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "x.py").write_text("")
    os.symlink(tmp_path / "real", tmp_path / "link")
    os.symlink(tmp_path / "missing", tmp_path / "broken.py")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "y.py").write_text("")
    os.chmod(locked, 0)
    try:
        hits = find("*.py", str(tmp_path))
    finally:
        os.chmod(locked, 0o755)
    assert "real/x.py" in hits
    assert not any(h.startswith("link/") for h in hits)
