"""Pluggable REPL helper functions with built-in defaults and user-level extensibility."""

from __future__ import annotations

import base64
import contextvars
import dataclasses
import difflib
import fnmatch
import importlib.util
import inspect
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.kernel.pycheck import check_python_source
from quartz.media import load_image_file
from quartz.settings import is_project_trusted

_IGNORE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tooling",
    ".next",
    "coverage",
}
_RG_DEFAULT_EXCLUDES = (
    "!**/dist/**",
    "!**/build/**",
    "!**/.tooling/**",
    "!**/.next/**",
    "!**/coverage/**",
    "!**/node_modules/**",
    "!**/.venv/**",
    "!**/__pycache__/**",
    "!*.min.js",
    "!*.min.css",
)
_RG_MAX_COLUMNS = 200
MAX_READ_CHARS = 16_000
MAX_EDIT_DIFF_CHARS = 1_500

_IMAGES_VAR: contextvars.ContextVar[list[dict[str, str]] | None] = contextvars.ContextVar(
    "quartz_images", default=None
)
_FILES_VAR: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "quartz_files", default=None
)

_EXTRA_MIME_TYPES: dict[str, str] = {
    ".apk": "application/vnd.android.package-archive",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".jsonl": "application/x-ndjson",
}


def _record_cell_image(img: dict[str, str]) -> None:
    sink = _IMAGES_VAR.get()
    if sink is not None:
        sink.append(img)


def _record_cell_file(file_meta: dict[str, Any]) -> None:
    sink = _FILES_VAR.get()
    if sink is not None:
        sink.append(file_meta)


def _format_file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"


def _guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _EXTRA_MIME_TYPES:
        return _EXTRA_MIME_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream" 


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n... [{omitted} chars truncated] ...\n{text[-half:]}"


# --- Built-in Helpers ---


def _closest_path_hint(p: Path) -> str:
    """Return a closest-sibling hint when `p` does not exist."""
    parent = p.parent
    if not parent.is_dir() or not p.name:
        return ""
    try:
        candidates = [entry.name for entry in parent.iterdir()]
    except OSError:
        return ""
    matches = difflib.get_close_matches(p.name, candidates, n=3, cutoff=0.5)
    if not matches:
        return ""
    return f" (did you mean: {', '.join(matches)}?)"


def _require_dir(path: str) -> Path:
    """Expand `path` and raise a clear error unless it is an existing directory."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"No such directory: {path}{_closest_path_hint(p)}")
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")
    return p


def _resolve_with_cwd(path: str, cwd: str | Path | None) -> str:
    """Resolve a relative path against `cwd` when provided."""
    if cwd is None:
        return path
    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return str(expanded)
    return str(Path(cwd).expanduser() / expanded)


def ls(path: str = ".", *, cwd: str | Path | None = None) -> list[str]:
    """List sorted entries in a directory (trailing `/` for directories)."""
    target = _resolve_with_cwd(path, cwd)
    p = _require_dir(target)
    try:
        items = list(p.iterdir())
    except PermissionError as exc:
        raise PermissionError(f"Permission denied: {path}") from exc
    items.sort(key=lambda x: (not x.is_dir(), x.name.lower()))
    return [f"{item.name}/" if item.is_dir() else item.name for item in items]


def find(
    pattern: str = "*", path: str = ".", limit: int = 200, *, cwd: str | Path | None = None
) -> list[str]:
    """Find files matching a glob pattern, skipping .git/.venv/node_modules.

    Symlinked directories are not followed; unreadable directories are skipped.
    """
    root = _require_dir(_resolve_with_cwd(path, cwd)).resolve()
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
    cmd = [
        "rg",
        "-n",
        "--no-heading",
        "--color=never",
        "--max-columns",
        str(_RG_MAX_COLUMNS),
        "--max-columns-preview",
        "-m",
        str(max_hits),
    ]
    if fixed:
        cmd.append("-F")
    # Exclude noisy build/vendor artifacts unless the caller explicitly targets one of them.
    path_parts = set(Path(path).parts)
    if not (path_parts & _IGNORE_DIRS):
        for excl in _RG_DEFAULT_EXCLUDES:
            cmd.extend(["-g", excl])
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
    files = [root] if root.is_file() else [root / rel for rel in find(glob or "*", str(root), limit=2000)]
    hits: list[str] = []
    for fpath in files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for idx, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                clipped = line if len(line) <= _RG_MAX_COLUMNS else f"{line[:_RG_MAX_COLUMNS]}... [truncated]"
                hits.append(f"{fpath}:{idx}:{clipped}")
                if len(hits) >= max_hits:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else "(no matches)"


def rg(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    max_hits: int = 100,
    fixed: bool = False,
    *,
    cwd: str | Path | None = None,
) -> str:
    """Search files for a regex pattern (or literal string if fixed=True) and return `file:line:text` matches."""
    expanded_path = str(Path(_resolve_with_cwd(path, cwd)).expanduser())
    if not Path(expanded_path).exists():
        return f"Path not found: {path}{_closest_path_hint(Path(expanded_path))}"
    if shutil.which("rg"):
        return _rg_external(pattern, expanded_path, glob, max_hits, fixed)
    return _rg_python(pattern, expanded_path, glob, max_hits, fixed)


def read(path: str, start: int = 1, end: int = 200) -> str:
    """Read 1-based inclusive line range from a file with line numbers."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"No such file: {path}{_closest_path_hint(p)}")
    s = max(1, start)
    out: list[str] = []
    total_lines = 0
    char_count = 0
    truncated_at: int | None = None
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for idx, raw_line in enumerate(fh, 1):
            total_lines = idx
            if idx < s:
                continue
            if end is not None and idx > end:
                break
            formatted = f"{idx}: {raw_line.rstrip(chr(10)).rstrip(chr(13))}"
            if out and char_count + len(formatted) + 1 > MAX_READ_CHARS:
                truncated_at = idx
                break
            out.append(formatted)
            char_count += len(formatted) + 1
    if s > total_lines:
        return f"(EOF: file has {total_lines} lines)"
    if truncated_at is not None:
        out.append(
            f"... [truncated at {MAX_READ_CHARS:,} chars; continue with read({path!r}, start={truncated_at}, end={end})]"
        )
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


