"""The web transport must not depend on the terminal or the CLI.

`cli/` is the composition root: it imports `web/` when `quartz web` runs. The
reverse edge would make the browser a reason the terminal exists, and would let a
`rich`-shaped assumption leak into a transport that has no terminal.

Mirrors `tests/core/test_import_boundaries.py`: it parses source rather than loaded
modules, so an import hidden inside a function still fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PKG = SRC / "quartz"


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.add(node.module or "")
    return found


def _belongs_to(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def test_web_does_not_import_the_terminal_or_the_cli() -> None:
    offenders: list[str] = []
    for path in sorted((PKG / "web").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imports(tree):
            if _belongs_to(imported, "quartz.tui") or _belongs_to(imported, "quartz.cli"):
                offenders.append(f"{path.name} -> {imported}")
    assert offenders == []


def _referenced_strings(tree: ast.Module) -> set[str]:
    """String literals a module uses as data, excluding docstrings.

    Docstrings are how `web/__init__.py` *explains* that it must not import the
    terminal; counting them would forbid saying so.
    """
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def test_web_does_not_name_the_terminal_packages_outside_a_docstring() -> None:
    """A `sys.modules.get("quartz.tui.render")` is a reference with no import."""
    offenders = [
        str(path.relative_to(PKG))
        for path in sorted((PKG / "web").rglob("*.py"))
        for text in _referenced_strings(ast.parse(path.read_text(encoding="utf-8")))
        if "quartz.tui" in text or "quartz.cli" in text
    ]
    assert offenders == []


def test_importing_quartz_web_costs_no_server_dependencies() -> None:
    """`import quartz.web` stays cheap: FastAPI is loaded at first use, not at import."""
    import subprocess
    import sys

    code = (
        "import sys, quartz.web; "
        "assert 'fastapi' not in sys.modules, 'fastapi imported eagerly'; "
        "assert 'uvicorn' not in sys.modules, 'uvicorn imported eagerly'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
