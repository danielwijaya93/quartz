"""Reasoning (ThinkingDelta) capture: loop, compaction stripping, session roundtrip."""

from __future__ import annotations

from pathlib import Path

from conftest import RecordingObserver

from quartz.core.compact import compact_messages, strip_reasoning
from quartz.core.loop import TurnUsage, agent_loop
from quartz.core.session import Session
from quartz.kernel.client import KernelClient
from quartz.providers.base import TextDelta, ThinkingDelta, ToolCallDelta, Usage, UsageEvent


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aiter__(self):
        for ev in self._events:
            yield ev

    async def aclose(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeClient:
    def __init__(self, first_events):
        self._first = first_events
        self.call_idx = 0

    async def stream(self, model, messages, tools=None, *, effort=None):
        del model, messages, tools, effort
        self.call_idx += 1
        usage = Usage(input_tokens=10, output_tokens=5)
        if self.call_idx == 1:
            return _FakeStream([*self._first, UsageEvent(usage=usage)])
        return _FakeStream([TextDelta(text="done"), UsageEvent(usage=usage)])


async def test_streaming_thinking_captured_and_callback_fired(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        observer = RecordingObserver()
        client = _FakeClient(
            [
                ThinkingDelta(text="plan..."),
                ThinkingDelta(text=" step 2"),
                ToolCallDelta(index=0, id="tc1", name="ipython", arguments_delta='{"code": "6 * 7"}'),
            ]
        )
        usage = TurnUsage()
        _, msgs, _ = await agent_loop(
            client,  # type: ignore[arg-type]
            "fake-model",
            "sys",
            [{"role": "user", "content": "calc"}],
            kernel,
            stream=True,
            observer=observer,
            usage_stats=usage,
        )
        assistant = next(m for m in msgs if m.get("role") == "assistant")
        assert assistant["reasoning"] == "plan... step 2"
        assert observer.thinking_deltas == ["plan...", " step 2"]
        # Tool result still delivered alongside reasoning
        assert any(m.get("role") == "tool" for m in msgs)
    finally:
        kernel.close()


async def test_no_thinking_deltas_omits_reasoning_key(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        client = _FakeClient(
            [ToolCallDelta(index=0, id="tc1", name="ipython", arguments_delta='{"code": "1"}')],
        )
        _, msgs, _ = await agent_loop(
            client,  # type: ignore[arg-type]
            "fake-model",
            "sys",
            [{"role": "user", "content": "hi"}],
            kernel,
            stream=True,
            usage_stats=TurnUsage(),
        )
        assistant = next(m for m in msgs if m.get("role") == "assistant")
        assert "reasoning" not in assistant
    finally:
        kernel.close()


async def test_empty_thinking_deltas_ignored(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        observer = RecordingObserver()
        client = _FakeClient(
            [
                ThinkingDelta(text=""),
                TextDelta(text="hello"),
            ]
        )
        _, msgs, _ = await agent_loop(
            client,  # type: ignore[arg-type]
            "fake-model",
            "sys",
            [{"role": "user", "content": "hi"}],
            kernel,
            stream=True,
            observer=observer,
            usage_stats=TurnUsage(),
        )
        assert observer.thinking_deltas == []
        assistant = next(m for m in msgs if m.get("role") == "assistant")
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


async def test_compact_messages_strips_reasoning() -> None:
    class _FakeCompactClient:
        async def complete(self, model, messages, tools=None, *, effort=None):
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
    result = await compact_messages(
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

    import quartz.tui.render as ui
    from quartz.tui.render import BlockPrinter
    from quartz.tui.replay import _replay_session

    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)

    # _active_printer reads tui.render.printer; patch it.
    old = ui.printer
    ui.printer = test_printer  # type: ignore[assignment]
    try:
        hist: list = []
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "reasoning": "SHOULD NOT APPEAR IN SCROLLBACK"},
        ]
        _replay_session(msgs, hist)
        out = buf.getvalue()
    finally:
        ui.printer = old  # type: ignore[assignment]
    assert "SHOULD NOT APPEAR IN SCROLLBACK" not in out
    assert "a" in out
