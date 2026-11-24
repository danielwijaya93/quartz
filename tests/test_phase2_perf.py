"""Tests for Phase 2: LRU web session hibernation and InProcessKernel."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from quartz.bootstrap import BootstrapOptions, build_runtime
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.kernel.client import KernelClient
from quartz.kernel.inprocess import InProcessKernel
from quartz.web.session import SessionManager


class _FakeKernel:
    def __init__(self) -> None:
        self.dirty = True
        self.closed = False
        self.snapshot_calls = 0
        self.running_bash_count = 0

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        self.snapshot_calls += 1
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "manifest.json").write_text('{"vars": {}, "skipped": []}', encoding="utf-8")
        self.dirty = False
        return {"vars": {}, "skipped": []}

    def restore(self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None) -> dict[str, list[str]]:
        return {"restored": [], "skipped": []}

    def interrupt(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _make_manager_builder(tmp_path: Path):
    kernels_by_sid: dict[str, _FakeKernel] = {}

    def build(session_id: str | None, cwd: Path | None = None) -> tuple[AgentRuntime, bool]:
        target_cwd = cwd or tmp_path
        kernel = _FakeKernel()
        if session_id is None:
            session = Session.create(target_cwd, sessions_dir=tmp_path / "sessions")
            messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
            session.save_messages(messages)
            resumed = False
        else:
            session, messages = Session.load(session_id, sessions_dir=tmp_path / "sessions")
            resumed = True
        kernels_by_sid[session.id] = kernel
        rt = AgentRuntime(
            cwd=target_cwd,
            kernel=kernel,  # type: ignore[arg-type]
            client=object(),  # type: ignore[arg-type]
            provider_name="stub",
            model="stub",
            effort="high",
            system_prompt="stub",
            active_session=session,
            messages=messages,
            usage_stats=TurnUsage(),
            max_context_tokens=128_000,
            compact_reserve_tokens=16_384,
            keep_recent_tokens=32_000,
            transport="web",
        )
        return rt, resumed

    return build, kernels_by_sid


def test_session_manager_lru_hibernates_idle_sessions_and_restores(tmp_path: Path) -> None:
    build, kernels = _make_manager_builder(tmp_path)
    mgr = SessionManager(build, cwd=tmp_path, max_live_sessions=2, idle_hours=0)

    ws1 = mgr.create_session()
    sid1 = ws1.runtime.active_session.id
    ws1._last_active_at = 100.0
    k1 = kernels[sid1]

    ws2 = mgr.create_session()
    sid2 = ws2.runtime.active_session.id
    ws2._last_active_at = 200.0
    k2 = kernels[sid2]

    # Creating a 3rd session exceeds cap=2: oldest idle session (sid1) must hibernate.
    ws3 = mgr.create_session()
    sid3 = ws3.runtime.active_session.id
    ws3._last_active_at = 300.0

    assert k1.closed is True
    assert k1.snapshot_calls == 1
    assert k2.closed is False
    assert mgr.get_session(sid1) is None
    assert mgr.get_session(sid2) is ws2
    assert mgr.get_session(sid3) is ws3

    # Clicking sid1 transparently restores it from disk and hibernates sid2 (now oldest).
    restored = mgr.get_or_load_session(sid1)
    assert restored.built
    assert restored.runtime.active_session.id == sid1
    assert k2.closed is True
    assert k2.snapshot_calls == 1
    mgr.close_all()


def test_session_manager_never_hibernates_attached_or_busy_sessions(tmp_path: Path) -> None:
    build, kernels = _make_manager_builder(tmp_path)
    gate = threading.Event()

    def slow_runner(rt: AgentRuntime, view: Any, **_: Any) -> TurnOutcome:
        gate.wait(timeout=5.0)
        return TurnOutcome(TurnStatus.OK, elapsed_s=0.01)

    mgr = SessionManager(build, cwd=tmp_path, runner=slow_runner, max_live_sessions=1, idle_hours=0)

    ws_attached = mgr.create_session()
    sid_attached = ws_attached.runtime.active_session.id
    ws_attached.attach(lambda _frame: None)

    ws_bg_job = mgr.create_session()
    sid_bg_job = ws_bg_job.runtime.active_session.id
    kernels[sid_bg_job].running_bash_count = 1

    ws_turn = mgr.create_session()
    sid_turn = ws_turn.runtime.active_session.id
    ws_turn.start_turn("working", [])

    # Creating a 4th session cannot evict any of the 3 protected sessions.
    mgr.create_session()
    assert len(mgr.all_sessions()) == 4
    assert not kernels[sid_attached].closed
    assert not kernels[sid_bg_job].closed
    assert not kernels[sid_turn].closed

    gate.set()
    mgr.close_all()


async def test_inprocess_kernel_exec_await_snapshot_and_restore(tmp_path: Path) -> None:
    k = InProcessKernel(cwd=tmp_path)
    k.start()
    try:
        k.configure(headless=True)
        r1 = await k.exec_cell("x = 21 * 2\nx")
        assert r1.ok and r1.result == "42"

        r2 = await k.exec_cell("await asyncio.sleep(0.01)\nprint('hi', x)")
        assert r2.ok and "hi 42" in r2.stdout

        # Headless bash() runs inline and returns output
        r3 = await k.exec_cell("res = await bash('echo hello-inprocess')\nprint(res.output.strip())")
        assert r3.ok and "hello-inprocess" in r3.stdout

        assert "x" in k.list_vars()
        assert "helpers" in k.list_helpers()

        snap_dir = tmp_path / "snap"
        manifest = k.snapshot(snap_dir)
        assert "x" in manifest.get("saved", [])
    finally:
        k.close()

    k2 = InProcessKernel(cwd=tmp_path)
    k2.start()
    try:
        restored = k2.restore(tmp_path / "snap", root=tmp_path)
        assert "x" in restored["restored"]
        r4 = await k2.exec_cell("x")
        assert r4.ok and r4.result == "42"
    finally:
        k2.close()


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
