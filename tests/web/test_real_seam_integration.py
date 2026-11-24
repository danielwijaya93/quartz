"""Real-seam integration tests wiring KernelClient (+ bwrap), agent_loop, and WebSession.

Unlike unit tests that stub the kernel and runner with `FakeKernel`, every test
here runs the real `KernelClient` subprocess, real `Session` persistence, real
`run_interactive_turn` / `agent_loop`, and real `WebSession` / `SessionManager`
(and FastAPI WebSocket transport). Only the LLM provider is scripted.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _bwrap_helpers import bwrap_unavailable, require_bwrap
from fastapi.testclient import TestClient

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.kernel.client import KernelClient
from quartz.providers.base import TextDelta, ToolCallDelta, Usage, UsageEvent
from quartz.web.app import create_app
from quartz.web.session import SessionManager, WebSession


class _FakeStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def __aiter__(self):
        for ev in self._events:
            yield ev

    async def aclose(self) -> None:
        return None


class _ScriptedProvider:
    """Minimal async LLMProvider that scripts real `ipython` tool calls and answers."""

    def __init__(self, handler: Callable[[list[dict[str, Any]], int], list[Any]]) -> None:
        self._handler = handler
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        effort: str | None = None,
    ) -> _FakeStream:
        del model, tools, effort
        idx = len(self.calls)
        snapshot = [dict(m) for m in messages]
        self.calls.append(snapshot)
        return _FakeStream(self._handler(snapshot, idx))

    def close(self) -> None:
        return None


def _tool_events(call_id: str, code: str) -> list[Any]:
    return [
        ToolCallDelta(
            index=0,
            id=call_id,
            name="ipython",
            arguments_delta=json.dumps({"code": code}),
        ),
        UsageEvent(usage=Usage(input_tokens=20, output_tokens=10)),
    ]


def _text_events(text: str) -> list[Any]:
    return [
        TextDelta(text=text),
        UsageEvent(usage=Usage(input_tokens=25, output_tokens=10)),
    ]


def _make_real_builder(
    provider: _ScriptedProvider,
    default_cwd: Path,
    *,
    sandbox: bool = False,
) -> Callable[[str | None, Path | None], tuple[AgentRuntime, bool]]:
    """Build a real `AgentRuntime` backed by a real `KernelClient` and `Session`."""

    def _build(session_id: str | None, target_cwd: Path | None = None) -> tuple[AgentRuntime, bool]:
        if session_id:
            session, loaded_messages = Session.load(session_id)
            resumed = True
            cwd = (target_cwd or Path(str(session.meta.get("cwd") or default_cwd))).resolve()
        else:
            cwd = (target_cwd or default_cwd).resolve()
            session = Session.create(cwd)
            loaded_messages = []
            resumed = False

        kernel = KernelClient(cwd=cwd, sandbox=sandbox)
        try:
            kernel.start(timeout=15.0)
        except Exception as exc:
            if sandbox:
                bwrap_unavailable(f"bwrap cannot start in this environment: {exc}")
            raise

        if resumed:
            session.restore_kernel(kernel)

        runtime = AgentRuntime(
            cwd=cwd,
            kernel=kernel,
            client=provider,  # type: ignore[arg-type]
            provider_name="scripted",
            model="scripted-model",
            effort="medium",
            system_prompt="You are Quartz.",
            active_session=session,
            messages=list(loaded_messages),
            usage_stats=TurnUsage(),
            max_context_tokens=128_000,
            compact_reserve_tokens=16_384,
            keep_recent_tokens=32_000,
            verbose=False,
            plain_output=False,
            headless=False,
        )
        return runtime, resumed

    return _build


def _recv_until(
    ws: Any, predicate: Callable[[dict[str, Any]], bool], *, limit: int = 80
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if predicate(frame):
            return frames
        if frame.get("type") == "error" and frame.get("fatal"):
            raise AssertionError(f"Fatal server error frame: {frame}")
    raise AssertionError(
        f"Did not receive expected frame within {limit} frames: {[f.get('type') for f in frames]}"
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("Condition timed out")


def test_real_seam_turn_disconnect_reconnect_and_state_preserved(tmp_path: Path) -> None:
    """Real kernel cell survives WS disconnect, replays on reconnect, and keeps REPL state."""
    started_flag = tmp_path / "cell_started.flag"
    release_flag = tmp_path / "release_cell.flag"

    def _script(messages: list[dict[str, Any]], _idx: int) -> list[Any]:
        last = messages[-1]
        if last["role"] == "user":
            text = str(last["content"])
            if "compute" in text:
                code = (
                    "import time\n"
                    "from pathlib import Path\n"
                    "x = 41 + 1\n"
                    f"Path({str(started_flag)!r}).write_text('go')\n"
                    f"deadline = time.monotonic() + 10\n"
                    f"while not Path({str(release_flag)!r}).exists() and time.monotonic() < deadline:\n"
                    "    time.sleep(0.02)\n"
                    "print(f'computed={x}')\n"
                )
                return _tool_events("tc-compute", code)
            if "double" in text:
                return _tool_events("tc-double", "print(f'doubled={x * 2}')")
        assert last["role"] == "tool"
        return _text_events(f"Result: {last['content']}")

    provider = _ScriptedProvider(_script)
    app = create_app(build=_make_real_builder(provider, tmp_path), cwd=tmp_path)

    with TestClient(app) as client:
        # 1. Start a real kernel cell, then disconnect mid-turn while it is still executing.
        with client.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "hello", "clientVersion": 1})
            _recv_until(ws1, lambda f: f.get("type") == "session_state")
            ws1.send_json({"type": "prompt", "text": "please compute"})
            _wait_until(started_flag.exists, timeout=10.0)
        # ws1 is now disconnected while the real Python cell is still running.

        # 2. Reconnect on a second socket, unblock the cell, and receive live completion.
        with client.websocket_connect("/ws") as ws2:
            ws2.send_json({"type": "hello", "clientVersion": 1})
            state = next(
                f
                for f in _recv_until(ws2, lambda f: f.get("type") == "session_state")
                if f["type"] == "session_state"
            )
            assert state["turnRunning"] is True

            release_flag.write_text("go")
            frames = _recv_until(ws2, lambda f: f.get("type") == "turn_end")
            tool_res = next(f for f in frames if f.get("type") == "tool_result")
            assert tool_res["ok"] is True
            assert "computed=42" in tool_res["output"]
            assert frames[-1]["status"] == "ok"

            # 3. Follow-up turn verifies REPL variable `x` survived disconnect/reconnect.
            ws2.send_json({"type": "prompt", "text": "now double"})
            frames2 = _recv_until(ws2, lambda f: f.get("type") == "turn_end")
            tool_res2 = next(f for f in frames2 if f.get("type") == "tool_result")
            assert tool_res2["ok"] is True
            assert "doubled=84" in tool_res2["output"]


def test_real_seam_disconnect_grace_aborts_live_kernel_cell(tmp_path: Path) -> None:
    """When a client disconnects and never returns, grace timeout aborts the live kernel cell."""
    started_flag = tmp_path / "stuck_started.flag"

    def _script(messages: list[dict[str, Any]], _idx: int) -> list[Any]:
        last = messages[-1]
        if last["role"] == "user":
            text = str(last["content"])
            if "hang" in text:
                code = (
                    "import time\n"
                    "from pathlib import Path\n"
                    f"Path({str(started_flag)!r}).write_text('started')\n"
                    "time.sleep(30)\n"
                )
                return _tool_events("tc-hang", code)
            return _tool_events("tc-after", "print('still-alive')")
        return _text_events(f"Done: {last['content']}")

    provider = _ScriptedProvider(_script)
    session = WebSession(
        _make_real_builder(provider, tmp_path),
        cwd=tmp_path,
    )
    events: list[dict[str, Any]] = []
    try:
        session.ensure_runtime(tmp_path)
        session.attach(events.append)
        session.start_turn("please hang", [])
        _wait_until(started_flag.exists, timeout=10.0)
        # Detach and do not reconnect -> grace timer must abort the running kernel cell.
        session.detach(events.append)
        session.begin_grace(timeout=0.25)
        _wait_until(lambda: not session.turn_running, timeout=10.0)

        # Re-attach and verify the session recovered cleanly and still executes Python cells.
        recovered: list[dict[str, Any]] = []
        session.attach(recovered.append)
        try:
            session.start_turn("check alive", [])
            _wait_until(lambda: any(f.get("type") == "turn_end" for f in recovered), timeout=10.0)
            tool_res = next(f for f in recovered if f.get("type") == "tool_result")
            assert tool_res["ok"] is True
            assert "still-alive" in tool_res["output"]
        finally:
            session.detach(recovered.append)
    finally:
        session.close()


def test_real_seam_bwrap_permission_allow_reject_cancel_and_retry(tmp_path: Path) -> None:
    """Real bwrap + host permission bridge + WS roundtrip: allow_once, reject, cancel & retry."""
    require_bwrap()

    # Set up a real local bare remote + workspace repo so `git ls-remote origin` works offline.
    bare = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "HEAD"], check=True, capture_output=True)

    def _script(messages: list[dict[str, Any]], idx: int) -> list[Any]:
        last = messages[-1]
        if last["role"] == "user":
            text = str(last["content"])
            if "remote-git" in text:
                code = (
                    "import subprocess\n"
                    "r = subprocess.run(['git', 'ls-remote', 'origin'], capture_output=True, text=True, timeout=30)\n"
                    "print(f'EXIT={r.returncode} OUT={r.stdout.strip()} ERR={r.stderr.strip()}')\n"
                )
                return _tool_events(f"tc-git-{idx}", code)
            return _tool_events(f"tc-ok-{idx}", "print('post-cancel-ok')")
        return _text_events(f"Observed: {last['content']}")

    provider = _ScriptedProvider(_script)
    app = create_app(build=_make_real_builder(provider, repo, sandbox=True), cwd=repo)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        _recv_until(ws, lambda f: f.get("type") == "session_state")

        # 1. Happy path: allow_once lets host bridge run `git ls-remote origin`.
        ws.send_json({"type": "prompt", "text": "run remote-git allow"})
        frames = _recv_until(ws, lambda f: f.get("type") == "permission_request")
        req = frames[-1]
        ws.send_json(
            {"type": "permission_response", "requestId": req["requestId"], "decision": "allow_once"}
        )
        done1 = _recv_until(ws, lambda f: f.get("type") == "turn_end")
        tr1 = next(f for f in done1 if f.get("type") == "tool_result")
        assert tr1["ok"] is True
        assert "EXIT=0" in tr1["output"]
        assert "HEAD" in tr1["output"]
        assert done1[-1]["status"] == "ok"

        # 2. Unhappy path: reject blocks execution cleanly.
        ws.send_json({"type": "prompt", "text": "run remote-git reject"})
        frames2 = _recv_until(ws, lambda f: f.get("type") == "permission_request")
        req2 = frames2[-1]
        ws.send_json(
            {"type": "permission_response", "requestId": req2["requestId"], "decision": "reject"}
        )
        done2 = _recv_until(ws, lambda f: f.get("type") == "turn_end")
        tr2 = next(f for f in done2 if f.get("type") == "tool_result")
        assert "EXIT=0" not in tr2["output"]
        assert done2[-1]["status"] == "ok"

        # 3. Edge case: cancel mid-permission unblocks without deadlock, and next turn succeeds.
        ws.send_json({"type": "prompt", "text": "run remote-git cancel"})
        _recv_until(ws, lambda f: f.get("type") == "permission_request")
        ws.send_json({"type": "cancel"})
        done3 = _recv_until(ws, lambda f: f.get("type") == "turn_end")
        assert done3[-1]["status"] == "aborted"

        ws.send_json({"type": "prompt", "text": "verify alive"})
        done4 = _recv_until(ws, lambda f: f.get("type") == "turn_end")
        tr4 = next(f for f in done4 if f.get("type") == "tool_result")
        assert tr4["ok"] is True
        assert "post-cancel-ok" in tr4["output"]


def test_real_seam_multi_session_lru_hibernation_restore_and_bg_guard(tmp_path: Path) -> None:
    """Real LRU hibernation persists & restores kernel snapshots across cwds and respects bg jobs."""
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir()
    ws_b.mkdir()

    def _script(messages: list[dict[str, Any]], idx: int) -> list[Any]:
        last = messages[-1]
        if last["role"] == "user":
            text = str(last["content"])
            if "store-a" in text:
                return _tool_events(f"tc-{idx}", "magic_val = {'answer': 42}; print('saved-a')")
            if "check-a" in text:
                return _tool_events(
                    f"tc-{idx}", "import os; print(f'ans={magic_val[\"answer\"]} cwd={os.getcwd()}')"
                )
            if "start-bg-b" in text:
                return _tool_events(f"tc-{idx}", "_bg = bash('sleep 15'); print(f'pid={_bg.pid}')")
            if "stop-bg-b" in text:
                return _tool_events(f"tc-{idx}", "_bg.kill(); print('killed')")
            return _tool_events(f"tc-{idx}", "other = 'beta'; print('saved-b')")
        return _text_events(f"Done: {last['content']}")

    provider = _ScriptedProvider(_script)
    manager = SessionManager(
        _make_real_builder(provider, ws_a),
        cwd=ws_a,
        max_live_sessions=1,
    )
    try:
        # 1. Run real cell in Session A (ws_a) and detach so it can hibernate.
        sess_a = manager.create_session(cwd=ws_a)
        ev_a: list[dict[str, Any]] = []
        sess_a.attach(ev_a.append)
        sess_a.start_turn("store-a", [])
        _wait_until(lambda: any(f.get("type") == "turn_end" for f in ev_a), timeout=10.0)
        sid_a = sess_a.runtime.active_session.id
        sess_a.detach(ev_a.append)

        # 2. Create Session B in ws_b -> Session A exceeds max_live_sessions=1 and hibernates.
        sess_b = manager.create_session(cwd=ws_b)
        ev_b: list[dict[str, Any]] = []
        sess_b.attach(ev_b.append)
        sess_b.start_turn("store-b", [])
        _wait_until(lambda: any(f.get("type") == "turn_end" for f in ev_b), timeout=10.0)
        sid_b = sess_b.runtime.active_session.id

        assert not sess_a.built, "Idle Session A should be hibernated to disk"
        active_ids = {item["sessionId"] for item in manager.active_sessions_items()}
        assert sid_a in active_ids, "Hibernated Session A must remain visible in Active Sessions"

        # 3. Start a real background bash job in Session B, detach, then switch back to Session A.
        ev_b.clear()
        sess_b.start_turn("start-bg-b", [])
        _wait_until(lambda: any(f.get("type") == "turn_end" for f in ev_b), timeout=10.0)
        sess_b.detach(ev_b.append)
        assert sess_b.has_running_background_jobs is True
        assert sess_b.can_hibernate() is False

        # 4. Resume Session A -> restores original cwd + kernel snapshot (`magic_val`) while B stays live.
        resumed_a = manager.get_or_load_session(sid_a)
        assert sess_b.built is True, "Session B with active bg job must NOT be hibernated"
        ev_a2: list[dict[str, Any]] = []
        resumed_a.attach(ev_a2.append)
        try:
            resumed_a.start_turn("check-a", [])
            _wait_until(lambda: any(f.get("type") == "turn_end" for f in ev_a2), timeout=10.0)
            tr = next(f for f in ev_a2 if f.get("type") == "tool_result")
            assert tr["ok"] is True
            assert "ans=42" in tr["output"]
            assert str(ws_a.resolve()) in tr["output"]
        finally:
            resumed_a.detach(ev_a2.append)

        # Cleanly stop bg job in B.
        reloaded_b = manager.get_or_load_session(sid_b)
        ev_b2: list[dict[str, Any]] = []
        reloaded_b.attach(ev_b2.append)
        try:
            reloaded_b.start_turn("stop-bg-b", [])
            _wait_until(lambda: any(f.get("type") == "turn_end" for f in ev_b2), timeout=10.0)
        finally:
            reloaded_b.detach(ev_b2.append)
    finally:
        manager.close_all()


def test_real_seam_bg_bash_wakeup_success_and_failure(tmp_path: Path) -> None:
    """Real bg bash jobs (both exit 0 and non-zero) wake an idle WebSession automatically."""
    go_ok = tmp_path / "go_ok.flag"
    go_fail = tmp_path / "go_fail.flag"

    def _script(messages: list[dict[str, Any]], idx: int) -> list[Any]:
        last = messages[-1]
        if last["role"] == "user":
            text = str(last["content"])
            if "launch-ok" in text:
                cmd = f"while [ ! -f {str(go_ok)!r} ]; do sleep 0.02; done; echo bg-hello"
                return _tool_events(f"tc-ok-{idx}", f"h = bash({cmd!r}); print(h.pid)")
            if "launch-fail" in text:
                cmd = f"while [ ! -f {str(go_fail)!r} ]; do sleep 0.02; done; echo bg-boom >&2; exit 7"
                return _tool_events(f"tc-fail-{idx}", f"h = bash({cmd!r}); print(h.pid)")
            # Auto-woken turn receives the `[bash-done ...]` notice directly as user content.
            return _text_events(f"Woke on: {text}")
        return _text_events("Launched job.")

    provider = _ScriptedProvider(_script)
    app = create_app(build=_make_real_builder(provider, tmp_path), cwd=tmp_path)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        _recv_until(ws, lambda f: f.get("type") == "session_state")

        # 1. Happy path: release bg job ONLY after Turn 1 completes (guaranteed idle wakeup).
        ws.send_json({"type": "prompt", "text": "launch-ok"})
        _recv_until(ws, lambda f: f.get("type") == "turn_end")
        go_ok.write_text("go", encoding="utf-8")

        wake_ok = _recv_until(ws, lambda f: f.get("type") == "turn_end")
        deltas_ok = "".join(f.get("text", "") for f in wake_ok if f.get("type") == "text_delta")
        assert "bg-hello" in deltas_ok
        assert "exit:0" in deltas_ok

        # 2. Unhappy path: release failing bg job (exit 7) after launch turn completes.
        ws.send_json({"type": "prompt", "text": "launch-fail"})
        _recv_until(ws, lambda f: f.get("type") == "turn_end")
        go_fail.write_text("go", encoding="utf-8")

        wake_fail = _recv_until(ws, lambda f: f.get("type") == "turn_end")
        deltas_fail = "".join(f.get("text", "") for f in wake_fail if f.get("type") == "text_delta")
        assert "bg-boom" in deltas_fail
        assert "exit:7" in deltas_fail


def test_real_seam_bwrap_session_manager_close_all_leaves_no_orphans(tmp_path: Path) -> None:
    """SessionManager.close_all() kills real bwrap workers and their child processes cleanly."""
    require_bwrap()

    workspace = tmp_path / "ws"
    workspace.mkdir()
    lock_file = workspace / "child.lock"

    def _script(messages: list[dict[str, Any]], _idx: int) -> list[Any]:
        last = messages[-1]
        if last["role"] == "user":
            child_code = (
                f"import fcntl, time; "
                f"f = open({str(lock_file)!r}, 'w'); "
                f"fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); "
                f"print('locked', flush=True); "
                f"time.sleep(30)"
            )
            code = (
                "import subprocess, sys\n"
                f"_p = subprocess.Popen([sys.executable, '-c', {child_code!r}], stdout=subprocess.PIPE, text=True)\n"
                "assert _p.stdout.readline().strip() == 'locked'\n"
                "print('child-running')\n"
            )
            return _tool_events("tc-spawn", code)
        return _text_events("spawned")

    provider = _ScriptedProvider(_script)
    manager = SessionManager(
        _make_real_builder(provider, workspace, sandbox=True),
        cwd=workspace,
    )
    sess = manager.create_session(cwd=workspace)
    events: list[dict[str, Any]] = []
    sess.attach(events.append)
    try:
        sess.start_turn("spawn child", [])
        _wait_until(lambda: any(f.get("type") == "turn_end" for f in events), timeout=15.0)
        assert lock_file.exists()

        # Verify child really holds the flock right now.
        with open(lock_file, "a+", encoding="utf-8") as probe, pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        sess.detach(events.append)
        manager.close_all()

    # After close_all(), the child process holding the flock must be dead (zero orphans).
    deadline = time.monotonic() + 5.0
    acquired = False
    while time.monotonic() < deadline:
        with open(lock_file, "a+", encoding="utf-8") as check:
            try:
                fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(check, fcntl.LOCK_UN)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.05)
    assert acquired, "Child process survived SessionManager.close_all() (orphaned bwrap worker)"
