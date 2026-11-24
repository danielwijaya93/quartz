"""The TurnObserver port: no-op default, subclassing, ordering, and call-site shape."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import Any

from conftest import RecordingObserver

from quartz.core import loop as loop_mod
from quartz.core.compact import CompactionReason, CompactionResult
from quartz.core.events import NO_OBSERVER, TurnObserver
from quartz.core.loop import TurnUsage, agent_loop
from quartz.kernel.client import CellResult
from quartz.providers.base import TextDelta, ToolCallDelta, Usage, UsageEvent

HOOKS = (
    "on_status",
    "on_text_delta",
    "on_thinking_delta",
    "on_tool_call",
    "on_tool_result",
    "on_compaction",
    "on_bash_notice",
    "on_steering",
)


class _ScriptedStream:
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


class _ScriptedClient:
    """Streams a text chunk plus one ``ipython`` call, then a final text answer."""

    def __init__(self) -> None:
        self.call_count = 0

    async def stream(self, model, messages, tools=None, *, effort=None):
        del model, messages, tools, effort
        self.call_count += 1
        usage = Usage(input_tokens=120, output_tokens=30)
        if self.call_count == 1:
            return _ScriptedStream(
                [
                    TextDelta(text="Checking..."),
                    ToolCallDelta(index=0, id="tc-1", name="ipython", arguments_delta='{"code": "6 * 7"}'),
                    UsageEvent(usage=usage),
                ]
            )
        return _ScriptedStream([TextDelta(text="The answer is 42."), UsageEvent(usage=usage)])


class _StubKernel:
    """Stands in for KernelClient: no background events, one canned cell result."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def drain_events(self) -> list[dict[str, Any]]:
        return []

    async def exec_cell(self, code: str) -> CellResult:
        self.executed.append(code)
        return CellResult(ok=True, stdout="", stderr="", result="42")

    def interrupt(self) -> None:
        pass


async def _run_scripted_turn(**loop_kwargs: Any) -> tuple[str, list[dict[str, Any]], int, TurnUsage]:
    kernel = _StubKernel()
    usage = TurnUsage()
    answer, messages, tokens = await agent_loop(
        _ScriptedClient(),  # type: ignore[arg-type]
        "fake-model",
        "sys",
        [{"role": "user", "content": "6 * 7?"}],
        kernel,  # type: ignore[arg-type]
        usage_stats=usage,
        **loop_kwargs,
    )
    return answer, messages, tokens, usage


def test_agent_loop_defaults_to_the_shared_no_op_observer() -> None:
    param = inspect.signature(agent_loop).parameters["observer"]
    assert param.default is NO_OBSERVER
    assert isinstance(param.default, TurnObserver)


