"""Hybrid model pricing registry and USD cost calculation."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

__all__ = [
    "MODEL_PRICING",
    "ModelPricing",
    "TurnUsage",
    "calculate_cost_usd",
    "format_cost_usd",
    "lookup_pricing",
]


@dataclasses.dataclass(frozen=True)
class ModelPricing:
    """USD price per 1M tokens for a model."""

    input_per_m: float
    cached_input_per_m: float
    output_per_m: float


# Google rates sourced from OpenRouter API (USD per 1M tokens).
# DeepSeek rates sourced from https://api-docs.deepseek.com/quick_start/pricing/ (PEAK pricing always).
MODEL_PRICING: dict[str, ModelPricing] = {
    # Google Gemini (OpenRouter rates)
    "gemini-4-neon": ModelPricing(input_per_m=3.0, cached_input_per_m=0.15, output_per_m=15.0),
    "gemini-4-hydrogen": ModelPricing(input_per_m=1.5, cached_input_per_m=0.075, output_per_m=7.5),
    "gemini-4-carbon": ModelPricing(input_per_m=2.0, cached_input_per_m=0.1, output_per_m=10.0),
    "gemini-4-argon": ModelPricing(input_per_m=2.0, cached_input_per_m=0.1, output_per_m=10.0),
    "gemini-3.8-flash": ModelPricing(input_per_m=0.825, cached_input_per_m=0.0825, output_per_m=4.125),
    "gemini-3.7-flash": ModelPricing(input_per_m=0.75, cached_input_per_m=0.075, output_per_m=3.75),
    "gemini-3.6-flash": ModelPricing(input_per_m=0.75, cached_input_per_m=0.075, output_per_m=3.75),
    "gemini-3.5-flash": ModelPricing(input_per_m=1.5, cached_input_per_m=0.15, output_per_m=9.0),
    "gemini-3.5-flash-lite": ModelPricing(input_per_m=0.3, cached_input_per_m=0.03, output_per_m=2.5),
    "gemini-3.1-pro-preview": ModelPricing(input_per_m=2.0, cached_input_per_m=0.2, output_per_m=12.0),
    "gemini-3.1-pro-preview-customtools": ModelPricing(input_per_m=2.0, cached_input_per_m=0.2, output_per_m=12.0),
    "gemini-3.1-flash-lite": ModelPricing(input_per_m=0.25, cached_input_per_m=0.025, output_per_m=1.5),
    "gemini-3-flash-preview": ModelPricing(input_per_m=0.5, cached_input_per_m=0.05, output_per_m=3.0),
    "gemini-2.5-pro": ModelPricing(input_per_m=1.25, cached_input_per_m=0.125, output_per_m=10.0),
    "gemini-2.5-flash": ModelPricing(input_per_m=0.3, cached_input_per_m=0.03, output_per_m=2.5),
    "gemini-2.5-flash-lite": ModelPricing(input_per_m=0.1, cached_input_per_m=0.01, output_per_m=0.4),
    # DeepSeek (Peak rates always)
    "deepseek-flash": ModelPricing(input_per_m=0.3, cached_input_per_m=0.006, output_per_m=1.2),
    "deepseek-v4-flash": ModelPricing(input_per_m=0.3, cached_input_per_m=0.006, output_per_m=1.2),
    "deepseek-v4-flash-vision-exp": ModelPricing(input_per_m=0.3, cached_input_per_m=0.006, output_per_m=1.2),
    "deepseek-v4-pro": ModelPricing(input_per_m=1.32, cached_input_per_m=0.044, output_per_m=3.96),
}


def lookup_pricing(model: str) -> ModelPricing | None:
    """Resolve model pricing by exact name, provider-stripped name, or longest base prefix."""
    normalized = model.strip().lower()
    if not normalized:
        return None
    if normalized in MODEL_PRICING:
        return MODEL_PRICING[normalized]

    # Strip provider prefix (e.g. "google/gemini-4-hydrogen" -> "gemini-4-hydrogen")
    bare = normalized.split("/")[-1]
    if bare in MODEL_PRICING:
        return MODEL_PRICING[bare]

    # Match dated/versioned aliases like "gemini-2.5-pro-05-06" by longest prefix
    best_match: str | None = None
    for key in MODEL_PRICING:
        if bare.startswith(f"{key}-") and (best_match is None or len(key) > len(best_match)):
            best_match = key
    return MODEL_PRICING[best_match] if best_match is not None else None


def calculate_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    provider_cost_usd: float | None = None,
) -> float:
    """Calculate USD cost, preferring provider-reported cost when available."""
    if provider_cost_usd is not None and provider_cost_usd >= 0.0:
        return float(provider_cost_usd)

    pricing = lookup_pricing(model)
    if pricing is None:
        return 0.0

    safe_input = max(0, int(input_tokens))
    safe_output = max(0, int(output_tokens))
    safe_cached = min(safe_input, max(0, int(cached_tokens)))
    uncached_input = safe_input - safe_cached

    return (
        uncached_input * pricing.input_per_m
        + safe_cached * pricing.cached_input_per_m
        + safe_output * pricing.output_per_m
    ) / 1_000_000.0


def format_cost_usd(cost_usd: float) -> str:
    """Format a USD cost compactly for footers and summaries."""
    if cost_usd <= 0.0:
        return "$0.00"
    if cost_usd < 0.01:
        return f"${cost_usd:.4f}"
    if cost_usd < 1.0:
        return f"${cost_usd:.3f}"
    return f"${cost_usd:.2f}"


@dataclasses.dataclass
class TurnUsage:
    """Cumulative token and USD cost usage across turns in a session."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    last_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    recorder: Callable[[Any, float], None] | None = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def update(self, usage: Any, *, model: str = "") -> float:
        if usage is None:
            return 0.0
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
        raw_cost = getattr(usage, "cost_usd", None)
        provider_cost = (
            float(raw_cost)
            if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
            else None
        )
        step_cost = calculate_cost_usd(
            model,
            input_tokens=pt,
            output_tokens=ct,
            cached_tokens=cached,
            provider_cost_usd=provider_cost,
        )
        if pt:
            self.prompt_tokens = pt
            self.cached_tokens = cached
        if ct:
            self.completion_tokens = ct
        self.total_prompt_tokens += pt
        self.total_completion_tokens += ct
        self.total_cached_tokens += cached
        self.last_cost_usd = step_cost
        self.total_cost_usd += step_cost
        if self.recorder is not None:
            self.recorder(usage, step_cost)
        return step_cost

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
            "last_cost_micros": round(self.last_cost_usd * 1_000_000),
            "total_cost_micros": round(self.total_cost_usd * 1_000_000),
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
            last_cost_usd=int(data.get("last_cost_micros", 0)) / 1_000_000.0,
            total_cost_usd=int(data.get("total_cost_micros", 0)) / 1_000_000.0,
        )
