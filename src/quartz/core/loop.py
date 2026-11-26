"""Core agent loop (learn-claude-code style) with a single `ipython` tool."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from quartz.core.background import format_bash_events
from quartz.core.compact import (
    DEFAULT_KEEP_RECENT_TOKENS,
    CompactionReason,
    CompactionResult,
    compact_messages,
    estimate_context_tokens,
)
from quartz.core.events import NO_OBSERVER, TurnObserver
from quartz.core.images import prune_old_images
from quartz.core.loop_model import (
    AbortSignal,
    TurnAborted,
    _abort_marker,
    _completion_to_dict,
    _is_transient_provider_error,
    _retry_delay_seconds,
    _sleep_interruptible,
    _wait_preempt,
)
from quartz.core.loop_model import _call_model as _model_call
from quartz.core.loop_model import _call_model_once as _model_call_once
from quartz.core.progress import ProgressCheckpointer, announced_to_user
from quartz.core.session import repair_dangling_tool_calls
from quartz.errors import ContextOverflowError
from quartz.kernel.base import CellExecutor, CellResult, Kernel
from quartz.providers.base import LLMProvider, Usage
from quartz.providers.types import ChatMessage, ToolCallDict, ToolDef

__all__ = [
    "TOOLS",
    "AbortSignal",
    "TurnAborted",
    "TurnUsage",
    "_call_model",
    "_call_model_once",
    "_completion_to_dict",
    "_is_transient_provider_error",
    "_retry_delay_seconds",
    "_sleep_interruptible",
    "_wait_preempt",
    "agent_loop",
    "drain_bash_events",
]


@dataclasses.dataclass
class TurnUsage:
    """Cumulative token usage across turns in a session."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0

    def update(self, usage: Any) -> None:
        if usage is None:
            return
        pt = getattr(usage, "input_tokens", None)
        if pt is None:
            pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "output_tokens", None)
        if ct is None:
            ct = getattr(usage, "completion_tokens", 0) or 0
        cached = getattr(usage, "cached_tokens", None)
        if cached is None:
            cached = 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0
        pt, ct, cached = int(pt or 0), int(ct or 0), int(cached or 0)
        if pt:
            self.prompt_tokens = pt
            self.cached_tokens = cached
        if ct:
            self.completion_tokens = ct
        self.total_prompt_tokens += pt
        self.total_completion_tokens += ct
        self.total_cached_tokens += cached

    @property
    def cache_hit_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0
        return min(1.0, max(0.0, self.cached_tokens / self.prompt_tokens))

    def context_pct(self, max_tokens: int) -> float:
        return (self.prompt_tokens / max_tokens * 100.0) if max_tokens > 0 else 0.0

    def is_overflowing(self, max_tokens: int) -> bool:
        return max_tokens > 0 and self.prompt_tokens > max_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cached_tokens": self.total_cached_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, int] | None) -> TurnUsage:
        if not data:
            return cls()
        return cls(
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            cached_tokens=int(data.get("cached_tokens", 0)),
            total_prompt_tokens=int(data.get("total_prompt_tokens", 0)),
            total_completion_tokens=int(data.get("total_completion_tokens", 0)),
            total_cached_tokens=int(data.get("total_cached_tokens", 0)),
        )


TOOLS: list[ToolDef] = [
    {
        "type": "function",
        "function": {
            "name": "ipython",
            "description": (
                "Execute Python code in the persistent CPython REPL. "
                "Top-level `await` is supported, and variables/functions persist across calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute in the REPL.",
                    }
                },
                "required": ["code"],
            },
        },
    }
]


async def _call_model(
    client: LLMProvider,
    model: str,
    full_messages: Sequence[Mapping[str, Any]],
    *,
    stream: bool,
    observer: TurnObserver = NO_OBSERVER,
    effort: str | None = None,
    abort_event: asyncio.Event | None = None,
    steering_queue: asyncio.Queue[str] | None = None,
) -> tuple[ChatMessage, Usage | None]:
    return await _model_call(
        client,
        model,
        full_messages,
        tools=TOOLS,
        stream=stream,
        observer=observer,
        effort=effort,
        abort_event=abort_event,
        steering_queue=steering_queue,
    )


async def _call_model_once(
    client: LLMProvider,
    model: str,
    full_messages: Sequence[Mapping[str, Any]],
    *,
    stream: bool,
    observer: TurnObserver = NO_OBSERVER,
    effort: str | None = None,
    abort_event: asyncio.Event | None = None,
    steering_queue: asyncio.Queue[str] | None = None,
    emitted: list[bool] | None = None,
) -> tuple[ChatMessage, Usage | None]:
    return await _model_call_once(
        client,
        model,
        full_messages,
        tools=TOOLS,
        stream=stream,
        observer=observer,
        effort=effort,
        abort_event=abort_event,
        steering_queue=steering_queue,
        emitted=emitted,
    )


type _MessageList = list[dict[str, Any]]


