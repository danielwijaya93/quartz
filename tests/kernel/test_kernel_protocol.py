"""Tests for execution engine protocols and FakeKernel test double."""

from __future__ import annotations

from pathlib import Path

import pytest

from quartz.kernel.base import (
    CellExecutor,
    Kernel,
    KernelIntrospector,
    KernelSnapshotter,
)
from quartz.kernel.client import KernelClient
from quartz.kernel.fake import FakeKernel


def test_kernel_client_conforms_to_protocols(tmp_path: Path) -> None:
    client = KernelClient(cwd=tmp_path)
    assert isinstance(client, CellExecutor)
    assert isinstance(client, KernelIntrospector)
    assert isinstance(client, KernelSnapshotter)
    assert isinstance(client, Kernel)


def test_fake_kernel_conforms_to_protocols(tmp_path: Path) -> None:
    fake = FakeKernel(cwd=tmp_path)
    assert isinstance(fake, CellExecutor)
    assert isinstance(fake, KernelIntrospector)
    assert isinstance(fake, KernelSnapshotter)
    assert isinstance(fake, Kernel)


async def test_fake_kernel_lifecycle(tmp_path: Path) -> None:
    fake = FakeKernel(cwd=tmp_path)
    assert fake.started is False
    fake.start()
    assert fake.started is True

    fake.configure(headless=True, background_check_every=10.0)
    assert fake.configured["headless"] is True
    assert fake.configured["background_check_every"] == 10.0

    res = await fake.exec_cell("x = 1")
    assert res.ok is True
    assert len(fake.cells) == 1

    snap = fake.snapshot(tmp_path / "snap")
    assert isinstance(snap, dict)

    restored = fake.restore(tmp_path / "snap")
    assert "restored" in restored

    fake.close()
    assert fake.closed is True


def test_kernel_client_start_distinguishes_early_exit_from_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import subprocess

    import pytest

    from quartz.kernel.client import KernelClient

    class _DeadProc:
        pid = 99999
        stdin = io.StringIO()
        stdout = io.StringIO("")
        stderr = io.StringIO("ImportError: broken\n")

        def poll(self) -> int:
            return 1

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 1

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _DeadProc())
    client = KernelClient(cwd=tmp_path)
    with pytest.raises(RuntimeError, match=r"exited during startup \(exit code 1\)"):
        client.start(timeout=1.0)
