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


def test_core_never_imports_a_front_end() -> None:
    """`core/` is the transport-independent half of quartz.

    A `rich` import in core is how "the web transport cannot reuse this" starts:
    the command layer would print again, and the web server's only remaining option
    would be a second implementation. Same for `quartz.tui`, which owns the terminal.
    """
    offenders = sorted(
        f"{name} -> {imported}"
        for name, tree in _modules().items()
        if _belongs_to(name, "quartz.core")
        for imported in _imports(name, tree)
        if imported == "rich"
        or imported.startswith("rich.")
        or _belongs_to(imported, "quartz.tui")
    )
    assert offenders == []


_SUBPACKAGES = ("auth", "mcp", "providers", "kernel", "core", "tui", "web", "cli")


def _subpackage_of(module: str) -> str | None:
    for pkg in _SUBPACKAGES:
        if _belongs_to(module, f"quartz.{pkg}"):
            return pkg
    return None


def test_subpackage_dependency_graph_is_acyclic() -> None:
    """The subpackage dependency graph across src/quartz must be a strict DAG."""
    edges: dict[str, set[str]] = {pkg: set() for pkg in _SUBPACKAGES}
    for name, tree in _modules().items():
        src_pkg = _subpackage_of(name)
        if src_pkg is None:
            continue
        for imported in _imports(name, tree):
            dst_pkg = _subpackage_of(imported)
            if dst_pkg is not None and dst_pkg != src_pkg:
                edges[src_pkg].add(dst_pkg)

    # Standard 3-color DFS cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {pkg: WHITE for pkg in _SUBPACKAGES}
    cycles: list[list[str]] = []

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in sorted(edges[node]):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                cycles.append([*stack[idx:], nxt])
            elif color[nxt] == WHITE:
                dfs(nxt, stack)
        stack.pop()
        color[node] = BLACK

    for pkg in _SUBPACKAGES:
        if color[pkg] == WHITE:
            dfs(pkg, [])

    assert cycles == [], f"Circular subpackage dependencies found: {cycles}"


def _forbidden_edges(src_pkg: str, forbidden: tuple[str, ...]) -> list[str]:
    offenders: list[str] = []
    for name, tree in _modules().items():
        if not _belongs_to(name, f"quartz.{src_pkg}"):
            continue
        for imported in _imports(name, tree):
            for target in forbidden:
                if "." in target:
                    hit = _belongs_to(imported, target)
                elif target in _SUBPACKAGES:
                    hit = _belongs_to(imported, f"quartz.{target}")
                else:
                    hit = imported == target or imported.startswith(f"{target}.")
                if hit:
                    offenders.append(f"{name} -> {imported}")
    return sorted(offenders)


def test_auth_layer_boundaries() -> None:
    assert _forbidden_edges("auth", ("providers", "kernel", "core", "tui", "web", "cli")) == []


def test_providers_layer_boundaries() -> None:
    assert _forbidden_edges("providers", ("core", "kernel", "tui", "web", "cli")) == []


def test_kernel_layer_boundaries() -> None:
    assert _forbidden_edges("kernel", ("core", "providers", "auth", "tui", "web", "cli")) == []


def test_mcp_layer_boundaries() -> None:
    assert _forbidden_edges("mcp", ("core", "kernel", "providers", "auth", "tui", "web", "cli")) == []


def test_core_layer_boundaries() -> None:
    assert _forbidden_edges("core", ("tui", "web", "cli", "rich", "fastapi")) == []


def test_web_layer_boundaries() -> None:
    assert _forbidden_edges("web", ("tui", "cli", "rich")) == []


def test_tui_layer_boundaries() -> None:
    assert _forbidden_edges("tui", ("web", "cli", "fastapi")) == []
