"""Core agent loop (learn-claude-code style) with a single `ipython` tool."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from openai import BadRequestError, OpenAI

from agent.compact import compact_messages
from agent.kernel_client import CellResult, KernelClient

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
    on_tool_call: Callable[[str], None] | None = None,
    on_tool_result: Callable[[CellResult], None] | None = None,
    on_compaction: Callable[[int, int], None] | None = None,
    on_bash_notice: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    """Run one turn of the agent loop until the model stops calling tools.

    Returns (final_text, updated_messages, last_prompt_tokens).
    """
    last_prompt_tokens = 0
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

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_prompt}, *messages],
                tools=TOOLS,
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
                    response = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "system", "content": system_prompt}, *messages],
                        tools=TOOLS,
                    )
                else:
                    raise
            else:
                raise

        if response.usage and response.usage.prompt_tokens:
            last_prompt_tokens = response.usage.prompt_tokens

        assistant_msg = response.choices[0].message
        messages.append(_assistant_msg_to_dict(assistant_msg))

        if not assistant_msg.tool_calls:
            return (assistant_msg.content or "").strip(), messages, last_prompt_tokens

        for tc in assistant_msg.tool_calls:
            if tc.function.name != "ipython":
                result_text = f"Unknown tool: {tc.function.name}. Only `ipython` is available."
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})
                continue

            try:
                args = json.loads(tc.function.arguments or "{}")
                code = args.get("code", "")
            except json.JSONDecodeError:
                code = tc.function.arguments or ""

            if on_tool_call:
                on_tool_call(code)

            cell_res = kernel.exec_cell(code)
            if on_tool_result:
                on_tool_result(cell_res)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": cell_res.format_for_model(),
                }
            )
