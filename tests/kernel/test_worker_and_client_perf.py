"""Kernel worker launch, action classification, event wake, and shared MCP pool tests."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from quartz.kernel.actions import _describe_shell_command
from quartz.kernel.client import KernelClient
from quartz.mcp.pool import close_shared_host_mcp_pool, get_shared_host_mcp_pool


def test_launch_worker_uses_python_module_instead_of_uv_run(tmp_path: Path) -> None:
    from quartz.kernel.worker import KernelWorker

    captured: list[str] = []

    worker = KernelWorker.__new__(KernelWorker)
    worker.headless = False

    def _fake_start_bash(cmd: str, timeout: float | None = None) -> Any:
        captured.append(cmd)
        return "handle"

    worker._start_bash = _fake_start_bash  # type: ignore[method-assign]
    out = worker._launch_worker(str(tmp_path / "p.md"), str(tmp_path), "sess-123")
    assert out == "handle"
    assert len(captured) == 1
    cmd = captured[0]
    assert "uv run" not in cmd
    assert "-m quartz -p" in cmd
    assert "sess-123" in cmd


def test_action_classifier_recognizes_python_m_quartz() -> None:
    label = _describe_shell_command(
        '/opt/venv/bin/python -m quartz -p "$(cat prompt.md)" -r abc --fork --cwd /repo/wt'
    )
    assert label == "Quartz worker · wt"


async def test_kernel_client_wait_until_event_wakes_immediately_on_wake_event_waiters() -> None:
    kc = KernelClient(cwd=Path.cwd())
    task = asyncio.create_task(kc.wait_event(timeout=None))
    await asyncio.sleep(0.02)
    assert not task.done()

    t0 = time.monotonic()
    kc.wake_event_waiters()
    res = await asyncio.wait_for(task, timeout=1.0)
    assert time.monotonic() - t0 < 0.5
    assert res is False


def test_kernel_client_shared_host_mcp_keeps_pool_alive_after_close(tmp_path: Path) -> None:
    close_shared_host_mcp_pool()
    try:
        kc1 = KernelClient(cwd=tmp_path, shared_host_mcp=True)
        kc2 = KernelClient(cwd=tmp_path, shared_host_mcp=True)

        mgr1 = kc1._get_host_mcp()
        mgr2 = kc2._get_host_mcp()
        assert mgr1 is mgr2

        loop1 = kc1._ensure_host_loop()
        loop2 = kc2._ensure_host_loop()
        assert loop1 is loop2

        # Closing kc1 must NOT stop the shared loop or evict the shared manager.
        kc1.close()
        assert loop1.is_running()
        assert get_shared_host_mcp_pool().manager_count() == 1

        kc2.close()
    finally:
        close_shared_host_mcp_pool()
