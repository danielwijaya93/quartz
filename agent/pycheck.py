"""Static undefined-name checking for Python source, built on stdlib `symtable`."""

from __future__ import annotations

import ast
import builtins
import symtable
from collections.abc import Iterator
from pathlib import Path

_STD_MODULE_DUNDERS: frozenset[str] = frozenset(
    {
        "__name__",
        "__file__",
        "__doc__",
        "__package__",
        "__spec__",
        "__all__",
        "__builtins__",
        "__annotations__",
        "__cached__",
        "__loader__",
        "__class__",
        "__qualname__",
        "__module__",
    }
)
BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins)) | _STD_MODULE_DUNDERS
MAX_WARNINGS = 10


def module_bound_names(table: symtable.SymbolTable) -> set[str]:
    """Names defined at module scope (assigned, imported, or def/class)."""
    return {
        s.get_name()
        for s in table.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace() or s.is_parameter()
    }


def _walk_tables(table: symtable.SymbolTable) -> Iterator[symtable.SymbolTable]:
    yield table
    for child in table.get_children():
        yield from _walk_tables(child)


def unresolved_global_names(table: symtable.SymbolTable, allowed: frozenset[str]) -> set[str]:
    """Names that are read somewhere but never bound at module scope nor in `allowed`."""
    bound = module_bound_names(table) | allowed
    missing: set[str] = set()
    for scope in _walk_tables(table):
        for sym in scope.get_symbols():
            if sym.is_referenced() and sym.is_global() and sym.get_name() not in bound:
                missing.add(sym.get_name())
    return missing


def _first_load_lines(tree: ast.AST, names: set[str]) -> dict[str, int]:
    lines: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            if node.id not in lines or node.lineno < lines[node.id]:
                lines[node.id] = node.lineno
    return lines


def _has_star_import(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    )


def _allowed_names(path: Path) -> frozenset[str]:
    if path.name == "helpers.py" and path.parent.name == ".quartz":
        from agent.helpers import BUILTIN_HELPERS, PROTECTED_NAMES

        return BUILTIN_NAMES | frozenset(BUILTIN_HELPERS) | PROTECTED_NAMES
    return BUILTIN_NAMES


def check_python_source(path: Path, content: str) -> list[str]:
    """Return up to 10 warnings (syntax errors / undefined names) for `.py` source."""
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(content, filename=str(path))
        table = symtable.symtable(content, str(path), "exec")
    except SyntaxError as exc:
        return [f"SyntaxError at line {exc.lineno}: {exc.msg}"]
    if _has_star_import(tree):
        return []
    missing = unresolved_global_names(table, _allowed_names(path))
    lines = _first_load_lines(tree, missing)
    # Names without a source-level load are compiler-synthesized (e.g. __conditional_annotations__).
    ordered = sorted((lines[n], n) for n in missing if n in lines)
    return [
        f"line {lineno}: undefined name '{name}' (missing import?)"
        for lineno, name in ordered[:MAX_WARNINGS]
    ]
