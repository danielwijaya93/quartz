"""Tests for native async kernel IPC and non-blocking event waiting."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path
from typing import Any

from quartz.kernel.base import (
    AsyncCellExecutor,
    CellExecutor,
    CellResult,
    exec_cell_async,
    wait_event_async,
)
from quartz.kernel.client import KernelClient
from quartz.kernel.fake import FakeKernel
from quartz.kernel.inprocess import InProcessKernel


class _SyncOnlyKernel:
    """Minimal sync-only CellExecutor without async methods."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: list[dict[str, Any]] = []
        self._cond = threading.Condition()

    def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        del timeout
        self.calls.append(code)
        return CellResult(ok=True, stdout="sync-ok\n", stderr="", result="42")

    def interrupt(self) -> None:
        pass

    def drain_events(self) -> list[dict[str, Any]]:
        with self._cond:
            ev = list(self.events)
            self.events.clear()
            return ev

    def has_pending_events(self) -> bool:
        with self._cond:
            return bool(self.events)

    def wait_until_event(self, stop_event: threading.Event, timeout: float | None = 0.25) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not stop_event.is_set():
            with self._cond:
                if self.events:
                    return True
                rem = None if deadline is None else max(0.0, deadline - time.monotonic())
                if rem == 0.0:
                    return False
                self._cond.wait(timeout=0.05 if rem is None else min(0.05, rem))
                if self.events:
                    return True
                if deadline is not None and time.monotonic() >= deadline:
                    return False
        return False

    def push_event(self, ev: dict[str, Any]) -> None:
        with self._cond:
            self.events.append(ev)
            self._cond.notify_all()

    def consume_restart_notice(self) -> str | None:
        return None


def test_async_protocol_conformance(tmp_path: Path) -> None:
    assert isinstance(KernelClient(cwd=tmp_path), AsyncCellExecutor)
    assert isinstance(InProcessKernel(cwd=tmp_path), AsyncCellExecutor)
    assert isinstance(FakeKernel(cwd=tmp_path), AsyncCellExecutor)

    sync_only = _SyncOnlyKernel()
    assert isinstance(sync_only, CellExecutor)
    assert not isinstance(sync_only, AsyncCellExecutor)


def test_exec_cell_and_wait_event_async_helpers(tmp_path: Path) -> None:
    async def _run() -> None:
        fake = FakeKernel(cwd=tmp_path)
        res = await exec_cell_async(fake, "x = 1")
        assert res.ok is True
        assert len(fake.cells) == 1

        assert await wait_event_async(fake, timeout=0.01) is False
        fake.events.append({"type": "event", "kind": "bash-done"})
        assert await wait_event_async(fake, timeout=0.01) is True

        sync_only = _SyncOnlyKernel()
        res_sync = await exec_cell_async(sync_only, "y = 2")
        assert res_sync.ok is True
        assert res_sync.result == "42"
        assert sync_only.calls == ["y = 2"]

        async def _push_soon() -> None:
            await asyncio.sleep(0.03)
            sync_only.push_event({"type": "event", "kind": "bash-done"})

        pusher = asyncio.create_task(_push_soon())
        try:
            assert await wait_event_async(sync_only, timeout=1.0) is True
        finally:
            await pusher

    asyncio.run(_run())


def test_kernel_client_aexec_cell_nonblocking_and_errors(tmp_path: Path) -> None:
    async def _run() -> None:
        client = KernelClient(cwd=tmp_path)
        try:
            r1 = await client.aexec_cell("x = 21 * 2; print('hi'); x")
            assert r1.ok is True
            assert r1.stdout == "hi\n"
            assert r1.result == "42"

            r_err = await client.aexec_cell("1 / 0")
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
            r_sleep = await client.aexec_cell("import time; time.sleep(0.15); 'done'")
            await ticker
            assert r_sleep.ok is True
            assert r_sleep.result == "'done'"
            assert ticks == 5

            # Concurrent aexec_cell calls both complete cleanly
            c1, c2 = await asyncio.gather(
                client.aexec_cell("x + 1"),
                client.aexec_cell("x + 2"),
            )
            assert c1.ok is True and c1.result == "43"
            assert c2.ok is True and c2.result == "44"
        finally:
            client.close()

    asyncio.run(_run())