def drain_bash_events(kernel: CellExecutor, messages: _MessageList) -> str | None:
    """Drain pending kernel events and append any background notice to ``messages``."""
    events = kernel.drain_events()
    notice = format_bash_events(events)
    if notice:
        messages.append({"role": "user", "content": notice})
    return notice


def _drain_steering(
    steering_queue: asyncio.Queue[str] | None,
    messages: _MessageList,
    observer: TurnObserver = NO_OBSERVER,
) -> list[str]:
    """Drain any mid-turn user steering messages and append them to `messages`."""
    if steering_queue is None:
        return []
    injected: list[str] = []
    while True:
        try:
            text = steering_queue.get_nowait().strip()
        except asyncio.QueueEmpty:
            break
        if not text:
            continue
        messages.append({"role": "user", "content": text})
        injected.append(text)
        observer.on_steering(text)
    return injected


def _parse_tool_calls(tool_calls: Sequence[ToolCallDict]) -> list[tuple[str, str, str | None]]:
    parsed: list[tuple[str, str, str | None]] = []
    for tc in tool_calls:
        fn_name = tc["function"]["name"]
        tc_id = tc["id"]
        if fn_name != "ipython":
            parsed.append((tc_id, fn_name, None))
            continue
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
            code = args.get("code", "")
        except json.JSONDecodeError:
            code = tc["function"]["arguments"] or ""
        parsed.append((tc_id, fn_name, code))
    return parsed


async def _execute_ipython_calls(
    kernel: Kernel,
    parsed_calls: list[tuple[str, str, str | None]],
    ipython_indices: list[int],
    tool_calls: Sequence[ToolCallDict],
    assistant_dict: ChatMessage,
    messages: _MessageList,
    *,
    observer: TurnObserver,
    abort_event: asyncio.Event | None,
) -> dict[int, CellResult]:
    results_by_idx: dict[int, CellResult] = {}
    if not ipython_indices:
        return results_by_idx
    for i in ipython_indices:
        tc_id, _, code = parsed_calls[i]
        observer.on_tool_call(tc_id, code or "")
    observer.on_status("Running ipython...")
    try:
        if len(ipython_indices) > 1:
            results = await asyncio.gather(
                *(kernel.exec_cell(parsed_calls[i][2] or "") for i in ipython_indices)
            )
            for i, cell_res in zip(ipython_indices, results, strict=True):
                results_by_idx[i] = cell_res
        else:
            i = ipython_indices[0]
            results_by_idx[i] = await kernel.exec_cell(parsed_calls[i][2] or "")
    except (KeyboardInterrupt, TurnAborted, asyncio.CancelledError) as exc:
        observer.on_status(None)
        kernel.interrupt()
        marker = _abort_marker(exc, abort_event)
        for rem_tc in tool_calls:
            messages.append({"role": "tool", "tool_call_id": rem_tc["id"], "content": marker})
        if isinstance(exc, asyncio.CancelledError) and abort_event is not None and abort_event.is_set():
            raise TurnAborted() from None
        raise
    except Exception:
        if messages and messages[-1] is assistant_dict:
            messages.pop()
        raise
    finally:
        observer.on_status(None)
    return results_by_idx


def _append_tool_results(
    parsed_calls: list[tuple[str, str, str | None]],
    results_by_idx: dict[int, CellResult],
    messages: _MessageList,
    observer: TurnObserver,
) -> None:
    step_images: list[dict[str, str]] = []
    for i, (tc_id, fn_name, _) in enumerate(parsed_calls):
        if fn_name != "ipython":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": f"Unknown tool: {fn_name}. Only `ipython` is available.",
                }
            )
            continue
        cell_res = results_by_idx[i]
        if cell_res.images:
            step_images.extend(cell_res.images)
        observer.on_tool_result(tc_id, cell_res)
        messages.append(
            {"role": "tool", "tool_call_id": tc_id, "content": cell_res.format_for_model()}
        )
    if step_images:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "[tool-images] Attached image(s) from tool output:"},
                    *[
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{img['mime_type']};base64,{img['data']}"},
                        }
                        for img in step_images
                    ],
                ],
            }
        )


async def _compact_and_update(
    client: LLMProvider,
    model: str,
    messages: _MessageList,
    kernel: Kernel,
    *,
    budget: int | None,
    keep_tokens: int,
    system_tokens: int,
    observer: TurnObserver,
) -> tuple[CompactionResult, int | None]:
    result = await compact_messages(
        client, model, messages, kernel, budget_tokens=budget, keep_recent_tokens=keep_tokens
    )
    messages[:] = result.messages
    if result.changed:
        observer.on_compaction(result)
        return result, system_tokens + result.estimated_tokens_after
    return result, None