async def test_default_observer_is_a_no_op_and_the_turn_is_unchanged() -> None:
    answer_default, messages_default, tokens_default, usage_default = await _run_scripted_turn()
    answer_bare, messages_bare, tokens_bare, usage_bare = await _run_scripted_turn(observer=TurnObserver())

    assert (answer_bare, messages_bare, tokens_bare) == (answer_default, messages_default, tokens_default)
    assert usage_bare.to_dict() == usage_default.to_dict()

    # Pinned so "unchanged" is measured against a transcript, not just itself.
    assert answer_default == "The answer is 42."
    assert tokens_default == 120
    assert messages_default == [
        {"role": "user", "content": "6 * 7?"},
        {
            "role": "assistant",
            "content": "Checking...",
            "tool_calls": [
                {
                    "id": "tc-1",
                    "type": "function",
                    "function": {"name": "ipython", "arguments": '{"code": "6 * 7"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc-1", "content": "42"},
        {"role": "assistant", "content": "The answer is 42."},
    ]
    assert usage_default.total_prompt_tokens == 240


def test_base_observer_hooks_are_all_no_ops() -> None:
    observer = TurnObserver()
    compaction = CompactionResult(messages=[], reason=CompactionReason.NOTHING_TO_DO)
    cell = CellResult(ok=True, stdout="", stderr="", result="2")

    assert observer.on_status("Thinking...") is None
    assert observer.on_status(None) is None
    assert observer.on_text_delta("hi") is None
    assert observer.on_thinking_delta("hmm") is None
    assert observer.on_tool_call("tc-1", "1 + 1") is None
    assert observer.on_tool_result("tc-1", cell) is None
    assert observer.on_compaction(compaction) is None
    assert observer.on_bash_notice("[bash-done pid:1 exit:0 duration:0.1s]") is None
    assert observer.on_steering("go left") is None


class _TextOnlyObserver(TurnObserver):
    """Overrides a single hook; everything else stays the inherited no-op."""

    def __init__(self) -> None:
        self.deltas: list[str] = []

    def on_text_delta(self, text: str) -> None:
        self.deltas.append(text)


async def test_subclass_overriding_only_text_delta_receives_deltas_and_ignores_the_rest() -> None:
    observer = _TextOnlyObserver()
    answer, _, _, _ = await _run_scripted_turn(observer=observer)

    assert observer.deltas == ["Checking...", "The answer is 42."]
    assert answer == "The answer is 42."
    # The other seven events are inherited no-ops: calling them cannot raise.
    for hook in HOOKS:
        if hook == "on_text_delta":
            assert getattr(_TextOnlyObserver, hook) is not getattr(TurnObserver, hook)
        else:
            assert getattr(_TextOnlyObserver, hook) is getattr(TurnObserver, hook)

    assert observer.on_status("Thinking...") is None
    assert observer.on_thinking_delta("hmm") is None
    assert observer.on_tool_call("tc-1", "1 + 1") is None
    assert observer.on_tool_result("tc-1", CellResult(ok=True, stdout="", stderr="", result="2")) is None
    assert observer.on_compaction(CompactionResult(messages=[])) is None
    assert observer.on_bash_notice("[bash-done pid:1 exit:0]") is None
    assert observer.on_steering("go left") is None


async def test_events_arrive_in_the_order_the_old_callbacks_did() -> None:
    observer = RecordingObserver()
    await _run_scripted_turn(observer=observer)

    rendered = [name for name, _ in observer.log if name in {"text", "tool_call", "tool_result"}]
    assert rendered == ["text", "tool_call", "tool_result", "text"]

    # The same ordering relative to status transitions the loop used to signal.
    assert observer.log == [
        ("status", "Thinking..."),
        ("status", None),
        ("text", "Checking..."),
        ("status", None),
        ("tool_call", ("tc-1", "6 * 7")),
        ("status", "Running ipython..."),
        ("status", None),
        ("tool_result", ("tc-1", observer.tool_results[0][1])),
        ("status", "Thinking..."),
        ("status", None),
        ("text", "The answer is 42."),
        ("status", None),
    ]
    assert observer.text_deltas == ["Checking...", "The answer is 42."]
    assert [tc_id for tc_id, _ in observer.tool_calls] == ["tc-1"]
    assert observer.tool_results[0][1].result == "42"


def _loop_ast() -> ast.Module:
    path = loop_mod.__file__
    assert path is not None
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in core/loop.py")


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    return [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]


def test_agent_loop_takes_an_observer_and_no_callback_parameters() -> None:
    # The budgets are a tripwire on turn configuration accumulating, not a target.
    # They count plain knob parameters too (`progress_every` was the last raise),
    # which is the point: adding one is a decision to make deliberately.
    tree = _loop_ast()
    for name, limit in (("agent_loop", 16), ("_call_model", 8)):
        params = _params(_function(tree, name))
        assert len(params) <= limit, [p.arg for p in params]
        for param in params:
            annotation = ast.unparse(param.annotation) if param.annotation is not None else ""
            assert "Callable" not in annotation, f"{name}({param.arg}: {annotation})"

    agent_loop_params = [p.arg for p in _params(_function(tree, "agent_loop"))]
    assert "observer" in agent_loop_params
    assert not [name for name in agent_loop_params if name.startswith("on_")]


def test_loop_module_keeps_no_on_star_identifiers() -> None:
    tree = _loop_ast()
    path = loop_mod.__file__
    assert path is not None
    source = Path(path).read_text(encoding="utf-8")

    # No `if on_x:` / `on_x=` leftovers, allowing `observer.on_x(...)` port calls.
    assert re.search(r"(?<![.\w])on_[a-z]", source) is None

    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg is not None):
            identifiers.add(node.arg)
        # ast.Attribute is skipped on purpose: `observer.on_text_delta` is the port.
    assert not sorted(name for name in identifiers if name.startswith("on_"))
