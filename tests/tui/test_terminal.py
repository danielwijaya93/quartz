from __future__ import annotations

from quartz.tui.terminal import (
    SYNC_BEGIN,
    SYNC_END,
    install_sync_output,
    supports_sync_output,
    wrap_sync,
)


def test_env_override_wins() -> None:
    assert supports_sync_output({"QUARTZ_SYNC_OUTPUT": "1"}, isatty=False) is True
    assert supports_sync_output({"QUARTZ_SYNC_OUTPUT": "0", "TERM_PROGRAM": "WezTerm"}, isatty=True) is False


def test_non_tty_never_synced() -> None:
    assert supports_sync_output({"TERM_PROGRAM": "WezTerm"}, isatty=False) is False


def test_known_terminals_detected() -> None:
    assert supports_sync_output({"TERM_PROGRAM": "iTerm.app"}, isatty=True)
    assert supports_sync_output({"TERM": "xterm-kitty"}, isatty=True)
    assert supports_sync_output({"KITTY_WINDOW_ID": "3", "TERM": "xterm-256color"}, isatty=True)
    assert supports_sync_output({"WT_SESSION": "abc"}, isatty=True)


def test_unknown_and_dumb_terminals_not_synced() -> None:
    assert not supports_sync_output({"TERM": "xterm-256color"}, isatty=True)
    assert not supports_sync_output({"TERM": "dumb", "KITTY_WINDOW_ID": "1"}, isatty=True)


def test_wrap_sync() -> None:
    assert wrap_sync("") == ""
    assert wrap_sync("x") == f"{SYNC_BEGIN}x{SYNC_END}"


class _FakeOutput:
    def __init__(self) -> None:
        self._buffer: list[str] = []
        self.flushed: list[str] = []

    def write(self, data: str) -> None:
        self._buffer.append(data)

    def flush(self) -> None:
        self.flushed.append("".join(self._buffer))
        self._buffer.clear()


def test_install_sync_output_brackets_frames() -> None:
    out = _FakeOutput()
    assert install_sync_output(out) is True
    out.write("frame")
    out.flush()
    out.flush()  # empty flush must not emit markers
    assert out.flushed[0] == f"{SYNC_BEGIN}frame{SYNC_END}"
    assert out.flushed[1] == ""


def test_install_sync_output_is_idempotent_and_skips_dummy() -> None:
    out = _FakeOutput()
    install_sync_output(out)
    install_sync_output(out)
    out.write("a")
    out.flush()
    assert out.flushed == [f"{SYNC_BEGIN}a{SYNC_END}"]

    class _NoBuffer:
        def flush(self) -> None: ...

    assert install_sync_output(_NoBuffer()) is False


def test_dock_emit_wraps_scrollback_writes_when_enabled(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from test_interactive import _make_dock

    monkeypatch.setenv("QUARTZ_SYNC_OUTPUT", "1")
    with _make_dock(tmp_path) as (dock, _):
        dock.emit_renderable("hello")
    out = capsys.readouterr().out
    assert out.startswith(SYNC_BEGIN) and out.endswith(SYNC_END)
    assert "hello" in out


def test_dock_emit_plain_when_disabled(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from test_interactive import _make_dock

    monkeypatch.setenv("QUARTZ_SYNC_OUTPUT", "0")
    with _make_dock(tmp_path) as (dock, _):
        dock.emit_renderable("hello")
    assert SYNC_BEGIN not in capsys.readouterr().out
