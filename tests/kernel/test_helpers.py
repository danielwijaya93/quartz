"""Tests for pluggable REPL helpers, dynamic loading, and system prompt integration."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from quartz.core.prompt import build_system_prompt
from quartz.core.runtime import AgentRuntime as _CLIRuntime
from quartz.core.session import Session
from quartz.kernel.client import KernelClient
from quartz.kernel.helpers import (
    format_helpers_for_prompt,
    load_helpers,
)
from quartz.tui.commands import _handle_slash_command


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

    loaded = load_helpers(cwd=tmp_path, global_helpers_path=Path("/nonexistent"), trusted=True)
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

    loaded = load_helpers(cwd=tmp_path, global_helpers_path=Path("/nonexistent"), trusted=True)
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

    loaded = load_helpers(cwd=tmp_path, global_helpers_path=global_file, trusted=True)
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

    prompt = build_system_prompt(tmp_path, global_helpers_path=Path("/nonexistent"), trusted=True)
    assert "## REPL Functions" in prompt
    assert "Custom user helpers:" in prompt
    assert "`deploy_db(env: str) -> bool`: Migrate database for target environment. (project)" in prompt


async def test_kernel_executes_custom_project_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARTZ_TRUST_PROJECT", "1")
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
        res = await kernel.exec_cell("multiply(6, 7)")
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
    from quartz.core.loop import TurnUsage
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

    loaded = load_helpers(cwd=tmp_path, global_helpers_path=Path("/nonexistent"), trusted=True)
    assert "read_first_line" in loaded.functions
    res = loaded.functions["read_first_line"](str(sample_file))
    assert "1: first line" in res


async def test_custom_helper_can_call_bash_in_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARTZ_TRUST_PROJECT", "1")
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
        res = await kernel.exec_cell("await custom_pwd()")
        assert res.ok
        assert str(tmp_path.resolve()) == res.result.strip("'\"")
    finally:
        kernel.close()


def test_edit_and_write_atomic_and_crlf_safe(tmp_path: Path) -> None:
    from quartz.kernel.helpers import edit, write

    # Test atomic write
    f = tmp_path / "crlf_test.txt"
    write(str(f), "hello\r\nworld\r\n")
    assert f.read_bytes() == b"hello\r\nworld\r\n"

    # Test edit with LF old/new string adapting to CRLF file
    edit(str(f), "world", "quartz")
    raw = f.read_bytes()
    assert raw == b"hello\r\nquartz\r\n"
    assert b"\r\n" in raw
    assert raw.count(b"\r\n") == 2

    # Test multiline edit with LF in arguments preserves CRLF in file
    edit(str(f), "hello\nquartz", "greeting\ngalaxy")
    raw2 = f.read_bytes()
    assert raw2 == b"greeting\r\ngalaxy\r\n"
    assert b"\n" not in raw2.replace(b"\r\n", b"")

def test_rg_fixed_literal_search(tmp_path: Path) -> None:
    from unittest.mock import patch

    from quartz.kernel.helpers import rg

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
    from quartz.core.prompt import PROMPT_RULES

    assert "Batch and chain operations generously into one cell" in PROMPT_RULES
    assert "Prioritize available REPL helpers and primitives" in PROMPT_RULES


def test_write_and_edit_py_check_missing_import(tmp_path: Path) -> None:
    from quartz.kernel.helpers import edit, write

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
    from quartz.kernel.helpers import _check_python_source, write

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
    repo_root = Path(__file__).resolve().parent.parent.parent
    for py_path in sorted((repo_root / "src" / "quartz").rglob("*.py")):
        warnings = _check_python_source(py_path, py_path.read_text(encoding="utf-8"))
        assert warnings == [], f"Unexpected warnings for {py_path}: {warnings}"


def test_write_py_check_syntax_error_and_non_py_skip(tmp_path: Path) -> None:
    from quartz.kernel.helpers import write

    bad_py = tmp_path / "bad.py"
    out_bad = write(str(bad_py), "def broken(:\n    pass\n")
    assert "[python check]" in out_bad
    assert "SyntaxError" in out_bad
    assert bad_py.exists()

    txt_file = tmp_path / "notes.txt"
    out_txt = write(str(txt_file), "undefined_name_here()\ndef broken(:\n")
    assert "[python check]" not in out_txt


def test_system_prompt_mentions_stdlib_auto_import_and_explicit_py_imports(tmp_path: Path) -> None:
    prompt = build_system_prompt(tmp_path, global_helpers_path=Path("/nonexistent"), trusted=True)
    assert "auto-imported" in prompt
    assert "explicit imports are still required" in prompt


def test_edit_preserves_mode_and_write_respects_umask(tmp_path: Path) -> None:
    import os
    import stat

    from quartz.kernel.helpers import edit, write

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
    from quartz.kernel.helpers import rg

    target = tmp_path / "flags.txt"
    target.write_text("--verbose flag\n-n line\n", encoding="utf-8")

    out_fixed = rg("--verbose", str(tmp_path), fixed=True)
    assert "--verbose flag" in out_fixed

    out_regex = rg("-n", str(tmp_path), fixed=False)
    assert "-n line" in out_regex




def _pycheck(src: str, name: str = "m.py") -> list[str]:
    from quartz.kernel.pycheck import check_python_source

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
    from quartz.kernel.pycheck import check_python_source

    p = tmp_path / ".quartz" / "helpers.py"
    assert check_python_source(p, "def h():\n    return read('x') + str(Path('.'))\n") == []
    assert check_python_source(tmp_path / "other.py", "def h():\n    return read('x')\n")


def test_atomic_write_never_touches_process_umask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import stat

    from quartz.kernel.helpers import _atomic_write

    def boom(_mask: int) -> int:
        raise AssertionError("os.umask must not be called")

    monkeypatch.setattr(os, "umask", boom)
    target = tmp_path / "new.txt"
    _atomic_write(target, "hi\n")
    assert stat.S_IMODE(target.stat().st_mode) in (0o644, 0o664, 0o666 & ~0o022, 0o666 & ~0o002)
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_atomic_write_cleans_tmp_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from quartz.kernel.helpers import _atomic_write

    def bad_replace(*_a: object) -> None:
        raise OSError("nope")

    monkeypatch.setattr(os, "replace", bad_replace)
    with pytest.raises(OSError):
        _atomic_write(tmp_path / "f.txt", "x")
    assert list(tmp_path.iterdir()) == []


def test_ls_find_rg_error_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from quartz.kernel import helpers

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


def test_rg_expands_tilde_for_external_rg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quartz.kernel import helpers

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "note.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(helpers.shutil, "which", lambda _n: "/usr/bin/rg")

    seen_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: object) -> Any:
        seen_cmds.append(cmd)
        return type("Proc", (), {"returncode": 0, "stdout": f"{sub / 'note.txt'}:1:needle\n", "stderr": ""})()

    monkeypatch.setattr(helpers.subprocess, "run", fake_run)
    out = helpers.rg("needle", "~/sub")
    assert "needle" in out
    assert seen_cmds and seen_cmds[0][-1] == str(sub)


def test_kernel_client_close_unregisters_atexit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import atexit

    from quartz.kernel.client import KernelClient

    unregistered: list[Any] = []
    monkeypatch.setattr(atexit, "unregister", unregistered.append)

    client = KernelClient(cwd=tmp_path)
    client._atexit_registered = True
    client.close()

    assert client._atexit_registered is False
    assert unregistered == [client.close]


def test_find_skips_unreadable_and_symlink_dirs(tmp_path: Path) -> None:
    import os

    from quartz.kernel.helpers import find

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


def test_untrusted_project_helpers_are_skipped_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)

    marker = tmp_path / "executed.txt"
    project_dir = tmp_path / ".quartz"
    project_dir.mkdir(parents=True)
    (project_dir / "helpers.py").write_text(
        f"from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('pwned', encoding='utf-8')\n"
        "def evil():\n    return 'evil'\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="Skipped untrusted project helpers"):
        loaded = load_helpers(cwd=tmp_path, global_helpers_path=Path("/nonexistent"))

    assert not marker.exists()
    assert "evil" not in loaded.functions
    assert any("Skipped untrusted project helpers" in w for w in loaded.warnings)

    # Once trusted via env, it loads cleanly.
    monkeypatch.setenv("QUARTZ_TRUST_PROJECT", "1")
    loaded_trusted = load_helpers(cwd=tmp_path, global_helpers_path=Path("/nonexistent"))
    assert marker.exists()
    assert loaded_trusted.functions["evil"]() == "evil"


def test_find_and_rg_skip_build_and_clip_long_lines(tmp_path: Path) -> None:
    from quartz.kernel.helpers import find, rg

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("const needle = 1;\n", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text(
        f"const needle = 2; {'x' * 500}\n", encoding="utf-8"
    )
    (tmp_path / ".tooling").mkdir()
    (tmp_path / ".tooling" / "sdk.txt").write_text("needle\n", encoding="utf-8")

    found = find("*", str(tmp_path))
    assert "src/app.ts" in found
    assert not any(f.startswith(("dist/", ".tooling/")) for f in found)

    hits = rg("needle", str(tmp_path))
    assert "app.ts" in hits
    assert "bundle.js" not in hits
    assert "sdk.txt" not in hits

    # Explicit target inside dist/ still works, but long lines are clipped
    dist_hits = rg("needle", str(tmp_path / "dist"))
    assert "bundle.js" in dist_hits
    assert len(dist_hits) < 350


def test_read_and_edit_output_caps(tmp_path: Path) -> None:
    from quartz.kernel.helpers import MAX_EDIT_DIFF_CHARS, MAX_READ_CHARS, edit, read

    big_file = tmp_path / "big.txt"
    big_file.write_text("\n".join(f"line {i} {'a' * 100}" for i in range(300)), encoding="utf-8")

    out = read(str(big_file), start=1, end=300)
    assert "truncated at" in out
    assert f"{MAX_READ_CHARS:,} chars" in out
    assert "continue with read(" in out
    assert len(out) < MAX_READ_CHARS + 300

    # Large edit diff is capped at MAX_EDIT_DIFF_CHARS
    edit_file = tmp_path / "edit_big.txt"
    old_block = "\n".join(f"old line {i} {'b' * 60}" for i in range(60))
    new_block = "\n".join(f"new line {i} {'c' * 60}" for i in range(60))
    edit_file.write_text(old_block, encoding="utf-8")

    edit_out = edit(str(edit_file), old_block, new_block)
    assert "chars truncated" in edit_out
    assert len(edit_out) <= MAX_EDIT_DIFF_CHARS + 200


def test_worker_output_limits_and_prompt_rules() -> None:
    from quartz.core.prompt import PROMPT_RULES
    from quartz.kernel.worker import MAX_OUTPUT_CHARS, MAX_REPR_CHARS

    assert MAX_OUTPUT_CHARS == 24_000
    assert MAX_REPR_CHARS == 24_000
    assert "Keep tool outputs lean" in PROMPT_RULES
    assert "pytest -q --tb=short" in PROMPT_RULES


def test_edit_trailing_whitespace_fallback_and_diagnostics(tmp_path: Path) -> None:
    from quartz.kernel.helpers import edit

    f = tmp_path / "sample.py"
    f.write_text("def alpha():  \n    return 1\n\ndef beta():\n    return 2\n", encoding="utf-8")

    # Trailing-whitespace-only mismatch succeeds when unique
    out = edit(str(f), "def alpha():\n    return 1", "def alpha():\n    return 10")
    assert "return 10" in f.read_text(encoding="utf-8")
    assert "Updated" in out

    # Missing target includes closest line-numbered snippet
    with pytest.raises(ValueError, match=r"Closest match around line"):
        edit(str(f), "def beta():\n    return 999", "def beta():\n    return 3")

    # Ambiguous target includes matching line numbers
    f2 = tmp_path / "dup.py"
    f2.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"lines 1, 3"):
        edit(str(f2), "x = 1", "x = 2")


def test_ls_find_rg_accept_cwd(tmp_path: Path) -> None:
    from quartz.kernel.helpers import find, ls, rg

    sub = tmp_path / "proj"
    sub.mkdir()
    (sub / "a.py").write_text("needle = 1\n", encoding="utf-8")

    assert ls(".", cwd=sub) == ["a.py"]
    assert find("*.py", ".", cwd=sub) == ["a.py"]
    assert "needle = 1" in rg("needle", ".", cwd=sub)
    assert rg("needle", ".", cwd=tmp_path / "missing").startswith("Path not found")


def test_path_helpers_include_closest_match_hints(tmp_path: Path) -> None:
    from quartz.kernel.helpers import ls, read, rg

    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "helpers.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match=r"helpers\.py"):
        read(str(sub / "helper.py"))

    with pytest.raises(FileNotFoundError, match=r"src"):
        ls(str(tmp_path / "srcc"))

    rg_out = rg("x", str(sub / "helper.py"))
    assert rg_out.startswith("Path not found") and "helpers.py" in rg_out


async def test_kernel_module_not_found_hints_project_venv(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = await kernel.exec_cell("import nonexistent_pkg_xyz_123")
        assert not res.ok
        assert ".venv/bin/python" in res.stderr
    finally:
        kernel.close()


def test_unified_read_raw_and_end_none(tmp_path: Path) -> None:
    from quartz.kernel.helpers import edit, read

    f = tmp_path / "sample.py"
    lines = [f"line_{i} = {i}" for i in range(1, 12)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # raw=True omits line numbers and marks remaining lines when end stops early
    raw_chunk = read(f, start=2, end=4, raw=True)
    assert raw_chunk.startswith("line_2 = 2\nline_3 = 3\nline_4 = 4")
    assert "2:" not in raw_chunk
    assert "7 more lines" in raw_chunk
    assert "raw=True" in raw_chunk

    # Output of raw=True feeds directly into edit()
    exact_block = read(f, start=2, end=3, raw=True).splitlines()[0:2]
    edit_res = edit(str(f), "\n".join(exact_block), "line_2 = 20\nline_3 = 30")
    assert "Updated" in edit_res

    # end=None reads to EOF without truncation marker
    full_raw = read(f, start=1, end=None, raw=True)
    assert "line_11 = 11" in full_raw
    assert "more lines" not in full_raw


def test_unified_read_multiple_paths_and_errors(tmp_path: Path) -> None:
    import pytest

    from quartz.kernel.helpers import read

    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    b.write_text("one\ntwo\n", encoding="utf-8")
    missing = tmp_path / "missing.txt"

    with pytest.raises(ValueError, match="at least one path"):
        read()

    with pytest.raises(FileNotFoundError):
        read(missing)

    multi = read(a, missing, b, start=1, end=2)
    assert f"=== {a} (1-2 of 3 lines) ===" in multi
    assert "1: alpha\n2: beta" in multi
    assert "1 more lines" in multi
    assert f"=== {missing} ===" in multi
    assert "[missing: no such file" in multi
    assert f"=== {b} (1-2 of 2 lines) ===" in multi
    assert "1: one\n2: two" in multi

    multi_raw = read(a, b, start=2, end=2, raw=True)
    assert "beta" in multi_raw and "2: beta" not in multi_raw
    assert "two" in multi_raw and "2: two" not in multi_raw


def test_prompt_rules_and_helpers_reflect_unified_read(tmp_path: Path) -> None:
    from quartz.core.prompt import PROMPT_RULES, build_system_prompt
    from quartz.kernel.helpers import LoadedHelpers, format_helpers_for_prompt

    formatted = format_helpers_for_prompt(LoadedHelpers(functions={}, infos={}))
    assert "read(*paths, start=1, end=200, raw=False)" in formatted

    assert "raw=True" in PROMPT_RULES
    assert "CODEBASE_MAP.md" in PROMPT_RULES
    assert "await bash(" in PROMPT_RULES

    (tmp_path / ".quartz").mkdir()
    (tmp_path / ".quartz" / "mcp.json").write_text('{"servers": {"demo": {"command": "echo"}}}', encoding="utf-8")
    prompt = build_system_prompt(cwd=tmp_path, trusted=True)
    assert "mcp.search_tools(query)" in prompt
    assert "no extra kwargs" in prompt
