"""The dock's idle wake: a status notice must start a turn, not just print.

This is the behaviour a waiting user described -- "I got impatient and asked how it
is going" -- automated: while the session sits idle, a running job crossing the
check period wakes the model so the answer is written rather than waited for.

The watcher loop is driven by hand here (``_bg_watcher_loop`` blocks forever), so
what is asserted is the decision: with an event pending and no turn running, drain
it and run a turn; while a turn is already running, do not.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.kernel.client import KernelClient
from quartz.tui.dock import InteractiveDock


def _dock(rt: AgentRuntime) -> InteractiveDock:
    dock = InteractiveDock.__new__(InteractiveDock)
    dock.rt = rt
    dock._lock = threading.RLock()
    dock._worker_lock = asyncio.Lock()
    dock._stop_event = asyncio.Event()
    dock._turn_running = False
    dock._aborting = False
    dock._live_renderable = None
    dock._transient_status = None
    dock._bg_jobs = []
    dock._bg_count = 0
    dock._modal = None
    dock._invalidate = lambda: None  # type: ignore[assignment]
    dock._refresh_bg_count = lambda: None  # type: ignore[assignment]
    return dock


def _runtime(kernel: KernelClient, tmp_path: Path) -> AgentRuntime:
    return AgentRuntime(
        cwd=tmp_path,
        kernel=kernel,
        client=None,  # type: ignore[arg-type]
        provider_name="test",
        model="m",
        effort=None,
        system_prompt="",
        active_session=None,  # type: ignore[arg-type]
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=100_000,
        compact_reserve_tokens=10_000,
        keep_recent_tokens=10_000,
        verbose=False,
        plain_output=True,
        headless=False,
    )


async def _run_watcher(dock: InteractiveDock, turns: list[str], *, seconds: float = 1.0) -> None:
    """Run the (endless) watcher loop as an asyncio task for a bounded moment, then stop it."""
    assert dock._stop_event is not None
    stop_event = dock._stop_event

    async def fake_turn() -> None:
        turns.append("turn")
        stop_event.set()

    dock._execute_dock_turn = fake_turn  # type: ignore[assignment]
    task = asyncio.create_task(dock._bg_watcher_loop())
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done(), "watcher loop did not stop"


async def test_idle_session_wakes_on_a_status_notice(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(background_check_every=0.4)
        rt = _runtime(kernel, tmp_path)
        assert (await kernel.exec_cell("bg = bash('sleep 3')")).ok

        dock = _dock(rt)
        turns: list[str] = []
        await _run_watcher(dock, turns)

        assert turns == ["turn"], "a status event did not wake the session"
        assert rt.messages, "the notice never reached the conversation"
        assert "[bash-status" in rt.messages[-1]["content"]
    finally:
        kernel.close()


async def test_a_running_turn_is_not_interrupted_by_a_status(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(background_check_every=0.4)
        rt = _runtime(kernel, tmp_path)
        assert (await kernel.exec_cell("bg = bash('sleep 3')")).ok
        # Let a status event actually arrive before checking the busy path.
        import time

        deadline = time.monotonic() + 6.0
        while not kernel.has_pending_events() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert kernel.has_pending_events()

        dock = _dock(rt)
        dock._turn_running = True
        turns: list[str] = []
        await _run_watcher(dock, turns, seconds=0.4)
        # Busy session: the event stays queued for the turn to pick up itself.
        assert turns == []
        assert kernel.has_pending_events()
    finally:
        kernel.close()
