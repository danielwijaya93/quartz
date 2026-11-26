"""Red-first tests for PR 3: native async WebSession, SessionManager, SinkHub, and non-blocking bot execution."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import ScriptedTurn, make_runtime
from fastapi.testclient import TestClient

from quartz.bots.models import BotConfig, CronConfig, PermissionPolicy, QueuedTrigger
from quartz.core.session import Session
from quartz.core.turn import TurnStatus
from quartz.web.app import create_app
from quartz.web.bot_executor import run_bot_trigger
from quartz.web.session_manager import SessionManager
from quartz.web.sink_hub import SinkHub
from quartz.web.web_session import WebSession


class TestSinkHubLifecycle:
    def test_detach_does_not_schedule_reconnect_abort_timer(self) -> None:
        events: list[dict[str, Any]] = []
        hub = SinkHub(on_event=events.append)
        def sink(_frame: Any) -> None:
            return None
        hub.attach(sink)
        assert hub.attached is True
        hub.detach(sink)
        assert hub.attached is False
        assert not hasattr(hub, "begin_grace")


class TestSessionManagerAsyncLifecycle:
    async def test_start_watcher_uses_asyncio_task_when_loop_bound(self, tmp_path: Path) -> None:
        ticks = asyncio.Event()
        mgr = SessionManager(
            build=lambda sid, cwd=None: (make_runtime(tmp_path), False),
            cwd=tmp_path,
            check_interval_s=0.05,
        )
        orig_check = mgr.check_idle_sessions

        def _counted_check(*args: Any, **kwargs: Any) -> list[str]:
            ticks.set()
            return orig_check(*args, **kwargs)

        mgr.check_idle_sessions = _counted_check  # type: ignore[method-assign]
        mgr.bind_loop(asyncio.get_running_loop())
        mgr.start_watcher()
        try:
            assert mgr._watcher_timer is None
            assert mgr._watcher_task is not None
            await asyncio.wait_for(ticks.wait(), timeout=1.0)
        finally:
            await mgr.aclose_all()
            assert mgr._watcher_task is None

    async def test_acreate_session_failure_leaks_no_session(self, tmp_path: Path) -> None:
        def exploding_build(_sid: str | None, _cwd: Path | None = None) -> tuple[Any, bool]:
            raise RuntimeError("kernel spawn failed")

        mgr = SessionManager(build=exploding_build, cwd=tmp_path)
        mgr.bind_loop(asyncio.get_running_loop())
        with pytest.raises(RuntimeError, match="kernel spawn failed"):
            await mgr.acreate_session()
        assert mgr.all_sessions() == []

    async def test_concurrent_aget_or_load_session_coalesces_duplicate_loads(
        self, tmp_path: Path
    ) -> None:
        build_calls = 0
        build_lock = threading.Lock()
        sid = "20260401-120000-dedup1"

        def slow_build(session_id: str | None, _cwd: Path | None = None) -> tuple[Any, bool]:
            nonlocal build_calls
            with build_lock:
                build_calls += 1
            time.sleep(0.1)
            rt = make_runtime(tmp_path)
            rt.active_session = Session(session_id or sid, sessions_dir=tmp_path / "sessions")
            return rt, True

        mgr = SessionManager(build=slow_build, cwd=tmp_path)
        mgr.bind_loop(asyncio.get_running_loop())
        try:
            ws1, ws2 = await asyncio.gather(
                mgr.aget_or_load_session(sid),
                mgr.aget_or_load_session(sid),
            )
            assert ws1 is ws2
            assert build_calls == 1
        finally:
            await mgr.aclose_all()

    async def test_web_session_async_lifecycle_methods(self, tmp_path: Path) -> None:
        ws = WebSession(
            build=lambda _sid, _cwd=None: (make_runtime(tmp_path), False),
            cwd=tmp_path,
        )
        rt = await ws.aensure_runtime()
        assert ws.built is True
        assert rt is ws.runtime
        snap = await ws.asnapshot()
        assert snap["type"] == "session_state"
        await ws.aclose()
        assert ws.built is False


class TestNonBlockingBotTriggerAndLifespan:
    async def test_run_bot_trigger_does_not_block_event_loop_during_slow_build(
        self, tmp_path: Path
    ) -> None:
        def slow_build(_sid: str | None, _cwd: Path | None = None) -> tuple[Any, bool]:
            time.sleep(0.25)
            return make_runtime(tmp_path), False

        from quartz.bots.store import BotStore
        from quartz.web.sidebar import SidebarStore

        mgr = SessionManager(build=slow_build, cwd=tmp_path, runner=ScriptedTurn())
        mgr.bind_loop(asyncio.get_running_loop())
        bot_store = BotStore(tmp_path / "bots")
        sidebar_store = SidebarStore(tmp_path / "sidebar.json")
        bot = BotConfig(
            id="slow-bot",
            name="Slow Bot",
            watcher_type="cron",
            instruction="Check status",
            cron=CronConfig(expression="0 9 * * *"),
            permissions=PermissionPolicy(),
        )
        bot_store.create_bot(bot)
        trigger = QueuedTrigger(
            trigger_id="trig-1",
            bot_id=bot.id,
            event_key="k-1",
            source="cron",
            payload={},
            enqueued_at=time.time(),
        )

        # Measure event loop responsiveness while run_bot_trigger is spawning the runtime
        max_lag = 0.0
        stop_probe = False

        async def _probe_loop() -> None:
            nonlocal max_lag
            while not stop_probe:
                t0 = time.monotonic()
                await asyncio.sleep(0.02)
                lag = (time.monotonic() - t0) - 0.02
                if lag > max_lag:
                    max_lag = lag

        probe_task = asyncio.create_task(_probe_loop())
        try:
            status = await run_bot_trigger(
                bot,
                trigger,
                manager=mgr,
                bot_store=bot_store,
                get_sidebar=lambda: sidebar_store,
            )
            assert status == TurnStatus.OK
        finally:
            stop_probe = True
            await probe_task
            await mgr.aclose_all()

        # If create_session blocked the loop for 0.25s, max_lag would be >= 0.23s.
        assert max_lag < 0.12

    def test_multiple_sequential_lifespans_bind_fresh_loops_cleanly(self, tmp_path: Path) -> None:
        for _ in range(2):
            app = create_app(
                build=lambda _sid, _cwd=None: (make_runtime(tmp_path), False),
                cwd=tmp_path,
                runner=ScriptedTurn(),
                bots_dir=tmp_path / "bots",
            )
            with TestClient(app) as client:
                res = client.get("/api/health")
                assert res.status_code == 200
                assert app.state.manager._watcher_task is not None
                assert app.state.manager._watcher_timer is None
            assert app.state.manager._watcher_task is None