def _find_unique_rstrip_span(text: str, old: str) -> str | None:
    """Return the exact slice in `text` matching `old` after rstrip() per line, if unique."""
    if not old:
        return None
    old_lines = [line.rstrip() for line in old.splitlines()]
    if not old_lines:
        return None
    raw_lines = text.splitlines(keepends=True)
    stripped_lines = [line.rstrip("\r\n").rstrip() for line in raw_lines]
    n = len(old_lines)
    matches: list[int] = [
        i for i in range(len(stripped_lines) - n + 1) if stripped_lines[i : i + n] == old_lines
    ]
    if len(matches) != 1:
        return None
    start_idx = matches[0]
    span = "".join(raw_lines[start_idx : start_idx + n])
    # Preserve whether `old` ended with a newline so surrounding line breaks stay intact.
    if not old.endswith(("\n", "\r")) and span.endswith(("\r\n", "\n")):
        span = span.rstrip("\r\n")
    return span


def _match_line_numbers(text: str, old: str, limit: int = 8) -> list[int]:
    """Return 1-based line numbers where `old` begins in `text`."""
    if not old:
        return []
    lines: list[int] = []
    pos = 0
    while len(lines) < limit:
        idx = text.find(old, pos)
        if idx == -1:
            break
        lines.append(text.count("\n", 0, idx) + 1)
        pos = idx + len(old)
    return lines


def _closest_match_hint(text: str, old: str) -> str:
    """Return a concise closest-match snippet with line number for failed `edit()` calls."""
    text_lines = text.splitlines()
    old_lines = old.splitlines()
    if not text_lines or not old_lines:
        return ""
    window = min(len(old_lines), len(text_lines))
    best_ratio = 0.0
    best_idx = 0
    old_joined = "\n".join(old_lines)
    # Cap scan on huge files to keep failure diagnostics instantaneous.
    max_start = min(len(text_lines) - window + 1, 2000)
    for i in range(max_start):
        cand = "\n".join(text_lines[i : i + window])
        ratio = difflib.SequenceMatcher(None, old_joined, cand).quick_ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i
    if best_ratio < 0.45:
        return ""
    snippet = "\n".join(f"{best_idx + j + 1}: {text_lines[best_idx + j]}" for j in range(min(window, 6)))
    return f". Closest match around line {best_idx + 1}:\n{snippet}"


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
        # Fallback: match ignoring trailing whitespace per line only when unique.
        matched_span = _find_unique_rstrip_span(text, old)
        if matched_span is not None:
            old = matched_span
            count = 1
        else:
            hint = _closest_match_hint(text, old)
            raise ValueError(f"Target text not found in {path}{hint}")
    if count > 1:
        lines = _match_line_numbers(text, old)
        line_info = f" (lines {', '.join(map(str, lines))})" if lines else ""
        raise ValueError(f"Target text matched {count} times in {path}{line_info}; provide more context")
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
        msg = f"Updated {path}\n{_truncate(chr(10).join(diff_lines), MAX_EDIT_DIFF_CHARS)}"
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


_MAX_SANDBOX_TRANSFER_BYTES = 16 * 1024 * 1024  # 16 MiB IPC cap for sandbox /tmp files


