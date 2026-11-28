"""Tests for native asyncio stateless one-shot KernelClient, HostRpcHandler, and KernelIOLoop."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from quartz.kernel.client import KernelClient
from quartz.kernel.host_rpc import HostRpcHandler
from quartz.kernel.io_loop import get_kernel_io_loop


class TestAsyncKernelClientNativeTransport:
    async def test_astart_on_bound_loop_spawns_zero_os_threads(self, tmp_path: Path) -> None:
        client = KernelClient(cwd=tmp_path)
        before_threads = {t.ident for t in threading.enumerate()}
        try:
            await client.astart()
            new_threads = [t for t in threading.enumerate() if t.ident not in before_threads]
            # Zero per-kernel Quartz reader/stderr/host-loop threads are spawned
            assert not any(t.name.startswith("quartz-") for t in new_threads)
            assert not hasattr(client, "_reader_thread")
            assert not hasattr(client, "_stderr_thread")
            assert client._active_proc is None

            res = await client.exec_cell("val = 6 * 7; val")
            assert res.ok is True
            assert res.result == "42"
            assert client._active_proc is None
        finally:
            await client.aclose()
            # Idempotent second close
            await client.aclose()

    async def test_large_jsonl_output_exceeding_64kb_stream_limit(self, tmp_path: Path) -> None:
        # 120 KiB PNG payload encodes to ~160 KiB base64 in a single JSONL line (>64 KiB asyncio default)
        big_png = tmp_path / "big.png"
        big_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"A" * (120 * 1024))
        client = KernelClient(cwd=tmp_path)
        try:
            await client.astart()
            res = await client.exec_cell(f"view_image({str(big_png)!r})")
            assert res.ok is True
            assert len(res.images) == 1
            assert len(res.images[0]["data"]) > 150_000
        finally:
            await client.aclose()

    async def test_native_async_helpers_and_unknown_jobs(self, tmp_path: Path) -> None:
        c1 = KernelClient(cwd=tmp_path)
        try:
            await c1.astart()
            r1 = await c1.exec_cell("answer = 99; answer")
            assert r1.ok is True and r1.result == "99"

            helpers = await c1.alist_helpers()
            assert "helpers" in helpers

            # Unknown job tail/kill return (False, "") / False cleanly
            assert await c1.abash_tail(999999) == (False, "")
            assert await c1.akill_bash(999999) is False
        finally:
            await c1.aclose()

    def test_sync_start_works_across_multiple_asyncio_run_loops(self, tmp_path: Path) -> None:
        io_loop = get_kernel_io_loop()
        assert io_loop.is_running()

        client = KernelClient(cwd=tmp_path)
        client.start()
        try:
            r1 = asyncio.run(client.exec_cell("10"))
            assert r1.ok is True and r1.result == "10"

            r2 = asyncio.run(client.exec_cell("10 + 5"))
            assert r2.ok is True and r2.result == "15"

            assert "helpers" in client.list_helpers()
        finally:
            client.close()


class TestHostRpcHandler:
    def test_rejects_unsupported_bridge_tool(self, tmp_path: Path) -> None:
        handler = HostRpcHandler(cwd=tmp_path)
        res = handler.exec_bridge_sync({"tool": "rm", "argv": ["-rf", "/"]})
        assert res["exit_code"] == 126
        assert "Unsupported bridge tool" in res["stderr"]
