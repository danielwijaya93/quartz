"""Pluggable REPL helper functions with built-in defaults and user-level extensibility."""

from __future__ import annotations

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
        return f"Updated {path} (no changes)"
    return f"Updated {path}\n{_truncate(chr(10).join(diff_lines), 8000)}"


def write(path: str, content: str) -> str:
    """Write `content` to `path`, creating parent directories as needed."""
    p = Path(path).expanduser()
    _atomic_write(p, content)
    return f"Wrote {len(content)} chars to {path}"


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
    ]

    custom_helpers = [
        info for info in loaded.infos.values() if info.source in ("global", "project")
    ]
    if custom_helpers:
        lines.append("- Custom user helpers:")
        for ch in sorted(custom_helpers, key=lambda x: (x.source != "project", x.name)):
            lines.append(f"  - `{ch.name}{ch.signature}`: {ch.doc} ({ch.source})")

    return "\n".join(lines)
