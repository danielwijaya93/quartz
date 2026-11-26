"""Core agent loop (learn-claude-code style) with a single `ipython` tool."""

from __future__ import annotations

import asyncio
import contextlib
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
from quartz.core.progress import ProgressCheckpointer, announced_to_user
from quartz.core.session import repair_dangling_tool_calls
from quartz.errors import AuthenticationError, ContextOverflowError, ProviderError, RateLimitError
from quartz.kernel.base import CellExecutor, CellResult, Kernel
from quartz.providers.base import (
    CompletionResult,
    LLMProvider,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageEvent,
)
from quartz.providers.types import ChatMessage, ToolCallAccumulator, ToolCallDict, ToolDef

__all__ = [
    "TOOLS",
    "AbortSignal",
    "TurnAborted",
    "TurnUsage",
    "agent_loop",
    "drain_bash_events",
]


class AbortSignal(asyncio.Event):
    """``asyncio.Event`` carrying an optional human-readable abort reason."""

    def __init__(self) -> None:
        super().__init__()
        self.reason: str | None = None

    def trigger(self, reason: str | None = None) -> None:
        if self.reason is None and reason is not None:
            self.reason = reason
        self.set()


def _abort_marker(exc: BaseException, abort_event: asyncio.Event | None) -> str:
    if isinstance(exc, KeyboardInterrupt):
        return "[interrupted by user]"
    reason = abort_event.reason if isinstance(abort_event, AbortSignal) else None
    if reason:
        return f"[aborted: {reason}]"
    return "[aborted by user]"


class TurnAborted(Exception):
    """Raised when an active turn is aborted (user cancel or shutdown)."""


