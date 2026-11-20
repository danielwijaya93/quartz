"""Pluggable REPL helper functions with built-in defaults and user-level extensibility."""

from __future__ import annotations

import ast
import builtins
import contextvars
import dataclasses
import difflib
import fnmatch
import importlib.util
import inspect
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.images import load_image_file

_IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}

_IMAGES_VAR: contextvars.ContextVar[list[dict[str, str]] | None] = contextvars.ContextVar(
    "quartz_images", default=None
)


def _record_cell_image(img: dict[str, str]) -> None:
    sink = _IMAGES_VAR.get()
    if sink is not None:
        sink.append(img)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n... [{omitted} chars truncated] ...\n{text[-half:]}"


# --- Built-in Helpers ---


def ls(path: str = ".") -> list[str]:
    """List sorted entries in a directory (trailing `/` for directories)."""
    p = Path(path).expanduser()
    entries = []
    for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        entries.append(f"{item.name}/" if item.is_dir() else item.name)
    return entries


def find(pattern: str = "*", path: str = ".", limit: int = 200) -> list[str]:
    """Find files matching a glob pattern, skipping .git/.venv/node_modules."""
    root = Path(path).expanduser().resolve()
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        for fname in sorted(filenames):
            rel = str(rel_dir / fname) if str(rel_dir) != "." else fname
            if fnmatch.fnmatch(fname, pattern) or fnmatch.fnmatch(rel, pattern):
                hits.append(rel)
                if len(hits) >= limit:
                    return hits
    return hits


def rg(pattern: str, path: str = ".", glob: str | None = None, max_hits: int = 100, fixed: bool = False) -> str:
    """Search files for a regex pattern (or literal string if fixed=True) and return `file:line:text` matches."""
    if shutil.which("rg"):
        cmd = ["rg", "-n", "--no-heading", "--color=never", "-m", str(max_hits)]
        if fixed:
            cmd.append("-F")
        if glob:
            cmd.extend(["-g", glob])
        cmd.extend([pattern, path])
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = proc.stdout.strip()
        if proc.returncode == 0:
            lines = out.splitlines()[:max_hits]
            return "\n".join(lines)
        if proc.returncode == 1:
            return "(no matches)"
        return proc.stderr.strip() or f"rg exited with code {proc.returncode}"

    regex_pattern = re.escape(pattern) if fixed else pattern
    regex = re.compile(regex_pattern)
    root = Path(path).expanduser()
    if root.is_file():
        files = [root]
    else:
        files = [root / rel for rel in find(glob or "*", str(root), limit=2000)]
    hits: list[str] = []
    for fpath in files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for idx, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{fpath}:{idx}:{line}")
                if len(hits) >= max_hits:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else "(no matches)"


def read(path: str, start: int = 1, end: int = 200) -> str:
    """Read 1-based inclusive line range from a file with line numbers."""
    p = Path(path).expanduser()
    s = max(1, start)
    out: list[str] = []
    total_lines = 0
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for idx, raw_line in enumerate(fh, 1):
            total_lines = idx
            if idx < s:
                continue
            if end is not None and idx > end:
                break
            out.append(f"{idx}: {raw_line.rstrip(chr(10)).rstrip(chr(13))}")
    if s > total_lines:
        return f"(EOF: file has {total_lines} lines)"
    return "\n".join(out)


