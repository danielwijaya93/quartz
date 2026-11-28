"""The transport-independent half of slash commands: results, not printing.

Every test here talks to `quartz.core.commands` only. That is the point of the
module -- if a command can be exercised without a terminal, a web transport can
run it too.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quartz.core.commands import (
    COMMAND_REGISTRY,
    CommandResult,
    parse_expand_range,
)
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.kernel.client import CellResult

#: The commands that must tell a non-terminal transport to stay away. Phase 4
#: turns them into real web controls one at a time.
NEEDS_TERMINAL = {
    "/login",
    "/logout",
    "/model",
    "/session",
    "/continue",
    "/image",
    "/paste",
}

KINDS = {"text", "list", "error", "needs_terminal"}


class _FakeKernel:
    """The slice of `KernelClient` commands actually touch."""

    dirty = False

    def __init__(self) -> None:
        self.vars: dict[str, str] = {}
        self.jobs: list[dict[str, object]] = []
        self.restarts = 0

    def list_vars(self) -> dict[str, str]:
        return self.vars

    def list_helpers(self) -> dict[str, object]:
        return {
            "helpers": [
                {"name": "bash", "source": "builtin", "signature": "bash(cmd)", "doc": "Run it"},
                {"name": "zap", "source": "custom", "signature": "zap()", "doc": "Custom"},
            ],
            "warnings": ["zap shadows nothing"],
        }

    def running_bash_jobs(self) -> list[dict[str, object]]:
        return self.jobs

    def restart(self) -> None:
        self.restarts += 1


def _make_rt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentRuntime:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    return AgentRuntime(
        cwd=tmp_path,
        kernel=_FakeKernel(),  # type: ignore[arg-type]
        client=SimpleNamespace(),  # type: ignore[arg-type]
        provider_name="deepseek",
        model="deepseek-flash",
        effort="high",
        system_prompt="You are helpful.",
        active_session=Session.create(tmp_path),
        messages=[{"role": "user", "content": "hello"}],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=True,
        headless=True,
    )


def _run(rt: AgentRuntime, name: str, arg: str = "") -> CommandResult | None:
    cmd = COMMAND_REGISTRY.get(name)
    assert cmd is not None, f"{name} is not registered"
    return cmd.handler(rt, arg)


async def _run_async(rt: AgentRuntime, name: str, arg: str = "") -> CommandResult | None:
    raw = f"{name} {arg}".strip() if arg else name
    return await COMMAND_REGISTRY.handle_async(rt, raw)


def test_the_registry_lists_every_command_once() -> None:
    names = [cmd.name for cmd in COMMAND_REGISTRY.list_commands()]
    assert len(names) == len(set(names))
    assert set(names) == {
        "/login",
        "/logout",
        "/model",
        "/rename",
        "/continue",
        "/image",
        "/paste",
        "/helpers",
        "/session",
        "/usage",
        "/expand",
        "/compact",
        "/settings",
        "/mcp",
        "/prompt",
        "/reset",
        "/jobs",
        "/goal",
        "/watch",
        "/help",
        "/exit",
    }


def test_aliases_resolve_to_their_command() -> None:
    assert COMMAND_REGISTRY.get("/quit") is COMMAND_REGISTRY.get("/exit")
    assert COMMAND_REGISTRY.get("/last") is COMMAND_REGISTRY.get("/expand")
    assert COMMAND_REGISTRY.get("/nope") is None


async def test_every_command_returns_a_documented_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler either says something (`CommandResult`) or says nothing (`/exit`)."""
    rt = _make_rt(tmp_path, monkeypatch)
    for cmd in COMMAND_REGISTRY.list_commands():
        if cmd.exits:
            assert await COMMAND_REGISTRY.handle_async(rt, cmd.name) is None, "/exit leaves; it has nothing to render"
            continue
        result = await COMMAND_REGISTRY.handle_async(rt, cmd.name)
        assert isinstance(result, CommandResult), f"{cmd.name} returned {result!r}"
        assert result.kind in KINDS, f"{cmd.name} returned kind={result.kind!r}"


async def test_only_the_terminal_commands_ask_for_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    asking: set[str] = set()
    for cmd in COMMAND_REGISTRY.list_commands():
        if cmd.exits:
            continue
        result = await COMMAND_REGISTRY.handle_async(rt, cmd.name)
        if isinstance(result, CommandResult) and result.kind == "needs_terminal":
            asking.add(cmd.name)
    assert asking == NEEDS_TERMINAL


#: Commands that must answer while a turn is running. They either touch only local
#: state or read a value the kernel already pushed; none of them waits on the worker.
#: `/jobs` belongs here because its whole purpose is showing the background work that a
#: running turn kicked off -- queueing it behind that turn made it answer the wrong
#: question, which is the bug this set exists to keep fixed.
IMMEDIATE = {"/jobs", "/settings", "/goal", "/watch", "/help"}


