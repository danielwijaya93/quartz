"""Tests for UX improvements (autocomplete, collapsible cells, etc.)."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent.completer import AgentCompleter
from agent.kernel_client import CellResult
from main import format_compact_cell_summary


def test_slash_command_autocomplete(tmp_path: Path) -> None:
    completer = AgentCompleter(tmp_path)
    comps = list(completer.get_completions(Document("/co"), CompleteEvent()))
    texts = [c.text for c in comps]
    assert texts == ["/compact"]

    all_comps = list(completer.get_completions(Document("/"), CompleteEvent()))
    assert "/vars" in [c.text for c in all_comps]
    assert "/usage" in [c.text for c in all_comps]


def test_at_file_autocomplete(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("", encoding="utf-8")

    completer = AgentCompleter(tmp_path)
    comps = list(completer.get_completions(Document("check @app"), CompleteEvent()))
    texts = [c.text for c in comps]
    assert "@src/app.py" in texts
    assert not any("ignored.py" in t for t in texts)


def test_compact_cell_summary() -> None:
    ok_res = CellResult(ok=True, stdout="line1\nline2\nline3\n", stderr="", result=None)
    s1 = format_compact_cell_summary(1, "x = 1\nprint('line1')\nprint('line2')", ok_res, 0.12)
    assert "✔ [#1] ipython (3L: x = 1) → 3 lines out (0.12s)" == s1

    err_res = CellResult(ok=False, stdout="", stderr="Traceback...\nNameError: name 'foo' is not defined\n", result=None)
    s2 = format_compact_cell_summary(2, "foo", err_res, 0.04)
    assert "✖ [#2] ipython (1L: foo) → NameError: name 'foo' is not defined (0.04s)" == s2
