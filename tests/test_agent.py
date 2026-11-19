"""Tests for the Python REPL kernel, prompt discovery, and compaction helpers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent.compact import _find_safe_split_index, format_python_state_notice
from agent.kernel_client import KernelClient
from agent.loop import TurnUsage, format_bash_done_events
from agent.prompt import build_system_prompt


def test_kernel_persistence_and_trailing_expr(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r1 = kernel.exec_cell("x = 21 * 2\ndef double(n): return n * 2")
        assert r1.ok
        assert r1.result is None

        r2 = kernel.exec_cell("double(x)")
        assert r2.ok
        assert r2.result == "84"

        vars_info = kernel.list_vars()
        assert vars_info["x"] == "int"
        assert vars_info["double"] == "function"
    finally:
        kernel.close()


def test_kernel_top_level_await(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r = kernel.exec_cell(
            "import asyncio\nawait asyncio.sleep(0.01)\n'done_async'"
        )
        assert r.ok
        assert r.result == "'done_async'"
    finally:
        kernel.close()


def test_kernel_bash_await_and_chdir(tmp_path: Path) -> None:
    sub = tmp_path / "subdir"
    sub.mkdir()
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r1 = kernel.exec_cell(
            f"import os\nos.chdir({str(sub)!r})\nh = bash('pwd')\nres = await h\n(res.exit_code, res.output.strip())"
        )
        assert r1.ok
        assert f"(0, {str(sub)!r})" == r1.result
    finally:
        kernel.close()


def test_kernel_bash_background_event(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r = kernel.exec_cell("bg = bash('sleep 0.15 && echo bg_ok')")
        assert r.ok
        # Wait for the background command to finish after the cell ended
        events = []
        for _ in range(30):
            events = kernel.drain_events()
            if events:
                break
            time.sleep(0.05)

        assert len(events) == 1
        assert events[0]["event"] == "bash_done"
        assert events[0]["exit_code"] == 0
        notice = format_bash_done_events(events)
        assert notice is not None
        assert "[bash-done" in notice
        assert "bg_ok" in notice
    finally:
        kernel.close()


def test_kernel_wait_for_events_and_running_count(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r = kernel.exec_cell("bg = bash('sleep 0.25 && echo wake_test')")
        assert r.ok
        assert kernel.running_bash_count() == 1
        events = kernel.wait_for_events(timeout=3.0)
        assert len(events) == 1
        assert events[0]["event"] == "bash_done"
        assert "wake_test" in events[0]["tail"]
        assert kernel.running_bash_count() == 0
    finally:
        kernel.close()


def test_kernel_bash_timeout(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r = kernel.exec_cell("res = await bash('sleep 10', timeout=0.2)\nres.timed_out")
        assert r.ok
        assert r.result == "True"
    finally:
        kernel.close()


def test_prompt_discovery(tmp_path: Path) -> None:
    global_agents = tmp_path / "global_AGENTS.md"
    global_agents.write_text("Global rule: be concise.", encoding="utf-8")

    (tmp_path / "AGENTS.md").write_text("Always use pytest.", encoding="utf-8")
    # Project-local .agents/skills should NOT be scanned; only explicit/global ~/.agents/skills
    ignored_skill_dir = tmp_path / ".agents" / "skills" / "ignored-skill"
    ignored_skill_dir.mkdir(parents=True)
    (ignored_skill_dir / "SKILL.md").write_text(
        "---\nname: ignored-skill\ndescription: Should not be loaded\n---\n",
        encoding="utf-8",
    )

    skills_root = tmp_path / "global_skills"
    skill_dir = skills_root / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A demo skill for testing\n---\n\nDetails here.\n",
        encoding="utf-8",
    )

    prompt = build_system_prompt(tmp_path, skills_dir=skills_root, global_agents_path=global_agents)
    assert "You have one tool, `ipython`" in prompt
    assert "Global rule: be concise." in prompt
    assert "Always use pytest." in prompt
    assert prompt.index("Global rule: be concise.") < prompt.index("Always use pytest.")
    assert "`demo-skill`" in prompt
    assert "A demo skill for testing" in prompt
    assert "ignored-skill" not in prompt
    assert "Details here." not in prompt


def test_compaction_safe_split_and_python_state() -> None:
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "r1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
        {"role": "tool", "tool_call_id": "2", "content": "r2"},
    ]
    # A small keep budget cuts inside the last tool loop; the split walks back to
    # the assistant owning the tool call so the pair is never orphaned.
    split = _find_safe_split_index(messages, 3)
    assert split == 5
    assert messages[split]["role"] == "assistant"
    assert messages[split]["tool_calls"][0]["id"] == "2"
    assert messages[split + 1]["role"] == "tool"

    # A budget larger than the whole conversation leaves everything intact.
    assert _find_safe_split_index(messages, 10_000) == 0

    notice = format_python_state_notice({"df": "DataFrame", "helper": "function"})
    assert "[python-state]" in notice
    assert "df (DataFrame)" in notice
    assert "helper (function)" in notice


def test_turn_usage_telemetry() -> None:
    class _Details:
        cached_tokens = 750

    class _Usage:
        prompt_tokens = 1000
        completion_tokens = 200
        prompt_tokens_details = _Details()

    stats = TurnUsage()
    stats.update(_Usage())
    assert stats.prompt_tokens == 1000
    assert stats.cached_tokens == 750
    assert stats.cache_hit_rate == 0.75
    assert round(stats.context_pct(10_000), 1) == 10.0

    # Provider without prompt_tokens_details
    class _BasicUsage:
        prompt_tokens = 500
        completion_tokens = 50

    stats.update(_BasicUsage())
    assert stats.total_prompt_tokens == 1500
    assert stats.total_cached_tokens == 750
    assert stats.cache_hit_rate == 0.0


def test_agent_loop_abort_event(tmp_path: Path) -> None:
    import pytest
    import queue
    import threading
    from types import SimpleNamespace
    from agent.loop import TurnAborted, agent_loop

    abort_event = threading.Event()
    abort_event.set()

    class _DummyCompletions:
        def create(self, **kwargs: object) -> object:
            raise AssertionError("Should not call API when already aborted")

    dummy_client = SimpleNamespace(chat=SimpleNamespace(completions=_DummyCompletions()))
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        messages = [{"role": "user", "content": "hi"}]
        with pytest.raises(TurnAborted):
            agent_loop(
                dummy_client,  # type: ignore[arg-type]
                "m",
                "sys",
                messages,
                kernel,
                stream=False,
                abort_event=abort_event,
                steering_queue=queue.Queue(),
            )
    finally:
        kernel.close()


def test_agent_loop_steering_queue(tmp_path: Path) -> None:
    import pytest
    import queue
    from types import SimpleNamespace
    from agent.loop import agent_loop

    steering_q: queue.Queue[str] = queue.Queue()
    seen_steering: list[str] = []
    calls = [0]

    class _DummyCompletions:
        def create(self, **kwargs: object) -> object:
            calls[0] += 1
            if calls[0] == 1:
                # Queue a steering message mid-turn before returning first answer
                steering_q.put("Actually check bar too")
                msg = SimpleNamespace(content="First step done", tool_calls=None)
            else:
                msg = SimpleNamespace(content="Checked bar as well", tool_calls=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None),
            )

    dummy_client = SimpleNamespace(chat=SimpleNamespace(completions=_DummyCompletions()))
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        messages = [{"role": "user", "content": "check foo"}]
        answer, updated, _ = agent_loop(
            dummy_client,  # type: ignore[arg-type]
            "m",
            "sys",
            messages,
            kernel,
            stream=False,
            steering_queue=steering_q,
            on_steering=seen_steering.append,
        )
        assert answer == "Checked bar as well"
        assert calls[0] == 2
        assert seen_steering == ["Actually check bar too"]
        assert any(m.get("role") == "user" and m.get("content") == "Actually check bar too" for m in updated)
    finally:
        kernel.close()


def test_kernel_close_cleans_up_child_processes(tmp_path: Path) -> None:
    import os
    import time

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    res = kernel.exec_cell("h = bash('sleep 60'); h.pid")
    assert res.ok and res.result
    child_pid = int(res.result)
    kernel.close()

    # Give OS a moment to reap the terminated child
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"Child process {child_pid} survived kernel.close()")


def test_multiline_skill_frontmatter_discovery(tmp_path: Path) -> None:
    from agent.prompt import discover_skills

    skills_dir = tmp_path / "skills"
    sdir = skills_dir / "demo-skill"
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: >\n"
        "  First line of description\n"
        "  and second line.\n"
        "---\n"
        "\nBody\n",
        encoding="utf-8",
    )
    found = discover_skills(skills_dir=skills_dir)
    assert len(found) == 1
    assert found[0][0] == "demo-skill"
    assert found[0][1] == "First line of description and second line."


def test_compact_messages_summarizes_and_keeps_kernel(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from agent.compact import CompactionReason, compact_messages

    dummy_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Summary of earlier work."))]
                )
            )
        )
    )
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.exec_cell("answer = 42")
        msgs = [
            {"role": "user", "content": "msg 1"},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "msg 2"},
            {"role": "assistant", "content": "reply 2"},
            {"role": "user", "content": "msg 3"},
        ]
        result = compact_messages(
            dummy_client, "m", msgs, kernel, keep_recent_tokens=16  # type: ignore[arg-type]
        )
        assert result.summarized == 2
        assert result.kept == 3
        assert result.reason is CompactionReason.SUMMARIZED
        assert "[compaction-summary]" in result.messages[0]["content"]
        assert "Summary of earlier work." in result.messages[0]["content"]
        assert "answer (int)" in result.messages[0]["content"]
    finally:
        kernel.close()


def test_bash_handle_pruned_and_output_preserved(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell("h = bash('echo hello_world')\nr = await h\n(r.output.strip(), h.output().strip(), h.tail(1).strip())")
        assert res.ok
        assert res.result == "('hello_world', 'hello_world', 'hello_world')"
        assert kernel.running_bash_count() == 0
    finally:
        kernel.close()


def test_kernel_wait_for_events_returns_fast_on_exit(tmp_path: Path) -> None:
    import time

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    assert kernel._proc is not None
    kernel._proc.kill()
    kernel._proc.wait(timeout=2.0)
    t0 = time.monotonic()
    events = kernel.wait_for_events(timeout=30.0)
    elapsed = time.monotonic() - t0
    assert events == []
    assert elapsed < 2.0
    kernel.close()


def test_kernel_spill_large_output(tmp_path: Path) -> None:
    import re

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    spill_file: Path | None = None
    try:
        res = kernel.exec_cell("print('A' * 140_000, end='')")
        assert res.ok
        assert "chars truncated" in res.stdout
        m = re.search(r"saved to ([^;]+); use read\(\)/rg\(\) to inspect", res.stdout)
        assert m is not None
        spill_file = Path(m.group(1))
        assert spill_file.is_file()
        assert len(spill_file.read_text(encoding="utf-8")) == 140_000
    finally:
        if spill_file is not None and spill_file.exists():
            spill_file.unlink()
        kernel.close()


def test_mcp_proxy_delegates_to_call_tool() -> None:
    import asyncio
    from agent.mcp_client import MCPManager

    mgr = MCPManager()
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    async def fake_call_tool(
        server: str, tool: str, arguments: dict[str, object] | None = None
    ) -> str:
        calls.append((server, tool, arguments))
        return "ok"

    mgr.call_tool = fake_call_tool  # type: ignore[method-assign]

    async def _run() -> None:
        r1 = await mgr.github.search_issues(q="bug", limit=5)
        r2 = await mgr["my-server"]["my-tool"](x=1)
        assert r1 == "ok"
        assert r2 == "ok"

    asyncio.run(_run())
    assert calls == [
        ("github", "search_issues", {"q": "bug", "limit": 5}),
        ("my-server", "my-tool", {"x": 1}),
    ]


def test_kernel_interrupt_sync_loop(tmp_path: Path) -> None:
    import threading
    from agent.kernel_client import CellResult

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    holder: list[CellResult] = []
    try:
        t = threading.Thread(
            target=lambda: holder.append(kernel.exec_cell("while True: pass", timeout=10.0))
        )
        t.start()
        time.sleep(0.2)
        kernel.interrupt()
        t.join(timeout=3.0)
        assert not t.is_alive()
        assert len(holder) == 1
        assert not holder[0].ok
        assert "KeyboardInterrupt" in holder[0].stderr

        # Kernel remains alive and responsive after interrupting a sync loop
        r2 = kernel.exec_cell("1 + 1")
        assert r2.ok
        assert r2.result == "2"
    finally:
        kernel.close()


def test_kernel_edit_returns_unified_diff(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("def greet():\n    return 'hi'\n", encoding="utf-8")

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell(
            f"edit({str(target)!r}, \"return 'hi'\", \"return 'hello'\")"
        )
        assert res.ok
        assert res.result is not None
        assert "Updated" in res.result
        assert "-    return 'hi'" in res.result
        assert "+    return 'hello'" in res.result
    finally:
        kernel.close()


def test_agent_loop_abort_during_parallel_ipython(tmp_path: Path) -> None:
    import threading
    from types import SimpleNamespace
    from agent.loop import TurnAborted, agent_loop

    class _ParallelAbortClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            del kwargs
            tc1 = SimpleNamespace(
                id="tc_1",
                function=SimpleNamespace(name="ipython", arguments='{"code": "import asyncio; await asyncio.sleep(5)"}'),
            )
            tc2 = SimpleNamespace(
                id="tc_2",
                function=SimpleNamespace(name="ipython", arguments='{"code": "import asyncio; await asyncio.sleep(5)"}'),
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tc1, tc2]))],
                usage=None,
            )

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    abort_event = threading.Event()
    messages: list[dict] = [{"role": "user", "content": "run two"}]
    try:
        timer = threading.Timer(0.25, lambda: (abort_event.set(), kernel.interrupt()))
        timer.start()
        try:
            with pytest.raises(TurnAborted):
                agent_loop(
                    _ParallelAbortClient(),  # type: ignore[arg-type]
                    "test-model",
                    "sys",
                    messages,
                    kernel,
                    stream=False,
                    abort_event=abort_event,
                )
        finally:
            timer.cancel()

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["tc_1", "tc_2"]
        assert all("KeyboardInterrupt" in m["content"] or m["content"] == "[aborted by user]" for m in tool_msgs)
    finally:
        kernel.close()


def test_kernel_snapshot_rejects_empty_dir() -> None:
    from agent.kernel_worker import KernelWorker

    worker = KernelWorker()
    with pytest.raises(ValueError, match="snapshot_dir"):
        worker.snapshot_vars("")


def test_agent_loop_streaming_steering_preemption(tmp_path: Path) -> None:
    import queue
    from types import SimpleNamespace
    from agent.loop import agent_loop

    steering_q: queue.Queue[str] = queue.Queue()
    seen_steering: list[str] = []
    calls = [0]
    chunks_yielded = [0]

    class _DummyChunkChoice:
        def __init__(self, text: str) -> None:
            self.delta = SimpleNamespace(content=text, tool_calls=None)

    class _DummyChunk:
        def __init__(self, text: str) -> None:
            self.choices = [_DummyChunkChoice(text)]
            self.usage = None

    class _DummyCompletions:
        def create(self, **kwargs: object) -> object:
            calls[0] += 1
            if calls[0] == 1:
                def _gen():
                    for i in range(10):
                        chunks_yielded[0] += 1
                        if i == 1:
                            steering_q.put("Stop and do Y instead")
                        yield _DummyChunk(f"Chunk {i} ")
                return _gen()
            else:
                def _gen2():
                    yield _DummyChunk("Handled Y instead")
                return _gen2()

    dummy_client = SimpleNamespace(chat=SimpleNamespace(completions=_DummyCompletions()))
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        messages = [{"role": "user", "content": "do X"}]
        answer, updated, _ = agent_loop(
            dummy_client,  # type: ignore[arg-type]
            "m",
            "sys",
            messages,
            kernel,
            stream=True,
            steering_queue=steering_q,
            on_steering=seen_steering.append,
        )
        assert answer == "Handled Y instead"
        assert calls[0] == 2
        assert seen_steering == ["Stop and do Y instead"]
        assert chunks_yielded[0] < 10
        assert any(m.get("role") == "user" and m.get("content") == "Stop and do Y instead" for m in updated)
    finally:
        kernel.close()
