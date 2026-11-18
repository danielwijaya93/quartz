"""Tests for the Python REPL kernel, prompt discovery, and compaction helpers."""

from __future__ import annotations

import time
from pathlib import Path

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
    # keep_recent=3 would land on index 4 ("u2"), which is safe
    assert _find_safe_split_index(messages, 3) == 4
    # keep_recent=2 would land on index 5 (assistant tool_calls), so it walks back to index 4 ("u2")
    assert _find_safe_split_index(messages, 2) == 4

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
