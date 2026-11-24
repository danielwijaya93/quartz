"""Tests for token-budgeted compaction, tool-result clearing, and overflow recovery."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import RecordingObserver

from quartz.core.compact import (
    CLEARED_TOOL_PLACEHOLDER,
    CompactionReason,
    _find_safe_split_index,
    clear_old_tool_results,
    compact_messages,
    estimate_context_tokens,
    estimate_text_tokens,
    message_tokens,
)
from quartz.core.loop import TurnAborted, agent_loop
from quartz.errors import ContextOverflowError, ProviderError, is_context_overflow

# --------------------------------------------------------------------------- #
# Token estimation
# --------------------------------------------------------------------------- #


def test_estimate_text_tokens_is_conservative() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcd") == 1
    # Rounds up, never down.
    assert estimate_text_tokens("abcde") == 2


def test_message_tokens_counts_content_tool_calls_and_images() -> None:
    plain = {"role": "user", "content": "x" * 400}
    with_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "ipython", "arguments": "y" * 400}}],
    }
    with_image = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    }

    assert message_tokens(plain) > 100
    assert message_tokens(with_call) > 100
    # A base64 image must not be charged by its string length.
    assert message_tokens(with_image) < 2_000


# --------------------------------------------------------------------------- #
# Split point: the regression that silently disabled compaction
# --------------------------------------------------------------------------- #


def _tool_loop_turn(steps: int, payload: str = "z" * 2_000) -> list[dict[str, Any]]:
    """One user message followed by an autonomous multi-step tool loop."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": "do the task"}]
    for index in range(steps):
        call_id = f"call_{index}"
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": call_id, "function": {"name": "ipython", "arguments": "{}"}}
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": payload})
    return messages


def test_split_works_inside_a_long_tool_loop_with_no_user_message() -> None:
    messages = _tool_loop_turn(steps=20)
    split = _find_safe_split_index(messages, keep_recent_tokens=8_000)

    # Old implementation searched backwards for a `user` message, found only
    # index 0 and returned 0 (treated as "nothing to compact"), so context grew
    # unbounded during long turns.
    assert split > 0, "compaction must be possible inside a tool loop"
    assert 0 < split < len(messages)


def test_split_never_orphans_a_tool_result() -> None:
    messages = _tool_loop_turn(steps=12)
    for budget in range(0, 30_000, 500):
        split = _find_safe_split_index(messages, keep_recent_tokens=budget)
        if split == 0:
            continue
        assert messages[split]["role"] != "tool", f"budget={budget} orphaned a tool result"
        if messages[split]["role"] == "assistant" and messages[split].get("tool_calls"):
            assert messages[split + 1]["role"] == "tool"


def test_split_returns_zero_when_everything_fits() -> None:
    messages = _tool_loop_turn(steps=2)
    assert _find_safe_split_index(messages, keep_recent_tokens=10_000_000) == 0


# --------------------------------------------------------------------------- #
# Tool-result clearing (cheapest lever)
# --------------------------------------------------------------------------- #


def test_clear_old_tool_results_only_touches_old_long_outputs() -> None:
    messages = _tool_loop_turn(steps=6, payload="q" * 2_000)
    messages.append({"role": "tool", "tool_call_id": "recent", "content": "short"})
    original = [dict(message) for message in messages]

    cleared_messages, cleared = clear_old_tool_results(messages, keep_recent_tokens=1_000)

    assert cleared > 0
    assert cleared_messages[-1]["content"] == "short"
    # Input is never mutated in place.
    assert messages == original
    placeholders = [
        message for message in cleared_messages if message.get("content") == CLEARED_TOOL_PLACEHOLDER
    ]
    assert len(placeholders) == cleared


def test_clear_old_tool_results_is_a_noop_when_everything_fits() -> None:
    messages = _tool_loop_turn(steps=2)
    cleared_messages, cleared = clear_old_tool_results(messages, keep_recent_tokens=10_000_000)
    assert cleared == 0
    assert cleared_messages is messages


# --------------------------------------------------------------------------- #
# compact_messages outcomes are always explicit
# --------------------------------------------------------------------------- #


class _ExplodingClient:
    """Fails the test if a summary request is attempted."""

    def __init__(self) -> None:
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("summarizer should not have been called")


class _StubKernel:
    def list_vars(self) -> dict[str, str]:
        return {}

    def drain_events(self) -> list[dict[str, Any]]:
        return []


async def test_compact_reports_nothing_to_do_instead_of_silently_failing() -> None:
    messages = _tool_loop_turn(steps=2)
    result = await compact_messages(
        _ExplodingClient(),  # type: ignore[arg-type]
        "m",
        messages,
        _StubKernel(),  # type: ignore[arg-type]
        keep_recent_tokens=10_000_000,
    )
    assert result.reason is CompactionReason.NOTHING_TO_DO
    assert result.changed is False
    assert result.messages is messages


