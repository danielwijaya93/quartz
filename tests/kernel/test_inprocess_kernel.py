"""Tests for InProcessKernel execution, snapshot, and restore."""

from __future__ import annotations

from pathlib import Path

from quartz.kernel.inprocess import InProcessKernel


async def test_inprocess_kernel_exec_await_snapshot_and_restore(tmp_path: Path) -> None:
    k = InProcessKernel(cwd=tmp_path)
    k.start()
    try:
        k.configure(headless=True)
        r1 = await k.exec_cell("x = 21 * 2\nx")
        assert r1.ok and r1.result == "42"

        r2 = await k.exec_cell("await asyncio.sleep(0.01)\nprint('hi', x)")
        assert r2.ok and "hi 42" in r2.stdout

        # Headless bash() runs inline and returns output
        r3 = await k.exec_cell("res = await bash('echo hello-inprocess')\nprint(res.output.strip())")
        assert r3.ok and "hello-inprocess" in r3.stdout

        assert "x" in k.list_vars()
        assert "helpers" in k.list_helpers()

        snap_dir = tmp_path / "snap"
        manifest = k.snapshot(snap_dir)
        assert "x" in manifest.get("saved", [])
    finally:
        k.close()

    k2 = InProcessKernel(cwd=tmp_path)
    k2.start()
    try:
        restored = k2.restore(tmp_path / "snap", root=tmp_path)
        assert "x" in restored["restored"]
        r4 = await k2.exec_cell("x")
        assert r4.ok and r4.result == "42"
    finally:
        k2.close()