def test_the_immediate_commands_are_exactly_the_ones_that_never_wait() -> None:
    """The dock runs these beside a live turn, so the set must not drift silently."""
    immediate = {cmd.name for cmd in COMMAND_REGISTRY.list_commands() if cmd.immediate}
    assert immediate == IMMEDIATE
    for name in ("/helpers",):
        cmd = COMMAND_REGISTRY.get(name)
        assert cmd is not None and cmd.immediate is False, f"{name} asks the kernel; it must queue"


def test_the_needs_terminal_flag_agrees_with_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag lets a client grey a command out without calling it; it cannot drift."""
    _make_rt(tmp_path, monkeypatch)
    flagged = {cmd.name for cmd in COMMAND_REGISTRY.list_commands() if cmd.needs_terminal}
    assert flagged == NEEDS_TERMINAL


def test_a_needs_terminal_result_tells_the_user_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    for name in sorted(NEEDS_TERMINAL):
        result = _run(rt, name)
        assert isinstance(result, CommandResult)
        assert result.kind == "needs_terminal"
        assert "terminal" in result.message, f"{name} must say where to run it"


def test_help_lists_every_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    result = _run(rt, "/help")
    assert isinstance(result, CommandResult)
    assert result.kind == "text"
    for cmd in COMMAND_REGISTRY.list_commands():
        if cmd.name == "/help":
            continue  # the help text lists the others, as it always has
        assert cmd.name in result.body
    assert "Keybindings:" in result.body


def test_session_sends_a_terminal_user_to_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal's own `/session` draws the paths; core's answer stays a redirect."""
    rt = _make_rt(tmp_path, monkeypatch)

    result = _run(rt, "/session")

    assert isinstance(result, CommandResult)
    assert result.kind == "needs_terminal"


def test_session_in_the_browser_prints_the_id_and_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser has no terminal to send someone to, so the values are the answer."""
    rt = _make_rt(tmp_path, monkeypatch)
    rt.transport = "web"

    result = _run(rt, "/session")

    assert isinstance(result, CommandResult)
    assert result.kind == "text"
    assert rt.active_session.id in result.body
    assert str(rt.active_session.jsonl_path) in result.body


#: Keys that only work in a terminal window. The web's help must not list any of
#: them, because a browser user has no way to press them.
_TERMINAL_ONLY_KEYS = ("Ctrl+T", "Ctrl+O", "Ctrl+D", "Ctrl+C", "Ctrl+J", "@path", "\\+Enter")


def test_help_lists_the_keybindings_of_the_transport_that_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminal output is unchanged; the browser gets keys a browser actually has."""
    rt = _make_rt(tmp_path, monkeypatch)

    terminal = _run(rt, "/help")
    assert isinstance(terminal, CommandResult)
    assert terminal.body == COMMAND_REGISTRY.format_help()
    for key in _TERMINAL_ONLY_KEYS:
        assert key in terminal.body, f"{key} is a terminal key; the terminal help lost it"

    rt.transport = "web"
    browser = _run(rt, "/help")
    assert isinstance(browser, CommandResult)
    for key in _TERMINAL_ONLY_KEYS:
        assert key not in browser.body, f"the browser help still advertises {key}"
    for browser_key in ("Enter", "Shift+Enter", "Esc", "Up", "command palette"):
        assert browser_key in browser.body

    # Same commands either way: only the keys describe a different surface.
    for cmd in COMMAND_REGISTRY.list_commands():
        if cmd.name != "/help":
            assert cmd.name in browser.body


def test_helpers_put_builtins_first_and_carry_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    result = _run(rt, "/helpers")
    assert isinstance(result, CommandResult)
    assert [item["name"] for item in result.items] == ["bash", "zap"]
    assert "zap shadows nothing" in result.body


def test_jobs_returns_raw_rows_the_label_is_the_transport_s_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    idle = _run(rt, "/jobs")
    assert isinstance(idle, CommandResult)
    assert idle.kind == "text" and "No background jobs" in idle.body

    rt.kernel.jobs = [{"pid": 42, "command": "pytest -q", "elapsed_s": 3.5}]  # type: ignore[attr-defined]
    busy = _run(rt, "/jobs")
    assert isinstance(busy, CommandResult)
    assert busy.items == [{"pid": 42, "command": "pytest -q", "elapsed_s": 3.5}]


def test_expand_returns_cells_and_rejects_an_out_of_range_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    empty = _run(rt, "/expand")
    assert isinstance(empty, CommandResult)
    assert empty.kind == "text" and "No ipython cells" in empty.body

    rt.cell_history = [("1 + 1", CellResult(ok=True, stdout="2", stderr="", result=None), 0.25)]
    cells = _run(rt, "/expand")
    assert isinstance(cells, CommandResult)
    assert cells.kind == "list"
    assert cells.items[0]["cell"] == 1
    assert cells.items[0]["code"] == "1 + 1"
    assert cells.items[0]["duration_s"] == 0.25

    bad = _run(rt, "/expand", "99")
    assert isinstance(bad, CommandResult)
    assert bad.kind == "error"
    assert "Choose 1..1" in bad.message


