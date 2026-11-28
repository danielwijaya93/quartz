"""Tests for hybrid model pricing and cost calculation."""

from __future__ import annotations

import pytest

from quartz.core.pricing import calculate_cost_usd, format_cost_usd, lookup_pricing


def test_google_openrouter_pricing_lookup_and_cost() -> None:
    # gemini-4-hydrogen: $1.50/1M input, $0.075/1M cached, $7.50/1M output
    pricing = lookup_pricing("google/gemini-4-hydrogen")
    assert pricing is not None
    assert pricing.input_per_m == pytest.approx(1.5)
    assert pricing.cached_input_per_m == pytest.approx(0.075)
    assert pricing.output_per_m == pytest.approx(7.5)

    # 1M total input (400k cached, 600k uncached) + 200k output
    cost = calculate_cost_usd(
        "gemini-4-hydrogen",
        input_tokens=1_000_000,
        cached_tokens=400_000,
        output_tokens=200_000,
    )
    expected = 0.6 * 1.5 + 0.4 * 0.075 + 0.2 * 7.5
    assert cost == pytest.approx(expected)


def test_deepseek_peak_pricing_and_legacy_alias() -> None:
    # deepseek-flash peak: $0.30/1M miss, $0.006/1M hit, $1.20/1M output
    cost_flash = calculate_cost_usd(
        "deepseek-flash",
        input_tokens=1_000_000,
        cached_tokens=500_000,
        output_tokens=1_000_000,
    )
    assert cost_flash == pytest.approx(0.5 * 0.30 + 0.5 * 0.006 + 1.0 * 1.20)

    # Legacy alias maps to deepseek-flash pricing
    assert lookup_pricing("deepseek-v4-flash") == lookup_pricing("deepseek-flash")

    # deepseek-v4-pro peak: $1.32/1M miss, $0.044/1M hit, $3.96/1M output
    cost_pro = calculate_cost_usd(
        "deepseek/deepseek-v4-pro",
        input_tokens=1_000_000,
        cached_tokens=0,
        output_tokens=1_000_000,
    )
    assert cost_pro == pytest.approx(1.32 + 3.96)


def test_provider_reported_cost_overrides_local_table() -> None:
    cost = calculate_cost_usd(
        "gemini-4-hydrogen",
        input_tokens=1_000_000,
        output_tokens=500_000,
        provider_cost_usd=0.042,
    )
    assert cost == pytest.approx(0.042)


def test_unknown_model_returns_zero_without_raising() -> None:
    assert lookup_pricing("unknown/nonexistent-model-x") is None
    assert (
        calculate_cost_usd(
            "unknown/nonexistent-model-x",
            input_tokens=10_000,
            output_tokens=5_000,
        )
        == 0.0
    )


def test_negative_and_overflow_cached_tokens_are_clamped() -> None:
    cost = calculate_cost_usd(
        "deepseek-flash",
        input_tokens=100_000,
        cached_tokens=500_000,  # exceeds input_tokens
        output_tokens=-50,
    )
    # All 100k input treated as cached, output clamped to 0
    assert cost == pytest.approx(0.1 * 0.006)


def test_versioned_model_suffix_matches_base_pricing() -> None:
    assert lookup_pricing("gemini-2.5-pro-05-06") == lookup_pricing("gemini-2.5-pro")


def test_format_cost_usd() -> None:
    assert format_cost_usd(0.0) == "$0.00"
    assert format_cost_usd(0.00042) == "$0.0004"
    assert format_cost_usd(0.1234) == "$0.123"
    assert format_cost_usd(1.5) == "$1.50"


def test_turn_usage_tracks_and_roundtrips_cost() -> None:
    from quartz.core.loop import TurnUsage
    from quartz.providers.base import Usage
    from quartz.tui.render import _format_turn_footer

    tu = TurnUsage()
    step = tu.update(
        Usage(input_tokens=1_000_000, output_tokens=200_000, cached_tokens=400_000),
        model="gemini-4-hydrogen",
    )
    expected = 0.6 * 1.5 + 0.4 * 0.075 + 0.2 * 7.5
    assert step == pytest.approx(expected)
    assert tu.total_cost_usd == pytest.approx(expected)

    restored = TurnUsage.from_dict(tu.to_dict())
    assert restored.total_cost_usd == pytest.approx(expected, abs=1e-6)
    assert "$2.43" in _format_turn_footer(restored, 128_000)


def test_openai_parse_usage_extracts_provider_cost() -> None:
    from quartz.providers.openai import _parse_usage

    parsed = _parse_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 250,
            "prompt_tokens_details": {"cached_tokens": 200},
            "cost": 0.0123,
        }
    )
    assert parsed is not None
    assert parsed.cached_tokens == 200
    assert parsed.cost_usd == pytest.approx(0.0123)
