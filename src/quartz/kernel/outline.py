"""Structural file outline and symbol-targeted reading for the Quartz REPL."""

from __future__ import annotations

import ast
import re
from pathlib import Path

__all__ = ["outline"]

_MAX_OUTLINE_LINES = 200
_TS_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_TS_DECL_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:export\s+(?:default\s+)?)?"
    r"(?:(?:declare|abstract)\s+)?"
    r"(?:(?P<kind>class|interface|type|enum)\s+(?P<tname>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"|(?:async\s+)?(?P<fkind>function)\s+(?P<fname>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"|(?:const|let|var)\s+(?P<cname>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>)"
)
_MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


def _py_symbols(source: str) -> list[tuple[str, str, int, int, int]]:
    """Return `(qualname, display, start, end, depth)` for Python AST defs/classes."""
    tree = ast.parse(source)
    entries: list[tuple[str, str, int, int, int]] = []

    def _visit(body: list[ast.stmt], prefix: str, depth: int) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                end = node.end_lineno or node.lineno
                qname = f"{prefix}.{node.name}" if prefix else node.name
                entries.append((qname, f"class {node.name}", start, end, depth))
                _visit(node.body, qname, depth + 1)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                end = node.end_lineno or node.lineno
                qname = f"{prefix}.{node.name}" if prefix else node.name
                kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                args = [a.arg for a in node.args.args]
                sig = ", ".join(args[:5]) + (", ..." if len(args) > 5 else "")
                entries.append((qname, f"{kw} {node.name}({sig})", start, end, depth))

    _visit(tree.body, "", 0)
    return entries


def _ts_block_end(lines: list[str], start_idx: int) -> int:
    """Estimate 1-based end line of a JS/TS declaration via brace balancing."""
    depth = 0
    seen_brace = False
    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        for ch in line:
            if ch == "{":
                depth += 1
                seen_brace = True
            elif ch == "}":
                depth -= 1
                if seen_brace and depth <= 0:
                    return idx + 1
        if not seen_brace and line.rstrip().endswith(";"):
            return idx + 1
    return min(len(lines), start_idx + 1)


def _ts_symbols(source: str) -> list[tuple[str, str, int, int, int]]:
    lines = source.splitlines()
    entries: list[tuple[str, str, int, int, int]] = []
    for idx, line in enumerate(lines):
        m = _TS_DECL_RE.match(line)
        if not m:
            continue
        indent = len(m.group("indent").expandtabs(2))
        if indent > 2:
            continue
        name = m.group("tname") or m.group("fname") or m.group("cname")
        if not name:
            continue
        kind = m.group("kind") or m.group("fkind") or "const"
        start = idx + 1
        end = _ts_block_end(lines, idx)
        entries.append((name, f"{kind} {name}", start, end, 0))
    return entries


def _extract_symbols(path: Path, source: str) -> list[tuple[str, str, int, int, int]]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _py_symbols(source)
    if suffix in _TS_EXTENSIONS:
        return _ts_symbols(source)
    return []


def outline(path: str | Path) -> str:
    """Return a compact line-numbered structural map of classes/functions/headings in `path`."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"No such file: {path}")
    source = p.read_text(encoding="utf-8", errors="replace")
    total_lines = len(source.splitlines())
    suffix = p.suffix.lower()

    if suffix == ".md":
        out = [f"{path} ({total_lines} lines)"]
        for idx, line in enumerate(source.splitlines(), 1):
            m = _MD_HEADING_RE.match(line)
            if m:
                out.append(f"L{idx}: {m.group(1)} {m.group(2)}")
                if len(out) >= _MAX_OUTLINE_LINES:
                    out.append("... [truncated]")
                    break
        return "\n".join(out)

    try:
        symbols = _extract_symbols(p, source)
    except SyntaxError as exc:
        return f"{path} ({total_lines} lines): SyntaxError while parsing outline: {exc}"

    if not symbols:
        return f"{path} ({total_lines} lines): (no symbols indexed; use read() or rg())"

    out = [f"{path} ({total_lines} lines)"]
    for _qname, display, start, end, depth in symbols[:_MAX_OUTLINE_LINES]:
        pad = "  " * depth
        out.append(f"L{start}-{end}: {pad}{display}")
    if len(symbols) > _MAX_OUTLINE_LINES:
        out.append(f"... [{len(symbols) - _MAX_OUTLINE_LINES} more symbols]")
    return "\n".join(out)
