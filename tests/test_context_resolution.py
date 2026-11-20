"""Tests for per-model context-window resolution (discovery > static > default)."""

from __future__ import annotations

from typing import Any

import pytest

import agent.models as models
from agent.models import (
    DEFAULT_CONTEXT_TOKENS,
    PROVIDERS,
    resolve_context_tokens,
)
from agent.openai_client import ModelDetails, parse_model_detail


class _DetailsClient:
    """Fake OpenAI-compatible client exposing list_model_details()."""

    def __init__(self, details: list[Any]) -> None:
        self._details = details

    def list_model_details(self, raw: list[Any] | None = None):  # noqa: ARG002
        return list(self._details)


class _ListModelsClient:
    """Fake client exposing only list_models() returning dicts."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def list_models(self) -> list[Any]:
        return list(self._items)


class _RaisingClient:
    def list_model_details(self, raw: list[Any] | None = None):  # noqa: ARG002
        raise RuntimeError("network down")


class _StringIdsClient:
    """Client whose list_models returns plain strings (no context info)."""

    def list_models(self) -> list[str]:
        return ["gpt-4.1", "unknown-model"]


def test_parse_model_detail_context_length_openrouter_shape() -> None:
    details = parse_model_detail(
        {"id": "anthropic/claude-sonnet-4", "context_length": 200_000}
    )
    assert details is not None
    assert details.context_length == 200_000


def test_parse_model_detail_context_length_alias_and_types() -> None:
    alias = parse_model_detail({"id": "x", "max_context_tokens": 64000})
    assert alias is not None and alias.context_length == 64_000

    as_str = parse_model_detail({"id": "x", "context_length": "128000"})
    assert as_str is not None and as_str.context_length == 128_000

    as_float = parse_model_detail({"id": "x", "context_length": 256000.0})
    assert as_float is not None and as_float.context_length == 256_000

    absent = parse_model_detail({"id": "x"})
    assert absent is not None and absent.context_length is None

    bad = parse_model_detail({"id": "x", "context_length": "not-a-number"})
    assert bad is not None and bad.context_length is None

    bool_val = parse_model_detail({"id": "x", "context_length": True})
    assert bool_val is not None and bool_val.context_length is None

    zero = parse_model_detail({"id": "x", "context_length": 0})
    assert zero is not None and zero.context_length is None


def test_model_details_default_context_length_none() -> None:
    assert ModelDetails(id="x").context_length is None


def test_resolve_static_table_entries() -> None:
    assert resolve_context_tokens("openai", "gpt-4.1") == 1_047_576
    assert resolve_context_tokens("openai", "gpt-4.1-mini") == 1_047_576
    assert resolve_context_tokens("openai", "o3") == 200_000
    assert resolve_context_tokens("openai", "o4-mini") == 200_000
    assert resolve_context_tokens("openrouter", "anthropic/claude-sonnet-4") == 200_000
    assert resolve_context_tokens("openrouter", "openai/gpt-4.1") == 1_047_576
    assert resolve_context_tokens("openrouter", "google/gemini-2.5-flash") == 1_048_576
    assert resolve_context_tokens("google", "gemini-3.8-flash-medium") == 1_048_576
    assert resolve_context_tokens("google", "gemini-3.8-flash-high") == 1_048_576
    assert resolve_context_tokens("google", "gemini-3.8-flash-low") == 1_048_576
    assert resolve_context_tokens("google", "gemini-3-flash") == 1_048_576
    assert resolve_context_tokens("google", "gemini-2.5-pro") == 1_048_576
    assert resolve_context_tokens("google", "gemini-2.5-flash") == 1_048_576
    assert resolve_context_tokens("deepseek", "deepseek-chat") == 64_000
    assert resolve_context_tokens("deepseek", "deepseek-reasoner") == 64_000
    assert resolve_context_tokens("deepseek", "deepseek-flash") == 64_000


def test_resolve_provider_default_for_unknown_model() -> None:
    # Unknown model under a known provider -> provider default (128k).
    assert resolve_context_tokens("openai", "some-future-model") ==         PROVIDERS["openai"].default_context_tokens == 128_000
    assert resolve_context_tokens("google", "some-future-model") == 128_000


def test_resolve_unknown_provider_falls_back_to_128k() -> None:
    assert resolve_context_tokens("nope", "also-unknown") == DEFAULT_CONTEXT_TOKENS == 128_000


def test_resolve_discovered_beats_static() -> None:
    client = _DetailsClient(
        [ModelDetails(id="gpt-4.1", context_length=33_333)]
    )
    assert resolve_context_tokens("openai", "gpt-4.1", client) == 33_333


def test_resolve_discovered_for_unknown_model() -> None:
    client = _DetailsClient(
        [ModelDetails(id="some-future-model", context_length=777_777)]
    )
    assert resolve_context_tokens("openai", "some-future-model", client) == 777_777


def test_resolve_discovered_none_falls_through_to_static() -> None:
    client = _DetailsClient([ModelDetails(id="gpt-4.1", context_length=None)])
    assert resolve_context_tokens("openai", "gpt-4.1", client) == 1_047_576

    mismatched = _DetailsClient(
        [ModelDetails(id="other-model", context_length=111_111)]
    )
    assert resolve_context_tokens("openai", "gpt-4.1", mismatched) == 1_047_576


def test_resolve_discovered_dicts_and_list_models_shapes() -> None:
    dict_client = _DetailsClient(
        [{"id": "gpt-4.1", "context_length": 222_222}]
    )
    assert resolve_context_tokens("openai", "gpt-4.1", dict_client) == 222_222

    list_client = _ListModelsClient(
        [{"id": "gpt-4.1", "max_context_tokens": 333_333}]
    )
    assert resolve_context_tokens("openai", "gpt-4.1", list_client) == 333_333


def test_resolve_non_throwing_on_client_errors() -> None:
    # Raising /models fetch -> fall through to static table, never raise.
    assert resolve_context_tokens("openai", "gpt-4.1", _RaisingClient()) == 1_047_576
    assert resolve_context_tokens("openai", "unknown-model", _RaisingClient()) == 128_000
    # Plain-string list_models (no context info) -> static table.
    assert resolve_context_tokens("openai", "gpt-4.1", _StringIdsClient()) == 1_047_576
    # None / garbage clients -> static table.
    assert resolve_context_tokens("openai", "gpt-4.1", None) == 1_047_576
    assert resolve_context_tokens("openai", "gpt-4.1", object()) == 1_047_576


def test_max_context_tokens_env_no_longer_affects_resolution(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAX_CONTEXT_TOKENS is ignored: resolution never reads the env."""
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "999999999")
    assert resolve_context_tokens("openai", "gpt-4.1") == 1_047_576
    assert resolve_context_tokens("deepseek", "deepseek-flash") == 64_000
    assert resolve_context_tokens("openai", "unknown-model") == 128_000

    # And the CLI budget helper no longer reads it either.
    from agent.cli import _context_budget_from_settings
    from agent.compact import DEFAULT_KEEP_RECENT_TOKENS

    monkeypatch.delenv("COMPACT_RESERVE_TOKENS", raising=False)
    monkeypatch.delenv("COMPACT_KEEP_RECENT_TOKENS", raising=False)
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "nope.json"))
    _, reserve, keep = _context_budget_from_settings()
    assert reserve == 16_384
    assert keep == DEFAULT_KEEP_RECENT_TOKENS
    # Resolution module must not reference the removed env var.
    assert "MAX_CONTEXT_TOKENS" not in open(models.__file__).read()


def test_context_budget_ignores_compaction_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """COMPACT_* env vars are silently ignored; settings come from config.json."""
    from agent.cli import _context_budget_from_settings

    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setenv("COMPACT_RESERVE_TOKENS", "999")
    monkeypatch.setenv("COMPACT_KEEP_RECENT_TOKENS", "111")
    _, reserve, keep = _context_budget_from_settings()
    assert (reserve, keep) == (16_384, 32_000)
