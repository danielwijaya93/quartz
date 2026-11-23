"""Tests for kernel helpers, concurrent cell execution, and parallel tool dispatch."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from quartz.kernel.actions import describe_cell_actions
from quartz.kernel.client import KernelClient
from quartz.core.loop import TOOLS
from quartz.core.prompt import build_system_prompt


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


def test_single_ipython_tool_and_headless_prompt(tmp_path: Path) -> None:
    assert [t["function"]["name"] for t in TOOLS] == ["ipython"]
    interactive = build_system_prompt(tmp_path, headless=False)
    headless = build_system_prompt(tmp_path, headless=True)
    assert "## Headless Mode" not in interactive
    assert "## Headless Mode" in headless


def test_parallel_cells_isolate_trailing_expr(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        code_a = "import asyncio\nawait asyncio.sleep(0.04)\n'result_from_A'"
        code_b = "import asyncio\nawait asyncio.sleep(0.02)\n'result_from_B'"
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(kernel.exec_cell, code_a)
            fb = pool.submit(kernel.exec_cell, code_b)
            ra = fa.result()
            rb = fb.result()

        assert ra.ok and rb.ok
        assert ra.result == "'result_from_A'"
        assert rb.result == "'result_from_B'"
    finally:
        kernel.close()


def test_kernel_captures_syntax_warning_in_cell_stderr(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        # Cell code with invalid escape sequences "\." that triggers SyntaxWarning in Python 3.12+
        res = kernel.exec_cell('x = "\\."\nrg_like = "\\d+"\nx')
        assert res.ok
        assert "SyntaxWarning" in res.stderr
        assert res.result == repr("\\.")
    finally:
        kernel.close()


def test_kernel_preimported_stdlib_available_without_imports(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell(
            "p = Path('file.txt')\n"
            "d = json.dumps({'key': 42})\n"
            "m = re.match(r'\\d+', '100')\n"
            "t = time.monotonic()\n"
            "o = os.path.join('a', 'b')\n"
            "s = sys.version_info.major\n"
            "sh = shutil.which('python3')\n"
            "is_async = asyncio.iscoroutinefunction(asyncio.sleep)\n"
            "(p.name, d, bool(m), o, is_async)"
        )
        assert res.ok
        assert "('file.txt', '{\"key\": 42}', True, 'a/b', True)" in (res.result or "")

        # Default bindings should not be listed as user variables in list_vars IPC
        vars_info = kernel.list_vars()
        assert "Path" not in vars_info
        assert "os" not in vars_info
        assert "json" not in vars_info
        assert "p" in vars_info
        assert "d" in vars_info
    finally:
        kernel.close()


def test_kernel_preimported_stdlib_preserved_across_snapshot(tmp_path: Path) -> None:
    kernel1 = KernelClient(cwd=tmp_path)
    kernel1.start()
    snap_dir = tmp_path / "snap"
    try:
        kernel1.exec_cell("user_num = 99")
        snap_res = kernel1.snapshot(snap_dir)
        assert "user_num" in snap_res.get("saved", [])
        # Pre-imported stdlibs should not be serialized into snapshot
        assert "Path" not in snap_res.get("saved", [])
        assert "os" not in snap_res.get("saved", [])
    finally:
        kernel1.close()

    kernel2 = KernelClient(cwd=tmp_path)
    kernel2.start()
    try:
        rest_res = kernel2.restore(snap_dir)
        assert "user_num" in rest_res.get("restored", [])
        # Check both restored user var and pre-imported stdlibs work in restored kernel
        res = kernel2.exec_cell("user_num == 99 and Path('.').is_dir() and os.name is not None and json.loads('1') == 1")
        assert res.ok
        assert res.result == "True"
    finally:
        kernel2.close()


def test_kernel_lazy_stdlib_auto_import_modules_and_symbols(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell(
            "dt = datetime.datetime.now()\n"
            "h = hashlib.sha256(b'x').hexdigest()\n"
            "q = urllib.parse.quote('a b')\n"
            "t = tomllib.loads('a = 1')\n"
            "sp = subprocess.run(['python3', '-c', 'print(123)'], capture_output=True, text=True)\n"
            "(dt.year >= 2020, len(h) == 64, q, t['a'], sp.stdout.strip())"
        )
        assert res.ok, res.stderr
        assert "(True, True, 'a%20b', 1, '123')" in (res.result or "")

        res_sym = kernel.exec_cell(
            "c = Counter('aab')\n"
            "dd = defaultdict(int)\n"
            "dd['k'] += 1\n"
            "@dataclass\n"
            "class Point:\n"
            "    x: int\n"
            "td = timedelta(days=1)\n"
            "(c['a'], dd['k'], Point(3).x, td.days)"
        )
        assert res_sym.ok, res_sym.stderr
        assert "(2, 1, 3, 1)" in (res_sym.result or "")
    finally:
        kernel.close()


def test_kernel_lazy_auto_import_guards_against_false_positives(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r1 = kernel.exec_cell("code.strip()")
        assert not r1.ok
        assert "NameError" in r1.stderr
        assert "AttributeError" not in r1.stderr

        r2 = kernel.exec_cell("types.append(1)")
        assert not r2.ok
        assert "NameError" in r2.stderr
        assert "AttributeError" not in r2.stderr
    finally:
        kernel.close()


def test_kernel_lazy_auto_import_shadowing_list_vars_and_snapshot(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    snap_dir = tmp_path / "snap_lazy"
    try:
        res = kernel.exec_cell(
            "math = 42\n"
            "digest = hashlib.sha256(b'hi').hexdigest()\n"
            "c = Counter('abb')\n"
            "(math, len(digest), c['b'])"
        )
        assert res.ok, res.stderr
        assert "(42, 64, 2)" in (res.result or "")

        vars_info = kernel.list_vars()
        assert vars_info.get("math") == "int"
        assert "hashlib" not in vars_info
        assert "Counter" not in vars_info

        snap_res = kernel.snapshot(snap_dir)
        assert "math" in snap_res.get("saved", [])
        assert "hashlib" not in snap_res.get("saved", [])
        assert "Counter" not in snap_res.get("saved", [])
    finally:
        kernel.close()


def test_kernel_lazy_auto_import_inside_function_and_blocklist(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell(
            "def f():\n"
            "    return hashlib.md5(b'a').hexdigest()\n"
            "f()"
        )
        assert res.ok, res.stderr
        assert len((res.result or "").strip("'\"")) == 32

        r_anti = kernel.exec_cell("antigravity.fly()")
        assert not r_anti.ok
        assert "NameError" in r_anti.stderr

        r_this = kernel.exec_cell("this.s")
        assert not r_this.ok
        assert "NameError" in r_this.stderr
    finally:
        kernel.close()



def test_kernel_auto_imports_dotted_stdlib_chains(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell("xml.etree.ElementTree.fromstring('<a><b/></a>').tag")
        assert res.ok, res.stderr
        assert res.result == "'a'"
        res = kernel.exec_cell("urllib.parse.quote('a b')")
        assert res.ok, res.stderr
        assert res.result == "'a%20b'"
        res = kernel.exec_cell("isinstance({}, collections.abc.Mapping)")
        assert res.ok, res.stderr
        assert res.result == "True"
        # locally bound name shadows stdlib module: no auto-import, no false binding
        res = kernel.exec_cell("def f(xml):\n    return xml.strip()\nf(' x ')")
        assert res.ok, res.stderr
        assert res.result == "'x'"
        res = kernel.exec_cell("antigravity.foo")
        assert not res.ok and "NameError" in res.stderr
    finally:
        kernel.close()


def test_concurrent_cells_are_serialized_without_cwd_interleaving(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        code_a = f"import asyncio, os\nos.chdir({str(tmp_path / 'a')!r})\nawait asyncio.sleep(0.15)\nos.path.basename(os.getcwd())"
        code_b = f"import os\nos.chdir({str(tmp_path / 'b')!r})\nos.path.basename(os.getcwd())"
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(kernel.exec_cell, code_a)
            fb = pool.submit(kernel.exec_cell, code_b)
            ra, rb = fa.result(), fb.result()
        assert ra.ok and rb.ok
        assert {ra.result, rb.result} == {"'a'", "'b'"}
        assert ra.result == "'a'" and rb.result == "'b'"
    finally:
        kernel.close()