def attach_file(path: str, label: str | None = None) -> str:
    """Attach any file from disk so the user can download or open it in the reply."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"No such file: {path}")
    if p.is_dir():
        raise IsADirectoryError(f"Expected a file, got a directory: {path}")
    if not p.is_file():
        raise OSError(f"Not a regular file: {path}")
    size = p.stat().st_size
    name = label.strip() if label and label.strip() else p.name
    mime_type = _guess_mime_type(p)
    meta: dict[str, Any] = {
        "path": str(p),
        "name": name,
        "mime_type": mime_type,
        "size": size,
    }
    # Inside OS sandboxes (bwrap), /tmp is private to the kernel and invisible to quartz-web.
    # Include base64 bytes for non-workspace files so the host can persist them.
    try:
        in_cwd = p.is_relative_to(Path.cwd().resolve())
    except OSError:
        in_cwd = False
    if not in_cwd and size <= _MAX_SANDBOX_TRANSFER_BYTES:
        meta["data_b64"] = base64.b64encode(p.read_bytes()).decode("ascii")
    _record_cell_file(meta)
    return f"Attached {name} ({mime_type}, {_format_file_size(size)}) for download"


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

def _parse_skill_frontmatter(skill_md: Path) -> tuple[str, str] | None:
    """Extract (name, description) from a SKILL.md YAML frontmatter block."""
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None

    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return None

    frontmatter = match.group(1)
    name = ""
    description = ""
    lines = frontmatter.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("description:"):
            raw_val = line.split(":", 1)[1].strip()
            if raw_val in (">", ">-", "|", "|-"):
                block_lines: list[str] = []
                idx += 1
                while idx < len(lines):
                    cont = lines[idx]
                    if cont and not cont[0].isspace():
                        idx -= 1
                        break
                    stripped = cont.strip()
                    if stripped:
                        block_lines.append(stripped)
                    idx += 1
                description = " ".join(block_lines)
            else:
                description = raw_val.strip("\"'")
        idx += 1
    if not name:
        name = skill_md.parent.name
    return (name, description)


def discover_skills(skills_dir: Path | None = None) -> list[tuple[str, str, Path]]:
    """Discover SKILL.md files exclusively from ~/.agents/skills."""
    root = (skills_dir or (Path.home() / ".agents" / "skills")).expanduser()
    if not root.is_dir():
        return []

    skills_by_name: dict[str, tuple[str, str, Path]] = {}
    for entry in sorted(root.iterdir()):
        skill_md = entry / "SKILL.md"
        if entry.is_dir() and skill_md.is_file():
            parsed = _parse_skill_frontmatter(skill_md)
            if parsed:
                name, desc = parsed
                skills_by_name[name] = (name, desc, skill_md)
    return list(skills_by_name.values())


def find_skills(
    query: str = "",
    skills_dir: Path | None = None,
    limit: int = 20,
) -> list[dict[str, str]]:
    """Search installed skills by keyword and return matching {name, description, path} entries."""
    terms = [t.lower() for t in query.split() if t.strip()]
    matches: list[dict[str, str]] = []
    for name, desc, path in discover_skills(skills_dir=skills_dir):
        if terms:
            haystack = f"{name} {desc}".lower()
            if not all(t in haystack for t in terms):
                continue
        matches.append({"name": name, "description": desc, "path": str(path)})
        if limit > 0 and len(matches) >= limit:
            break
    return matches


BUILTIN_HELPERS: dict[str, Callable[..., Any]] = {
    "ls": ls,
    "find": find,
    "rg": rg,
    "read": read,
    "edit": edit,
    "write": write,
    "view_image": view_image,
    "attach_file": attach_file,
    "find_skills": find_skills,
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
    trusted: bool | None = None,
) -> LoadedHelpers:
    """Load built-in helpers, then merge global ~/.quartz/helpers.py and trusted project .quartz/helpers.py."""
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

    # 3. Project-local helpers (.quartz/helpers.py) -- only when project is trusted
    base_dir = (cwd or Path.cwd()).resolve()
    p_path = project_helpers_path if project_helpers_path is not None else base_dir / ".quartz" / "helpers.py"
    if p_path.is_file():
        is_trusted = is_project_trusted(base_dir) if trusted is None else trusted
        if is_trusted:
            _load_helper_file(p_path, "project", loaded)
        else:
            msg = (
                f"Skipped untrusted project helpers at {p_path}. "
                "Pass --trust-project or set QUARTZ_TRUST_PROJECT=1 to enable."
            )
            loaded.warnings.append(msg)
            warnings.warn(msg, UserWarning, stacklevel=2)

    return loaded


def format_helpers_for_prompt(loaded: LoadedHelpers) -> str:
    """Assemble the ## REPL Functions markdown block for system prompt."""
    lines = [
        "## REPL Functions",
        "- `bash(command, timeout=600) -> BashHandle`: background shell (`await handle -> BashResult(.exit_code, .output, .duration)`, "
        "`.pid`, `.running`, `.tail(n=50)`, `.output()`, `.poll()`, `.kill()`). Emits `[bash-done]` on completion and `[bash-status]` while running "
        "(update user, don't kill). Use `launch_worker(prompt_file, cwd, session_id)` or `bash()` (never `Popen`/`sh()`) for `quartz -p` workers. "
        "In `-p` runs, `bash()` runs synchronously and `launch_worker` is refused.",
        "- `ls(path='.')`, `find(pattern='*', path='.', limit=200)`, `rg(pattern, path='.', glob=None, max_hits=100, fixed=False)` "
        "(use raw strings r'...' for regex patterns with backslashes to avoid Python SyntaxWarning, or fixed=True for literal string search)",

        "- `read(path, start=1, end=200)`, `edit(path, old, new)`, `write(path, content)`, `view_image(path)`, `attach_file(path, label=None)`, `find_skills(query='', limit=20)`, `gather(*coros)`",
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