def test_prompt_and_usage_are_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    prompt = _run(rt, "/prompt")
    assert isinstance(prompt, CommandResult)
    assert prompt.kind == "text"
    assert prompt.title == "System Prompt"
    assert prompt.body == "You are helpful."

    usage = _run(rt, "/usage")
    assert isinstance(usage, CommandResult)
    assert usage.kind == "text"
    assert "Last context:" in usage.body
    assert "128,000" in usage.body


async def test_compact_says_nothing_to_do_and_reports_the_new_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    untouched = await _run_async(rt, "/compact")
    assert isinstance(untouched, CommandResult)
    assert untouched.kind == "text"
    assert untouched.body == "Nothing to compact yet."
    assert untouched.items == []


async def test_compact_carries_the_numbers_a_renderer_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compaction line is drawing, but its numbers are data."""
    from quartz.core.compact import CompactionReason, CompactionResult

    rt = _make_rt(tmp_path, monkeypatch)
    rt.messages = [{"role": "user", "content": "hello"}]

    async def _fake_compact(self: AgentRuntime, focus: str | None = None) -> CompactionResult:
        del focus
        return CompactionResult(
            messages=[{"role": "user", "content": "[summary]"}],
            summarized=4,
            kept=2,
            reason=CompactionReason.SUMMARIZED,
            estimated_tokens_before=900,
            estimated_tokens_after=120,
        )

    monkeypatch.setattr(AgentRuntime, "compact", _fake_compact)
    result = await _run_async(rt, "/compact", "keep the plan")
    assert isinstance(result, CommandResult)
    assert result.items == [
        {
            "reason": "summarized",
            "summarized": 4,
            "kept": 2,
            "cleared": 0,
            "tokens_before": 900,
            "tokens_after": 120,
        }
    ]
    assert "120" in result.body


def test_settings_mutates_the_live_runtime_and_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next turn -- terminal or web -- must see a /settings change immediately."""
    rt = _make_rt(tmp_path, monkeypatch)
    result = _run(rt, "/settings", "reserve 32768")
    assert isinstance(result, CommandResult)
    assert result.kind == "text"
    assert "reserve_tokens = 32,768" in result.body
    assert rt.compact_reserve_tokens == 32768
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert stored["compact_reserve_tokens"] == 32768

    progress = _run(rt, "/settings", "progress 0")
    assert isinstance(progress, CommandResult)
    assert "progress_every = 0 (off)" in progress.body
    assert rt.progress_every == 0


def test_settings_show_and_list_are_titled_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    shown = _run(rt, "/settings")
    assert isinstance(shown, CommandResult)
    assert shown.title == "Settings"
    assert "reserve_tokens: 16,384" in shown.body

    listed = _run(rt, "/settings", "list")
    assert isinstance(listed, CommandResult)
    assert listed.title == "Settings (stored)"
    assert json.loads(listed.body)["compact_reserve_tokens"] == 16_384


@pytest.mark.parametrize(
    "arg",
    ["reserve abc", "reserve 100", "reserve", "progress abc", "progress 5000", "bogus 1"],
)
def test_settings_errors_are_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arg: str
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    result = _run(rt, "/settings", arg)
    assert isinstance(result, CommandResult)
    assert result.kind == "error"
    assert result.message
    assert result.body.startswith("Usage:"), "an error says how to do it right"
    assert rt.compact_reserve_tokens == 16_384
    assert not (tmp_path / "config.json").exists()


def test_reset_starts_a_new_session_on_the_same_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    old_session_id = rt.active_session.id
    result = _run(rt, "/reset")
    assert isinstance(result, CommandResult)
    assert result.kind == "text"
    assert rt.messages == []
    assert rt.active_session.id != old_session_id
    assert rt.kernel.restarts == 1  # type: ignore[attr-defined]


def test_handle_ignores_anything_that_is_not_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert COMMAND_REGISTRY.handle(rt, "hello there") is None
    assert COMMAND_REGISTRY.handle(rt, "/nope") is None
    assert isinstance(COMMAND_REGISTRY.handle(rt, "/usage"), CommandResult)


def test_parse_expand_range_is_shared_vocabulary() -> None:
    assert parse_expand_range("", 5) == (5, 5)
    assert parse_expand_range("3", 5) == (3, 3)
    assert parse_expand_range("2..4", 5) == (2, 4)
    for bad in ("4..2", "0..2", "6"):
        with pytest.raises(ValueError):
            parse_expand_range(bad, 5)
