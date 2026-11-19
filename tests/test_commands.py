"""Tests for the SlashCommand registry, dispatch, and dynamic help formatting."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent.commands import (
    COMMAND_REGISTRY,
    CommandRegistry,
    SlashCommand,
    _handle_slash_command,
)
from agent.completer import AgentCompleter
from agent.loop import TurnUsage
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
