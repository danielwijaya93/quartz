"""Core agent loop (learn-claude-code style) with a single `ipython` tool."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

from openai import BadRequestError, OpenAI

from agent.compact import compact_messages
from agent.kernel_client import CellResult, KernelClient

@dataclasses.dataclass
class TurnUsage:
    """Tracks context window usage and prompt cache hit rate across turns."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0

    def update(self, usage: Any) -> None:
        if usage is None:
            return
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0

        if pt:
            self.prompt_tokens = pt
        if ct:
            self.completion_tokens = ct
        self.cached_tokens = cached

        self.total_prompt_tokens += pt
        self.total_completion_tokens += ct
        self.total_cached_tokens += cached

    @property
    def cache_hit_rate(self) -> float:
        return (self.cached_tokens / self.prompt_tokens) if self.prompt_tokens > 0 else 0.0

    def context_pct(self, max_context_tokens: int) -> float:
        return (self.prompt_tokens / max_context_tokens * 100.0) if max_context_tokens > 0 else 0.0

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


def format_bash_done_events(events: list[dict[str, Any]]) -> str | None:
    """Format background bash_done events as Unharnessed-style harness notices."""
    notices: list[str] = []
    for ev in events:
        if ev.get("event") != "bash_done":
            continue
        pid = ev.get("pid")
        exit_code = ev.get("exit_code")
        duration = ev.get("duration")
        cmd = ev.get("command", "")
        timed_out = ev.get("timed_out", False)
        tail = (ev.get("tail") or "").rstrip()

        header = f"[bash-done pid:{pid} exit:{exit_code} duration:{duration}s"
        if timed_out:
            header += " timed_out:true"
        header += f"]\ncommand: {cmd}"
        if tail:
            header += f"\ntail:\n{tail}"
        notices.append(header)

    return "\n\n".join(notices) if notices else None


def drain_bash_events(kernel: KernelClient, messages: list[dict[str, Any]]) -> str | None:
    """Drain pending kernel events and append a [bash-done] notice to messages if any."""
    events = kernel.drain_events()
    notice = format_bash_done_events(events)
    if notice:
        messages.append({"role": "user", "content": notice})
    return notice


def _assistant_msg_to_dict(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return out


def _call_model(
    client: OpenAI,
    model: str,
    full_messages: list[dict[str, Any]],
    *,
    stream: bool,
    on_status: Callable[[str | None], None] | None,
    on_text_delta: Callable[[str], None] | None,
) -> tuple[dict[str, Any], Any]:
    """Call chat completions (streaming or non-streaming) and return (assistant_dict, usage)."""
    if not stream:
        if on_status:
            on_status("Thinking...")
        try:
            response = client.chat.completions.create(
                model=model,
                messages=full_messages,
                tools=TOOLS,
            )
        finally:
            if on_status:
                on_status(None)
        return _assistant_msg_to_dict(response.choices[0].message), response.usage

    if on_status:
        on_status("Thinking...")
    try:
        try:
            stream_resp = client.chat.completions.create(
                model=model,
                messages=full_messages,
                tools=TOOLS,
                stream=True,
                stream_options={"include_usage": True},
            )
        except BadRequestError as exc:
            if "stream_options" in str(exc).lower():
                stream_resp = client.chat.completions.create(
                    model=model,
                    messages=full_messages,
                    tools=TOOLS,
                    stream=True,
                )
            else:
                raise

        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, str]] = {}
        usage: Any = None
        status_cleared = False

        for chunk in stream_resp:
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = choices[0].delta

            text = getattr(delta, "content", None)
            if text:
                if not status_cleared:
                    if on_status:
                        on_status(None)
                    status_cleared = True
                content_parts.append(text)
                if on_text_delta:
                    on_text_delta(text)

            tc_deltas = getattr(delta, "tool_calls", None) or []
            for tc_delta in tc_deltas:
                idx = getattr(tc_delta, "index", 0)
                entry = tool_calls_acc.setdefault(
                    idx, {"id": "", "name": "", "arguments": "", "thought_signature": ""}
                )
                if getattr(tc_delta, "id", None):
                    entry["id"] = tc_delta.id
                if getattr(tc_delta, "thought_signature", None):
                    entry["thought_signature"] = tc_delta.thought_signature
                fn = getattr(tc_delta, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        entry["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        entry["arguments"] += fn.arguments
    finally:
        if on_status:
            on_status(None)

    assistant_dict: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) if content_parts else None,
    }
    if tool_calls_acc:
        tc_list: list[dict[str, Any]] = []
        for _, entry in sorted(tool_calls_acc.items()):
            tc_obj: dict[str, Any] = {
                "id": entry["id"],
                "type": "function",
                "function": {"name": entry["name"], "arguments": entry["arguments"]},
            }
            if entry["thought_signature"]:
                tc_obj["thought_signature"] = entry["thought_signature"]
            tc_list.append(tc_obj)
        assistant_dict["tool_calls"] = tc_list
    return assistant_dict, usage


