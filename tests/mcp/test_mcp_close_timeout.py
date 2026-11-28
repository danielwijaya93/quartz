"""Tests for bounded MCP teardown and io_loop-affined close."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from quartz.kernel.client import KernelClient
from quartz.kernel.io_loop import get_kernel_io_loop
from quartz.mcp.client import MCPManager


class _HangingStack:
    async def aclose(self) -> None:
        await asyncio.Future()


@pytest.mark.asyncio
async def test_mcp_manager_close_bounds_hanging_stack(tmp_path: Path) -> None:
    mgr = MCPManager(tmp_path)
    mgr._stacks["hanging"] = _HangingStack()  # type: ignore[assignment]
    mgr._sessions["hanging"] = object()  # type: ignore[assignment]

    await asyncio.wait_for(mgr.close(timeout=0.05), timeout=1.0)
    assert mgr._stacks == {}
    assert mgr._sessions == {}


def test_kernel_client_close_closes_mcp_on_io_loop(tmp_path: Path) -> None:
    io_loop = get_kernel_io_loop()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    class _FakeMCP:
        async def close(self) -> None:
            observed_loops.append(asyncio.get_running_loop())

    client = KernelClient(cwd=tmp_path)
    client._io_loop = io_loop
    client._host_rpc._host_mcp = _FakeMCP()  # type: ignore[assignment]

    client.close()
    assert observed_loops == [io_loop]
