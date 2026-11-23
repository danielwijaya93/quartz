"""Tests for desktop notifications in quartz tui."""

from __future__ import annotations

import io
from pathlib import Path

from quartz.core.commands import _cmd_settings
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.loop import TurnUsage
from quartz.kernel.fake import FakeKernel
from quartz.settings import load_settings
from quartz.tui.notify import notify_desktop


class _TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class _NonTtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_notify_disabled_returns_none() -> None:
    buf = _TtyBuffer()
    assert notify_desktop("Title", "Body", enabled=False, stream=buf, env={"TERM_PROGRAM": "iTerm.app"}) is None
    assert buf.getvalue() == ""


def test_notify_osc9_for_iterm_and_wt() -> None:
    buf = _TtyBuffer()
    method = notify_desktop("Quartz", "Turn finished", stream=buf, env={"TERM_PROGRAM": "iTerm.app"})
    assert method == "osc9"
    assert buf.getvalue() == "\x1b]9;Quartz: Turn finished\x07"

    buf2 = _TtyBuffer()
    method2 = notify_desktop("Quartz", "Approval", stream=buf2, env={"WT_SESSION": "1"})
    assert method2 == "osc9"
    assert "\x1b]9;Quartz: Approval\x07" == buf2.getvalue()


def test_notify_osc777_for_ghostty_wezterm_kitty() -> None:
    for env in (
        {"TERM_PROGRAM": "ghostty"},
        {"TERM_PROGRAM": "WezTerm"},
        {"KITTY_WINDOW_ID": "1"},
        {"TERM": "xterm-kitty"},
    ):
        buf = _TtyBuffer()
        method = notify_desktop("Quartz", "Done", stream=buf, env=env)
        assert method == "osc777"
        assert buf.getvalue() == "\x1b]777;notify;Quartz;Done\x07"


def test_notify_sanitizes_control_characters() -> None:
    buf = _TtyBuffer()
    method = notify_desktop(
        "Quartz\nTitle",
        "Line1\x1b[31m\x07;Line2",
        stream=buf,
        env={"TERM_PROGRAM": "ghostty"},
    )
    assert method == "osc777"
    assert buf.getvalue() == "\x1b]777;notify;Quartz Title;Line1 [31m Line2\x07"


def test_notify_local_linux_fallback_uses_notify_send(monkeypatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr("quartz.tui.notify.shutil.which", lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
    monkeypatch.setattr("quartz.tui.notify._spawn_detached", lambda args: spawned.append(args) or True)

    buf = _TtyBuffer()
    method = notify_desktop(
        "Quartz",
        "Turn finished",
        stream=buf,
        env={"TERM": "xterm-256color", "DISPLAY": ":0"},
        platform="linux",
    )
    assert method == "notify-send"
    assert spawned == [["notify-send", "-a", "Quartz", "Quartz", "Turn finished"]]


def test_notify_ssh_skips_local_os_and_falls_back_to_bell(monkeypatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr("quartz.tui.notify.shutil.which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr("quartz.tui.notify._spawn_detached", lambda args: spawned.append(args) or True)

    buf = _TtyBuffer()
    method = notify_desktop(
        "Quartz",
        "Done",
        stream=buf,
        env={"TERM": "xterm-256color", "DISPLAY": ":0", "SSH_CONNECTION": "1.2.3.4 22"},
        platform="linux",
    )
    assert method == "bell"
    assert buf.getvalue() == "\x07"
    assert spawned == []


def test_notify_non_tty_without_os_notifier_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("quartz.tui.notify.shutil.which", lambda name: None)
    buf = _NonTtyBuffer()
    assert notify_desktop("Quartz", "Done", stream=buf, env={"TERM": "xterm-256color"}, platform="linux") is None


def test_settings_notifications_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    rt = AgentRuntime(
        cwd=tmp_path,
        kernel=FakeKernel(),
        client=None,  # type: ignore[arg-type]
        provider_name="test",
        model="test-model",
        effort="medium",
        system_prompt="sys",
        active_session=Session.create(tmp_path),
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=100_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
    )
    res_off = _cmd_settings(rt, "notifications off")
    assert res_off is not None and "off" in (res_off.body or "")
    assert rt.notifications is False
    assert load_settings(tmp_path / "config.json").notifications is False

    res_on = _cmd_settings(rt, "notifications on")
    assert res_on is not None and "on" in (res_on.body or "")
    assert rt.notifications is True
    assert load_settings(tmp_path / "config.json").notifications is True
