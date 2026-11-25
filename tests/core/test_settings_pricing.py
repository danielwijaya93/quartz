"""Red-first tests for user-editable model pricing, cost resolution, and batch settings."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.bots.budgets import UsageStore, resolve_usage_cost
from quartz.settings import DEFAULT_BOT_MAX_USD_PER_DAY, ModelPricingRule, Settings, load_settings, save_settings
from quartz.storage import QuartzDB
from quartz.web.app import create_app


class _FakeRuntime:
    def __init__(self, cwd: Path | None = None) -> None:
        self.compact_reserve_tokens = 16_384
        self.keep_recent_tokens = 32_000
        self.progress_every = 5
        self.background_check_every = 300
        self.notifications = True
        self.kernel = SimpleNamespace(configure=lambda **_kw: None)
        self.cwd = cwd or Path(".")
        self.system_prompt = ""
        self.base_system_prompt = ""
        self.system_prompt_append = ""
        self.active_session = SimpleNamespace(id="sess-test", cwd=self.cwd)

    def close(self) -> None:
        return None


def _fake_runtime(cwd: Path | None = None) -> Any:
    return _FakeRuntime(cwd=cwd)


def test_defaults_include_five_dollar_bot_budget_and_pricing_fields(tmp_path: Path) -> None:
    assert DEFAULT_BOT_MAX_USD_PER_DAY == 5.0
    loaded = load_settings(path=tmp_path / "missing.json")
    assert loaded.bot_max_usd_per_day == 5.0
    assert loaded.track_chat_usage is True
    assert loaded.unpriced_fallback_input_per_m == 1.0
    assert loaded.unpriced_fallback_output_per_m == 5.0
    assert loaded.model_pricing == ()


def test_settings_persists_model_pricing_and_fallback(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    s = Settings(
        model_pricing=(
            ModelPricingRule("google/gemini-4-*", input_per_m=0.5, cached_input_per_m=0.125, output_per_m=2.0),
            ModelPricingRule("anthropic/claude-opus-4", input_per_m=15.0, cached_input_per_m=1.5, output_per_m=75.0),
        ),
        unpriced_fallback_input_per_m=0.75,
        unpriced_fallback_output_per_m=3.0,
        track_chat_usage=False,
    )
    save_settings(s, path=cfg)
    loaded = load_settings(path=cfg)
    assert len(loaded.model_pricing) == 2
    assert loaded.model_pricing[0] == ModelPricingRule(
        "google/gemini-4-*", input_per_m=0.5, cached_input_per_m=0.125, output_per_m=2.0
    )
    assert loaded.unpriced_fallback_input_per_m == 0.75
    assert loaded.unpriced_fallback_output_per_m == 3.0
    assert loaded.track_chat_usage is False


def test_resolve_usage_cost_priority_and_cache_write_at_input_rate() -> None:
    rules = (
        ModelPricingRule("google/*", input_per_m=1.0, cached_input_per_m=0.25, output_per_m=4.0),
        ModelPricingRule("google/gemini-4-hydrogen", input_per_m=0.5, cached_input_per_m=0.1, output_per_m=2.0),
    )

    # 1. Provider-reported cost wins over rules
    prov = resolve_usage_cost(
        provider="google",
        model="gemini-4-hydrogen",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        provider_cost_usd=0.42,
        rules=rules,
    )
    assert prov.source == "provider"
    assert prov.cost_usd == 0.42

    # 2. Exact provider/model rule beats wildcard; cache_write billed at input_per_m
    exact = resolve_usage_cost(
        provider="google",
        model="gemini-4-hydrogen",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        rules=rules,
    )
    assert exact.source == "rule"
    assert exact.matched_pattern == "google/gemini-4-hydrogen"
    # 0.5 (in) + 2.0 (out) + 0.1 (cached) + 0.5 (cache_write at input rate) = 3.1
    assert exact.cost_usd == pytest.approx(3.1)

    # 3. Wildcard match
    wild = resolve_usage_cost(
        provider="google",
        model="gemini-4-flash",
        input_tokens=1_000_000,
        output_tokens=0,
        rules=rules,
    )
    assert wild.source == "rule"
    assert wild.matched_pattern == "google/*"
    assert wild.cost_usd == pytest.approx(1.0)

    # 4. Unknown model uses configurable fallback
    fb = resolve_usage_cost(
        provider="local",
        model="llama-4",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        rules=rules,
        fallback_input_per_m=0.2,
        fallback_output_per_m=0.8,
    )
    assert fb.source == "fallback"
    assert fb.cost_usd == pytest.approx(1.0)


def test_usage_store_splits_bot_vs_chat_and_lists_recent_models(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    try:
        store = UsageStore(db=db)
        now = 1_750_000_000.0
        store.record_usage("google", "gemini-4-hydrogen", 1000, 500, bot_id="bot-1", cost_usd=0.25, ts=now - 60)
        store.record_usage("openai", "gpt-5-nano", 2000, 1000, session_id="sess-1", bot_id=None, cost_usd=0.10, ts=now - 30)
        summary = store.summary(since_ts=now - 3600)
        assert summary["botCostUsd"] == pytest.approx(0.25)
        assert summary["chatCostUsd"] == pytest.approx(0.10)
        assert summary["costUsd"] == pytest.approx(0.35)
        recent = store.recent_models()
        assert {"provider": "google", "model": "gemini-4-hydrogen"} in recent
        assert {"provider": "openai", "model": "gpt-5-nano"} in recent
    finally:
        db.close()


def test_web_settings_batch_update_and_pricing_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_PATH", str(cfg))

    def _dummy_build(_sid: str | None = None, _cwd: Path | None = None) -> tuple[Any, Any]:
        return _fake_runtime(cwd=tmp_path), _FakeRuntime(cwd=tmp_path)

    token = "tok-settings-pricing"
    app = create_app(cwd=tmp_path, build=_dummy_build, auth_token=token, enable_bot_runner=False)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with TestClient(app) as client:
        r_get = client.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
        assert r_get.status_code == 200
        body = r_get.json()
        assert body["defaults"]["botMaxUsdPerDay"] == 5.0
        assert body["values"]["trackChatUsage"] is True
        assert isinstance(body["recentModels"], list)

        # Batch update multiple settings + model pricing in one request
        r_batch = client.post(
            "/api/settings",
            headers=headers,
            json={
                "updates": {
                    "reserve": "20000",
                    "bot_budget": "12.5",
                    "track_chat_usage": "off",
                    "unpriced_fallback": {"inputPerM": 0.5, "outputPerM": 2.5},
                    "model_pricing": [
                        {
                            "pattern": "google/gemini-4-hydrogen",
                            "inputPerM": 0.4,
                            "cachedInputPerM": 0.1,
                            "outputPerM": 1.6,
                        }
                    ],
                }
            },
        )
        assert r_batch.status_code == 200
        vals = r_batch.json()["values"]
        assert vals["reserve"] == 20000
        assert vals["botMaxUsdPerDay"] == 12.5
        assert vals["trackChatUsage"] is False
        assert vals["unpricedFallback"] == {"inputPerM": 0.5, "outputPerM": 2.5}
        assert vals["modelPricing"] == [
            {
                "pattern": "google/gemini-4-hydrogen",
                "inputPerM": 0.4,
                "cachedInputPerM": 0.1,
                "outputPerM": 1.6,
            }
        ]

        # Reject negative rates
        r_bad = client.post(
            "/api/settings",
            headers=headers,
            json={
                "key": "model_pricing",
                "value": [{"pattern": "google/*", "inputPerM": -1, "cachedInputPerM": 0, "outputPerM": 1}],
            },
        )
        assert r_bad.status_code == 400
