"""Tests for WebSession permission request/response flow."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

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

    def close(self) -> None:
        pass


class _DummySession:
    def __init__(self, cwd: Path) -> None:
        self.id = "sess-1"
        self.jsonl_path = cwd / "sess.jsonl"


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
