"""Tests for per-model reasoning-effort discovery (OpenRouter / DeepSeek /models metadata)."""

from __future__ import annotations

from typing import Any

import pytest

import agent.models as models
from agent.openai_client import ModelDetails, parse_model_detail, parse_model_details


@pytest.fixture(autouse=True)
def _enable_effort_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery tests must run enabled regardless of outer CI/env settings."""
    monkeypatch.setenv("QUARTZ_EFFORT_DISCOVERY", "1")
    models.clear_effort_cache()

OPENROUTER_ITEM = {
    "id": "meta/muse-ember",
    "reasoning": {
        "mandatory": True,
        "supported_efforts": ["max", "xhigh", "high", "medium", "low", "minimal"],
        "default_effort": "max",
    },
    "supported_parameters": [
        "include_reasoning",
        "max_tokens",
        "reasoning",
        "reasoning_effort",
        "tools",
    ],
}

DEEPSEEK_ITEM = {
    "id": "deepseek-flash",
    "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
}


def test_parse_openrouter_shape() -> None:
    details = parse_model_detail(OPENROUTER_ITEM)
    assert details == ModelDetails(
        id="meta/muse-ember",
        supported_efforts=["max", "xhigh", "high", "medium", "low", "minimal"],
        default_effort="max",
        reasoning_param_supported=True,
        reasoning_effort_param_supported=True,
    )


def test_parse_deepseek_shape() -> None:
    details = parse_model_detail(DEEPSEEK_ITEM)
    assert details is not None
    assert details.id == "deepseek-flash"
    assert details.supported_efforts == ["low", "high", "max"]
    assert details.default_effort == "high"
    # DeepSeek does not advertise supported_parameters -> unknown, not False.
    assert details.reasoning_param_supported is None


def test_parse_unknown_and_malformed_shapes() -> None:
    plain = parse_model_detail({"id": "openai/gpt-4.1"})
    assert plain is not None
    assert plain.supported_efforts is None
    assert plain.default_effort is None
    assert parse_model_detail({"id": ""}) is None
    assert parse_model_detail({}) is None
    assert parse_model_detail("nope") is None
    assert parse_model_detail({"id": "x", "reasoning": {"supported_efforts": []}}) is not None


def test_parse_model_details_dedupes_and_skips_bad_entries() -> None:
    items: Any = [
        OPENROUTER_ITEM,
        {"id": "meta/muse-ember", "reasoning": {"supported_efforts": ["low"]}},
        {"no_id": True},
        DEEPSEEK_ITEM,
        "garbage",
    ]
    details = parse_model_details(items)
    assert [d.id for d in details] == ["meta/muse-ember", "deepseek-flash"]
    assert details[0].default_effort == "max"


def test_list_model_details_offline_parse() -> None:
    from agent.openai_client import OpenAIProvider

    client = OpenAIProvider(api_key="k", base_url="https://openrouter.ai/api/v1")
    try:
        details = client.list_model_details(raw=[OPENROUTER_ITEM, DEEPSEEK_ITEM])
    finally:
        client.close()
    assert [d.id for d in details] == ["meta/muse-ember", "deepseek-flash"]


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    models.clear_effort_cache()


def test_resolve_effort_prefers_discovered_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models,
        "discover_model_efforts",
        lambda provider, model, **kw: (["max", "xhigh", "high"], "max"),
    )
    assert models.resolve_effort("openrouter", "meta/muse-ember") == "max"


def test_resolve_effort_accepts_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models,
        "discover_model_efforts",
        lambda provider, model, **kw: (["max", "xhigh", "high"], "max"),
    )
    assert models.resolve_effort("openrouter", "m", override="xhigh") == "xhigh"


def test_resolve_effort_warns_and_falls_back_on_invalid_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        models,
        "discover_model_efforts",
        lambda provider, model, **kw: (["max", "high"], "max"),
    )
    assert models.resolve_effort("openrouter", "m", override="ultra", override_source="--effort") == "max"
    err = capsys.readouterr().err
    assert "ultra" in err
    assert "max, high" in err


def test_resolve_effort_static_fallback_without_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "discover_model_efforts", lambda provider, model, **kw: (None, None))
    # openrouter static default
    assert models.resolve_effort("openrouter", "unknown/model") == "medium"
    # explicit override still wins when nothing to validate against
    assert models.resolve_effort("openrouter", "unknown/model", override="xhigh") == "xhigh"


def test_resolve_effort_uses_first_level_when_default_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "discover_model_efforts", lambda provider, model, **kw: (["low", "high"], None))
    assert models.resolve_effort("deepseek", "deepseek-flash") == "low"


def test_discover_model_efforts_caches_per_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _FakeClient:
        def __init__(self, **kw: Any) -> None:
            self.kw = kw

        def list_model_details(self) -> list[ModelDetails]:
            return [ModelDetails(id="meta/muse-ember", supported_efforts=["max"], default_effort="max")]

        def close(self) -> None:
            calls.append("close")

    import agent.openai_client as oc

    monkeypatch.setattr(oc, "OpenAIProvider", _FakeClient)
    monkeypatch.setattr(
        models, "resolve_provider_credentials", lambda provider, path=None: ("key", "https://x/v1", "auth.json")
    )

    fetched: list[str] = []

    class _CountingClient(_FakeClient):
        def list_model_details(self) -> list[ModelDetails]:
            fetched.append("fetch")
            return super().list_model_details()

    monkeypatch.setattr(oc, "OpenAIProvider", _CountingClient)
    assert models.discover_model_efforts("openrouter", "meta/muse-ember") == (["max"], "max")
    assert models.discover_model_efforts("openrouter", "meta/muse-ember") == (["max"], "max")
    assert fetched == ["fetch"]  # cached: second call did not re-fetch


def test_discovery_respects_env_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARTZ_EFFORT_DISCOVERY", "0")
    assert models.discover_model_efforts("openrouter", "meta/muse-ember") == (None, None)


def test_policy_efforts_prefers_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models, "discover_model_efforts", lambda provider, model, **kw: (["max", "xhigh"], "max")
    )
    assert models.policy_efforts("openrouter", "m") == ["max", "xhigh"]
    assert models.policy_efforts("openai", "gpt-4.1") == ["max", "xhigh"]


def test_model_command_picker_uses_discovered_levels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The /model picker offers discovered levels and preselects the model default."""
    import agent.commands as commands
    from agent.runtime import AgentRuntime
    from agent.loop import TurnUsage

    monkeypatch.setattr(commands, "_provider_ready", lambda p: True)
    monkeypatch.setattr(commands, "discover_models", lambda p: (["meta/muse-ember"], None))
    monkeypatch.setattr(
        models, "discover_model_efforts", lambda provider, model, **kw: (["max", "xhigh", "high"], "max")
    )
    captured: dict[str, Any] = {}

    def _fake_pick(title: str, options: list[tuple[str, str]], current: str | None = None) -> str:
        if title == "Provider":
            return "openrouter"
        if title.startswith("Reasoning Effort"):
            captured["options"] = options
            captured["current"] = current
            return options[0][0]
        return options[0][0]  # model

    monkeypatch.setattr(commands, "_pick_option", _fake_pick)
    monkeypatch.setattr(commands, "_pick_searchable_option", lambda t, o, **kw: o[0][0])
    monkeypatch.setattr(
        commands,
        "build_provider",
        lambda **kw: (object(), "openrouter", kw["model_override"], kw["effort_override"]),
    )
    monkeypatch.setattr(commands, "save_model_config", lambda *a, **kw: None)

    rt = AgentRuntime(
        cwd=tmp_path,
        kernel=None,  # type: ignore[arg-type]
        client=None,  # type: ignore[arg-type]
        provider_name="deepseek",
        model="deepseek-flash",
        effort="high",
        system_prompt="",
        active_session=None,  # type: ignore[arg-type]
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=1000,
        compact_reserve_tokens=100,
        keep_recent_tokens=100,
        verbose=False,
        plain_output=True,
        headless=True,
    )
    assert commands._cmd_model(rt, "") is False
    assert captured["options"] == [("max", "max"), ("xhigh", "xhigh"), ("high", "high")]
    assert captured["current"] == "max"


def test_build_provider_uses_discovered_effort(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from agent import subcommands

    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("QUARTZ_EFFORT_DISCOVERY", "1")
    monkeypatch.delenv("EFFORT", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setattr(models, "clear_effort_cache", lambda: None)

    from agent.auth_store import save_provider_auth

    save_provider_auth("openrouter", "sk-or-test", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(
        models, "discover_model_efforts", lambda provider, model, **kw: (["max", "xhigh"], "max")
    )
    monkeypatch.setattr(subcommands, "resolve_effort", lambda p, m, **kw: models.resolve_effort(p, m, **kw))

    _client, provider, _model, effort = subcommands.build_provider(
        provider="openrouter", model_override="meta/muse-ember"
    )
    assert provider == "openrouter"
    assert effort == "max"

    # An explicit but unsupported override falls back to the discovered default.
    _client2, _p, _m, effort2 = subcommands.build_provider(
        provider="openrouter", model_override="meta/muse-ember", effort_override="minimal"
    )
    assert effort2 == "max"