def _atomic_write(path: Path, content: str, newline: str | None = None) -> None:
    """Atomically write content to path using a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline=newline,
        dir=path.parent,
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def edit(path: str, old: str, new: str) -> str:
    """Replace an exact unique occurrence of `old` with `new` in `path` and return a unified diff."""
    p = Path(path).expanduser()
    raw_bytes = p.read_bytes()
    has_crlf = b"\r\n" in raw_bytes
    text = raw_bytes.decode("utf-8")

    # If the file has CRLF but old was provided with LF, or vice versa, adapt old
    if old not in text:
        if has_crlf and "\r\n" not in old and "\n" in old:
            adapted_old = old.replace("\n", "\r\n")
            if adapted_old in text:
                old = adapted_old
        elif not has_crlf and "\r\n" in old:
            adapted_old = old.replace("\r\n", "\n")
            if adapted_old in text:
                old = adapted_old

    # If the file uses CRLF, ensure replacement new maintains CRLF consistency
    if has_crlf and "\r\n" not in new and "\n" in new:
        new = new.replace("\n", "\r\n")

    count = text.count(old)
    if count == 0:
        raise ValueError(f"Target text not found in {path}")
    if count > 1:
        raise ValueError(f"Target text matched {count} times in {path}; provide more context")
    updated = text.replace(old, new, 1)
    _atomic_write(p, updated, newline="")
    diff_lines = list(
        difflib.unified_diff(
            text.splitlines(),
            updated.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=2,
            lineterm="",
        )
    )
    if not diff_lines:
        msg = f"Updated {path} (no changes)"
    else:
        msg = f"Updated {path}\n{_truncate(chr(10).join(diff_lines), 8000)}"
    if p.suffix == ".py":
        warnings = _check_python_source(p, updated)
        if warnings:
            msg += "\n[python check]\n" + "\n".join(warnings)
    return msg


def write(path: str, content: str) -> str:
    """Write `content` to `path`, creating parent directories as needed."""
    p = Path(path).expanduser()
    _atomic_write(p, content)
    msg = f"Wrote {len(content)} chars to {path}"
    if p.suffix == ".py":
        warnings = _check_python_source(p, content)
        if warnings:
            msg += "\n[python check]\n" + "\n".join(warnings)
    return msg


def view_image(path: str) -> str:
    """Attach an image file from disk so the model can inspect it on the next step."""
    att = load_image_file(Path(path))
    _record_cell_image({"mime_type": att.mime_type, "data": att.data_b64})
    return f"Attached {att.label} ({att.mime_type}) for viewing"


# --- Registry and Extensibility ---

PROTECTED_NAMES: frozenset[str] = frozenset({
    "bash",
    "BashHandle",
    "BashResult",
    "mcp",
    "gather",
    "Path",
    "os",
    "sys",
    "json",
    "re",
    "asyncio",
    "time",
    "shutil",
    "__name__",
    "__builtins__",
    "_",
})

BUILTIN_HELPERS: dict[str, Callable[..., Any]] = {
    "ls": ls,
    "find": find,
    "rg": rg,
    "read": read,
    "edit": edit,
    "write": write,
    "view_image": view_image,
}

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
    }
)
_BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins)) | _STD_MODULE_DUNDERS


def _is_type_checking_if(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _collect_walrus_targets(node: ast.AST, bound: set[str]) -> None:
    """Collect NamedExpr (:=) targets inside comprehensions, stopping at def/class/lambda."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
            bound.add(child.target.id)
        _collect_walrus_targets(child, bound)


def _collect_scope_bindings(nodes: list[ast.stmt] | ast.AST, *, is_comprehension: bool = False) -> set[str]:
    """Collect names bound directly in the current scope without descending into child scopes."""
    bound: set[str] = set()
    stack: list[ast.AST] = list(nodes) if isinstance(nodes, list) else [nodes]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            continue
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    bound.add(alias.asname or alias.name.split(".")[0])
            continue
        elif isinstance(node, ast.MatchAs):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, ast.MatchStar):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                bound.add(node.rest)
        elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
            if isinstance(node.name, ast.Name):
                bound.add(node.name.id)
            continue
        elif isinstance(node, ast.Lambda):
            continue
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            if not is_comprehension:
                _collect_walrus_targets(node, bound)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return bound


def _arg_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _type_param_names(node: ast.AST) -> set[str]:
    return {tp.name for tp in getattr(node, "type_params", ()) if hasattr(tp, "name")}


