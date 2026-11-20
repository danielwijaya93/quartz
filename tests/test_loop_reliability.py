"""Reliability tests for agent_loop steering preemption, compaction, and token accounting."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.errors import ContextOverflowError
from agent.kernel_client import KernelClient
from agent.loop import TurnAborted, agent_loop


def _make_chunk(content: str | None = None, prompt_tokens: int = 0):
    delta = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(delta=delta)
    usage = (
        SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=10, prompt_tokens_details=None)
        if prompt_tokens
        else None
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def test_steering_preemption_swallows_close_error_and_omits_empty_assistant(tmp_path: Path) -> None:
    steering_q: queue.Queue[str] = queue.Queue()
    stream_started = threading.Event()
    stream_closed = threading.Event()
    calls = 0

    class _BlockingStream:
        def __init__(self) -> None:
            self._done = False

        def __iter__(self):
            return self

        def __next__(self):
            if self._done:
                raise StopIteration
            stream_started.set()
            stream_closed.wait(timeout=3.0)
            self._done = True
            raise OSError("socket closed by preemption")

        def close(self) -> None:
            stream_closed.set()

    class _FakeCompletions:
        def create(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _BlockingStream()
            return iter([_make_chunk("Done B", prompt_tokens=50)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        def _inject_steering() -> None:
            assert stream_started.wait(timeout=2.0)
            steering_q.put("please stop and do B")

        t = threading.Thread(target=_inject_steering, daemon=True)
        t.start()

        messages = [{"role": "user", "content": "start A"}]
        text, updated, _ = agent_loop(
            client,
            "test-model",
            "sys",
            messages,
            kernel,
            steering_queue=steering_q,
        )
        t.join(timeout=2.0)

        assert text == "Done B"
        assert updated == [
            {"role": "user", "content": "start A"},
            {"role": "user", "content": "please stop and do B"},
            {"role": "assistant", "content": "Done B"},
        ]
    finally:
        kernel.close()


def test_initial_prompt_tokens_does_not_trigger_premature_compaction(tmp_path: Path) -> None:
    compactions = []

    class _FakeCompletions:
        def create(self, **kwargs):
            return iter([_make_chunk("ok", prompt_tokens=60_100)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        # ~60k tokens of existing history (~240k chars) + 1 new short user message
        messages = [
            {"role": "user", "content": "x" * 120_000},
            {"role": "assistant", "content": "y" * 120_000},
            {"role": "user", "content": "next question"},
        ]
        text, _, _ = agent_loop(
            client,
            "test-model",
            "sys",
            messages,
            kernel,
            max_context_tokens=128_000,
            compact_reserve_tokens=16_384,
            initial_prompt_tokens=60_000,
            on_compaction=compactions.append,
        )
        assert text == "ok"
        assert compactions == []
    finally:
        kernel.close()


def test_overflow_compaction_does_not_double_compact(tmp_path: Path) -> None:
    compactions = []
    calls = 0

    class _FakeCompletions:
        def create(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ContextOverflowError("prompt too long")
            if not kwargs.get("stream"):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Short summary."))]
                )
            return iter([_make_chunk("Recovered.", prompt_tokens=5_000)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
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

        text, _, _ = agent_loop(
            client,
            "test-model",
            "sys",
            messages,
            kernel,
            max_context_tokens=20_000,
            compact_reserve_tokens=4_000,
            keep_recent_tokens=8_000,
            on_compaction=compactions.append,
        )
        assert text == "Recovered."
        assert len(compactions) == 1
    finally:
        kernel.close()


def test_compaction_mutates_messages_in_place_before_turn_aborted(tmp_path: Path) -> None:
    abort_event = threading.Event()
    compactions = []

    class _FakeCompletions:
        def create(self, **kwargs):
            if not kwargs.get("stream"):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Compacted summary."))]
                )
            abort_event.set()
            return iter([_make_chunk("partial")])

    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        messages = []
        for i in range(10):
            messages.append({"role": "user", "content": f"u{i} " + ("a" * 4_000)})
            messages.append({"role": "assistant", "content": f"a{i} " + ("b" * 4_000)})
        original_len = len(messages)

        with pytest.raises(TurnAborted):
            agent_loop(
                client,
                "test-model",
                "sys",
                messages,
                kernel,
                max_context_tokens=16_000,
                compact_reserve_tokens=4_000,
                keep_recent_tokens=4_000,
                on_compaction=compactions.append,
                abort_event=abort_event,
            )

        assert len(compactions) == 1
        assert len(messages) < original_len
    finally:
        kernel.close()
