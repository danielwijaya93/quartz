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
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.images import load_image_file
from quartz.pycheck import check_python_source

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


def _require_dir(path: str) -> Path:
    """Expand `path` and raise a clear error unless it is an existing directory."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"No such directory: {path}")
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")
    return p


def ls(path: str = ".") -> list[str]:
    """List sorted entries in a directory (trailing `/` for directories)."""
    p = _require_dir(path)
    try:
        items = list(p.iterdir())
    except PermissionError as exc:
        raise PermissionError(f"Permission denied: {path}") from exc
    items.sort(key=lambda x: (not x.is_dir(), x.name.lower()))
    return [f"{item.name}/" if item.is_dir() else item.name for item in items]


def find(pattern: str = "*", path: str = ".", limit: int = 200) -> list[str]:
    """Find files matching a glob pattern, skipping .git/.venv/node_modules.

    Symlinked directories are not followed; unreadable directories are skipped.
    """
    root = _require_dir(path).resolve()
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        for fname in sorted(filenames):
            rel = str(rel_dir / fname) if str(rel_dir) != "." else fname
            if fnmatch.fnmatch(fname, pattern) or fnmatch.fnmatch(rel, pattern):
                hits.append(rel)
                if len(hits) >= limit:
                    return hits
    return hits


_RG_TIMEOUT = 15


def _rg_external(pattern: str, path: str, glob: str | None, max_hits: int, fixed: bool) -> str:
    cmd = ["rg", "-n", "--no-heading", "--color=never", "-m", str(max_hits)]
    if fixed:
        cmd.append("-F")
    if glob:
        cmd.extend(["-g", glob])
    cmd.extend(["--", pattern, path])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_RG_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"rg timed out after {_RG_TIMEOUT}s; narrow the pattern or path"
    if proc.returncode == 0:
        return "\n".join(proc.stdout.strip().splitlines()[:max_hits])
    if proc.returncode == 1:
        return "(no matches)"
    return proc.stderr.strip() or f"rg exited with code {proc.returncode}"


def _rg_python(pattern: str, path: str, glob: str | None, max_hits: int, fixed: bool) -> str:
    try:
        regex = re.compile(re.escape(pattern) if fixed else pattern)
    except re.error as exc:
        return f"Invalid regex {pattern!r}: {exc}"
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


def rg(pattern: str, path: str = ".", glob: str | None = None, max_hits: int = 100, fixed: bool = False) -> str:
    """Search files for a regex pattern (or literal string if fixed=True) and return `file:line:text` matches."""
    if not Path(path).expanduser().exists():
        return f"Path not found: {path}"
    if shutil.which("rg"):
        return _rg_external(pattern, path, glob, max_hits, fixed)
    return _rg_python(pattern, path, glob, max_hits, fixed)


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
    """Atomically write content to path using a temp file in the same directory.

    New files get the process umask applied by the kernel at `open(2)` time (no global
    `os.umask` mutation); existing files keep their current permission bits.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as tmp:
            tmp.write(content)
            tmp.flush()
            if existing_mode is not None:
                os.fchmod(tmp.fileno(), existing_mode)
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except BaseException:
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
    "launch_worker",
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

def _check_python_source(path: Path, content: str) -> list[str]:
    """Run a fast stdlib-symtable check on `.py` source, returning up to 10 warnings."""
    return check_python_source(path, content)


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
    mod = None
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
        # Register before executing, mirroring the import system. Decorators that
        # resolve annotations through sys.modules (dataclass, NamedTuple, Enum)
        # otherwise fail with "'NoneType' object has no attribute '__dict__'".
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    except Exception as exc:
        # Like importlib._bootstrap._load: never leave a half-executed module
        # registered, or a later load would find a broken entry.
        if mod is not None and sys.modules.get(mod_name) is mod:
            sys.modules.pop(mod_name, None)
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
        "`.tail(n=50)`, `.output()`, `.poll()`, `.kill()`. Commands outliving a cell emit `[bash-done]`. "
        "MUST use `bash()` (never `subprocess.Popen`/`sh()`) to launch forked `quartz -p` workers, "
        "or the harness will never wake you on completion — prefer `launch_worker(prompt_file, cwd, session_id)` "
        "which wraps `bash()` with correct quoting and guarantees `[bash-done]` delivery.",
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
