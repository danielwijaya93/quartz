"""Startup and kernel-selection performance tests for quartz.bootstrap."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from quartz.bootstrap import BootstrapOptions, build_runtime
from quartz.kernel.client import KernelClient
from quartz.kernel.inprocess import InProcessKernel


async def test_bootstrap_selects_inprocess_kernel_for_unsandboxed_headless(tmp_path: Path) -> None:
    def _fake_client():
        return object(), "stub", "stub-model", "high", 128_000

    rt_inproc, _ = build_runtime(
        BootstrapOptions(
            cwd=tmp_path,
            headless=True,
            sandbox=False,
            client_factory=_fake_client,
        )
    )
    try:
        assert isinstance(rt_inproc.kernel, InProcessKernel)
        res = await rt_inproc.kernel.exec_cell("2 + 2")
        assert res.ok and res.result == "4"
    finally:
        rt_inproc.kernel.close()

    rt_interactive, _ = build_runtime(
        BootstrapOptions(
            cwd=tmp_path,
            headless=False,
            sandbox=False,
            client_factory=_fake_client,
        )
    )
    try:
        assert isinstance(rt_interactive.kernel, KernelClient)
    finally:
        rt_interactive.kernel.close()


def test_build_runtime_does_not_block_on_slow_model_discovery(tmp_path: Path, monkeypatch: Any) -> None:
    started_discovery = threading.Event()
    finish_discovery = threading.Event()

    class _SlowClient:
        def list_model_details(self) -> list[Any]:
            started_discovery.set()
            finish_discovery.wait(timeout=2.0)
            return [SimpleNamespace(id="gpt-4.1", context_length=555_555)]

        def close(self) -> None:
            pass

    class _FakeKernel:
        def __init__(self, cwd: Path | None = None) -> None:
            self.cwd = cwd
            self.dirty = False

        def start(self) -> None:
            pass

        def configure(self, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "quartz.bootstrap.build_provider",
        lambda **kwargs: (_SlowClient(), "openai", "gpt-4.1", "medium"),
    )

    t0 = time.monotonic()
    rt, _ = build_runtime(
        BootstrapOptions(
            cwd=tmp_path,
            headless=True,
            kernel_factory=_FakeKernel,
        )
    )
    elapsed = time.monotonic() - t0

    # Must return immediately using the offline table without waiting for _SlowClient.
    assert elapsed < 0.5
    assert rt.max_context_tokens == 1_047_576

    # Background thread updates max_context_tokens once discovery finishes.
    finish_discovery.set()
    assert started_discovery.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while rt.max_context_tokens != 555_555 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rt.max_context_tokens == 555_555
