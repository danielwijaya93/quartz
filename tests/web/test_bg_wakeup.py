"""Background bash events wake an idle web session automatically."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.web.session import WebSession


class _BgKernel:
    dirty = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._notify = threading.Event()
        self.closed = False

    def push_event(self, ev: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(ev)
            self._notify.set()

    def has_pending_events(self) -> bool:
        with self._lock:
            return bool(self._events)

    def drain_events(self) -> list[dict[str, Any]]:
        with self._lock:
            out = list(self._events)
            self._events.clear()
            self._notify.clear()
            return out

    async def wait_event(self, timeout: float | None = 0.25) -> bool:
        import asyncio

        deadline = time.monotonic() + (timeout or 0.25)
        while True:
            with self._lock:
                if self._events:
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(remaining, 0.02))

    def interrupt(self) -> None: ...

    def close(self) -> None:
        self.closed = True


def _runtime(cwd: Path, kernel: _BgKernel) -> AgentRuntime:
    return AgentRuntime(
        cwd=cwd,
        kernel=kernel,  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="stub",
        model="stub",
        effort="high",
        system_prompt="stub",
        active_session=Session.create(cwd),
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=False,
        headless=False,
        transport="web",
    )


def _wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_bg_done_wakes_idle_web_session(tmp_path: Path) -> None:
    kernel = _BgKernel()
    rt = _runtime(tmp_path, kernel)
    frames: list[dict[str, Any]] = []
    ran = threading.Event()

    def runner(runtime: AgentRuntime, view: Any, **_: Any) -> TurnOutcome:
        ran.set()
        return TurnOutcome(TurnStatus.OK, elapsed_s=0.01)

    session = WebSession(
        build=lambda session_id, cwd=None: (rt, False),
        cwd=tmp_path,
        runner=runner,
    )
    session.attach(frames.append)
    session.ensure_runtime()

    try:
        kernel.push_event(
            {
                "event": "bash_done",
                "pid": 1234,
                "exit_code": 0,
                "duration": 12.5,
                "command": "npm run build",
                "tail": "done!",
            }
        )
        assert ran.wait(timeout=3.0)
        assert _wait_until(lambda: any(f.get("type") == "turn_end" for f in frames))
        assert any(f.get("type") == "turn_start" for f in frames)
        assert len(rt.messages) == 1
        assert "[bash-done pid:1234 exit:0 duration:12.5s]" in rt.messages[0]["content"]
    finally:
        session.close()


def test_bg_status_wakes_idle_web_session(tmp_path: Path) -> None:
    kernel = _BgKernel()
    rt = _runtime(tmp_path, kernel)
    ran = threading.Event()

    session = WebSession(
        build=lambda session_id, cwd=None: (rt, False),
        cwd=tmp_path,
        runner=lambda runtime, view, **_: (ran.set(), TurnOutcome(TurnStatus.OK, elapsed_s=0.01))[1],
    )
    session.ensure_runtime()

    try:
        kernel.push_event(
            {
                "event": "bash_status",
                "pid": 5678,
                "elapsed_s": 65,
                "command": "gradle assembleDebug",
                "tail": "> Task :app:compileDebug",
            }
        )
        assert ran.wait(timeout=3.0)
        assert _wait_until(lambda: not session.turn_running)
        assert len(rt.messages) == 1
        assert "[bash-status pid:5678 elapsed:1m05s]" in rt.messages[0]["content"]
    finally:
        session.close()


def test_bg_event_waits_while_turn_is_running(tmp_path: Path) -> None:
    kernel = _BgKernel()
    rt = _runtime(tmp_path, kernel)
    turn_entered = threading.Event()
    release_turn = threading.Event()
    run_count = 0

    def runner(runtime: AgentRuntime, view: Any, **_: Any) -> TurnOutcome:
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            turn_entered.set()
            release_turn.wait(timeout=3.0)
        return TurnOutcome(TurnStatus.OK, elapsed_s=0.01)

    session = WebSession(
        build=lambda session_id, cwd=None: (rt, False),
        cwd=tmp_path,
        runner=runner,
    )
    session.ensure_runtime()

    try:
        session.start_turn("initial prompt", [])
        assert turn_entered.wait(timeout=3.0)

        # Event arrives while turn 1 is still running: watcher must NOT steal it.
        kernel.push_event(
            {
                "event": "bash_done",
                "pid": 999,
                "exit_code": 0,
                "duration": 1.0,
                "command": "echo hi",
                "tail": "hi",
            }
        )
        time.sleep(0.15)
        assert kernel.has_pending_events()
        assert run_count == 1

        # Once turn 1 finishes, the watcher drains the event and starts turn 2.
        release_turn.set()
        assert _wait_until(lambda: run_count == 2 and not session.turn_running)
        assert not kernel.has_pending_events()
        assert any("[bash-done pid:999" in str(m.get("content", "")) for m in rt.messages)
    finally:
        release_turn.set()
        session.close()


def test_close_stops_bg_watcher(tmp_path: Path) -> None:
    kernel = _BgKernel()
    rt = _runtime(tmp_path, kernel)
    ran = threading.Event()

    session = WebSession(
        build=lambda session_id, cwd=None: (rt, False),
        cwd=tmp_path,
        runner=lambda runtime, view, **_: (ran.set(), TurnOutcome(TurnStatus.OK, elapsed_s=0.01))[1],
    )
    session.ensure_runtime()
    session.close()

    kernel.push_event(
        {
            "event": "bash_done",
            "pid": 42,
            "exit_code": 0,
            "duration": 1.0,
            "command": "echo after close",
            "tail": "",
        }
    )
    time.sleep(0.15)
    assert not ran.is_set()
    assert kernel.closed
