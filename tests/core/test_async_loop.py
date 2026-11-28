"""Tests for native async agent loop, compaction, and turn runner."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from quartz.core.compact import CompactionReason, CompactionResult, compact_messages
from quartz.core.events import TurnObserver
from quartz.core.loop import TurnAborted, TurnUsage, agent_loop
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnStatus, TurnView, run_interactive_turn
from quartz.kernel.base import CellResult
from quartz.providers.base import (
    CompletionResult,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageEvent,
)


class _RecordingObserver(TurnObserver):
    def __init__(self) -> None:
        self.statuses: list[str | None] = []
        self.thinking: list[str] = []
        self.text: list[str] = []
        self.tool_calls: list[tuple[str, str]] = []
        self.tool_results: list[tuple[str, CellResult]] = []
        self.compactions: list[CompactionResult] = []
        self.steerings: list[str] = []

    def on_status(self, label: str | None) -> None:
        self.statuses.append(label)

    def on_thinking_delta(self, text: str) -> None:
        self.thinking.append(text)

    def on_text_delta(self, text: str) -> None:
        self.text.append(text)

    def on_tool_call(self, tool_call_id: str, code: str) -> None:
        self.tool_calls.append((tool_call_id, code))

    def on_tool_result(self, tool_call_id: str, result: CellResult) -> None:
        self.tool_results.append((tool_call_id, result))

    def on_compaction(self, result: CompactionResult) -> None:
        self.compactions.append(result)

    def on_steering(self, text: str) -> None:
        self.steerings.append(text)


class _FakeAsyncStreamHandle:
    def __init__(self, events: list[StreamEvent], *, delay_s: float = 0.0) -> None:
        self._events = events
        self._delay_s = delay_s
        self.closed = False

    async def __aenter__(self) -> _FakeAsyncStreamHandle:
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


class _FakeAsyncProvider:
    def __init__(
        self,
        streams: list[_FakeAsyncStreamHandle] | None = None,
        completions: list[CompletionResult] | None = None,
    ) -> None:
        self._streams = list(streams or [])
        self._completions = list(completions or [])
        self.stream_calls: list[list[dict[str, Any]]] = []
        self.complete_calls: list[list[dict[str, Any]]] = []

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> _FakeAsyncStreamHandle:
        del model, tools, effort
        self.stream_calls.append([dict(m) for m in messages])
        return self._streams.pop(0)

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult:
        del model, tools, effort
        self.complete_calls.append([dict(m) for m in messages])
        if self._completions:
            return self._completions.pop(0)
        return CompletionResult(text="Summary of earlier conversation.", tool_calls=[], usage=Usage())


class _FakeKernel:
    dirty = False

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.interrupted = 0
        self.vars = {"x": "int"}

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        del timeout
        self.executed.append(code)
        return CellResult(ok=True, stdout="42\n", stderr="", result=None)

    async def wait_event(self, timeout: float | None = None) -> bool:
        del timeout
        return False

    def interrupt(self) -> None:
        self.interrupted += 1

    def drain_events(self) -> list[dict[str, Any]]:
        return []

    def has_pending_events(self) -> bool:
        return False

    def consume_restart_notice(self) -> str | None:
        return None

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        del timeout
        return dict(self.vars)

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        del timeout
        return {"helpers": [{"name": "sh", "source": "global"}]}


async def test_agent_loop_streaming_and_tool_calls() -> None:
    stream1 = _FakeAsyncStreamHandle(
        [
            ThinkingDelta("Planning"),
            ToolCallDelta(index=0, id="tc_1", name="ipython", arguments_delta=json.dumps({"code": "print(42)"})),
            UsageEvent(Usage(input_tokens=100, output_tokens=20, cached_tokens=50)),
        ]
    )
    stream2 = _FakeAsyncStreamHandle(
        [
            TextDelta("Done: "),
            TextDelta("42"),
            UsageEvent(Usage(input_tokens=150, output_tokens=10, cached_tokens=100)),
        ]
    )
    provider = _FakeAsyncProvider(streams=[stream1, stream2])
    kernel = _FakeKernel()
    obs = _RecordingObserver()
    usage_stats = TurnUsage()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "compute"}]

    answer, updated, tokens = await agent_loop(
        provider,  # type: ignore[arg-type]
        "test-model",
        "sys",
        messages,
        kernel,  # type: ignore[arg-type]
        observer=obs,
        usage_stats=usage_stats,
    )

    assert answer == "Done: 42"
    assert tokens == 150
    assert kernel.executed == ["print(42)"]
    assert obs.thinking == ["Planning"]
    assert obs.text == ["Done: ", "42"]
    assert obs.tool_calls == [("tc_1", "print(42)")]
    assert len(obs.tool_results) == 1
    assert usage_stats.prompt_tokens == 150
    assert updated[-1] == {"role": "assistant", "content": "Done: 42"}


async def test_compact_messages_and_loop_compaction() -> None:
    provider = _FakeAsyncProvider(completions=[CompletionResult(text="Concise summary.", tool_calls=[], usage=Usage())])
    kernel = _FakeKernel()
    msgs = [
        {"role": "user", "content": "a " * 3000},
        {"role": "assistant", "content": "b " * 3000},
        {"role": "user", "content": "c " * 3000},
        {"role": "assistant", "content": "d " * 3000},
        {"role": "user", "content": "latest question"},
    ]

    res = await compact_messages(
        provider,  # type: ignore[arg-type]
        "test-model",
        msgs,
        kernel,
        budget_tokens=100,
        keep_recent_tokens=50,
    )
    assert res.reason is CompactionReason.SUMMARIZED
    assert "Concise summary." in res.messages[0]["content"]
    assert "[python-state]" in res.messages[0]["content"]
    assert "sh()" in res.messages[0]["content"]

    stream = _FakeAsyncStreamHandle([TextDelta("All good")])
    provider2 = _FakeAsyncProvider(
        streams=[stream],
        completions=[CompletionResult(text="Loop summary.", tool_calls=[], usage=Usage())],
    )
    obs = _RecordingObserver()
    loop_msgs = [dict(m) for m in msgs]
    answer, _, _ = await agent_loop(
        provider2,  # type: ignore[arg-type]
        "test-model",
        "sys",
        loop_msgs,
        kernel,  # type: ignore[arg-type]
        observer=obs,
        max_context_tokens=5_000,
        compact_reserve_tokens=4_500,
        keep_recent_tokens=50,
    )
    assert answer == "All good"
    assert len(obs.compactions) == 1
    assert obs.compactions[0].reason is CompactionReason.SUMMARIZED


async def test_agent_loop_abort_via_asyncio_event() -> None:
    stream = _FakeAsyncStreamHandle(
        [TextDelta("part1"), TextDelta("part2"), TextDelta("part3")],
        delay_s=0.05,
    )
    provider = _FakeAsyncProvider(streams=[stream])
    kernel = _FakeKernel()
    abort = asyncio.Event()

    async def _trigger_abort() -> None:
        await asyncio.sleep(0.03)
        abort.set()

    trigger = asyncio.create_task(_trigger_abort())
    with pytest.raises(TurnAborted):
        await agent_loop(
            provider,  # type: ignore[arg-type]
            "test-model",
            "sys",
            [{"role": "user", "content": "hi"}],
            kernel,  # type: ignore[arg-type]
            abort_event=abort,
        )
    await trigger
    assert stream.closed


async def test_agent_loop_mid_turn_steering() -> None:
    stream1 = _FakeAsyncStreamHandle(
        [TextDelta("initial "), TextDelta("slow1"), TextDelta("slow2")],
        delay_s=0.04,
    )
    stream2 = _FakeAsyncStreamHandle([TextDelta("steered answer")])
    provider = _FakeAsyncProvider(streams=[stream1, stream2])
    kernel = _FakeKernel()
    obs = _RecordingObserver()
    steering: asyncio.Queue[str] = asyncio.Queue()

    async def _send_steer() -> None:
        await asyncio.sleep(0.03)
        steering.put_nowait("change direction")

    steer_task = asyncio.create_task(_send_steer())
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    answer, updated, _ = await agent_loop(
        provider,  # type: ignore[arg-type]
        "test-model",
        "sys",
        messages,
        kernel,  # type: ignore[arg-type]
        observer=obs,
        steering_queue=steering,
    )
    await steer_task
    assert answer == "steered answer"
    assert obs.steerings == ["change direction"]
    assert any(m.get("role") == "user" and m.get("content") == "change direction" for m in updated)


async def test_run_interactive_turn_outcomes(tmp_path: Path) -> None:
    class _View(TurnView):
        def __init__(self) -> None:
            super().__init__()
            self.final: list[str] = []

        def show_final(self, answer: str) -> None:
            self.final.append(answer)

    def _make_rt(provider: Any) -> AgentRuntime:
        return AgentRuntime(
            cwd=tmp_path,
            kernel=_FakeKernel(),  # type: ignore[arg-type]
            client=provider,
            provider_name="test",
            model="test-model",
            effort="",
            system_prompt="sys",
            active_session=Session.create(tmp_path),
            messages=[{"role": "user", "content": "hi"}],
            usage_stats=TurnUsage(),
            max_context_tokens=128_000,
            compact_reserve_tokens=16_384,
            keep_recent_tokens=32_000,
            verbose=False,
            plain_output=True,
            headless=True,
        )

    # OK
    rt_ok = _make_rt(_FakeAsyncProvider(streams=[_FakeAsyncStreamHandle([TextDelta("ok")])]))
    outcome_ok = await run_interactive_turn(rt_ok, _View())
    assert outcome_ok.status is TurnStatus.OK
    assert rt_ok.turn_count == 1

    # ABORTED
    rt_abort = _make_rt(_FakeAsyncProvider(streams=[_FakeAsyncStreamHandle([TextDelta("x")], delay_s=0.1)]))
    abort_ev = asyncio.Event()
    abort_ev.set()
    outcome_abort = await run_interactive_turn(rt_abort, _View(), abort_event=abort_ev)
    assert outcome_abort.status is TurnStatus.ABORTED

    # ERROR
    class _BoomProvider:
        async def stream(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("boom")

    rt_err = _make_rt(_BoomProvider())
    outcome_err = await run_interactive_turn(rt_err, _View())
    assert outcome_err.status is TurnStatus.ERROR
    assert isinstance(outcome_err.error, RuntimeError)
    assert len(rt_err.messages) == 1
