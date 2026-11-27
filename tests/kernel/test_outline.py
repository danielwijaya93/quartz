"""Unit tests for `outline()`, `read_symbol()`, and `rg(..., context=)`."""

from __future__ import annotations

from pathlib import Path

import pytest

from quartz.kernel.helpers import rg
from quartz.kernel.outline import outline, read_symbol


def test_python_outline_and_read_symbol(tmp_path: Path) -> None:
    p = tmp_path / "sample.py"
    p.write_text(
        "class Foo:\n"
        "    def bar(self, x: int) -> int:\n"
        "        return x + 1\n\n"
        "async def top(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    out = outline(p)
    assert "L1-3: class Foo" in out
    assert "L2-3:   def bar(self, x)" in out
    assert "L5-6: async def top(a, b)" in out

    sym = read_symbol(p, "Foo.bar")
    assert "2:     def bar(self, x: int) -> int:" in sym
    assert "3:         return x + 1" in sym

    raw = read_symbol(p, "bar", raw=True)
    assert raw == "    def bar(self, x: int) -> int:\n        return x + 1"

    with pytest.raises(LookupError, match=r"Foo\.bar"):
        read_symbol(p, "Foo.baz")


def test_ts_outline_and_read_symbol(tmp_path: Path) -> None:
    p = tmp_path / "widget.ts"
    p.write_text(
        "export interface Config {\n"
        "  enabled: boolean;\n"
        "}\n\n"
        "export function greet(name: string) {\n"
        "  return `hi ${name}`;\n"
        "}\n",
        encoding="utf-8",
    )
    out = outline(p)
    assert "L1-3: interface Config" in out
    assert "L5-7: function greet" in out
    assert "return `hi ${name}`;" in read_symbol(p, "greet", raw=True)


def test_rg_context_lines(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("line1\nline2\nTARGET_HIT\nline4\nline5\n", encoding="utf-8")
    out = rg("TARGET_HIT", str(p), fixed=True, context=1)
    assert "line2" in out
    assert "TARGET_HIT" in out
    assert "line4" in out
    assert "line1" not in out
