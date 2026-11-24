"""The web transport against a *real* uvicorn server and a *real* socket.

`TestClient` is fine for routing and frame shape, but a few behaviours only exist
in a real server: an actual TCP close, an actual separate event loop, and an actual
turn thread. Those are the ones worth proving here.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.web.app import create_app


class _Kernel:
    dirty = False

    def __init__(self) -> None:
        self.closed = False

    def interrupt(self) -> None: ...
    def close(self) -> None:
        self.closed = True
    def list_vars(self) -> dict[str, str]:
        return {}
    def running_bash_jobs(self) -> list[dict[str, object]]:
        return []
    def list_helpers(self) -> dict[str, object]:
        return {"helpers": [], "warnings": []}


def _runtime(cwd: Path) -> AgentRuntime:
    return AgentRuntime(
        cwd=cwd,
        kernel=_Kernel(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="stub",
        model="stub-model",
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


@pytest.fixture()
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Start a real uvicorn server in a thread; yields ``(url, app, state)``."""
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    websockets = pytest.importorskip("websockets")

    state: dict[str, Any] = {"aborted": False}

    async def runner(rt: AgentRuntime, observer: Any, *, abort_event: Any = None, steering_queue: Any = None, **_: Any):
        """A turn that ends only when it is aborted -- never on its own.

        A blocking wait rather than a poll, so the turn is provably still running
        between `turn_start` and the abort instead of merely running *yet*.
        """
        import asyncio

        if abort_event is not None:
            await asyncio.wait_for(abort_event.wait(), timeout=30)
            state["aborted"] = True
            return TurnOutcome(TurnStatus.ABORTED, elapsed_s=0.01)
        raise AssertionError("the turn was never aborted")

    runtime = _runtime(tmp_path)
    app = create_app(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        static_dir=tmp_path / "missing",
        runner=runner,
    )

    import socket as _socket

    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "server never started"

    yield f"ws://127.0.0.1:{port}/ws", runtime, state, websockets

    server.should_exit = True
    thread.join(timeout=15)


def test_a_real_socket_close_keeps_the_turn_running(live_server: Any) -> None:
    """Closing the tab mid-turn must NOT abort it: a phone is not a resignation.

    The answer is still being written, and the same page comes back seconds later.
    Only the grace expiring with nobody attached may stop it (covered by
    `test_web.reconnect`).
    """
    url, runtime, state, websockets = live_server

    async def scenario() -> list[str]:
        seen: list[str] = []
        async with websockets.connect(url, max_size=None) as socket:
            await socket.send(json.dumps({"type": "hello", "clientVersion": 1}))
            await socket.send(json.dumps({"type": "prompt", "text": "long job"}))
            while True:
                frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                seen.append(frame["type"])
                if frame["type"] == "turn_start":
                    break
        return seen

    seen = asyncio.run(scenario())
    assert "turn_start" in seen

    # Give the close handler time to run, then assert it changed nothing.
    time.sleep(0.5)
    assert state["aborted"] is False
    # The kernel outlives the tab either way: a reconnect reconciles the session.
    assert runtime.kernel.closed is False  # type: ignore[attr-defined]

    # And the turn is still reachable: a reconnect sees it running, and the answer
    # it produced is still the text the turn is writing.
    async def reconnect() -> dict[str, Any]:
        async with websockets.connect(url, max_size=None) as socket:
            await socket.send(json.dumps({"type": "hello", "clientVersion": 1}))
            while True:
                frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                if frame["type"] == "session_state":
                    return frame

    snapshot = asyncio.run(reconnect())
    assert snapshot["turnRunning"] is True


def test_reconnecting_mid_turn_reattaches_instead_of_blocking(live_server: Any) -> None:
    """The race: a fast reconnect must be able to steer or prompt, not be refused.

    A phone waking sends `hello` before the old socket's close handler has finished
    aborting anything. That used to leave `turnRunning` true forever behind a socket
    nobody owned, and every later prompt answered "a turn is already running" -- the
    page looked connected and never moved again.
    """
    url, _rt, _state, websockets = live_server

    async def scenario() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        first = await websockets.connect(url, max_size=None)
        await first.send(json.dumps({"type": "hello", "clientVersion": 1}))
        await first.send(json.dumps({"type": "prompt", "text": "long job"}))
        while True:
            frame = json.loads(await asyncio.wait_for(first.recv(), timeout=10))
            if frame["type"] == "turn_start":
                break
        await first.close()

        # Reconnect immediately, with no pause for the close handler.
        second = await websockets.connect(url, max_size=None)
        await second.send(json.dumps({"type": "hello", "clientVersion": 1}))
        snapshot: dict[str, Any] = {}
        replayed: list[dict[str, Any]] = []
        for _ in range(12):
            frame = json.loads(await asyncio.wait_for(second.recv(), timeout=10))
            if frame["type"] == "session_state":
                snapshot = frame
                # Keep reading: the live turn is retold after the snapshot.
                continue
            replayed.append(frame)
            if frame["type"] in ("turn_start", "status", "text_delta"):
                break
        await second.close()
        return snapshot, replayed

    snapshot, replayed = asyncio.run(scenario())

    # The turn is owned by the session, not by the dead socket, and the new socket
    # is told about it rather than being handed a frozen "running".
    assert snapshot["turnRunning"] is True
    assert [f["type"] for f in replayed], "the live turn was not retold to the new socket"
    assert replayed[0]["type"] == "turn_start"


def test_the_health_route_answers_over_a_real_server(live_server: Any) -> None:
    url, _rt, _state, _ws = live_server
    import urllib.request

    base = url.replace("ws://", "http://").removesuffix("/ws")
    with urllib.request.urlopen(f"{base}/api/health", timeout=10) as response:
        body = json.loads(response.read())
    assert body["status"] == "ok"
