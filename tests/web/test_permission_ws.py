"""Tests for WebSession permission request/response flow."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from quartz.kernel.bridge import PermissionRequest
from quartz.web.session import WebSession


class _DummyKernel:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.dirty = False
        self.perm_handler = None

    def set_bg_count_callback(self, cb: Any) -> None:
        pass

    def set_permission_handler(self, cb: Any) -> None:
        self.perm_handler = cb

    def running_bash_count(self) -> int:
        return 0

    def interrupt(self) -> None:
        pass

    def close(self) -> None:
        pass


class _DummySession:
    def __init__(self, cwd: Path) -> None:
        self.id = "sess-1"
        self.jsonl_path = cwd / "sess.jsonl"

    def save_messages(self, _messages: Any) -> None:
        pass


class _DummyRuntime:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.model = "stub-model"
        self.effort = "medium"
        self.max_context_tokens = 128000
        self.system_prompt = "sys"
        self.messages: list[dict[str, Any]] = []
        self.headless = False
        self.active_session = _DummySession(cwd)
        self.kernel = _DummyKernel(cwd)
        self.client = self

        class _Usage:
            def context_pct(self, _max: int) -> float:
                return 0.0

        self.usage_stats = _Usage()

    def wait_snapshot(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_web_session_permission_roundtrip(tmp_path: Path) -> None:
    rt = _DummyRuntime(tmp_path)
    frames: list[dict[str, Any]] = []

    ws = WebSession(build=lambda _sid, _cwd: (rt, False), cwd=tmp_path)
    ws.attach(frames.append)
    ws.ensure_runtime()
    assert rt.kernel.perm_handler is not None

    req = PermissionRequest(
        tool="git",
        argv=("push", "-u", "origin", "main"),
        cwd=str(tmp_path),
        scope="git_remote",
        command_display="git push -u origin main",
    )

    result_holder: list[str] = []

    def _run_perm() -> None:
        result_holder.append(ws._handle_permission_request(req))

    t = threading.Thread(target=_run_perm)
    t.start()

    # Wait briefly until permission_request frame is emitted
    for _ in range(50):
        if any(f.get("type") == "permission_request" for f in frames):
            break
        threading.Event().wait(0.02)

    perm_frame = next(f for f in frames if f.get("type") == "permission_request")
    assert perm_frame["command"] == "git push -u origin main"
    snap = ws.snapshot()
    assert snap["pendingPermission"]["requestId"] == perm_frame["requestId"]

    assert ws.resolve_permission(perm_frame["requestId"], "allow_session") is True
    t.join(timeout=2.0)
    assert result_holder == ["allow_session"]
    assert ws.snapshot()["pendingPermission"] is None
    assert any(f.get("type") == "permission_resolved" for f in frames)
    ws.close()


def test_web_session_permission_unblocked_on_cancel(tmp_path: Path) -> None:
    rt = _DummyRuntime(tmp_path)
    frames: list[dict[str, Any]] = []

    ws = WebSession(build=lambda _sid, _cwd: (rt, False), cwd=tmp_path)
    ws.attach(frames.append)
    ws.ensure_runtime()

    req = PermissionRequest(
        tool="git",
        argv=("push",),
        cwd=str(tmp_path),
        scope="git_remote",
        command_display="git push",
    )

    result_holder: list[str] = []
    t = threading.Thread(target=lambda: result_holder.append(ws._handle_permission_request(req)))
    t.start()

    for _ in range(50):
        if any(f.get("type") == "permission_request" for f in frames):
            break
        threading.Event().wait(0.02)

    ws.cancel()
    t.join(timeout=2.0)
    assert result_holder == ["reject"]
    assert ws.snapshot()["pendingPermission"] is None
    assert any(
        f.get("type") == "permission_resolved" and f.get("decision") == "reject"
        for f in frames
    )
    ws.close()


def test_web_session_permission_unblocked_on_request_cancel_event(tmp_path: Path) -> None:
    rt = _DummyRuntime(tmp_path)
    frames: list[dict[str, Any]] = []
    cancel_ev = threading.Event()

    ws = WebSession(build=lambda _sid, _cwd: (rt, False), cwd=tmp_path)
    ws.attach(frames.append)
    ws.ensure_runtime()

    req = PermissionRequest(
        tool="git",
        argv=("push",),
        cwd=str(tmp_path),
        scope="git_remote",
        command_display="git push",
        cancel_event=cancel_ev,
    )

    result_holder: list[str] = []
    t = threading.Thread(target=lambda: result_holder.append(ws._handle_permission_request(req)))
    t.start()

    for _ in range(50):
        if any(f.get("type") == "permission_request" for f in frames):
            break
        threading.Event().wait(0.02)

    assert ws.snapshot()["pendingPermission"] is not None
    cancel_ev.set()
    t.join(timeout=2.0)
    assert result_holder == ["reject"]
    assert ws.snapshot()["pendingPermission"] is None
    assert any(
        f.get("type") == "permission_resolved" and f.get("decision") == "reject"
        for f in frames
    )
    ws.close()


def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("Timed out waiting for condition")


def test_permission_parks_turn_after_grace_and_wakes_on_reply(tmp_path: Path) -> None:
    from quartz.core.turn import TurnOutcome, TurnStatus

    rt = _DummyRuntime(tmp_path)
    frames: list[dict[str, Any]] = []
    req = PermissionRequest(
        tool="git",
        argv=("push",),
        cwd=str(tmp_path),
        scope="git_remote",
        command_display="git push",
    )
    decisions: list[str] = []

    def _runner(_rt: Any, _obs: Any, **_kw: Any) -> TurnOutcome:
        decisions.append(ws._handle_permission_request(req))
        return TurnOutcome(TurnStatus.OK)

    ws = WebSession(
        build=lambda _sid, _cwd: (rt, False),
        cwd=tmp_path,
        runner=_runner,
        permission_park_delay_s=0.05,
    )
    ws.attach(frames.append)
    ws.ensure_runtime()

    turn_id = ws.start_turn("push it", [])
    _wait_until(
        lambda: any(
            f.get("type") == "turn_end" and f.get("status") == "waiting_permission"
            for f in frames
        )
    )

    assert ws.turn_running is False
    snap = ws.snapshot()
    assert snap["turnRunning"] is False
    assert snap["pendingPermission"] is not None
    assert ws.can_hibernate() is False

    req_id = snap["pendingPermission"]["requestId"]
    assert ws.resolve_permission(req_id, "allow_once") is True

    _wait_until(
        lambda: any(
            f.get("type") == "turn_end" and f.get("status") == "ok"
            for f in frames
        )
    )
    assert decisions == ["allow_once"]
    turn_starts = [f for f in frames if f.get("type") == "turn_start"]
    assert len(turn_starts) == 2
    assert turn_starts[0]["turnId"] == turn_id
    assert turn_starts[1]["turnId"] == turn_id
    assert ws.turn_running is False
    ws.close()


def test_permission_fast_reply_does_not_park_turn(tmp_path: Path) -> None:
    from quartz.core.turn import TurnOutcome, TurnStatus

    rt = _DummyRuntime(tmp_path)
    frames: list[dict[str, Any]] = []
    req = PermissionRequest(
        tool="git",
        argv=("push",),
        cwd=str(tmp_path),
        scope="git_remote",
        command_display="git push",
    )

    def _runner(_rt: Any, _obs: Any, **_kw: Any) -> TurnOutcome:
        ws._handle_permission_request(req)
        return TurnOutcome(TurnStatus.OK)

    ws = WebSession(
        build=lambda _sid, _cwd: (rt, False),
        cwd=tmp_path,
        runner=_runner,
        permission_park_delay_s=0.5,
    )
    ws.attach(frames.append)
    ws.ensure_runtime()
    ws.start_turn("push it", [])

    _wait_until(lambda: any(f.get("type") == "permission_request" for f in frames))
    req_id = next(f["requestId"] for f in frames if f.get("type") == "permission_request")
    assert ws.resolve_permission(req_id, "allow_once") is True

    _wait_until(lambda: any(f.get("type") == "turn_end" and f.get("status") == "ok" for f in frames))
    assert not any(
        f.get("type") == "turn_end" and f.get("status") == "waiting_permission"
        for f in frames
    )
    assert len([f for f in frames if f.get("type") == "turn_start"]) == 1
    ws.close()


def test_permission_parked_not_aborted_on_reconnect_grace(tmp_path: Path) -> None:
    from quartz.core.turn import TurnOutcome, TurnStatus

    rt = _DummyRuntime(tmp_path)
    frames: list[dict[str, Any]] = []
    req = PermissionRequest(
        tool="git",
        argv=("push",),
        cwd=str(tmp_path),
        scope="git_remote",
        command_display="git push",
    )
    decisions: list[str] = []

    def _runner(_rt: Any, _obs: Any, **_kw: Any) -> TurnOutcome:
        decisions.append(ws._handle_permission_request(req))
        return TurnOutcome(TurnStatus.OK)

    ws = WebSession(
        build=lambda _sid, _cwd: (rt, False),
        cwd=tmp_path,
        runner=_runner,
        permission_park_delay_s=0.05,
    )
    ws.attach(frames.append)
    ws.ensure_runtime()
    ws.start_turn("push it", [])

    _wait_until(
        lambda: any(
            f.get("type") == "turn_end" and f.get("status") == "waiting_permission"
            for f in frames
        )
    )
    ws.detach(frames.append)
    assert not hasattr(ws, "begin_grace")
    assert ws.has_pending_permission is True

    ws.attach(frames.append)
    req_id = ws.snapshot()["pendingPermission"]["requestId"]
    assert ws.resolve_permission(req_id, "allow_session") is True
    _wait_until(lambda: any(f.get("type") == "turn_end" and f.get("status") == "ok" for f in frames))
    assert decisions == ["allow_session"]
    ws.close()


def test_permission_parked_cancelled_cleanly(tmp_path: Path) -> None:
    from quartz.core.turn import TurnOutcome, TurnStatus

    rt = _DummyRuntime(tmp_path)
    frames: list[dict[str, Any]] = []
    req = PermissionRequest(
        tool="git",
        argv=("push",),
        cwd=str(tmp_path),
        scope="git_remote",
        command_display="git push",
    )

    def _runner(_rt: Any, _obs: Any, abort_event: Any = None, **_kw: Any) -> TurnOutcome:
        ws._handle_permission_request(req)
        if abort_event is not None and abort_event.is_set():
            return TurnOutcome(TurnStatus.ABORTED)
        return TurnOutcome(TurnStatus.OK)

    ws = WebSession(
        build=lambda _sid, _cwd: (rt, False),
        cwd=tmp_path,
        runner=_runner,
        permission_park_delay_s=0.05,
    )
    ws.attach(frames.append)
    ws.ensure_runtime()
    ws.start_turn("push it", [])

    _wait_until(
        lambda: any(
            f.get("type") == "turn_end" and f.get("status") == "waiting_permission"
            for f in frames
        )
    )
    assert ws.cancel() is True
    _wait_until(lambda: any(f.get("type") == "turn_end" and f.get("status") == "aborted" for f in frames))
    assert ws.has_pending_permission is False
    assert ws.turn_running is False
    ws.close()

