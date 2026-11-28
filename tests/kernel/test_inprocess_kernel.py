"""Tests for stateless InProcessKernel execution and headless inline bash."""

from __future__ import annotations

from pathlib import Path

from quartz.kernel.inprocess import InProcessKernel


async def test_inprocess_kernel_exec_await_and_stateless_isolation(tmp_path: Path) -> None:
    k = InProcessKernel(cwd=tmp_path)
    k.start()
    try:
        k.configure(headless=True)
        r1 = await k.exec_cell("x = 21 * 2\nx")
        assert r1.ok and r1.result == "42"

        # Stateless: variables do not leak across cells
        r2 = await k.exec_cell("await asyncio.sleep(0.01)\nprint('has_x', 'x' in globals())")
        assert r2.ok and "has_x False" in r2.stdout

        # Headless bash() runs inline and returns output
        r3 = await k.exec_cell("res = await bash('echo hello-inprocess')\nprint(res.output.strip())")
        assert r3.ok and "hello-inprocess" in r3.stdout

        assert "helpers" in k.list_helpers()
    finally:
        k.close()