class _UndefinedNameChecker(ast.NodeVisitor):
    """Lightweight scope-aware AST pass detecting unbound Name(ctx=Load) references."""

    def __init__(self, allowed: frozenset[str], skip_annotations: bool) -> None:
        self._allowed = allowed
        self._skip_annotations = skip_annotations
        self._scopes: list[set[str]] = []
        self._class_scope: set[str] | None = None
        self.undefined: list[tuple[int, str]] = []
        self._seen_names: set[str] = set()

    def _is_bound(self, name: str) -> bool:
        if name in self._allowed:
            return True
        if self._class_scope is not None and name in self._class_scope:
            return True
        return any(name in scope for scope in self._scopes)

    def _visit_in_scope(self, nodes: list[ast.AST] | ast.AST, extra_bindings: set[str]) -> None:
        self._scopes.append(extra_bindings)
        saved_class = self._class_scope
        self._class_scope = None
        try:
            if isinstance(nodes, list):
                for item in nodes:
                    self.visit(item)
            else:
                self.visit(nodes)
        finally:
            self._class_scope = saved_class
            self._scopes.pop()

    def visit_Module(self, node: ast.Module) -> None:
        bindings = _collect_scope_bindings(node.body)
        self._visit_in_scope(list(node.body), bindings)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        if not _is_type_checking_if(node):
            for stmt in node.body:
                self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if not self._skip_annotations:
            self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for d in node.args.defaults:
            self.visit(d)
        for d in node.args.kw_defaults:
            if d is not None:
                self.visit(d)

        tp_names = _type_param_names(node)
        self._scopes.append(tp_names)
        saved_class = self._class_scope
        self._class_scope = None
        try:
            if not self._skip_annotations:
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    if arg.annotation is not None:
                        self.visit(arg.annotation)
                if node.args.vararg and node.args.vararg.annotation is not None:
                    self.visit(node.args.vararg.annotation)
                if node.args.kwarg and node.args.kwarg.annotation is not None:
                    self.visit(node.args.kwarg.annotation)
                if node.returns is not None:
                    self.visit(node.returns)

            func_bindings = _arg_names(node.args) | _collect_scope_bindings(node.body)
            self._visit_in_scope(list(node.body), func_bindings)
        finally:
            self._class_scope = saved_class
            self._scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        tp_names = _type_param_names(node)
        self._scopes.append(tp_names)
        saved_class = self._class_scope
        self._class_scope = None
        try:
            for base in node.bases:
                self.visit(base)
            for kw in node.keywords:
                self.visit(kw.value)
            # Class body bindings are visible inside the class body itself,
            # but NOT to nested functions/classes/methods inside it.
            self._class_scope = _collect_scope_bindings(node.body)
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self._class_scope = saved_class
            self._scopes.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for d in node.args.defaults:
            self.visit(d)
        for d in node.args.kw_defaults:
            if d is not None:
                self.visit(d)
        self._visit_in_scope(node.body, _arg_names(node.args))

    def _visit_comprehension(self, generators: list[ast.comprehension], body_nodes: list[ast.AST]) -> None:
        if generators:
            self.visit(generators[0].iter)
        comp_bindings: set[str] = set()
        for gen in generators:
            comp_bindings |= _collect_scope_bindings(gen.target, is_comprehension=True)
        self._scopes.append(comp_bindings)
        saved_class = self._class_scope
        self._class_scope = None
        try:
            for i, gen in enumerate(generators):
                if i > 0:
                    self.visit(gen.iter)
                for cond in gen.ifs:
                    self.visit(cond)
            for b in body_nodes:
                self.visit(b)
        finally:
            self._class_scope = saved_class
            self._scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])

    def visit_TypeAlias(self, node: Any) -> None:
        if not self._skip_annotations:
            self._visit_in_scope(node.value, _type_param_names(node))

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and not self._is_bound(node.id):
            if node.id not in self._seen_names:
                self._seen_names.add(node.id)
                self.undefined.append((node.lineno, node.id))


def _check_python_source(path: Path, content: str) -> list[str]:
    """Run a fast stdlib-AST check on `.py` source, returning up to 10 warnings."""
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(content, filename=str(path))
    except SyntaxError as exc:
        return [f"SyntaxError at line {exc.lineno}: {exc.msg}"]

    has_star_import = any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    )
    if has_star_import:
        return []

    has_future_annotations = any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "__future__"
        and any(alias.name == "annotations" for alias in stmt.names)
        for stmt in tree.body
    )

    allowed = _BUILTIN_NAMES
    if path.name == "helpers.py" and path.parent.name == ".quartz":
        allowed = _BUILTIN_NAMES | frozenset(BUILTIN_HELPERS.keys()) | PROTECTED_NAMES

    checker = _UndefinedNameChecker(allowed=allowed, skip_annotations=has_future_annotations)
    checker.visit(tree)
    return [
        f"line {lineno}: undefined name '{name}' (missing import?)"
        for lineno, name in checker.undefined[:10]
    ]


@dataclasses.dataclass
class HelperInfo:
    name: str
    func: Callable[..., Any]
    source: str  # "builtin", "global", "project"
    signature: str
    doc: str
    file_path: str | None = None


@dataclasses.dataclass
class LoadedHelpers:
    functions: dict[str, Callable[..., Any]]
    infos: dict[str, HelperInfo]
    warnings: list[str] = dataclasses.field(default_factory=list)


def _extract_doc(func: Callable[..., Any]) -> str:
    doc = inspect.getdoc(func)
    if not doc:
        return "User-defined helper function."
    lines = doc.strip().splitlines()
    return lines[0].strip() if lines else "User-defined helper function."


def _extract_sig(func: Callable[..., Any]) -> str:
    try:
        return str(inspect.signature(func))
    except Exception:
        return "(...)"


