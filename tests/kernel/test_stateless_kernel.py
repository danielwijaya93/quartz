"""Red-first tests for the stateless one-shot Python kernel and host-managed bash jobs."""

from __future__ import annotations

import asyncio
import importlib.util
import tomllib
from pathlib import Path

import pytest

from quartz.kernel.base import CellExecutor, Kernel, KernelIntrospector
from quartz.kernel.client import KernelClient
from quartz.kernel.inprocess import InProcessKernel
from quartz.kernel.sandbox import SandboxConfig, detect_sandbox_backend


async def _wait_for_event(kernel: KernelClient, kind: str, timeout: float = 5.0) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        for ev in kernel.drain_events():
            if ev.get("event") == kind:
                return ev
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"Timed out waiting for kernel event {kind!r}")
        await kernel.wait_event(timeout=min(remaining, 0.5))


async def test_stateless_isolation_vars_cwd_and_env(tmp_path: Path) -> None:
    sub = tmp_path / "subdir"
    sub.mkdir()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r1 = await kernel.exec_cell(
            f"x = 42\nos.chdir({str(sub)!r})\nos.environ['QUARTZ_EPHEMERAL'] = 'yes'\nprint(x, Path.cwd().name)"
        )
        assert r1.ok, r1.stderr
        assert "42 subdir" in r1.stdout

        r2 = await kernel.exec_cell(
            "import os\n"
            "has_x = 'x' in globals()\n"
            "print(has_x, Path.cwd().resolve(), os.environ.get('QUARTZ_EPHEMERAL'))"
        )
        assert r2.ok, r2.stderr
        assert f"False {tmp_path.resolve()} None" in r2.stdout
    finally:
        await kernel.aclose()


