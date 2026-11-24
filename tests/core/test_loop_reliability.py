"""Reliability tests for agent_loop steering preemption, compaction, and token accounting."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import RecordingObserver

from quartz.core.loop import TurnAborted, agent_loop
from quartz.errors import ContextOverflowError
from quartz.kernel.client import KernelClient
from quartz.providers.base import CompletionResult, TextDelta, Usage, UsageEvent


class _FakeStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aiter__(self):
        for ev in self._events:
            yield ev

    async def aclose(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


def _make_events(content: str | None = None, prompt_tokens: int = 0) -> _FakeStream:
    events: list[Any] = []
    if content is not None:
        events.append(TextDelta(text=content))
    if prompt_tokens:
        events.append(UsageEvent(usage=Usage(input_tokens=prompt_tokens, output_tokens=10)))
    return _FakeStream(events)


async def test_steering_preemption_swallows_close_error_and_omits_empty_assistant(tmp_path: Path) -> None:
    steering_q: asyncio.Queue[str] = asyncio.Queue()
    stream_started = asyncio.Event()
    stream_closed = asyncio.Event()
    calls = 0

    class _BlockingStream:
        def __init__(self) -> None:
            self._done = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            await self.aclose()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            stream_started.set()
            await asyncio.wait_for(stream_closed.wait(), timeout=3.0)
            self._done = True
            raise OSError("socket closed by preemption")

        async def aclose(self) -> None:
            stream_closed.set()

    class _FakeClient:
        async def stream(self, model, messages, tools=None, *, effort=None):
            nonlocal calls
            del model, messages, tools, effort
            calls += 1
            if calls == 1:
                return _BlockingStream()
            return _make_events("Done B", prompt_tokens=50)

    client = _FakeClient()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        async def _inject_steering() -> None:
            await asyncio.wait_for(stream_started.wait(), timeout=2.0)
            steering_q.put_nowait("please stop and do B")

        steer_task = asyncio.create_task(_inject_steering())

        messages = [{"role": "user", "content": "start A"}]
        text, updated, _ = await agent_loop(
            client,  # type: ignore[arg-type]
            "test-model",
            "sys",
            messages,
            kernel,
            steering_queue=steering_q,
        )
        await steer_task

        assert text == "Done B"
        assert updated == [
            {"role": "user", "content": "start A"},
            {"role": "user", "content": "please stop and do B"},
            {"role": "assistant", "content": "Done B"},
        ]
    finally:
        kernel.close()


async def test_initial_prompt_tokens_does_not_trigger_premature_compaction(tmp_path: Path) -> None:
    observer = RecordingObserver()

    class _FakeClient:
        async def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            return _make_events("ok", prompt_tokens=60_100)

    client = _FakeClient()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        # ~60k tokens of existing history (~240k chars) + 1 new short user message
        messages = [
            {"role": "user", "content": "x" * 120_000},
            {"role": "assistant", "content": "y" * 120_000},
            {"role": "user", "content": "next question"},
        ]
        text, _, _ = await agent_loop(
            client,  # type: ignore[arg-type]
            "test-model",
            "sys",
            messages,
            kernel,
            max_context_tokens=128_000,
            compact_reserve_tokens=16_384,
            initial_prompt_tokens=60_000,
            observer=observer,
        )
        assert text == "ok"
        assert observer.compactions == []
    finally:
        kernel.close()


async def test_overflow_compaction_does_not_double_compact(tmp_path: Path) -> None:
    observer = RecordingObserver()
    calls = 0

    class _FakeClient:
        async def complete(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            return CompletionResult(text="Short summary.", tool_calls=[], usage=Usage())

        async def stream(self, model, messages, tools=None, *, effort=None):
            nonlocal calls
            del model, messages, tools, effort
            calls += 1
            if calls == 1:
                raise ContextOverflowError("prompt too long")
            return _make_events("Recovered.", prompt_tokens=5_000)

    client = _FakeClient()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        # 12 messages totaling ~12k tokens (~48k chars) -> overflow compacts to ~4-6k tokens.
        # With compact_threshold = 20_000 - 4_000 = 16_000, double-counting would exceed 16k
        # and trigger a second compaction if measured_message_count were reset to 0.
        messages = []
        for i in range(6):
            messages.append({"role": "user", "content": f"u{i} " + ("a" * 4_000)})
            messages.append({"role": "assistant", "content": f"a{i} " + ("b" * 4_000)})

        text, _, _ = await agent_loop(
            client,  # type: ignore[arg-type]
            "test-model",
            "sys",
            messages,
            kernel,
            max_context_tokens=20_000,
            compact_reserve_tokens=4_000,
            keep_recent_tokens=8_000,
            observer=observer,
        )
        assert text == "Recovered."
        assert len(observer.compactions) == 1
    finally:
        kernel.close()


async def test_compaction_mutates_messages_in_place_before_turn_aborted(tmp_path: Path) -> None:
    abort_event = asyncio.Event()
    observer = RecordingObserver()

    class _FakeClient:
        async def complete(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            return CompletionResult(text="Compacted summary.", tool_calls=[], usage=Usage())

        async def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            abort_event.set()
            return _make_events("partial")

    client = _FakeClient()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        messages = []
        for i in range(10):
            messages.append({"role": "user", "content": f"u{i} " + ("a" * 4_000)})
            messages.append({"role": "assistant", "content": f"a{i} " + ("b" * 4_000)})
        original_len = len(messages)

        with pytest.raises(TurnAborted):
            await agent_loop(
                client,  # type: ignore[arg-type]
                "test-model",
                "sys",
                messages,
                kernel,
                max_context_tokens=16_000,
                compact_reserve_tokens=4_000,
                keep_recent_tokens=4_000,
                observer=observer,
                abort_event=abort_event,
            )

        assert len(observer.compactions) == 1
        assert len(messages) < original_len
    finally:
        kernel.close()


async def test_agent_loop_rolls_back_assistant_tool_call_on_exec_error() -> None:
    from quartz.providers.base import ToolCall

    class _FailingKernel:
        async def exec_cell(self, code: str, timeout: float | None = 300.0):
            del code, timeout
            raise RuntimeError("Python kernel failed to start within 10.0s.")

        def drain_events(self) -> list[dict]:
            return []

        def interrupt(self) -> None:
            pass

    class _Provider:
        async def complete(self, **kwargs):
            del kwargs
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id="tc_1", name="ipython", arguments='{"code": "1+1"}')],
                usage=Usage(input_tokens=1, output_tokens=1),
            )

    messages: list[dict] = [{"role": "user", "content": "Run 1+1"}]
    with pytest.raises(RuntimeError, match="Python kernel failed to start"):
        await agent_loop(_Provider(), "test-model", "sys", messages, _FailingKernel(), stream=False)  # type: ignore[arg-type]

    assert messages == [{"role": "user", "content": "Run 1+1"}]