def _load_helper_file(
    file_path: Path,
    source: str,
    target: LoadedHelpers,
) -> None:
    if not file_path.is_file():
        return
    mod_name = f"_quartz_helpers_{source}_{file_path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, file_path)
        if spec is None or spec.loader is None:
            target.warnings.append(f"Could not load spec for helper file: {file_path}")
            return
        mod = importlib.util.module_from_spec(spec)
        # Pre-inject built-in helpers so custom helpers can call them directly
        for name, fn in BUILTIN_HELPERS.items():
            mod.__dict__.setdefault(name, fn)
        # Also pre-inject any already-registered custom functions from prior files
        for name, fn in target.functions.items():
            mod.__dict__.setdefault(name, fn)
        spec.loader.exec_module(mod)
    except Exception as exc:
        target.warnings.append(f"Failed to load helpers from {file_path}: {exc}")
        return

    exported_names = getattr(mod, "__all__", None)
    if exported_names is not None:
        candidates = [attr for attr in exported_names if hasattr(mod, attr)]
    else:
        candidates = [
            attr
            for attr in dir(mod)
            if not attr.startswith("_")
            and inspect.isfunction(getattr(mod, attr))
            and getattr(getattr(mod, attr), "__module__", None) == mod.__name__
        ]

    for attr_name in candidates:
        val = getattr(mod, attr_name)
        if not callable(val):
            continue
        if attr_name in PROTECTED_NAMES:
            target.warnings.append(
                f"Cannot override protected primitive '{attr_name}' in {file_path}"
            )
            continue

        target.functions[attr_name] = val
        target.infos[attr_name] = HelperInfo(
            name=attr_name,
            func=val,
            source=source,
            signature=_extract_sig(val),
            doc=_extract_doc(val),
            file_path=str(file_path),
        )


def load_helpers(
    cwd: Path | None = None,
    *,
    global_helpers_path: Path | None = None,
    project_helpers_path: Path | None = None,
) -> LoadedHelpers:
    """Load built-in helpers, then merge global ~/.quartz/helpers.py and project .quartz/helpers.py."""
    functions: dict[str, Callable[..., Any]] = {}
    infos: dict[str, HelperInfo] = {}
    loaded = LoadedHelpers(functions=functions, infos=infos, warnings=[])

    # 1. Register built-ins
    for name, fn in BUILTIN_HELPERS.items():
        functions[name] = fn
        infos[name] = HelperInfo(
            name=name,
            func=fn,
            source="builtin",
            signature=_extract_sig(fn),
            doc=_extract_doc(fn),
            file_path=None,
        )

    # 2. Global helpers (~/.quartz/helpers.py)
    if global_helpers_path is not None:
        g_path = global_helpers_path
    else:
        env_g = os.getenv("QUARTZ_GLOBAL_HELPERS")
        g_path = Path(env_g).expanduser() if env_g else Path.home() / ".quartz" / "helpers.py"
    _load_helper_file(g_path, "global", loaded)

    # 3. Project-local helpers (.quartz/helpers.py)
    if project_helpers_path is not None:
        p_path = project_helpers_path
    else:
        base_dir = (cwd or Path.cwd()).resolve()
        p_path = base_dir / ".quartz" / "helpers.py"
    _load_helper_file(p_path, "project", loaded)

    return loaded


def format_helpers_for_prompt(loaded: LoadedHelpers) -> str:
    """Assemble the ## REPL Functions markdown block for system prompt."""
    lines = [
        "## REPL Functions",
        "- `bash(command, timeout=600) -> BashHandle`: runs a shell command in the background; "
        "`await handle -> BashResult(.exit_code, .output, .duration)`. Also `.pid`, `.running`, "
        "`.tail(n=50)`, `.output()`, `.poll()`, `.kill()`. Commands outliving a cell emit `[bash-done]`.",
        "- `ls(path='.')`, `find(pattern='*', path='.', limit=200)`, `rg(pattern, path='.', glob=None, max_hits=100, fixed=False)` "
        "(use raw strings r'...' for regex patterns with backslashes to avoid Python SyntaxWarning, or fixed=True for literal string search)",

        "- `read(path, start=1, end=200)`, `edit(path, old, new)`, `write(path, content)`, `view_image(path)`, `gather(*coros)`",
        "- Python stdlib modules (e.g. `Path`, `os`, `json`, `re`, `datetime`, `subprocess`, `collections`, etc.) "
        "are auto-imported on attribute access in `<ipython>` cells, while explicit imports are still required "
        "when writing or editing project `.py` files.",
    ]

    custom_helpers = [
        info for info in loaded.infos.values() if info.source in ("global", "project")
    ]
    if custom_helpers:
        lines.append("- Custom user helpers:")
        for ch in sorted(custom_helpers, key=lambda x: (x.source != "project", x.name)):
            lines.append(f"  - `{ch.name}{ch.signature}`: {ch.doc} ({ch.source})")

    return "\n".join(lines)
