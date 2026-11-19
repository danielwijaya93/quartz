"""Context compaction that preserves the Python kernel and reports surviving state."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import OpenAI

from agent.images import content_to_text
from agent.kernel_client import KernelClient

COMPACT_SYSTEM_PROMPT = (
    "You are summarizing an earlier portion of a coding agent's conversation. "
    "Preserve all important facts: the user's goal, key decisions, files read or modified, "
    "commands run and their outcomes, errors encountered, and remaining next steps. "
    "Be concise and factual."
)


def _find_safe_split_index(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """Find a split index >= 0 that lands on a user message so tool-call pairs are never split."""
    if len(messages) <= keep_recent:
        return 0
    target = len(messages) - keep_recent
    for idx in range(target, -1, -1):
        if messages[idx].get("role") == "user":
            return idx
    return 0


def _serialize_for_summary(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = content_to_text(content)
        if role == "assistant" and msg.get("tool_calls"):
            calls = [
                f"{tc.get('function', {}).get('name')}({tc.get('function', {}).get('arguments', '')[:400]})"
                for tc in msg["tool_calls"]
            ]
            lines.append(f"[assistant tool_calls]: {'; '.join(calls)}")
        elif role == "tool":
            lines.append(f"[tool result]: {str(content)[:800]}")
        else:
            lines.append(f"[{role}]: {str(content)[:1500]}")
    return "\n".join(lines)


def format_python_state_notice(vars_info: dict[str, str]) -> str:
    """Format Unharnessed's [python-state] notice for the model."""
    if not vars_info:
        return (
            "[python-state]\n\n"
            "Your Python kernel persisted through compaction, and currently has no user-defined variables."
        )
    names = ", ".join(f"{k} ({v})" for k, v in vars_info.items())
    return (
        "[python-state]\n\n"
        "Your Python kernel persisted through compaction; its remaining variables, imports, "
        f"and helpers are still available. These names are still defined: {names}."
    )


def compact_messages(
    client: OpenAI,
    model: str,
    messages: list[dict[str, Any]],
    kernel: KernelClient,
    keep_recent: int = 10,
    focus: str | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Summarize older messages while keeping recent turns and kernel state intact.

    Returns (new_messages, summarized_count, kept_count).
    """
    split_idx = _find_safe_split_index(messages, keep_recent)
    if split_idx <= 0:
        return messages, 0, len(messages)

    older = messages[:split_idx]
    recent = messages[split_idx:]

    transcript = _serialize_for_summary(older)
    user_prompt = "Summarize this conversation history:\n\n" + transcript
    if focus:
        user_prompt += f"\n\nFocus especially on: {focus}"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    summary_text = (response.choices[0].message.content or "").strip()

    vars_info = kernel.list_vars()
    state_notice = format_python_state_notice(vars_info)

    compacted_header = {
        "role": "user",
        "content": (
            f"[compaction-summary]\n\n{summary_text}\n\n{state_notice}"
        ),
    }
    new_messages = [compacted_header, *recent]
    return new_messages, len(older), len(recent)
