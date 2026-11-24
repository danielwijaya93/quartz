"""Tests for native async kernel IPC and non-blocking event waiting."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from quartz.kernel.base import CellExecutor
from quartz.kernel.client import KernelClient
from quartz.kernel.fake import FakeKernel
from quartz.kernel.inprocess import InProcessKernel


def test_async_protocol_conformance(tmp_path: Path) -> None:
    assert isinstance(KernelClient(cwd=tmp_path), CellExecutor)
    assert isinstance(InProcessKernel(cwd=tmp_path), CellExecutor)
    assert isinstance(FakeKernel(cwd=tmp_path), CellExecutor)


async def test_fake_kernel_exec_cell_and_wait_event(tmp_path: Path) -> None:
    fake = FakeKernel(cwd=tmp_path)
    res = await fake.exec_cell("x = 1")
    assert res.ok is True
    assert len(fake.cells) == 1

    assert await fake.wait_event(timeout=0.01) is False
    fake.events.append({"type": "event", "kind": "bash-done"})
    assert await fake.wait_event(timeout=0.01) is True


async def test_kernel_client_exec_cell_nonblocking_and_errors(tmp_path: Path) -> None:
    client = KernelClient(cwd=tmp_path)
    try:
        r1 = await client.exec_cell("x = 21 * 2; print('hi'); x")
        assert r1.ok is True
        assert r1.stdout == "hi\n"
        assert r1.result == "42"

        r_err = await client.exec_cell("1 / 0")
        assert r_err.ok is False
        assert "ZeroDivisionError" in r_err.stderr

        # Event loop remains unblocked while cell sleeps
        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        ticker = asyncio.create_task(_ticker())
        r_sleep = await client.exec_cell("import time; time.sleep(0.15); 'done'")
        await ticker
        assert r_sleep.ok is True
        assert r_sleep.result == "'done'"
        assert ticks == 5

        # Concurrent exec_cell calls both complete cleanly
        c1, c2 = await asyncio.gather(
            client.exec_cell("x + 1"),
            client.exec_cell("x + 2"),
        )
        assert c1.ok is True and c1.result == "43"
        assert c2.ok is True and c2.result == "44"
    finally:
        client.close()


async def test_kernel_client_exec_cell_timeout_and_cancel(tmp_path: Path) -> None:
    client = KernelClient(cwd=tmp_path)
    try:
        await client.exec_cell("ready = 1")

        # Timeout interrupts the long-running cell and returns KeyboardInterrupt result
        res = await client.exec_cell("import time; time.sleep(5.0)", timeout=0.2)
        assert res.ok is False
        assert "KeyboardInterrupt" in res.stderr
        assert client._pending_async == {}

        # Task cancellation cleans up _pending_async and leaves kernel usable
        task = asyncio.create_task(client.exec_cell("import time; time.sleep(5.0)", timeout=10.0))
        await asyncio.sleep(0.1)
        task.cancel()
        cancelled = False
        with contextlib.suppress(asyncio.CancelledError):
            await task
            cancelled = True
        assert cancelled is False
        assert client._pending_async == {}

        # Kernel still works after interrupt
        await asyncio.sleep(0.05)
        r_after = await client.exec_cell("ready + 1")
        assert r_after.ok is True
        assert r_after.result == "2"
    finally:
        client.close()


async def test_kernel_client_exec_cell_unexpected_exit(tmp_path: Path) -> None:
    client = KernelClient(cwd=tmp_path)
    try:
        await client.exec_cell("a = 1")
        task = asyncio.create_task(client.exec_cell("import os, time; time.sleep(0.05); os._exit(1)"))
        res = await task
        assert res.ok is False
        assert "exited unexpectedly" in res.stderr
    finally:
        client.close()


async def test_kernel_client_wait_event_bash_timeout_cancel(tmp_path: Path) -> None:
    client = KernelClient(cwd=tmp_path)
    try:
        await client.exec_cell("pass")

        # Timeout returns False and cleans up waiter
        assert await client.wait_event(timeout=0.05) is False
        assert client._event_waiters == []

        # Cancellation cleans up waiter
        waiter = asyncio.create_task(client.wait_event(timeout=5.0))
        await asyncio.sleep(0.03)
        assert len(client._event_waiters) == 1
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        assert client._event_waiters == []

        # Background bash() completion wakes wait_event immediately
        r_bg = await client.exec_cell("h = bash('sleep 0.1 && echo ok-async')")
        assert r_bg.ok is True

        t0 = time.monotonic()
        got_event = await client.wait_event(timeout=3.0)
        elapsed = time.monotonic() - t0
        assert got_event is True
        assert elapsed < 2.0

        # Calling wait_event when events are already pending returns True immediately
        assert await client.wait_event(timeout=0.0) is True

        events = client.drain_events()
        assert any(ev.get("event") == "bash_done" for ev in events)
    finally:
        client.close()


async def test_inprocess_kernel_async_methods(tmp_path: Path) -> None:
    kernel = InProcessKernel(cwd=tmp_path)
    try:
        r1 = await kernel.exec_cell("val = 10 + 5; val")
        assert r1.ok is True
        assert r1.result == "15"

        r_err = await kernel.exec_cell("raise ValueError('boom')")
        assert r_err.ok is False
        assert "ValueError: boom" in r_err.stderr

        assert await kernel.wait_event(timeout=0.03) is False
        assert kernel._event_waiters == []

        r_bg = await kernel.exec_cell("h = bash('sleep 0.08 && echo inproc')")
        assert r_bg.ok is True
        assert await kernel.wait_event(timeout=3.0) is True
        events = kernel.drain_events()
        assert any(ev.get("event") == "bash_done" for ev in events)
    finally:
        kernel.close()