async def test_zero_idle_worker_processes(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        # No long-lived worker process should be running when idle.
        assert getattr(kernel, "_active_proc", None) is None
        assert getattr(kernel, "_raw_proc", None) is None

        res = await kernel.exec_cell("print(1 + 2)")
        assert res.ok and res.stdout.strip() == "3"

        # After cell completion, no subprocess remains alive.
        assert getattr(kernel, "_active_proc", None) is None
        assert getattr(kernel, "_raw_proc", None) is None
    finally:
        await kernel.aclose()


async def test_tool_chaining_auto_import_and_top_level_await(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = await kernel.exec_cell(
            "write('a.txt', 'alpha\\nbeta\\ngamma\\n')\n"
            "edit('a.txt', 'beta', 'BETA')\n"
            "txt = read('a.txt', raw=True)\n"
            "hits = rg('BETA', '.')\n"
            "await asyncio.sleep(0.01)\n"
            "json.dumps({'lines': len(txt.splitlines()), 'hit': 'BETA' in hits})"
        )
        assert res.ok, res.stderr
        assert '"lines": 3' in (res.result or "")
        assert '"hit": true' in (res.result or "")
    finally:
        await kernel.aclose()


async def test_host_managed_background_bash_survives_cell_exit(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r1 = await kernel.exec_cell("h = bash('sleep 0.25 && echo BG_SURVIVED')\nprint(h.pid)")
        assert r1.ok, r1.stderr
        pid = int(r1.stdout.strip())
        assert pid > 0
        # Cell exited, yet job is still tracked on host.
        assert kernel.running_bash_count() >= 1
        jobs = kernel.running_bash_jobs()
        assert any(j.get("pid") == pid for j in jobs)

        ev = await _wait_for_event(kernel, "bash_done", timeout=5.0)
        assert ev["pid"] == pid
        assert ev["exit_code"] == 0
        assert "BG_SURVIVED" in str(ev.get("tail", ""))
        assert kernel.running_bash_count() == 0
    finally:
        await kernel.aclose()


async def test_awaited_bash_inside_cell_emits_no_duplicate_done_event(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r = await kernel.exec_cell("res = await bash('echo INLINE_AWAIT')\nprint(res.exit_code, res.output.strip())")
        assert r.ok, r.stderr
        assert "0 INLINE_AWAIT" in r.stdout
        await asyncio.sleep(0.05)
        done_events = [e for e in kernel.drain_events() if e.get("event") == "bash_done"]
        assert done_events == []
    finally:
        await kernel.aclose()


async def test_remote_bash_handle_methods_inside_cell(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r = await kernel.exec_cell(
            "h = bash('echo line1; sleep 0.2; echo line2')\n"
            "assert h.pid > 0\n"
            "assert h.running\n"
            "res = await h\n"
            "assert not h.running\n"
            "assert h.poll() is not None\n"
            "assert 'line2' in h.tail(5)\n"
            "assert 'line1' in h.output()\n"
            "print('ok', res.exit_code)"
        )
        assert r.ok, r.stderr
        assert "ok 0" in r.stdout
    finally:
        await kernel.aclose()


async def test_headless_bash_runs_inline_and_refuses_launch_worker(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.configure(headless=True)
    kernel.start()
    try:
        r1 = await kernel.exec_cell("h = bash('echo HEADLESS_OK')")
        assert r1.ok, r1.stderr
        assert "HEADLESS_OK" in r1.stdout

        r2 = await kernel.exec_cell("launch_worker('p.md', '.', 'sid-1')")
        assert not r2.ok
        assert "headless" in r2.stderr.lower() or "-p" in r2.stderr
    finally:
        await kernel.aclose()


async def test_cell_crash_does_not_affect_next_cell(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r1 = await kernel.exec_cell("import os; os._exit(9)")
        assert not r1.ok
        r2 = await kernel.exec_cell("print('still_clean')")
        assert r2.ok and "still_clean" in r2.stdout
    finally:
        await kernel.aclose()


async def test_cell_timeout_and_interrupt(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r_to = await kernel.exec_cell("import time; time.sleep(10)", timeout=0.3)
        assert not r_to.ok
        assert "timed out" in r_to.stderr.lower()

        task = asyncio.create_task(kernel.exec_cell("import time; time.sleep(10)", timeout=10.0))
        await asyncio.sleep(0.15)
        kernel.interrupt()
        r_int = await asyncio.wait_for(task, timeout=3.0)
        assert not r_int.ok

        r_next = await kernel.exec_cell("print('recovered')")
        assert r_next.ok and "recovered" in r_next.stdout
    finally:
        await kernel.aclose()


async def test_inprocess_kernel_is_stateless(tmp_path: Path) -> None:
    sub = tmp_path / "inner"
    sub.mkdir()
    kernel = InProcessKernel(cwd=tmp_path)
    kernel.start()
    try:
        r1 = await kernel.exec_cell(f"v = 99\nos.chdir({str(sub)!r})\nprint(v, Path.cwd().name)")
        assert r1.ok and "99 inner" in r1.stdout
        r2 = await kernel.exec_cell("print('v' in globals(), Path.cwd().resolve())")
        assert r2.ok
        assert f"False {tmp_path.resolve()}" in r2.stdout
    finally:
        kernel.close()


async def test_sandboxed_cell_and_host_bash_enforce_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if detect_sandbox_backend() != "bwrap":
        pytest.skip("Requires Linux bwrap sandbox")

    fake_home = tmp_path / "home"
    quartz_dir = fake_home / ".quartz"
    quartz_dir.mkdir(parents=True)
    secret_file = quartz_dir / "web-token"
    secret_file.write_text("SUPER_SECRET_TOKEN", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-do-not-leak")

    kernel = KernelClient(cwd=work)
    kernel.enable_sandbox(SandboxConfig(enabled=True))
    kernel.start()
    try:
        # 1. Direct Python cell cannot see secret file or env secret
        r1 = await kernel.exec_cell(
            f"import os\n"
            f"print('TOKEN_EXISTS', Path({str(secret_file)!r}).exists())\n"
            f"print('KEY_ENV', os.environ.get('OPENAI_API_KEY'))"
        )
        assert r1.ok, r1.stderr
        assert "TOKEN_EXISTS False" in r1.stdout
        assert "KEY_ENV None" in r1.stdout

        # 2. Host-managed background bash() ALSO runs inside bwrap sandbox!
        r2 = await kernel.exec_cell(
            f"res = await bash('test -f {secret_file} && echo LEAKED || echo MASKED; echo KEY=$OPENAI_API_KEY')\n"
            "print(res.output)"
        )
        assert r2.ok, r2.stderr
        assert "MASKED" in r2.stdout
        assert "sk-secret-do-not-leak" not in r2.stdout
    finally:
        await kernel.aclose()


def test_snapshots_module_and_dill_removed(tmp_path: Path) -> None:
    assert importlib.util.find_spec("quartz.kernel.snapshots") is None
    root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    assert not any(d.lower().startswith("dill") for d in deps)
    client = KernelClient(cwd=tmp_path)
    assert isinstance(client, CellExecutor)
    assert isinstance(client, KernelIntrospector)
    assert isinstance(client, Kernel)
    assert not hasattr(client, "snapshot")
    assert not hasattr(client, "restore")
    assert not hasattr(client, "list_vars")
