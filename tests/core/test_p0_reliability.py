"""Regression tests for P0 reliability fixes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import RecordingObserver

from quartz.core.commands import cmd_compact
from quartz.core.compact import _serialize_for_summary, clear_old_tool_results
from quartz.core.loop import TurnAborted, TurnUsage, agent_loop
from quartz.core.runtime import AgentRuntime as _CLIRuntime
from quartz.core.session import Session
from quartz.errors import ProviderError
from quartz.kernel.client import KernelClient
from quartz.providers.base import CompletionResult, TextDelta, Usage, UsageEvent
from quartz.providers.registry import resolve_provider_credentials


class _FakeStream:
    def __init__(self, events) -> None:
        self._events = events

    async def __aiter__(self):
        for ev in self._events:
            yield ev

    async def aclose(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        pass


def _make_events(text: str | None = None, prompt_tokens: int = 100) -> _FakeStream:
    events = []
    if text is not None:
        events.append(TextDelta(text=text))
    events.append(UsageEvent(usage=Usage(input_tokens=prompt_tokens, output_tokens=10)))
    return _FakeStream(events)


async def test_running_bash_count_push_does_not_kill_sync_cell(tmp_path: Path) -> None:
    """Repeated running_bash_count() calls must not use IPC or SIGINT-kill a running sync cell."""
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        assert kernel.running_bash_count() == 0

        # Run a 1.2s synchronous sleep in a task while querying running_bash_count repeatedly.
        task = asyncio.create_task(kernel.exec_cell("import time; time.sleep(1.2); 'done'"))
        counts: list[int] = []
        for _ in range(15):
            counts.append(kernel.running_bash_count(timeout=0.05))
            await asyncio.sleep(0.05)
        res = await asyncio.wait_for(task, timeout=10.0)
        assert res.ok, res.stderr
        assert res.result == "'done'"
        assert all(c == 0 for c in counts)

        # Now start a background bash job and verify bg_count push updates start -> 1 -> finish -> 0.
        res_start = await kernel.exec_cell("h = bash('sleep 0.3')")
        assert res_start.ok, res_start.stderr
        deadline = time.monotonic() + 3.0
        saw_one = False
        while time.monotonic() < deadline:
            if kernel.running_bash_count() == 1:
                saw_one = True
                break
            time.sleep(0.02)
        assert saw_one

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if kernel.running_bash_count() == 0:
                break
            time.sleep(0.02)
        assert kernel.running_bash_count() == 0
    finally:
        kernel.close()


async def test_request_timeout_does_not_interrupt_non_exec(tmp_path: Path) -> None:
    """_request() must not send SIGINT when a non-exec request times out."""
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        interrupts: list[bool] = []
        kernel.interrupt = lambda: interrupts.append(True)  # type: ignore[method-assign]
        task = asyncio.create_task(kernel.exec_cell("import time; time.sleep(0.6); 'ok'"))
        await asyncio.sleep(0.1)
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(kernel.list_vars, timeout=0.05)
        res = await asyncio.wait_for(task, timeout=5.0)
        assert res.ok
        assert res.result == "'ok'"
        assert interrupts == []
    finally:
        kernel.close()


async def test_kernel_stderr_over_128kib_does_not_deadlock(tmp_path: Path) -> None:
    """Writing >128 KiB to fd 2 / sys.__stderr__ must be drained without deadlocking."""
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = await kernel.exec_cell(
            "import os\n"
            "payload = (b'X' * 1023 + b'\\n') * 140\n"  # > 140 KiB
            "os.write(2, payload)\n"
            "'wrote'"
        )
        assert res.ok, res.stderr
        assert res.result == "'wrote'"
        time.sleep(0.1)
        assert len(kernel._stderr_tail) > 0
    finally:
        kernel.close()


def test_resolve_provider_credentials_no_openai_fallback_for_deepseek(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "missing_auth.json"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-only")

    ds_key, _, ds_src = resolve_provider_credentials("deepseek")
    assert ds_key is None
    assert ds_src == "none"

    # OPENAI_API_KEY alone no longer authenticates openai: login-only.
    oai_key, _, oai_src = resolve_provider_credentials("openai")
    assert oai_key is None
    assert oai_src == "none"


async def test_agent_loop_raises_provider_error_with_nonempty_steering_queue(tmp_path: Path) -> None:
    """A real ProviderError during stream iteration must not be swallowed when steering_queue is non-empty."""

    class _ErrorStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            await self.aclose()

        async def __aiter__(self):
            yield TextDelta(text="partial ")
            raise ProviderError("upstream failure", status_code=502)

        async def aclose(self) -> None:
            pass

    class _FakeClient:
        async def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            return _ErrorStream()

    client = _FakeClient()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        sq: asyncio.Queue[str] = asyncio.Queue()
        sq.put_nowait("steer me")
        messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
        with pytest.raises(ProviderError, match="upstream failure"):
            await agent_loop(
                client,
                "test-model",
                "sys",
                messages,
                kernel,
                steering_queue=sq,
            )
    finally:
        kernel.close()


async def test_agent_loop_abort_event_closes_stream_immediately(tmp_path: Path) -> None:
    closed = asyncio.Event()
    started = asyncio.Event()

    class _SlowStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            await self.aclose()

        async def __aiter__(self):
            started.set()
            yield TextDelta(text="hello ")
            await asyncio.wait_for(closed.wait(), timeout=5.0)
            raise RuntimeError("stream closed")

        async def aclose(self) -> None:
            closed.set()

    class _FakeClient:
        async def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            return _SlowStream()

    client = _FakeClient()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        abort_ev = asyncio.Event()

        async def _trigger_abort() -> None:
            await asyncio.wait_for(started.wait(), timeout=2.0)
            abort_ev.set()

        trig = asyncio.create_task(_trigger_abort())
        messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
        with pytest.raises(TurnAborted):
            await agent_loop(
                client,
                "test-model",
                "sys",
                messages,
                kernel,
                abort_event=abort_ev,
            )
        await trig
        assert closed.is_set()
        assert len(messages) == 1
    finally:
        kernel.close()


async def test_agent_loop_steering_preemption_discards_partial_text(tmp_path: Path) -> None:
    closed = asyncio.Event()
    started = asyncio.Event()
    calls = 0

    class _PreemptedStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            await self.aclose()

        async def __aiter__(self):
            started.set()
            yield TextDelta(text="truncated half-")
            await asyncio.wait_for(closed.wait(), timeout=5.0)
            raise RuntimeError("stream closed by preempt")

        async def aclose(self) -> None:
            closed.set()

    class _FakeClient:
        async def stream(self, model, messages, tools=None, *, effort=None):
            nonlocal calls
            del model, messages, tools, effort
            calls += 1
            if calls == 1:
                return _PreemptedStream()
            return _make_events("Final steered answer.")

    client = _FakeClient()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        sq: asyncio.Queue[str] = asyncio.Queue()

        async def _push_steering() -> None:
            await asyncio.wait_for(started.wait(), timeout=2.0)
            sq.put_nowait("change direction")

        steer_task = asyncio.create_task(_push_steering())
        messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
        final_text, updated, _ = await agent_loop(
            client,
            "test-model",
            "sys",
            messages,
            kernel,
            steering_queue=sq,
        )
        await steer_task
        assert final_text == "Final steered answer."
        # No truncated half-sentence should remain in messages.
        assert all("truncated half-" not in (m.get("content") or "") for m in updated)
        assert any(m.get("role") == "user" and m.get("content") == "change direction" for m in updated)
    finally:
        kernel.close()


async def test_cmd_compact_resets_prompt_tokens_preventing_double_compact(tmp_path: Path, monkeypatch) -> None:
    class _FakeClient:
        async def complete(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            return CompletionResult(text="Compact summary.", tool_calls=[], usage=Usage())

        async def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            return _make_events("Done.", prompt_tokens=2_000)

    client = _FakeClient()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        session = Session.create(tmp_path, sessions_dir=tmp_path / "sessions")
        messages = []
        for i in range(6):
            messages.append({"role": "user", "content": f"u{i} " + ("x" * 2_000)})
            messages.append({"role": "assistant", "content": f"a{i} " + ("y" * 2_000)})

        rt = _CLIRuntime(
            client=client,
            provider_name="deepseek",
            model="test-model",
            effort="medium",
            cwd=tmp_path,
            system_prompt="sys",
            messages=messages,
            kernel=kernel,
            active_session=session,
            usage_stats=TurnUsage(prompt_tokens=90_000),
            max_context_tokens=20_000,
            compact_reserve_tokens=4_000,
            keep_recent_tokens=2_000,
            verbose=False,
            plain_output=True,
            headless=True,
        )
        await cmd_compact(rt, "")
        assert rt.usage_stats.prompt_tokens < 16_000

        observer = RecordingObserver()
        text, _, _ = await agent_loop(
            client,
            "test-model",
            "sys",
            rt.messages,
            kernel,
            max_context_tokens=20_000,
            compact_reserve_tokens=4_000,
            keep_recent_tokens=2_000,
            initial_prompt_tokens=rt.usage_stats.prompt_tokens,
            observer=observer,
        )
        assert text == "Done."
        assert observer.compactions == []
    finally:
        kernel.close()


def test_session_load_survives_corrupt_line_and_detects_nested_mutation(tmp_path: Path) -> None:
    sdir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sdir)
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": "hi"},
    ]
    s.save_messages(msgs)

    # Append a truncated trailing JSONL line.
    with s.jsonl_path.open("a", encoding="utf-8") as f:
        f.write('{"type": "message", "message": {"role": "user", "con\n')

    with pytest.warns(UserWarning, match="Skipping corrupt JSONL line"):
        loaded, loaded_msgs = Session.load(s.id, sessions_dir=sdir)
    assert loaded_msgs == msgs

    # Mutate nested content list in-place and ensure save_messages detects prefix_changed.
    loaded_msgs[0]["content"][0]["text"] = "mutated"
    loaded.save_messages(loaded_msgs)

    with pytest.warns(UserWarning, match="Skipping corrupt JSONL line"):
        _, reloaded_msgs = Session.load(s.id, sessions_dir=sdir)
    assert reloaded_msgs[0]["content"][0]["text"] == "mutated"


def test_serialize_for_summary_respects_max_total_chars_and_clear_old_tool_results_safe_split() -> None:
    msgs = [{"role": "user", "content": f"msg {i}: " + ("z" * 400)} for i in range(50)]
    serialized = _serialize_for_summary(msgs, max_total_chars=2_500)
    assert len(serialized) <= 2_500
    assert "older messages omitted" in serialized

    # Parallel tool calls in one assistant turn should not be split unevenly.
    conv = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "ipython", "arguments": "{}"}},
                {"id": "t2", "type": "function", "function": {"name": "ipython", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "A" * 1_000},
        {"role": "tool", "tool_call_id": "t2", "content": "B" * 1_000},
    ]
    # Keep budget only covers the second tool result (~250 tokens), so _find_safe_split_index
    # walks back to the assistant message (index 1) and neither tool result is cleared.
    cleared_msgs, cleared_count = clear_old_tool_results(conv, keep_recent_tokens=300)
    assert cleared_count == 0
    assert cleared_msgs == conv


async def test_kernel_crash_restart_surfaces_notice(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        assert (await kernel.exec_cell("x = 1")).ok
        assert kernel._proc is not None
        kernel._proc.kill()
        kernel._proc.wait(timeout=2.0)
        res = await kernel.exec_cell("1 + 1")
        assert res.ok
        assert res.result == "2"
        assert "kernel exited unexpectedly" in res.stderr
        assert kernel.consume_restart_notice() is None
        # A clean second call carries no notice
        assert "unexpectedly" not in (await kernel.exec_cell("2")).stderr
    finally:
        kernel.close()


def test_kernel_close_is_idempotent_and_takes_write_lock(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    proc = kernel._proc
    assert proc is not None
    kernel.close()
    assert proc.poll() is not None
    kernel.close()
    assert not kernel._write_lock.locked()
