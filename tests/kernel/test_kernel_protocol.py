"""Tests for stateless execution engine protocols and FakeKernel test double."""

from __future__ import annotations

from pathlib import Path

from quartz.kernel.base import CellExecutor, Kernel, KernelIntrospector
from quartz.kernel.client import KernelClient
from quartz.kernel.fake import FakeKernel


def test_kernel_client_conforms_to_protocols(tmp_path: Path) -> None:
    client = KernelClient(cwd=tmp_path)
    assert isinstance(client, CellExecutor)
    assert isinstance(client, KernelIntrospector)
    assert isinstance(client, Kernel)


def test_fake_kernel_conforms_to_protocols(tmp_path: Path) -> None:
    fake = FakeKernel(cwd=tmp_path)
    assert isinstance(fake, CellExecutor)
    assert isinstance(fake, KernelIntrospector)
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

    fake.close()
    assert fake.closed is True
