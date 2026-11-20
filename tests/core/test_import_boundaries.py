"""Import boundaries: `tui/` never imports `cli/`, and `core/` stays front-end free.

`cli/` is the composition root and may import `tui/` and `core/`; the reverse edge
turns the two packages into a cycle and is what this test exists to prevent. It
parses the source rather than the loaded modules, so an import added anywhere in
`tui/` fails here before it can hide behind a lazy import inside a function.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PKG = SRC / "quartz"


def _modules() -> dict[str, ast.Module]:
    out: dict[str, ast.Module] = {}
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(SRC).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        out[".".join(parts)] = ast.parse(path.read_text(encoding="utf-8"))
    return out


def _imports(name: str, tree: ast.Module) -> set[str]:
    """Absolute names a module imports, resolving relative imports against ``name``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                found.add(node.module or "")
                continue
            parts = name.split(".")[:-1][: -(node.level - 1) or None]
            found.add(".".join([*parts, node.module]) if node.module else ".".join(parts))
    return found


def _belongs_to(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def test_tui_does_not_import_cli() -> None:
    offenders = [
        f"{name} -> {imported}"
        for name, tree in _modules().items()
        if _belongs_to(name, "quartz.tui")
        for imported in _imports(name, tree)
        if _belongs_to(imported, "quartz.cli")
    ]
    assert offenders == []


def test_tui_sources_do_not_even_name_the_cli_package() -> None:
    # `sys.modules.get("quartz.cli.app")` is a reference without an import; the
    # string check is what catches it.
    offenders = [
        str(path.relative_to(PKG))
        for path in sorted((PKG / "tui").rglob("*.py"))
        if "quartz.cli" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_core_turn_has_no_front_end_edge() -> None:
    tree = _modules()["quartz.core.turn"]
    imported = _imports("quartz.core.turn", tree)
    assert sorted(m for m in imported if _belongs_to(m, "quartz.tui") or _belongs_to(m, "quartz.cli")) == []
