"""Core agent loop (learn-claude-code style) with a single `ipython` tool."""

from __future__ import annotations

import dataclasses
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from quartz.core.background import format_bash_events
from quartz.core.compact import (
    DEFAULT_KEEP_RECENT_TOKENS,
    CompactionReason,
    CompactionResult,
    compact_messages,
    estimate_context_tokens,
)
from quartz.core.events import NO_OBSERVER, TurnObserver
from quartz.core.progress import ProgressCheckpointer, announced_to_user
from quartz.errors import ContextOverflowError
from quartz.kernel.client import CellResult, KernelClient
from quartz.providers.base import (
    CompletionResult,
    LLMProvider,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageEvent,
)


class TurnAborted(Exception):
    """Raised when the user aborts an active turn (e.g., by pressing Esc)."""


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


TOOLS = [
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


def drain_bash_events(kernel: KernelClient, messages: list[dict[str, Any]]) -> str | None:
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


def _completion_to_dict(result: CompletionResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "role": "assistant",
        "content": result.text if result.text else None,
    }
    if result.tool_calls:
        tc_list: list[dict[str, Any]] = []
        for tc in result.tool_calls:
            tc_obj: dict[str, Any] = {
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


def _call_model(
    client: LLMProvider,
    model: str,
    full_messages: list[dict[str, Any]],
    *,
    stream: bool,
    observer: TurnObserver = NO_OBSERVER,
    effort: str | None = None,
    abort_event: threading.Event | None = None,
    steering_queue: queue.Queue[str] | None = None,
) -> tuple[dict[str, Any], Usage | None]:
    """Call provider (streaming or non-streaming) and return (assistant_dict, usage)."""
    if abort_event is not None and abort_event.is_set():
        raise TurnAborted()

    if not stream:
        observer.on_status("Thinking...")
        try:
            result = client.complete(
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
    stream_done = threading.Event()
    preempted = threading.Event()
    aborted_stream = threading.Event()
    try:
        stream_handle = client.stream(
            model=model,
            messages=full_messages,
            tools=TOOLS,
            effort=effort,
        )

        if abort_event is not None or steering_queue is not None:
            def _watch_preempt() -> None:
                while not stream_done.is_set():
                    if abort_event is not None and abort_event.is_set():
                        aborted_stream.set()
                        try:
                            stream_handle.close()
                        except Exception:
                            pass
                        return
                    if steering_queue is not None and not steering_queue.empty():
                        preempted.set()
                        try:
                            stream_handle.close()
                        except Exception:
                            pass
                        return
                    if stream_done.wait(timeout=0.05):
                        return

            threading.Thread(target=_watch_preempt, daemon=True).start()

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        usage: Usage | None = None
        status_cleared = False

        with stream_handle as events:
            for event in events:
                if abort_event is not None and abort_event.is_set():
                    aborted_stream.set()
                    raise TurnAborted()
                if steering_queue is not None and not steering_queue.empty():
                    preempted.set()
                    try:
                        stream_handle.close()
                    except Exception:
                        pass
                    break

                if isinstance(event, UsageEvent):
                    usage = event.usage
                elif isinstance(event, ThinkingDelta):
                    if event.text:
                        thinking_parts.append(event.text)
                        observer.on_thinking_delta(event.text)
                elif isinstance(event, TextDelta):
                    if event.text:
                        if not status_cleared:
                            observer.on_status(None)
                            status_cleared = True
                        content_parts.append(event.text)
                        observer.on_text_delta(event.text)
                elif isinstance(event, ToolCallDelta):
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
    except Exception:
        if aborted_stream.is_set() or (abort_event is not None and abort_event.is_set()):
            raise TurnAborted()
        if not preempted.is_set():
            raise
    finally:
        stream_done.set()
        observer.on_status(None)

    if preempted.is_set():
        tool_calls_acc.clear()
        content_parts.clear()
        thinking_parts.clear()

    assistant_dict: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) if content_parts else None,
    }
    reasoning_text = "".join(thinking_parts)
    if reasoning_text:
        assistant_dict["reasoning"] = reasoning_text
    if tool_calls_acc:
        tc_list: list[dict[str, Any]] = []
        for _, entry in sorted(tool_calls_acc.items()):
            tc_obj: dict[str, Any] = {
                "id": entry["id"],
                "type": "function",
                "function": {"name": entry["name"], "arguments": entry["arguments"]},
            }
            if entry["meta"].get("thought_signature"):
                tc_obj["thought_signature"] = entry["meta"]["thought_signature"]
            tc_list.append(tc_obj)
        assistant_dict["tool_calls"] = tc_list
    return assistant_dict, usage


def _drain_steering(
    steering_queue: queue.Queue[str] | None,
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
        except queue.Empty:
            break
        if not text:
            continue
        messages.append({"role": "user", "content": text})
        injected.append(text)
        observer.on_steering(text)
    return injected


def agent_loop(
    client: LLMProvider,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    kernel: KernelClient,
    *,
    observer: TurnObserver = NO_OBSERVER,
    max_context_tokens: int = 128_000,
    compact_reserve_tokens: int = 16_384,
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
    stream: bool = True,
    usage_stats: TurnUsage | None = None,
    initial_prompt_tokens: int = 0,
    effort: str | None = None,
    abort_event: threading.Event | None = None,
    steering_queue: queue.Queue[str] | None = None,
    progress_every: int = 0,
) -> tuple[str, list[dict[str, Any]], int]:
    """Run one turn of the agent loop until the model stops calling tools.

    Every lifecycle event (streamed text, thinking, tool call/result, compaction,
    background bash notices, steering) is reported to ``observer``; the default
    observer ignores all of them. Returns (final_text, updated_messages,
    last_prompt_tokens).

    ``progress_every`` schedules user updates: every that many executed ipython
    calls, a ``[progress]`` notice asks the model to tell the user where things
    stand before carrying on in the same response. ``0`` (the default) never asks,
    which is what a headless run wants.
    """
    progress = ProgressCheckpointer(progress_every)
    last_prompt_tokens = initial_prompt_tokens
    # Number of leading messages already covered by `last_prompt_tokens`.
    measured_message_count = max(0, len(messages) - 1) if initial_prompt_tokens > 0 else 0
    system_tokens = estimate_context_tokens([{"role": "system", "content": system_prompt}])
    compact_threshold = max(4096, max_context_tokens - compact_reserve_tokens)
    overflow_recoveries = 0

    def projected_prompt_tokens() -> int:
        """Provider-reported size plus an estimate for messages appended since."""
        return last_prompt_tokens + estimate_context_tokens(messages[measured_message_count:])

    def run_compaction(*, budget: int | None, keep_tokens: int) -> CompactionResult:
        nonlocal last_prompt_tokens, measured_message_count
        result = compact_messages(
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
            run_compaction(budget=compact_threshold, keep_tokens=keep_recent_tokens)

        # Scheduled user update: the harness owns the timing, the model the words.
        # Injected after compaction so the request the model sees is never the one
        # copy that got summarised away.
        update_request = progress.take(user_was_told=announced_to_user(messages))
        if update_request is not None:
            messages.append(update_request)

        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        try:
            assistant_dict, usage = _call_model(
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
            # The provider says the prompt is too long: compact harder, retry once.
            if overflow_recoveries >= 1:
                raise
            overflow_recoveries += 1
            forced = run_compaction(
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

        # A checkpoint the model answered with work instead of words: hand back an
        # error in place of the tool results, so the next thing it reads is what the
        # user did not get. Thinking is not an update -- the user cannot see it.
        answered = bool((assistant_dict.get("content") or "").strip())
        progress.settle(updated=answered)
        if not answered:
            refusal = progress.refuse()
            if refusal is not None:
                if tool_calls:
                    messages.append(assistant_dict)
                    for tc in tool_calls:
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": refusal})
                else:
                    messages.append({"role": "user", "content": refusal})
                continue

        messages.append(assistant_dict)

        if not tool_calls:
            if _drain_steering(steering_queue, messages, observer):
                continue
            return (assistant_dict.get("content") or "").strip(), messages, last_prompt_tokens

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
                    with ThreadPoolExecutor(max_workers=min(len(ipython_indices), 8)) as pool:
                        futures = {i: pool.submit(kernel.exec_cell, parsed_calls[i][2] or "") for i in ipython_indices}
                        for i in ipython_indices:
                            results_by_idx[i] = futures[i].result()
                else:
                    i = ipython_indices[0]
                    results_by_idx[i] = kernel.exec_cell(parsed_calls[i][2] or "")
            except (KeyboardInterrupt, TurnAborted) as exc:
                observer.on_status(None)
                kernel.interrupt()
                marker = "[aborted by user]" if isinstance(exc, TurnAborted) else "[interrupted by user]"
                for rem_tc in tool_calls:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": rem_tc["id"],
                            "content": marker,
                        }
                    )
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
