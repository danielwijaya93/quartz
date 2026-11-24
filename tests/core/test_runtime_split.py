"""Tests for split SessionState, ViewConfig, and AgentRuntime composition."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime, SessionState, ViewConfig
from quartz.core.session import Session
from quartz.kernel.fake import FakeKernel


def test_view_config_defaults_and_mutation() -> None:
    cfg = ViewConfig()
    assert cfg.verbose is False
    assert cfg.plain_output is False
    assert cfg.show_thinking is False
    assert cfg.show_details is False

    cfg.verbose = True
    assert cfg.verbose is True


def test_session_state_delegation(tmp_path: Path) -> None:
    session = Session.create(tmp_path)
    state = SessionState(session=session, messages=[{"role": "user", "content": "hello"}])
    assert state.session.id == session.id
    assert len(state.messages) == 1
    assert state.turn_count == 0


def test_agent_runtime_composes_view_config_and_session_state(tmp_path: Path) -> None:
    session = Session.create(tmp_path)
    kernel = FakeKernel(tmp_path)
    client = MagicMock()

    view_cfg = ViewConfig(verbose=True, show_thinking=True)
    session_st = SessionState(session=session, messages=[{"role": "user", "content": "test"}])

    rt = AgentRuntime(
        cwd=tmp_path,
        kernel=kernel,
        client=client,
        provider_name="test",
        model="m",
        effort="e",
        system_prompt="sys",
        active_session=session,
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=1000,
        compact_reserve_tokens=100,
        keep_recent_tokens=100,
        session_state=session_st,
        view_config=view_cfg,
    )

    # Assert composite properties forward to sub-components
    assert rt.view_config is view_cfg
    assert rt.session_state is session_st
    assert rt.verbose is True
    assert rt.show_thinking is True
    assert rt.messages == [{"role": "user", "content": "test"}]
    assert rt.active_session.id == session.id

    # Mutating via property updates sub-component
    rt.verbose = False
    assert view_cfg.verbose is False

    rt.messages.append({"role": "assistant", "content": "reply"})
    assert len(session_st.messages) == 2


def test_agent_runtime_from_configs_and_property_sync(tmp_path: Path) -> None:
    from quartz.core.runtime import ModelConfig, RuntimeOptions

    session = Session.create(tmp_path)
    kernel = FakeKernel(tmp_path)
    client = MagicMock()

    model_cfg = ModelConfig(
        provider_name="anthropic",
        model="claude-sonnet-4-5",
        effort="high",
        system_prompt="be helpful",
        max_context_tokens=200_000,
        compact_reserve_tokens=16_000,
        keep_recent_tokens=24_000,
    )
    session_st = SessionState(session=session, messages=[{"role": "user", "content": "hi"}])
    view_cfg = ViewConfig(verbose=True, show_details=True)
    opts = RuntimeOptions(
        headless=True,
        progress_every=5,
        background_check_every=30,
        notifications=False,
        transport="web",
    )

    rt = AgentRuntime.from_configs(
        cwd=tmp_path,
        kernel=kernel,
        client=client,
        model_config=model_cfg,
        session_state=session_st,
        view_config=view_cfg,
        options=opts,
    )

    assert rt.model_config is model_cfg
    assert rt.options is opts
    assert rt.view_config is view_cfg
    assert rt.session_state is session_st

    # Read-through properties
    assert rt.provider_name == "anthropic"
    assert rt.model == "claude-sonnet-4-5"
    assert rt.effort == "high"
    assert rt.system_prompt == "be helpful"
    assert rt.max_context_tokens == 200_000
    assert rt.compact_reserve_tokens == 16_000
    assert rt.keep_recent_tokens == 24_000
    assert rt.headless is True
    assert rt.progress_every == 5
    assert rt.background_check_every == 30
    assert rt.notifications is False
    assert rt.transport == "web"
    assert rt.pending_images == []

    # Mutating via AgentRuntime properties syncs back to ModelConfig / RuntimeOptions
    rt.provider_name = "openai"
    rt.model = "gpt-5"
    rt.effort = "medium"
    rt.system_prompt = "new prompt"
    rt.max_context_tokens = 128_000
    rt.compact_reserve_tokens = 8_000
    rt.keep_recent_tokens = 12_000
    assert model_cfg.provider_name == "openai"
    assert model_cfg.model == "gpt-5"
    assert model_cfg.effort == "medium"
    assert model_cfg.system_prompt == "new prompt"
    assert model_cfg.max_context_tokens == 128_000
    assert model_cfg.compact_reserve_tokens == 8_000
    assert model_cfg.keep_recent_tokens == 12_000

    rt.headless = False
    rt.progress_every = 10
    rt.background_check_every = 60
    rt.notifications = True
    rt.transport = "cli"
    assert opts.headless is False
    assert opts.progress_every == 10
    assert opts.background_check_every == 60
    assert opts.notifications is True
    assert opts.transport == "cli"


def test_agent_runtime_from_configs_defaults(tmp_path: Path) -> None:
    from quartz.core.runtime import ModelConfig

    session = Session.create(tmp_path)
    kernel = FakeKernel(tmp_path)
    client = MagicMock()
    model_cfg = ModelConfig(
        provider_name="test",
        model="m",
        effort="low",
        system_prompt="sys",
        max_context_tokens=1000,
        compact_reserve_tokens=100,
        keep_recent_tokens=100,
    )
    session_st = SessionState(session=session)

    rt = AgentRuntime.from_configs(
        cwd=tmp_path,
        kernel=kernel,
        client=client,
        model_config=model_cfg,
        session_state=session_st,
    )
    assert rt.headless is False
    assert rt.progress_every == 0
    assert rt.background_check_every == 0
    assert rt.notifications is True
    assert rt.transport == "cli"
    assert rt.verbose is False
    assert rt.pending_images == []
