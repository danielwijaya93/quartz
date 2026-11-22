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
