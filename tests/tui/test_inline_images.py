from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from quartz.core.images import ImageAttachment
from quartz.tui.images import (
    InlineImage,
    detect_image_protocol,
    image_sequence,
    iterm2_sequence,
    kitty_sequence,
    preview_renderable,
)

PNG = ImageAttachment("image/png", "QUJD" * 2000, "a.png")
JPG = ImageAttachment("image/jpeg", "QUJD", "a.jpg")


def test_detect_protocols() -> None:
    assert detect_image_protocol({"QUARTZ_IMAGE_PROTOCOL": "kitty"}, isatty=False) == "kitty"
    assert detect_image_protocol({"KITTY_WINDOW_ID": "1"}, isatty=True) == "kitty"
    assert detect_image_protocol({"TERM": "xterm-ghostty"}, isatty=True) == "kitty"
    assert detect_image_protocol({"TERM_PROGRAM": "iTerm.app"}, isatty=True) == "iterm2"
    assert detect_image_protocol({"TERM_PROGRAM": "WezTerm"}, isatty=True) == "iterm2"


def test_detect_none_for_pipes_tmux_unknown() -> None:
    assert detect_image_protocol({"KITTY_WINDOW_ID": "1"}, isatty=False) == "none"
    assert detect_image_protocol({"KITTY_WINDOW_ID": "1", "TMUX": "x"}, isatty=True) == "none"
    assert detect_image_protocol({"TERM": "xterm-256color"}, isatty=True) == "none"
    assert detect_image_protocol({"QUARTZ_IMAGE_PROTOCOL": "none", "KITTY_WINDOW_ID": "1"}, isatty=True) == "none"


def test_kitty_chunking() -> None:
    seq = kitty_sequence("A" * 10000, 30)
    parts = seq.split("\x1b\\")[:-1]
    assert len(parts) == 3
    assert parts[0].startswith("\x1b_Ga=T,f=100,c=30,m=1;")
    assert parts[1].startswith("\x1b_Gm=1;")
    assert parts[2].startswith("\x1b_Gm=0;")


def test_iterm2_sequence() -> None:
    seq = iterm2_sequence("QUJD", 20, "x.png")
    assert seq.startswith("\x1b]1337;File=inline=1;") and seq.endswith(":QUJD\x07")
    assert "width=20" in seq


def test_format_support_and_fallback() -> None:
    assert image_sequence(PNG, "kitty", 20) is not None
    assert image_sequence(JPG, "kitty", 20) is None  # kitty needs PNG
    assert image_sequence(JPG, "iterm2", 20) is not None
    assert image_sequence(PNG, "none", 20) is None
    assert preview_renderable(JPG, protocol="kitty") is None


def test_renderable_emits_raw_sequence_on_terminal_only() -> None:
    seq = "\x1b_Gtest\x1b\\"
    term = io.StringIO()
    Console(file=term, force_terminal=True, width=80).print(InlineImage(seq))
    assert seq in term.getvalue()
    plain = io.StringIO()
    Console(file=plain, force_terminal=False, width=80).print(InlineImage(seq))
    assert seq not in plain.getvalue()


def test_corrupt_inline_image_mention_does_not_crash_cli_or_dock(
    tmp_path: Path, monkeypatch
) -> None:
    from types import SimpleNamespace

    import quartz.cli.app as cli_app
    from quartz.tui.dock import InteractiveDock

    (tmp_path / "bad.png").write_bytes(b"not-a-png")
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=100, color_system=None)
    monkeypatch.setattr(cli_app, "console", test_console)

    rt = SimpleNamespace(cwd=tmp_path, messages=[], pending_images=[])
    cli_app._run_headless(rt, "check @bad.png", [])  # type: ignore[arg-type]
    assert "Error:" in buf.getvalue()
    assert rt.messages == []

    # Dock idle submission catches the same error cleanly without spawning a turn.
    printed: list[object] = []
    fake_dock = SimpleNamespace(
        rt=SimpleNamespace(cwd=tmp_path, messages=[], pending_images=[]),
        terminal_width=100,
        _invalidate=lambda: None,
        _execute_dock_turn=lambda: (_ for _ in ()).throw(AssertionError("should not run turn")),
    )
    monkeypatch.setattr(
        "quartz.tui.dock._active_printer",
        lambda: SimpleNamespace(block=printed.append),
    )
    __import__("asyncio").run(InteractiveDock._handle_submitted_idle(fake_dock, "check @bad.png"))  # type: ignore[arg-type]
    assert any("Error:" in str(item) for item in printed)