def _handle_progress_refusal(
    progress: ProgressCheckpointer,
    assistant_dict: ChatMessage,
    tool_calls: Sequence[ToolCallDict],
    messages: _MessageList,
    text_content: str,
) -> bool:
    answered = bool(text_content.strip())
    progress.settle(updated=answered)
    if answered or (refusal := progress.refuse()) is None:
        return False
    if tool_calls:
        messages.append(cast(Any, assistant_dict))
        for tc in tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": refusal})
    else:
        messages.append({"role": "user", "content": refusal})
    return True


async def _run_tool_step(
    kernel: Kernel,
    tool_calls: Sequence[ToolCallDict],
    assistant_dict: ChatMessage,
    messages: _MessageList,
    progress: ProgressCheckpointer,
    *,
    observer: TurnObserver,
    abort_event: asyncio.Event | None,
    steering_queue: asyncio.Queue[str] | None,
) -> None:
    parsed_calls = _parse_tool_calls(tool_calls)
    ipython_indices = [i for i, (_, fn, _) in enumerate(parsed_calls) if fn == "ipython"]
    results_by_idx = await _execute_ipython_calls(
        kernel, parsed_calls, ipython_indices, tool_calls, assistant_dict, messages,
        observer=observer, abort_event=abort_event,
    )
    _append_tool_results(parsed_calls, results_by_idx, messages, observer)
    progress.record(len(ipython_indices))
    if abort_event is not None and abort_event.is_set():
        raise TurnAborted()
    _drain_steering(steering_queue, messages, observer)


async def agent_loop(
    client: LLMProvider, model: str, system_prompt: str, messages: _MessageList, kernel: Kernel, *,
    observer: TurnObserver = NO_OBSERVER, max_context_tokens: int = 128_000,
    compact_reserve_tokens: int = 16_384, keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
    stream: bool = True, usage_stats: TurnUsage | None = None, initial_prompt_tokens: int = 0,
    effort: str | None = None, abort_event: asyncio.Event | None = None,
    steering_queue: asyncio.Queue[str] | None = None, progress_every: int = 0,
) -> tuple[str, _MessageList, int]:
    """Run the pure-async single-tool (`ipython`) agent loop until completion or abort."""
    messages[:] = repair_dangling_tool_calls(messages)
    progress = ProgressCheckpointer(progress_every)
    last_prompt_tokens = initial_prompt_tokens
    measured_count = max(0, len(messages) - 1) if initial_prompt_tokens > 0 else 0
    system_tokens = estimate_context_tokens([{"role": "system", "content": system_prompt}])
    compact_threshold = max(4096, max_context_tokens - compact_reserve_tokens)
    overflow_recoveries = 0

    while True:
        if abort_event is not None and abort_event.is_set():
            raise TurnAborted()
        if notice := drain_bash_events(kernel, messages):
            observer.on_bash_notice(notice)
        if last_prompt_tokens + estimate_context_tokens(messages[measured_count:]) >= compact_threshold and len(messages) > 1:
            _, new_tokens = await _compact_and_update(
                client, model, messages, kernel, budget=compact_threshold,
                keep_tokens=keep_recent_tokens, system_tokens=system_tokens, observer=observer,
            )
            if new_tokens is not None:
                last_prompt_tokens, measured_count = new_tokens, len(messages)
        if (update_request := progress.take(user_was_told=announced_to_user(messages))) is not None:
            messages.append(update_request)

        full_messages = [{"role": "system", "content": system_prompt}, *prune_old_images(messages)]
        try:
            assistant_dict, usage = await _call_model(
                client, model, full_messages, stream=stream, observer=observer,
                effort=effort, abort_event=abort_event, steering_queue=steering_queue,
            )
        except ContextOverflowError:
            if overflow_recoveries >= 1:
                raise
            overflow_recoveries += 1
            forced, new_tokens = await _compact_and_update(
                client, model, messages, kernel, budget=None,
                keep_tokens=max(4_000, keep_recent_tokens // 2), system_tokens=system_tokens, observer=observer,
            )
            if new_tokens is not None:
                last_prompt_tokens, measured_count = new_tokens, len(messages)
            if forced.reason is not CompactionReason.SUMMARIZED:
                raise
            continue

        if usage and usage.input_tokens:
            last_prompt_tokens = int(usage.input_tokens)
        measured_count = len(messages)
        if usage_stats is not None and usage is not None:
            usage_stats.update(usage)

        tool_calls = assistant_dict.get("tool_calls") or []
        if not tool_calls and assistant_dict.get("content") is None and _drain_steering(steering_queue, messages, observer):
            continue
        raw_content = assistant_dict.get("content")
        text_content = raw_content if isinstance(raw_content, str) else ""
        if _handle_progress_refusal(progress, assistant_dict, tool_calls, messages, text_content):
            continue

        messages.append(cast(Any, assistant_dict))
        if not tool_calls:
            if _drain_steering(steering_queue, messages, observer):
                continue
            return text_content.strip(), messages, last_prompt_tokens

        await _run_tool_step(
            kernel, tool_calls, assistant_dict, messages, progress,
            observer=observer, abort_event=abort_event, steering_queue=steering_queue,
        )
