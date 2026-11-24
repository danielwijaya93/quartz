"""Context compaction that preserves the Python kernel and reports surviving state.

Two levers, cheapest first:
  1. Clear tool results that are already far behind the keep window (lossless for
     decisions, very cheap).
  2. Summarize the older half of the conversation with the model.

Every call returns an explicit :class:`CompactionResult`; "nothing to do" is a
reported outcome, never a silent no-op.
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from quartz.providers.base import LLMProvider

from quartz.core.images import content_to_text
from quartz.kernel.base import KernelIntrospector

COMPACT_SYSTEM_PROMPT = (
    "You are summarizing an earlier portion of a coding agent's conversation. "
    "Preserve all important facts: the user's goal, key decisions, files read or modified, "
    "commands run and their outcomes, errors encountered, and remaining next steps. "
    "Be concise and factual."
)

# Rough token estimate. Deliberately conservative (over-estimates) so compaction
# fires slightly early rather than too late.
CHARS_PER_TOKEN = 4

# An inline image costs a fixed budget; base64 length would over-estimate wildly.
IMAGE_TOKEN_ESTIMATE = 1_600

# Tokens of recent conversation kept verbatim after a compaction.
DEFAULT_KEEP_RECENT_TOKENS = 32_000

# Tool results shorter than this are left alone; the placeholder is not shorter.
_MIN_CLEARABLE_TOOL_CHARS = 400

CLEARED_TOOL_PLACEHOLDER = "[tool output cleared to free context]"


class CompactionReason(enum.StrEnum):
    """What a compaction call actually did."""

    SUMMARIZED = "summarized"
    CLEARED_TOOL_RESULTS = "cleared-tool-results"
    NOTHING_TO_DO = "nothing-to-do"


@dataclass
class CompactionResult:
    """Outcome of one compaction attempt."""

    messages: list[dict[str, Any]]
    summarized: int = 0
    kept: int = 0
    cleared: int = 0
    reason: CompactionReason = CompactionReason.NOTHING_TO_DO
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0

    @property
    def changed(self) -> bool:
        return self.reason is not CompactionReason.NOTHING_TO_DO


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for a string using a conservative chars-per-token ratio."""
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _content_tokens(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                total += estimate_text_tokens(str(part))
                continue
            part_type = part.get("type")
            if part_type == "image_url" or "image_url" in part or part_type == "image" or "inlineData" in part:
                total += IMAGE_TOKEN_ESTIMATE
            else:
                total += estimate_text_tokens(str(part.get("text", "")))
        return total
    return estimate_text_tokens(str(content))


def message_tokens(message: dict[str, Any]) -> int:
    """Estimate the token cost of a single chat message including its tool calls."""
    total = 4  # per-message framing overhead
    total += _content_tokens(message.get("content"))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += estimate_text_tokens(str(function.get("name") or ""))
        total += estimate_text_tokens(str(function.get("arguments") or ""))
    return total


def estimate_context_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate tokens for a whole conversation."""
    return sum(message_tokens(message) for message in messages)


def strip_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return copies without ``reasoning`` (display-only field; never sent to models)."""
    if not any("reasoning" in m for m in messages):
        return messages
    return [{k: v for k, v in m.items() if k != "reasoning"} for m in messages]


def _keep_boundary(messages: list[dict[str, Any]], keep_recent_tokens: int) -> int:
    """Index of the first message kept verbatim, counted backwards by token budget."""
    if not messages:
        return 0
    total = 0
    index = len(messages)
    while index > 0 and total < keep_recent_tokens:
        index -= 1
        total += message_tokens(messages[index])
    # Always retain at least the final message so the index stays addressable.
    return min(index, len(messages) - 1)


def _find_safe_split_index(
    messages: list[dict[str, Any]], keep_recent_tokens: int
) -> int:
    """Split index that never orphans a tool result from its tool call.

    Walking back from a token boundary to the owning assistant message keeps
    tool-call/tool-result pairs together. Returns 0 when nothing can be split.
    """
    index = _keep_boundary(messages, keep_recent_tokens)
    while index > 0 and messages[index].get("role") == "tool":
        index -= 1
    return index if index > 0 else 0


def clear_old_tool_results(
    messages: list[dict[str, Any]], keep_recent_tokens: int
) -> tuple[list[dict[str, Any]], int]:
    """Replace tool output older than the keep window with a short placeholder.

    Returns (messages, cleared_count). Message objects are never mutated in place.
    """
    boundary = _find_safe_split_index(messages, keep_recent_tokens)
    if boundary <= 0:
        return messages, 0

    cleared = 0
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if (
            index < boundary
            and message.get("role") == "tool"
            and isinstance(content, str)
            and len(content) > _MIN_CLEARABLE_TOOL_CHARS
        ):
            cleared_message = dict(message)
            cleared_message["content"] = CLEARED_TOOL_PLACEHOLDER
            result.append(cleared_message)
            cleared += 1
        else:
            result.append(message)
    return result, cleared


def _serialize_for_summary(
    messages: list[dict[str, Any]],
    max_total_chars: int = 120_000,
) -> str:
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

    joined = "\n".join(lines)
    if len(joined) <= max_total_chars:
        return joined

    if max_total_chars <= 0:
        return ""

    head: list[str] = []
    start_idx = 0
    budget = max_total_chars
    marker_reserve = 64
    if lines and len(lines[0]) + marker_reserve <= max_total_chars // 3:
        head.append(lines[0])
        start_idx = 1
        budget -= len(lines[0]) + 1

    tail: list[str] = []
    tail_chars = 0
    end_idx = len(lines)
    while end_idx > start_idx:
        candidate = lines[end_idx - 1]
        needed = len(candidate) + (1 if tail else 0)
        if tail_chars + needed + marker_reserve > budget:
            break
        tail.append(candidate)
        tail_chars += needed
        end_idx -= 1
    tail.reverse()

    omitted = end_idx - start_idx
    parts: list[str] = []
    if head:
        parts.extend(head)
    if omitted > 0:
        parts.append(f"[... {omitted} older messages omitted ...]")
    parts.extend(tail)
    result = "\n".join(parts)
    if len(result) > max_total_chars:
        return result[:max_total_chars]
    return result


def format_python_state_notice(
    vars_info: dict[str, str], helper_names: list[str] | None = None
) -> str:
    """Format Unharnessed's [python-state] notice for the model."""
    parts: list[str] = ["[python-state]\n"]
    if not vars_info:
        parts.append(
            "Your Python kernel persisted through compaction, and currently has no user-defined variables."
        )
    else:
        names = ", ".join(f"{k} ({v})" for k, v in vars_info.items())
        parts.append(
            "Your Python kernel persisted through compaction; its remaining variables, imports, "
            f"and helpers are still available. These names are still defined: {names}."
        )
    if helper_names:
        parts.append(
            f"Loaded REPL helpers are also still available: {', '.join(f'{h}()' for h in sorted(helper_names))}."
        )
    return "\n".join(parts)


def _build_summary_messages(
    older: list[dict[str, Any]],
    focus: str | None,
) -> list[dict[str, Any]]:
    user_prompt = "Summarize this conversation history:\n\n" + _serialize_for_summary(older)
    if focus:
        user_prompt += f"\n\nFocus especially on: {focus}"
    return [
        {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


async def _summarize(
    client: LLMProvider,
    model: str,
    older: list[dict[str, Any]],
    focus: str | None,
) -> str:
    result = await client.complete(
        model=model,
        messages=_build_summary_messages(older, focus),
    )
    return result.text.strip()


async def compact_messages(
    client: LLMProvider,
    model: str,
    messages: list[dict[str, Any]],
    kernel: KernelIntrospector,
    *,
    budget_tokens: int | None = None,
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
    focus: str | None = None,
) -> CompactionResult:
    """Free context space while keeping recent turns and kernel state intact.

    Clearing old tool results runs first; a model-written summary runs only when
    that alone does not bring the estimate under `budget_tokens` (when given).
    """
    messages = strip_reasoning(messages)
    tokens_before = estimate_context_tokens(messages)
    cleared_messages, cleared = clear_old_tool_results(
        messages, keep_recent_tokens=keep_recent_tokens
    )
    tokens_after_clearing = estimate_context_tokens(cleared_messages)

    def _cleared_only() -> CompactionResult:
        reason = (
            CompactionReason.CLEARED_TOOL_RESULTS
            if cleared
            else CompactionReason.NOTHING_TO_DO
        )
        return CompactionResult(
            messages=cleared_messages,
            summarized=0,
            kept=len(cleared_messages),
            cleared=cleared,
            reason=reason,
            estimated_tokens_before=tokens_before,
            estimated_tokens_after=tokens_after_clearing,
        )

    if budget_tokens is not None and tokens_after_clearing <= budget_tokens:
        return _cleared_only()

    split_index = _find_safe_split_index(cleared_messages, keep_recent_tokens)
    if split_index <= 0:
        return _cleared_only()

    older = cleared_messages[:split_index]
    recent = cleared_messages[split_index:]
    summary_text = await _summarize(client, model, older, focus)

    vars_info = await asyncio.to_thread(kernel.list_vars)
    try:
        helpers_data = await asyncio.to_thread(kernel.list_helpers)
        custom_helpers = [
            h["name"]
            for h in helpers_data.get("helpers", [])
            if h.get("source") in ("global", "project")
        ]
    except Exception:
        custom_helpers = []
    state_notice = format_python_state_notice(vars_info, helper_names=custom_helpers)

    compacted_header = {
        "role": "user",
        "content": f"[compaction-summary]\n\n{summary_text}\n\n{state_notice}",
    }
    new_messages = [compacted_header, *recent]
    return CompactionResult(
        messages=new_messages,
        summarized=len(older),
        kept=len(recent),
        cleared=cleared,
        reason=CompactionReason.SUMMARIZED,
        estimated_tokens_before=tokens_before,
        estimated_tokens_after=estimate_context_tokens(new_messages),
    )
