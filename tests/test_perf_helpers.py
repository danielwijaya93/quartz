"""Tests for kernel helpers, concurrent cell execution, and parallel tool dispatch."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent.actions import describe_cell_actions
from agent.kernel_client import KernelClient


def test_kernel_helpers_ls_find_rg_read_edit_write(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "hello.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res_ls = kernel.exec_cell("ls()")
        assert res_ls.ok
        assert "sub/" in (res_ls.result or "")

        res_find = kernel.exec_cell("find('*.py')")
        assert res_find.ok
        assert "sub/hello.py" in (res_find.result or "")

        res_rg = kernel.exec_cell("rg('beta')")
        assert res_rg.ok
        assert "hello.py" in (res_rg.result or "")

        res_read = kernel.exec_cell("read('sub/hello.py', 1, 2)")
        assert res_read.ok
        assert "1: alpha = 1" in (res_read.result or "")
        assert "2: beta = 2" in (res_read.result or "")

        res_edit = kernel.exec_cell("edit('sub/hello.py', 'beta = 2', 'beta = 42')")
        assert res_edit.ok
        assert "beta = 42" in (tmp_path / "sub" / "hello.py").read_text(encoding="utf-8")

        res_write = kernel.exec_cell("write('sub/new.txt', 'hi')")
        assert res_write.ok
        assert (tmp_path / "sub" / "new.txt").read_text(encoding="utf-8") == "hi"
    finally:
        kernel.close()


def test_parallel_exec_cell_isolates_stdout(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        code_a = "import asyncio\nprint('A1')\nawait asyncio.sleep(0.05)\nprint('A2')"
        code_b = "import asyncio\nprint('B1')\nawait asyncio.sleep(0.05)\nprint('B2')"
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(kernel.exec_cell, code_a)
            fb = pool.submit(kernel.exec_cell, code_b)
            ra = fa.result()
            rb = fb.result()

        assert ra.ok and rb.ok
        assert ra.stdout.splitlines() == ["A1", "A2"]
        assert rb.stdout.splitlines() == ["B1", "B2"]
    finally:
        kernel.close()


def test_action_classifier_recognizes_kernel_helpers() -> None:
    assert describe_cell_actions("ls('agent')") == "List directory and files"
    assert describe_cell_actions("find('*.py')") == "Find files"
    assert describe_cell_actions("rg('foo', 'agent')") == "Find files"
    assert describe_cell_actions("read('agent/cli.py', 1, 50)") == "Read files"
    assert describe_cell_actions("edit('a.py', 'x', 'y')") == "Edit files"
    assert describe_cell_actions("write('a.py', 'x')") == "Edit files"