def agent_loop(
    client: OpenAI,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    kernel: KernelClient,
    *,
    max_context_tokens: int = 128_000,
    compact_reserve_tokens: int = 16_384,
    keep_recent_messages: int = 10,
    stream: bool = True,
    on_tool_call: Callable[[str], None] | None = None,
    on_tool_result: Callable[[CellResult], None] | None = None,
    on_compaction: Callable[[int, int], None] | None = None,
    on_bash_notice: Callable[[str], None] | None = None,
    on_status: Callable[[str | None], None] | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    usage_stats: TurnUsage | None = None,
    initial_prompt_tokens: int = 0,
) -> tuple[str, list[dict[str, Any]], int]:
    """Run one turn of the agent loop until the model stops calling tools.

    Returns (final_text, updated_messages, last_prompt_tokens).
    """
    last_prompt_tokens = initial_prompt_tokens
    compact_threshold = max(4096, max_context_tokens - compact_reserve_tokens)

    while True:
        notice = drain_bash_events(kernel, messages)
        if notice and on_bash_notice:
            on_bash_notice(notice)

        if last_prompt_tokens >= compact_threshold and len(messages) > keep_recent_messages:
            messages, summarized, kept = compact_messages(
                client, model, messages, kernel, keep_recent=keep_recent_messages
            )
            if summarized > 0 and on_compaction:
                on_compaction(summarized, kept)

        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        try:
            assistant_dict, usage = _call_model(
                client,
                model,
                full_messages,
                stream=stream,
                on_status=on_status,
                on_text_delta=on_text_delta,
            )
        except BadRequestError as exc:
            err_msg = str(exc).lower()
            if ("context" in err_msg or "token" in err_msg or "length" in err_msg) and len(messages) > keep_recent_messages:
                messages, summarized, kept = compact_messages(
                    client, model, messages, kernel, keep_recent=keep_recent_messages
                )
                if summarized > 0:
                    if on_compaction:
                        on_compaction(summarized, kept)
                    full_messages = [{"role": "system", "content": system_prompt}, *messages]
                    assistant_dict, usage = _call_model(
                        client,
                        model,
                        full_messages,
                        stream=stream,
                        on_status=on_status,
                        on_text_delta=on_text_delta,
                    )
                else:
                    raise
            else:
                raise

        if usage and getattr(usage, "prompt_tokens", None):
            last_prompt_tokens = int(usage.prompt_tokens)
        if usage_stats is not None and usage is not None:
            usage_stats.update(usage)

        messages.append(assistant_dict)

        tool_calls = assistant_dict.get("tool_calls") or []
        if not tool_calls:
            return (assistant_dict.get("content") or "").strip(), messages, last_prompt_tokens

        for idx, tc in enumerate(tool_calls):
            fn_name = tc["function"]["name"]
            tc_id = tc["id"]
            if fn_name != "ipython":
                result_text = f"Unknown tool: {fn_name}. Only `ipython` is available."
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_text})
                continue

            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                code = args.get("code", "")
            except json.JSONDecodeError:
                code = tc["function"]["arguments"] or ""

            if on_tool_call:
                on_tool_call(code)

            if on_status:
                on_status("Running ipython...")
            try:
                cell_res = kernel.exec_cell(code)
            except KeyboardInterrupt:
                if on_status:
                    on_status(None)
                kernel.interrupt()
                # Keep message history valid so the next turn never has orphaned tool_calls.
                for rem_tc in tool_calls[idx:]:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": rem_tc["id"],
                            "content": "[interrupted by user]",
                        }
                    )
                raise
            finally:
                if on_status:
                    on_status(None)
            if on_tool_result:
                on_tool_result(cell_res)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": cell_res.format_for_model(),
                }
            )
