"""Regression tests for bounded WebSocket outbox queue."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from quartz.web.attachments import AttachedFileRegistry
from quartz.web.context import AppContext
from quartz.web.routes.ws import _open_ws_stream


@pytest.mark.asyncio
async def test_ws_outbox_queue_full_closes_slow_consumer_without_unhandled_error(
    tmp_path: Path,
) -> None:
    block_send = asyncio.Event()

    async def slow_send(_text: str) -> None:
        await block_send.wait()

    socket = MagicMock()
    socket.headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000"}
    socket.accept = AsyncMock()
    socket.send_text = AsyncMock(side_effect=slow_send)
    socket.close = AsyncMock()

    ctx = AppContext(
        manager=MagicMock(cwd=tmp_path),
        sidebar=MagicMock(),
        push_store=MagicMock(),
        file_registry=AttachedFileRegistry(),
        static_dir=None,
        dev=False,
    )

    exceptions: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    orig_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: exceptions.append(context))
    try:
        async with _open_ws_stream(socket, ctx) as push:
            assert push is not None
            # Let the writer task dequeue the first frame and block on slow_send.
            push({"type": "status", "label": "first"})
            await asyncio.sleep(0)

            # Fill the 2048-item outbox and push one more to trigger QueueFull.
            for idx in range(2050):
                push({"type": "status", "label": f"frame-{idx}"})

            await asyncio.sleep(0.05)
            block_send.set()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(orig_handler)

    assert exceptions == []
    socket.close.assert_awaited_once_with(code=1013, reason="Slow consumer")