def test_kernel_client_aexec_cell_timeout_and_cancel(tmp_path: Path) -> None:
    async def _run() -> None:
        client = KernelClient(cwd=tmp_path)
        try:
            await client.aexec_cell("ready = 1")

            # Timeout interrupts the long-running cell and returns KeyboardInterrupt result
            res = await client.aexec_cell("import time; time.sleep(5.0)", timeout=0.2)
            assert res.ok is False
            assert "KeyboardInterrupt" in res.stderr
            assert client._pending_async == {}

            # Task cancellation cleans up _pending_async and leaves kernel usable
            task = asyncio.create_task(client.aexec_cell("import time; time.sleep(5.0)", timeout=10.0))
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
            r_after = await client.aexec_cell("ready + 1")
            assert r_after.ok is True
            assert r_after.result == "2"
        finally:
            client.close()

    asyncio.run(_run())


def test_kernel_client_aexec_cell_unexpected_exit(tmp_path: Path) -> None:
    async def _run() -> None:
        client = KernelClient(cwd=tmp_path)
        try:
            await client.aexec_cell("a = 1")
            task = asyncio.create_task(client.aexec_cell("import os, time; time.sleep(0.05); os._exit(1)"))
            res = await task
            assert res.ok is False
            assert "exited unexpectedly" in res.stderr
        finally:
            client.close()

    asyncio.run(_run())


def test_kernel_client_await_event_bash_timeout_cancel(tmp_path: Path) -> None:
    async def _run() -> None:
        client = KernelClient(cwd=tmp_path)
        try:
            await client.aexec_cell("pass")

            # Timeout returns False and cleans up waiter
            assert await client.await_event(timeout=0.05) is False
            assert client._event_waiters == []

            # Cancellation cleans up waiter
            waiter = asyncio.create_task(client.await_event(timeout=5.0))
            await asyncio.sleep(0.03)
            assert len(client._event_waiters) == 1
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
            assert client._event_waiters == []

            # Background bash() completion wakes await_event immediately
            r_bg = await client.aexec_cell("h = bash('sleep 0.1 && echo ok-async')")
            assert r_bg.ok is True

            t0 = time.monotonic()
            got_event = await client.await_event(timeout=3.0)
            elapsed = time.monotonic() - t0
            assert got_event is True
            assert elapsed < 2.0

            # Calling await_event when events are already pending returns True immediately
            assert await client.await_event(timeout=0.0) is True

            events = client.drain_events()
            assert any(ev.get("event") == "bash_done" for ev in events)
        finally:
            client.close()

    asyncio.run(_run())


def test_inprocess_kernel_async_methods(tmp_path: Path) -> None:
    async def _run() -> None:
        kernel = InProcessKernel(cwd=tmp_path)
        try:
            r1 = await kernel.aexec_cell("val = 10 + 5; val")
            assert r1.ok is True
            assert r1.result == "15"

            r_err = await kernel.aexec_cell("raise ValueError('boom')")
            assert r_err.ok is False
            assert "ValueError: boom" in r_err.stderr

            assert await kernel.await_event(timeout=0.03) is False
            assert kernel._event_waiters == []

            r_bg = await kernel.aexec_cell("h = bash('sleep 0.08 && echo inproc')")
            assert r_bg.ok is True
            assert await kernel.await_event(timeout=3.0) is True
            events = kernel.drain_events()
            assert any(ev.get("event") == "bash_done" for ev in events)
        finally:
            kernel.close()

    asyncio.run(_run())
