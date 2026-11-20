"""Tests for the SlashCommand registry, dispatch, and dynamic help formatting."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent.commands import (
    COMMAND_REGISTRY,
    CommandRegistry,
    SlashCommand,
    _handle_slash_command,
)
from agent.compact import CompactionReason, CompactionResult
from agent.completer import AgentCompleter
from agent.errors import CredentialError
from agent.kernel_client import CellResult
from agent.loop import TurnUsage
from agent.runtime import AgentRuntime
from agent.session import Session


def test_command_registry_registration_and_dispatch() -> None:
    reg = CommandRegistry()
    calls: list[str] = []

    @reg.register("/foo", "Do foo", usage="[arg]", aliases=["/f"])
    def _foo_handler(rt: MagicMock, arg: str) -> bool | None:
        calls.append(f"foo:{arg}")
        return False

    assert reg.get("/foo") is not None
    assert reg.get("/f") is not None
    assert reg.get("/f").name == "/foo"
    assert reg.get("/unknown") is None

    mock_rt = MagicMock()
    # Test dispatch
    assert reg.handle(mock_rt, "/foo bar") is False
    assert calls == ["foo:bar"]

    # Test alias dispatch
    assert reg.handle(mock_rt, "/f baz") is False
    assert calls == ["foo:bar", "foo:baz"]

    # Test non-slash input
    assert reg.handle(mock_rt, "hello world") is None

    # Test unknown command
    assert reg.handle(mock_rt, "/unknown arg") is None


def test_command_registry_completion_dict_and_help() -> None:
    reg = CommandRegistry()

    @reg.register("/alpha", "Alpha command", usage="<x>")
    def _alpha(rt: MagicMock, arg: str) -> bool | None:
        return False

    @reg.register("/beta", "Beta command")
    def _beta(rt: MagicMock, arg: str) -> bool | None:
        return False

    comp_dict = reg.to_completion_dict()
    assert comp_dict == {
        "/alpha": "Alpha command",
        "/beta": "Beta command",
    }

    help_text = reg.format_help()
    assert "/alpha <x>" in help_text
    assert "Alpha command" in help_text
    assert "/beta" in help_text
    assert "Beta command" in help_text


def test_builtin_commands_in_global_registry(tmp_path: Path) -> None:
    mock_rt = MagicMock()
    mock_rt.cwd = tmp_path
    mock_rt.active_session = Session.create(tmp_path)
    mock_rt.system_prompt = "system instructions"
    mock_rt.usage_stats = TurnUsage()
    mock_rt.max_context_tokens = 128_000
    mock_rt.kernel.list_vars.return_value = {}

    # /exit and /quit return True
    assert _handle_slash_command(mock_rt, "/exit") is True
    assert _handle_slash_command(mock_rt, "/quit") is True

    # /help returns False
    assert _handle_slash_command(mock_rt, "/help") is False

    # /prompt returns False
    assert _handle_slash_command(mock_rt, "/prompt") is False

    # /session returns False
    assert _handle_slash_command(mock_rt, "/session") is False

    # /vars returns False
    assert _handle_slash_command(mock_rt, "/vars") is False

    # /usage returns False
    assert _handle_slash_command(mock_rt, "/usage") is False

    # Unknown command returns None
    assert _handle_slash_command(mock_rt, "/nonexistent_command") is None


def test_agent_completer_reflects_registry(tmp_path: Path) -> None:
    completer = AgentCompleter(tmp_path)
    all_comps = list(completer.get_completions(Document("/"), CompleteEvent()))
    comp_texts = [c.text for c in all_comps]

    for expected in ["/login", "/logout", "/model", "/continue", "/image", "/paste", "/vars", "/help", "/exit"]:
        assert expected in comp_texts


def _make_real_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentRuntime:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    restarted = []
    restored = []

    class _FakeKernel:
        dirty = False

        def list_vars(self) -> dict[str, str]:
            return {}

        def restart(self) -> None:
            restarted.append(True)

        def exec_cell(self, code: str) -> CellResult:
            restored.append(code)
            return CellResult(ok=True, stdout="", stderr="", result=None)

    sess = Session.create(tmp_path)
    rt = AgentRuntime(
        cwd=tmp_path,
        kernel=_FakeKernel(),  # type: ignore[arg-type]
        client=SimpleNamespace(),  # type: ignore[arg-type]
        provider_name="deepseek",
        model="deepseek-flash",
        effort="high",
        system_prompt="You are helpful.",
        active_session=sess,
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
        usage_stats=TurnUsage(prompt_tokens=100, total_prompt_tokens=100),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=True,
        headless=True,
        cell_history=[("x = 1", CellResult(ok=True, stdout="", stderr="", result=None), 0.1)],
        total_elapsed_s=1.5,
        turn_count=2,
    )
    rt._restarted_log = restarted  # type: ignore[attr-defined]
    return rt


def test_real_runtime_cmd_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_real_runtime(tmp_path, monkeypatch)
    compacted_msgs = [
        {"role": "user", "content": "[compacted summary]"},
        {"role": "assistant", "content": "ready"},
    ]

    def _fake_compact_messages(*args, **kwargs) -> CompactionResult:
        return CompactionResult(
            messages=compacted_msgs,
            summarized=2,
            kept=1,
            reason=CompactionReason.SUMMARIZED,
            estimated_tokens_before=500,
            estimated_tokens_after=40,
        )

    monkeypatch.setattr("agent.runtime.compact_messages", _fake_compact_messages)

    assert _handle_slash_command(rt, "/compact keep key facts") is False
    assert rt.messages == compacted_msgs
    assert rt.usage_stats.prompt_tokens > 0
    _, loaded_msgs = Session.load(rt.active_session.id)
    assert loaded_msgs == compacted_msgs


def test_real_runtime_cmd_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_real_runtime(tmp_path, monkeypatch)
    old_session_id = rt.active_session.id

    assert _handle_slash_command(rt, "/reset") is False
    assert rt.messages == []
    assert rt.cell_history == []
    assert rt.pending_images == []
    assert rt.usage_stats.prompt_tokens == 0
    assert rt.total_elapsed_s == 0.0
    assert rt.turn_count == 0
    assert rt.active_session.id != old_session_id
    assert rt._restarted_log == [True]  # type: ignore[attr-defined]


def test_real_runtime_cmd_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_real_runtime(tmp_path, monkeypatch)
    target_session = Session.create(tmp_path)
    target_session.save_messages(
        [{"role": "user", "content": "old task"}, {"role": "assistant", "content": "done"}]
    )
    target_session.save_usage({"prompt_tokens": 420, "total_prompt_tokens": 420})
    monkeypatch.setattr(Session, "restore_kernel", lambda self, k: "[restored vars]")

    assert _handle_slash_command(rt, f"/continue {target_session.id}") is False
    assert rt.active_session.id == target_session.id
    assert rt.messages[0]["content"] == "old task"
    assert rt.messages[-1]["content"] == "[restored vars]"
    assert rt.usage_stats.prompt_tokens == 420
    assert rt._restarted_log == [True]  # type: ignore[attr-defined]


def test_real_runtime_cmd_model_handles_credential_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_real_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("agent.commands._provider_ready", lambda p: True)
    monkeypatch.setattr("agent.commands.discover_models", lambda p: (["deepseek-chat"], None))
    monkeypatch.setattr(
        "agent.commands._pick_option",
        lambda title, opts, current=None: "deepseek" if title == "Provider" else "high",
    )
    monkeypatch.setattr(
        "agent.commands._pick_searchable_option",
        lambda title, opts, current=None, allow_custom=True: "deepseek-chat",
    )

    def _raise_cred(*args, **kwargs):
        raise CredentialError("Missing key")

    monkeypatch.setattr("agent.commands.build_provider", _raise_cred)

    # Should catch CredentialError cleanly and return False without crashing
    assert _handle_slash_command(rt, "/model") is False
    assert rt.model == "deepseek-flash"


def test_real_runtime_cmd_image_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_real_runtime(tmp_path, monkeypatch)
    img_path = tmp_path / "sample.png"
    # 1x1 transparent PNG
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    img_path.write_bytes(png_bytes)

    assert _handle_slash_command(rt, "/image sample.png") is False
    assert len(rt.pending_images) == 1
    assert rt.pending_images[0].label == "sample.png"

    assert _handle_slash_command(rt, "/image clear") is False
    assert rt.pending_images == []
