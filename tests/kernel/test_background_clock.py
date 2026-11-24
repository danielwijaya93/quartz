"""The background status clock, exercised against a real kernel worker.

These use a one-second period and short commands: the point is the *sequence*
(status while running, nothing after it finishes), not the wall-clock default.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from quartz.kernel.client import KernelClient


async def _wait_for_event(kernel: KernelClient, kind: str, timeout: float = 8.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ev in kernel.drain_events():
            if ev.get("event") == kind:
                return ev
        await asyncio.sleep(0.05)
    return None


async def test_status_event_reports_a_running_job(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(background_check_every=1.0)
        res = await kernel.exec_cell("bg = bash('echo tick; sleep 2.5')")
        assert res.ok

        status = await _wait_for_event(kernel, "bash_status", timeout=6.0)
        assert status is not None, "no status event while the job was still running"
        assert status["command"] == "echo tick; sleep 2.5"
        assert isinstance(status["pid"], int)
        assert float(status["elapsed_s"]) >= 1.0
        # The tail is what makes the notice useful: it shows the job is alive.
        assert "tick" in status["tail"]

        done = await _wait_for_event(kernel, "bash_done", timeout=8.0)
        assert done is not None
        assert done["exit_code"] == 0
    finally:
        kernel.close()


async def test_status_repeats_every_period(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(background_check_every=0.4)
        assert (await kernel.exec_cell("bg = bash('sleep 3')")).ok

        seen: list[float] = []
        deadline = time.monotonic() + 8.0
        while len(seen) < 2 and time.monotonic() < deadline:
            for ev in kernel.drain_events():
                if ev.get("event") == "bash_status":
                    seen.append(float(ev["elapsed_s"]))
            await asyncio.sleep(0.05)
        assert len(seen) >= 2, f"expected repeated status events, got {seen}"
        assert seen[1] > seen[0]
    finally:
        kernel.close()


async def test_no_status_after_the_job_finishes(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(background_check_every=0.4)
        assert (await kernel.exec_cell("bg = bash('echo done_quickly')")).ok
        assert await _wait_for_event(kernel, "bash_done", timeout=6.0) is not None

        # The ticker stops with the job: a finished command is never reported as
        # still running, which would be the one actively misleading notice.
        await asyncio.sleep(1.5)
        assert [e for e in kernel.drain_events() if e.get("event") == "bash_status"] == []
    finally:
        kernel.close()


async def test_status_disabled_by_default(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        assert (await kernel.exec_cell("bg = bash('sleep 1.2')")).ok
        await asyncio.sleep(1.0)
        assert [e for e in kernel.drain_events() if e.get("event") == "bash_status"] == []
        assert await _wait_for_event(kernel, "bash_done", timeout=6.0) is not None
    finally:
        kernel.close()


async def test_configure_survives_a_kernel_restart(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(background_check_every=0.4)
        kernel.restart()
        assert (await kernel.exec_cell("bg = bash('sleep 2')")).ok
        assert await _wait_for_event(kernel, "bash_status", timeout=6.0) is not None
    finally:
        kernel.close()


async def test_headless_bash_runs_inline_and_returns_output(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(headless=True)
        # No `await`: in a `-p` run the job has to finish inside the cell, because
        # nothing survives the turn to report on it later.
        res = await kernel.exec_cell("h = bash('echo headless_out')")
        assert res.ok
        assert "headless_out" in res.stdout
        assert "[bash]" in res.stdout
        # By the time the cell answered, the job it started is over: nothing is
        # left running for the turn to lose track of.
        assert kernel.running_bash_count() == 0
        assert (await kernel.exec_cell("h.running")).result == "False"
    finally:
        kernel.close()


async def test_headless_await_still_works(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(headless=True)
        res = await kernel.exec_cell("r = await bash('echo awaited')\nr.output.strip()")
        assert res.ok
        assert res.result == "'awaited'"
    finally:
        kernel.close()


async def test_headless_refuses_launch_worker(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(headless=True)
        res = await kernel.exec_cell("launch_worker('p.md', '.', 'sid')")
        # A `-p` worker must not fork another `-p` worker: that is how a background
        # task ends up waiting on a background task.
        assert not res.ok
        assert "not available in `-p`" in res.stderr
    finally:
        kernel.close()
