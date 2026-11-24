"""Tests for native asyncio turn execution in WebSession."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.providers.base import StreamEvent, TextDelta, Usage, UsageEvent
from quartz.web.web_session import WebSession


class _AsyncStream:
    def __init__(self, events: list[StreamEvent], *, delay_s: float = 0.0) -> None:
        self._events = events
        self._delay_s = delay_s
        self.closed = False

    async def __aenter__(self) -> _AsyncStream:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True

    async def __aiter__(self) -> AsyncIterator[StreamEvent]:
        for event in self._events:
            if self.closed:
                return
            if self._delay_s > 0:
                await asyncio.sleep(self._delay_s)
            if self.closed:
                return
            yield event


class _AsyncProvider:
    def __init__(self, streams: list[_AsyncStream]) -> None:
        self._streams = list(streams)

    async def astream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> _AsyncStream:
        del model, messages, tools, effort
        return self._streams.pop(0)

    def close(self) -> None:
        pass


class _FakeKernel:
    dirty = False

    def __init__(self) -> None:
        self.interrupts = 0

    def interrupt(self) -> None:
        self.interrupts += 1

    def close(self) -> None:
        pass

    def drain_events(self) -> list[dict[str, Any]]:
        return []


def _build_session(tmp_path: Path, provider: _AsyncProvider) -> WebSession:
    def _build(_sid: str | None, _cwd: Path | None = None) -> tuple[AgentRuntime, bool]:
        rt = AgentRuntime(
            cwd=tmp_path,
            kernel=_FakeKernel(),  # type: ignore[arg-type]
            client=provider,  # type: ignore[arg-type]
            provider_name="test",
            model="test-model",
            effort="",
            system_prompt="sys",
            active_session=Session.create(tmp_path),
            messages=[],
            usage_stats=TurnUsage(),
            max_context_tokens=128_000,
            compact_reserve_tokens=16_384,
            keep_recent_tokens=32_000,
            verbose=False,
            plain_output=False,
            headless=True,
            transport="web",
        )
        return rt, False

    session = WebSession(_build, cwd=tmp_path)
    session.ensure_runtime()
    return session


def test_web_session_runs_turn_as_native_asyncio_task(tmp_path: Path) -> None:
    asyncio.run(_test_web_session_runs_turn_as_native_asyncio_task(tmp_path))


async def _test_web_session_runs_turn_as_native_asyncio_task(tmp_path: Path) -> None:
    provider = _AsyncProvider(
        [
            _AsyncStream(
                [
                    TextDelta("hello "),
                    TextDelta("world"),
                    UsageEvent(Usage(input_tokens=42, output_tokens=5, cached_tokens=21)),
                ],
                delay_s=0.01,
            )
        ]
    )
    session = _build_session(tmp_path, provider)
    frames: list[dict[str, Any]] = []
    done = asyncio.Event()

    def _sink(frame: dict[str, Any]) -> None:
        frames.append(frame)
        if frame.get("type") == "turn_end":
            done.set()

    session.attach(_sink)
    turn_id = session.start_turn("hi", [])
    assert turn_id.startswith("turn-")

    turn = session._turn
    assert turn is not None
    assert turn.task is not None
    assert turn.thread is None

    await asyncio.wait_for(done.wait(), timeout=2.0)
    await asyncio.sleep(0)  # allow _finalize_turn's finally to clear self._turn

    assert not session.turn_running
    types = [f["type"] for f in frames]
    assert "turn_start" in types
    assert "text_delta" in types
    assert types[-1] == "turn_end"
    end_frame = frames[-1]
    assert end_frame["status"] == "ok"
    session.close()


def test_web_session_async_steer_and_cancel(tmp_path: Path) -> None:
    asyncio.run(_test_web_session_async_steer_and_cancel(tmp_path))


async def _test_web_session_async_steer_and_cancel(tmp_path: Path) -> None:
    # 1. Mid-turn steer
    provider = _AsyncProvider(
        [
            _AsyncStream([TextDelta("first "), TextDelta("slow")], delay_s=0.04),
            _AsyncStream([TextDelta("steered done")]),
        ]
    )
    session = _build_session(tmp_path, provider)
    frames: list[dict[str, Any]] = []
    done = asyncio.Event()

    def _sink(frame: dict[str, Any]) -> None:
        frames.append(frame)
        if frame.get("type") == "turn_end":
            done.set()

    session.attach(_sink)
    session.start_turn("initial", [])
    await asyncio.sleep(0.02)
    session.steer("redirect")
    await asyncio.wait_for(done.wait(), timeout=2.0)
    await asyncio.sleep(0)

    assert any(f.get("type") == "steering" and f.get("text") == "redirect" for f in frames)
    assert frames[-1]["status"] == "ok"

    # 2. Cancel mid-stream
    provider2 = _AsyncProvider(
        [_AsyncStream([TextDelta("part1"), TextDelta("part2"), TextDelta("part3")], delay_s=0.05)]
    )
    session2 = _build_session(tmp_path, provider2)
    frames2: list[dict[str, Any]] = []
    done2 = asyncio.Event()

    def _sink2(frame: dict[str, Any]) -> None:
        frames2.append(frame)
        if frame.get("type") == "turn_end":
            done2.set()

    session2.attach(_sink2)
    session2.start_turn("will cancel", [])
    await asyncio.sleep(0.03)
    assert session2.cancel() is True
    await asyncio.wait_for(done2.wait(), timeout=2.0)
    await asyncio.sleep(0)

    assert not session2.turn_running
    assert frames2[-1]["type"] == "turn_end"
    assert frames2[-1]["status"] == "aborted"
    session.close()
    session2.close()