async def test_compact_clears_tool_results_when_that_is_enough() -> None:
    messages = _tool_loop_turn(steps=8, payload="w" * 3_000)
    tokens_before = estimate_context_tokens(messages)
    result = await compact_messages(
        _ExplodingClient(),  # type: ignore[arg-type]
        "m",
        messages,
        _StubKernel(),  # type: ignore[arg-type]
        budget_tokens=tokens_before - 1,
        keep_recent_tokens=2_000,
    )
    assert result.reason is CompactionReason.CLEARED_TOOL_RESULTS
    assert result.cleared > 0
    assert result.estimated_tokens_after < tokens_before


# --------------------------------------------------------------------------- #
# Provider error classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        '{"error":{"code":"context_length_exceeded","message":"too long"}}',
        "This model's maximum context length is 128000 tokens.",
        "The input token count (200000) exceeds the maximum number of tokens allowed (128000).",
    ],
)
def test_is_context_overflow_matches_real_provider_bodies(body: str) -> None:
    assert is_context_overflow(400, body) is True


@pytest.mark.parametrize(
    "status,body",
    [
        (400, '{"error":{"message":"Incorrect API key provided"}}'),
        (401, "unauthorized"),
        (400, '{"error":{"message":"invalid token in field x"}}'),
        (429, "context window rate limited"),
    ],
)
def test_is_context_overflow_ignores_unrelated_errors(status: int, body: str) -> None:
    assert is_context_overflow(status, body) is False


# --------------------------------------------------------------------------- #
# Loop-level overflow recovery
# --------------------------------------------------------------------------- #


def _text_turn_response(text: str) -> Any:
    from quartz.providers.base import CompletionResult, Usage

    return CompletionResult(text=text, tool_calls=[], usage=Usage())


class _OverflowClient:
    """First model call overflows, then everything succeeds."""

    def __init__(self) -> None:
        self.model_calls = 0
        self.summary_calls = 0

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: Any = None,
        *,
        effort: str | None = None,
    ) -> Any:
        del model, messages, effort
        if tools is None:  # summarizer request
            self.summary_calls += 1
            return _text_turn_response("SUMMARY")
        self.model_calls += 1
        if self.model_calls == 1:
            raise ContextOverflowError(
                "Prompt exceeds the model context window",
                provider="test",
                status_code=400,
                detail="maximum context length is 128000 tokens",
            )
        return _text_turn_response("recovered")


async def test_loop_compacts_and_retries_once_on_context_overflow() -> None:
    client = _OverflowClient()
    messages = _tool_loop_turn(steps=10)
    observer = RecordingObserver()

    answer, updated, _ = await agent_loop(
        client,  # type: ignore[arg-type]
        "m",
        "system",
        messages,
        _StubKernel(),  # type: ignore[arg-type]
        stream=False,
        keep_recent_tokens=4_000,
        observer=observer,
    )

    assert answer == "recovered"
    assert client.model_calls == 2
    assert client.summary_calls == 1
    assert len(observer.compactions) == 1
    assert observer.compactions[0].reason is CompactionReason.SUMMARIZED
    assert updated[0]["content"].startswith("[compaction-summary]")


class _AlwaysOverflowClient:
    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: Any = None,
        *,
        effort: str | None = None,
    ) -> Any:
        del model, messages, effort
        if tools is None:
            return _text_turn_response("SUMMARY")
        raise ContextOverflowError("still too long", provider="test", status_code=400)


async def test_loop_raises_after_a_single_overflow_recovery() -> None:
    with pytest.raises(ContextOverflowError):
        await agent_loop(
            _AlwaysOverflowClient(),  # type: ignore[arg-type]
            "m",
            "system",
            _tool_loop_turn(steps=10),
            _StubKernel(),  # type: ignore[arg-type]
            stream=False,
            keep_recent_tokens=4_000,
        )


def test_provider_error_str_includes_status_and_detail() -> None:
    error = ProviderError("boom", provider="openai", status_code=500, detail="server error")
    assert str(error) == "boom (status 500): server error"
    assert isinstance(error, Exception)


def test_turn_aborted_is_not_a_provider_error() -> None:
    assert not issubclass(TurnAborted, ProviderError)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_render_compaction_distinguishes_summary_from_clearing(monkeypatch: Any) -> None:
    import io

    from rich.console import Console

    import quartz.tui.render as ui
    from quartz.core.compact import CompactionReason, CompactionResult
    from quartz.tui.render import BlockPrinter, _render_compaction

    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=200, color_system=None)
    monkeypatch.setattr(ui, "printer", BlockPrinter(test_console))

    _render_compaction(
        CompactionResult(
            messages=[],
            summarized=12,
            kept=8,
            reason=CompactionReason.SUMMARIZED,
            estimated_tokens_before=90_000,
            estimated_tokens_after=30_000,
        )
    )
    _render_compaction(
        CompactionResult(
            messages=[],
            cleared=5,
            reason=CompactionReason.CLEARED_TOOL_RESULTS,
            estimated_tokens_before=60_000,
            estimated_tokens_after=45_000,
        )
    )
    _render_compaction(CompactionResult(messages=[]))  # nothing-to-do renders nothing

    output = buf.getvalue()
    assert "summarized 12 older messages" in output
    assert "cleared 5 stale tool results" in output
    assert "60.0k tokens freed" in output
    assert output.count("· compact ·") == 2
