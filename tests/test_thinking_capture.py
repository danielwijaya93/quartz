"""Reasoning (ThinkingDelta) capture: loop, compaction stripping, session roundtrip."""

from __future__ import annotations

from pathlib import Path

import pytest

from quartz.core.compact import compact_messages, strip_reasoning
from quartz.kernel.client import KernelClient
from quartz.core.loop import TurnUsage, agent_loop
from quartz.providers.base import TextDelta, ThinkingDelta, ToolCallDelta, Usage, UsageEvent
from quartz.core.session import Session


class _FakeStream:
    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeClient:
    def __init__(self, first_events):
        self._first = first_events
        self.call_idx = 0

    def stream(self, model, messages, tools=None, *, effort=None):
        del model, messages, tools, effort
        self.call_idx += 1
        usage = Usage(input_tokens=10, output_tokens=5)
        if self.call_idx == 1:
            return _FakeStream([*self._first, UsageEvent(usage=usage)])
        return _FakeStream([TextDelta(text="done"), UsageEvent(usage=usage)])


def test_streaming_thinking_captured_and_callback_fired(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        deltas: list[str] = []
        client = _FakeClient(
            [
                ThinkingDelta(text="plan..."),
                ThinkingDelta(text=" step 2"),
                ToolCallDelta(index=0, id="tc1", name="ipython", arguments_delta='{"code": "6 * 7"}'),
            ]
        )
        usage = TurnUsage()
        _, msgs, _ = agent_loop(
            client,  # type: ignore[arg-type]
            "fake-model",
            "sys",
            [{"role": "user", "content": "calc"}],
            kernel,
            stream=True,
            on_thinking_delta=deltas.append,
            usage_stats=usage,
        )
        assistant = [m for m in msgs if m.get("role") == "assistant"][0]
        assert assistant["reasoning"] == "plan... step 2"
        assert deltas == ["plan...", " step 2"]
        # Tool result still delivered alongside reasoning
        assert any(m.get("role") == "tool" for m in msgs)
    finally:
        kernel.close()


def test_no_thinking_deltas_omits_reasoning_key(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        client = _FakeClient(
            [ToolCallDelta(index=0, id="tc1", name="ipython", arguments_delta='{"code": "1"}')],
        )
        _, msgs, _ = agent_loop(
            client,  # type: ignore[arg-type]
            "fake-model",
            "sys",
            [{"role": "user", "content": "hi"}],
            kernel,
            stream=True,
            usage_stats=TurnUsage(),
        )
        assistant = [m for m in msgs if m.get("role") == "assistant"][0]
        assert "reasoning" not in assistant
    finally:
        kernel.close()


def test_empty_thinking_deltas_ignored(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        deltas: list[str] = []
        client = _FakeClient(
            [
                ThinkingDelta(text=""),
                TextDelta(text="hello"),
            ]
        )
        _, msgs, _ = agent_loop(
            client,  # type: ignore[arg-type]
            "fake-model",
            "sys",
            [{"role": "user", "content": "hi"}],
            kernel,
            stream=True,
            on_thinking_delta=deltas.append,
            usage_stats=TurnUsage(),
        )
        assert deltas == []
        assistant = [m for m in msgs if m.get("role") == "assistant"][0]
        assert "reasoning" not in assistant
        assert assistant["content"] == "hello"
    finally:
        kernel.close()


def test_strip_reasoning_helper() -> None:
    msgs = [
        {"role": "assistant", "content": "a", "reasoning": "secret plan"},
        {"role": "user", "content": "hi"},
    ]
    out = strip_reasoning(msgs)
    assert all("reasoning" not in m for m in out)
    assert out[0]["content"] == "a"
    # Original untouched
    assert msgs[0]["reasoning"] == "secret plan"
    # Identity fast-path when nothing to strip
    clean = [{"role": "user", "content": "x"}]
    assert strip_reasoning(clean) is clean


def test_compact_messages_strips_reasoning() -> None:
    class _FakeCompactClient:
        def complete(self, model, messages, tools=None, *, effort=None):
            del model, tools, effort
            assert all(
                "reasoning" not in m for m in messages
            ), "reasoning must not be sent to summarizer"
            from quartz.providers.base import CompletionResult

            return CompletionResult(text="summary")

    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1", "reasoning": "thinking one"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2", "reasoning": "thinking two"},
    ]
    # Force nothing-to-do path (tiny history, generous keep window): output must already be stripped.
    result = compact_messages(
        _FakeCompactClient(),  # type: ignore[arg-type]
        "m",
        msgs,
        kernel=None,  # type: ignore[arg-type]
        budget_tokens=10**9,
        keep_recent_tokens=10**9,
    )
    assert all("reasoning" not in m for m in result.messages)


def test_session_roundtrips_reasoning(tmp_path: Path) -> None:
    session = Session.create(tmp_path, sessions_dir=tmp_path / "sessions")
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "reasoning": "plan..."},
    ]
    session.save_messages(msgs)
    loaded, restored = Session.load(session.id, sessions_dir=tmp_path / "sessions")
    assert restored[1]["reasoning"] == "plan..."
    assert restored[1]["content"] == "hello"
    del loaded


def test_replay_ignores_reasoning_without_crashing() -> None:
    import io

    from rich.console import Console

    import quartz.cli.app as cli
    from quartz.ui import BlockPrinter, _replay_session

    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    import quartz.ui as ui

    # _active_printer resolves via agent.cli.printer; patch it.
    old = cli.printer
    cli.printer = test_printer  # type: ignore[assignment]
    try:
        hist: list = []
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "reasoning": "SHOULD NOT APPEAR IN SCROLLBACK"},
        ]
        _replay_session(msgs, hist)
        out = buf.getvalue()
    finally:
        cli.printer = old  # type: ignore[assignment]
    assert "SHOULD NOT APPEAR IN SCROLLBACK" not in out
    assert "a" in out