@dataclasses.dataclass
class TurnUsage:
    """Tracks context window usage and prompt cache hit rate across turns."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0

    def update(self, usage: Usage | None) -> None:
        """Fold one ``Usage`` event into last-turn and session-total counters.

        Last-turn ``prompt_tokens``/``cached_tokens`` are only overwritten
        together and only when the event actually carries an input count
        (``pt > 0``): a zero/partial usage event then does not corrupt the
        CH ratio by pairing a fresh cached count with a stale prompt count
        (or vice versa). Session totals always accumulate what the event
        reports, even when ``pt == 0``.
        """
        if usage is None:
            return
        pt = int(usage.input_tokens or 0)
        ct = int(usage.output_tokens or 0)
        cached = int(usage.cached_tokens or 0)

        if pt > 0:
            self.prompt_tokens = pt
            self.cached_tokens = cached
        if ct:
            self.completion_tokens = ct

        self.total_prompt_tokens += pt
        self.total_completion_tokens += ct
        self.total_cached_tokens += cached

    @property
    def cache_hit_rate(self) -> float:
        """Cached fraction of the last prompt, clamped to ``[0.0, 1.0]``.

        Providers may report ``cached > prompt`` due to field-semantics
        differences (e.g. a cache-hit counter that excludes uncached
        reprompt tokens), so clamp rather than display >100%.
        """
        if self.prompt_tokens <= 0:
            return 0.0
        return min(1.0, max(0.0, self.cached_tokens / self.prompt_tokens))

    def context_pct(self, max_context_tokens: int) -> float:
        return (self.prompt_tokens / max_context_tokens * 100.0) if max_context_tokens > 0 else 0.0

    def is_overflowing(self, max_context_tokens: int) -> bool:
        """True when the last prompt exceeds the given context window.

        Kept unclamped (``context_pct`` stays raw) so a stale denominator
        after model/session switches is visible as an overflow warning
        instead of being silently capped at 100%.
        """
        return max_context_tokens > 0 and self.context_pct(max_context_tokens) > 100.0

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


def drain_bash_events(kernel: CellExecutor, messages: list[dict[str, Any]]) -> str | None:
    """Drain pending kernel events and append any background notice to ``messages``.

    Two kinds arrive here: ``bash_done`` when a job finishes, and ``bash_status``
    when a running job crossed the status-check period. Both are harness notices
    and both take the user role, which is the only role the wire lets the harness
    inject with (see ``core/background.py``).
    """
    events = kernel.drain_events()
    notice = format_bash_events(events)
    if notice:
        messages.append({"role": "user", "content": notice})
    return notice


def _completion_to_dict(result: CompletionResult) -> ChatMessage:
    out: ChatMessage = {
        "role": "assistant",
        "content": result.text if result.text else None,
    }
    if result.tool_calls:
        tc_list: list[ToolCallDict] = []
        for tc in result.tool_calls:
            tc_obj: ToolCallDict = {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.arguments,
                },
            }
            if tc.meta.get("thought_signature"):
                tc_obj["thought_signature"] = tc.meta["thought_signature"]
            tc_list.append(tc_obj)
        out["tool_calls"] = tc_list
    return out




def _drain_steering(
    steering_queue: asyncio.Queue[str] | None,
    messages: list[dict[str, Any]],
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


async def _wait_preempt(
    abort_event: asyncio.Event | None,
    steering_queue: asyncio.Queue[str] | None,
) -> str:
    """Wait until either abort is requested or a steering message arrives."""
    if abort_event is not None and abort_event.is_set():
        return "abort"
    if steering_queue is not None and not steering_queue.empty():
        return "steer"
    if abort_event is not None and steering_queue is None:
        await abort_event.wait()
        return "abort"
    while True:
        if abort_event is not None and abort_event.is_set():
            return "abort"
        if steering_queue is not None and not steering_queue.empty():
            return "steer"
        await asyncio.sleep(0.02)


_TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BASE_DELAY_S = 1.0
_MAX_RETRY_AFTER_S = 30.0


def _is_transient_provider_error(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, (AuthenticationError, ContextOverflowError)):
        return False
    return isinstance(exc, ProviderError) and exc.status_code in _TRANSIENT_HTTP_STATUSES


def _retry_delay_seconds(exc: ProviderError, attempt: int) -> float:
    if isinstance(exc, RateLimitError) and exc.retry_after is not None and exc.retry_after >= 0:
        return min(float(exc.retry_after), _MAX_RETRY_AFTER_S)
    return min(_RETRY_BASE_DELAY_S * (2**attempt), _MAX_RETRY_AFTER_S)


async def _sleep_interruptible(
    delay: float,
    abort_event: asyncio.Event | None,
    steering_queue: asyncio.Queue[str] | None,
) -> str | None:
    """Sleep for `delay` seconds unless aborted or steered first."""
    if abort_event is None and steering_queue is None:
        await asyncio.sleep(delay)
        return None
    if abort_event is not None and abort_event.is_set():
        return "abort"
    if steering_queue is not None and not steering_queue.empty():
        return "steer"
    sleep_task = asyncio.create_task(asyncio.sleep(delay))
    preempt_task = asyncio.create_task(_wait_preempt(abort_event, steering_queue))
    try:
        done, _ = await asyncio.wait(
            {sleep_task, preempt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if preempt_task in done:
            return preempt_task.result()
        return None
    finally:
        for task in (sleep_task, preempt_task):
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):
                await task


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
    """Async provider call with bounded exponential backoff on transient 429/5xx errors."""
    for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
        emitted = [False]
        try:
            return await _call_model_once(
                client,
                model,
                full_messages,
                stream=stream,
                observer=observer,
                effort=effort,
                abort_event=abort_event,
                steering_queue=steering_queue,
                emitted=emitted,
            )
        except ProviderError as exc:
            # Never retry once tokens, thinking, or tool calls have already been emitted,
            # or once the retry budget is exhausted.
            if emitted[0] or attempt >= _MAX_TRANSIENT_RETRIES or not _is_transient_provider_error(exc):
                raise
            delay = _retry_delay_seconds(exc, attempt)
            status = exc.status_code
            label = (
                f"Rate limited; retrying in {delay:g}s..."
                if status == 429
                else f"Provider error ({status}); retrying in {delay:g}s..."
            )
            observer.on_status(label)
            try:
                reason = await _sleep_interruptible(delay, abort_event, steering_queue)
            finally:
                observer.on_status(None)
            if reason == "abort":
                raise TurnAborted() from None
            if reason == "steer":
                return {"role": "assistant", "content": None}, None
    raise AssertionError("unreachable")


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
    """Execute a single async provider call (streaming or non-streaming)."""
    if abort_event is not None and abort_event.is_set():
        raise TurnAborted()

    if not stream:
        observer.on_status("Thinking...")
        try:
            result = await client.complete(
                model=model,
                messages=full_messages,
                tools=TOOLS,
                effort=effort,
            )
        finally:
            observer.on_status(None)
        if abort_event is not None and abort_event.is_set():
            raise TurnAborted()
        return _completion_to_dict(result), result.usage

    observer.on_status("Thinking...")
    preempted = False
    aborted_stream = False
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls_acc: dict[int, ToolCallAccumulator] = {}
    usage: Usage | None = None
    preempt_task: asyncio.Task[str] | None = None
    try:
        stream_handle = await client.stream(
            model=model,
            messages=full_messages,
            tools=TOOLS,
            effort=effort,
        )
        if abort_event is not None or steering_queue is not None:
            preempt_task = asyncio.create_task(_wait_preempt(abort_event, steering_queue))

        status_cleared = False
        async with stream_handle as events:
            event_iter = aiter(events)

            async def _next_event() -> Any:
                return await anext(event_iter)

            while True:
                if abort_event is not None and abort_event.is_set():
                    aborted_stream = True
                    with contextlib.suppress(Exception):
                        await stream_handle.aclose()
                    raise TurnAborted()
                if steering_queue is not None and not steering_queue.empty():
                    preempted = True
                    with contextlib.suppress(Exception):
                        await stream_handle.aclose()
                    break

                if preempt_task is not None:
                    next_task = asyncio.create_task(_next_event())
                    try:
                        done, _ = await asyncio.wait(
                            {next_task, preempt_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if preempt_task in done:
                            if not next_task.done():
                                next_task.cancel()
                            with contextlib.suppress(BaseException):
                                await next_task
                            reason = preempt_task.result()
                            with contextlib.suppress(Exception):
                                await stream_handle.aclose()
                            if reason == "abort":
                                aborted_stream = True
                                raise TurnAborted()
                            preempted = True
                            break
                        try:
                            event = next_task.result()
                        except StopAsyncIteration:
                            break
                    except BaseException:
                        if not next_task.done():
                            next_task.cancel()
                        with contextlib.suppress(BaseException):
                            await next_task
                        raise
                else:
                    try:
                        event = await anext(event_iter)
                    except StopAsyncIteration:
                        break

                if isinstance(event, UsageEvent):
                    usage = event.usage
                elif isinstance(event, ThinkingDelta):
                    if event.text:
                        if emitted is not None:
                            emitted[0] = True
                        thinking_parts.append(event.text)
                        observer.on_thinking_delta(event.text)
                elif isinstance(event, TextDelta):
                    if event.text:
                        if emitted is not None:
                            emitted[0] = True
                        if not status_cleared:
                            observer.on_status(None)
                            status_cleared = True
                        content_parts.append(event.text)
                        observer.on_text_delta(event.text)
                elif isinstance(event, ToolCallDelta):
                    if emitted is not None:
                        emitted[0] = True
                    entry = tool_calls_acc.setdefault(
                        event.index,
                        {"id": "", "name": "", "arguments": "", "meta": {}},
                    )
                    if event.id:
                        entry["id"] = event.id
                    if event.name:
                        entry["name"] += event.name
                    if event.arguments_delta:
                        entry["arguments"] += event.arguments_delta
                    if event.meta:
                        entry["meta"].update(event.meta)
    except asyncio.CancelledError:
        if aborted_stream or (abort_event is not None and abort_event.is_set()):
            raise TurnAborted() from None
        raise
    except Exception:
        if aborted_stream or (abort_event is not None and abort_event.is_set()):
            raise TurnAborted() from None
        if not preempted:
            raise
    finally:
        if preempt_task is not None and not preempt_task.done():
            preempt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await preempt_task
        observer.on_status(None)

    if preempted:
        tool_calls_acc.clear()
        content_parts.clear()
        thinking_parts.clear()

    assistant_dict: ChatMessage = {
        "role": "assistant",
        "content": "".join(content_parts) if content_parts else None,
    }
    reasoning_text = "".join(thinking_parts)
    if reasoning_text:
        assistant_dict["reasoning"] = reasoning_text
    if tool_calls_acc:
        tc_list: list[ToolCallDict] = []
        for _, entry in sorted(tool_calls_acc.items()):
            tc_obj: ToolCallDict = {
                "id": entry["id"],
                "type": "function",
                "function": {"name": entry["name"], "arguments": entry["arguments"]},
            }
            if entry["meta"].get("thought_signature"):
                tc_obj["thought_signature"] = entry["meta"]["thought_signature"]
            tc_list.append(tc_obj)
        assistant_dict["tool_calls"] = tc_list
    return assistant_dict, usage



async def agent_loop(
    client: LLMProvider,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    kernel: Kernel,
    *,
    observer: TurnObserver = NO_OBSERVER,
    max_context_tokens: int = 128_000,
    compact_reserve_tokens: int = 16_384,
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
    stream: bool = True,
    usage_stats: TurnUsage | None = None,
    initial_prompt_tokens: int = 0,
    effort: str | None = None,
    abort_event: asyncio.Event | None = None,
    steering_queue: asyncio.Queue[str] | None = None,
    progress_every: int = 0,
) -> tuple[str, list[dict[str, Any]], int]:
    """Run the pure-async single-tool (`ipython`) agent loop until completion or abort."""
    messages[:] = repair_dangling_tool_calls(messages)
    progress = ProgressCheckpointer(progress_every)
    last_prompt_tokens = initial_prompt_tokens
    measured_message_count = max(0, len(messages) - 1) if initial_prompt_tokens > 0 else 0
    system_tokens = estimate_context_tokens([{"role": "system", "content": system_prompt}])
    compact_threshold = max(4096, max_context_tokens - compact_reserve_tokens)
    overflow_recoveries = 0

    def projected_prompt_tokens() -> int:
        return last_prompt_tokens + estimate_context_tokens(messages[measured_message_count:])

    async def run_compaction(*, budget: int | None, keep_tokens: int) -> CompactionResult:
        nonlocal last_prompt_tokens, measured_message_count
        result = await compact_messages(
            client,
            model,
            messages,
            kernel,
            budget_tokens=budget,
            keep_recent_tokens=keep_tokens,
        )
        messages[:] = result.messages
        if result.changed:
            last_prompt_tokens = system_tokens + result.estimated_tokens_after
            measured_message_count = len(messages)
            observer.on_compaction(result)
        return result

    while True:
        if abort_event is not None and abort_event.is_set():
            raise TurnAborted()

        notice = drain_bash_events(kernel, messages)
        if notice:
            observer.on_bash_notice(notice)

        if projected_prompt_tokens() >= compact_threshold and len(messages) > 1:
            await run_compaction(budget=compact_threshold, keep_tokens=keep_recent_tokens)

        update_request = progress.take(user_was_told=announced_to_user(messages))
        if update_request is not None:
            messages.append(update_request)

        full_messages = [{"role": "system", "content": system_prompt}, *prune_old_images(messages)]
        try:
            assistant_dict, usage = await _call_model(
                client,
                model,
                full_messages,
                stream=stream,
                observer=observer,
                effort=effort,
                abort_event=abort_event,
                steering_queue=steering_queue,
            )
        except ContextOverflowError:
            if overflow_recoveries >= 1:
                raise
            overflow_recoveries += 1
            forced = await run_compaction(
                budget=None,
                keep_tokens=max(4_000, keep_recent_tokens // 2),
            )
            if forced.reason is not CompactionReason.SUMMARIZED:
                raise
            continue

        if usage and usage.input_tokens:
            last_prompt_tokens = int(usage.input_tokens)
        measured_message_count = len(messages)
        if usage_stats is not None and usage is not None:
            usage_stats.update(usage)

        tool_calls = assistant_dict.get("tool_calls") or []
        if (
            not tool_calls
            and assistant_dict.get("content") is None
            and _drain_steering(steering_queue, messages, observer)
        ):
            continue

        raw_content = assistant_dict.get("content")
        text_content = raw_content if isinstance(raw_content, str) else ""
        answered = bool(text_content.strip())
        progress.settle(updated=answered)
        if not answered:
            refusal = progress.refuse()
            if refusal is not None:
                if tool_calls:
                    messages.append(cast(Any, assistant_dict))
                    for tc in tool_calls:
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": refusal})
                else:
                    messages.append({"role": "user", "content": refusal})
                continue

        messages.append(cast(Any, assistant_dict))

        if not tool_calls:
            if _drain_steering(steering_queue, messages, observer):
                continue
            return text_content.strip(), messages, last_prompt_tokens

        parsed_calls: list[tuple[str, str, str | None]] = []
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            tc_id = tc["id"]
            if fn_name != "ipython":
                parsed_calls.append((tc_id, fn_name, None))
                continue
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                code = args.get("code", "")
            except json.JSONDecodeError:
                code = tc["function"]["arguments"] or ""
            parsed_calls.append((tc_id, fn_name, code))

        ipython_indices = [i for i, (_, fn, _) in enumerate(parsed_calls) if fn == "ipython"]
        step_images: list[dict[str, str]] = []
        results_by_idx: dict[int, CellResult] = {}

        if ipython_indices:
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
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": rem_tc["id"],
                            "content": marker,
                        }
                    )
                if isinstance(exc, asyncio.CancelledError) and abort_event is not None and abort_event.is_set():
                    raise TurnAborted() from None
                raise
            except Exception:
                if messages and messages[-1] is assistant_dict:
                    messages.pop()
                raise
            finally:
                observer.on_status(None)

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
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": cell_res.format_for_model(),
                }
            )
        progress.record(len(ipython_indices))

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

        if abort_event is not None and abort_event.is_set():
            raise TurnAborted()
        _drain_steering(steering_queue, messages, observer)
