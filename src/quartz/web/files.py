"""Directory listing for the web folder picker, and nothing else.

`docs/web-protocol.md` (*Sidebar and filesystem API*) allows the browser to send a
filesystem path, which is a real widening of the old "never accept a raw path"
rule. This module is the fence around it, and the fence is the whole design:

- **List only directories.** Entries carry a name and `isDir`. No file contents,
  no sizes, no symlink targets, no way to read a file.
- **`mkdir` is the only mutation.** One new directory inside a parent. No delete,
  no rename, no move, no write.
- **Absolute or `~` only.** A relative path is an error rather than a resolution
  against whatever directory the server happens to be in.
- **A bad path is a value.** Missing, unreadable, not-a-directory: all of them come
  back as `error` with an empty listing, because the picker has to render the
  failure and let the person go back up.

Everything here is blocking filesystem work; callers run it off the event loop.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from typing import Any

__all__ = [
    "DirectoryEntry",
    "DirectoryListing",
    "create_directory",
    "list_directories",
    "list_workspace_files",
    "resolve_directory",
]


@dataclasses.dataclass(frozen=True)
class DirectoryEntry:
    """One row of the picker.

    `path` is the whole child path, so the client never joins a name onto a parent:
    a client that built paths from strings would be one escaping bug away from
    reading a different directory than the row it drew. `is_dir` is always true
    today, and stays in the wire shape so the client never has to assume it.
    """

    name: str
    path: str
    is_dir: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "isDir": self.is_dir}


@dataclasses.dataclass(frozen=True)
class DirectoryListing:
    """What the picker needs: where it is, how to go up, and what is below."""

    path: str
    parents: list[dict[str, str]]
    entries: list[DirectoryEntry]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "parents": list(self.parents),
            "entries": [entry.to_dict() for entry in self.entries],
            "error": self.error,
        }


def resolve_directory(raw: Any, fallback: Path) -> tuple[Path | None, str | None]:
    """Turn a client path into a real directory, or explain why not.

    Returns ``(path, error)``. An absent value means "start where the server
    starts", which is the only sane default for a picker: `quartz web --cwd`.
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return fallback.expanduser().resolve(), None
    if not isinstance(raw, str):
        return None, "path must be a string"
    if "\x00" in raw:
        raise ValueError("Invalid path: null byte")
    text = raw.strip()
    if not text.startswith(("~", os.sep)):
        return None, "path must be absolute"
    try:
        candidate = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"cannot resolve path: {exc}"
    if not candidate.is_dir():
        return None, f"not a directory: {candidate}"
    return candidate, None


def _breadcrumbs(path: Path) -> list[dict[str, str]]:
    """Every ancestor of `path`, root first, so the picker can walk up by tapping.

    Each crumb carries its full path because that is what `/api/fs/list` takes: the
    client never assembles a path from names, which would be one escaping bug away
    from something that means a different directory.
    """
    crumbs: list[dict[str, str]] = []
    for parent in reversed(path.parents):
        crumbs.append({"name": parent.name or os.sep, "path": str(parent)})
    crumbs.append({"name": path.name or os.sep, "path": str(path)})
    return crumbs


def list_directories(raw: Any, fallback: Path) -> DirectoryListing:
    """Directories inside `raw`, plus its breadcrumb. Never raises."""
    path, error = resolve_directory(raw, fallback)
    if path is None or error is not None:
        return DirectoryListing(path=str(raw or fallback), parents=[], entries=[], error=error)
    try:
        with os.scandir(path) as scan:
            names = sorted(
                (entry.name for entry in scan if _is_dir_entry(entry)),
                key=str.casefold,
            )
    except OSError as exc:
        return DirectoryListing(
            path=str(path), parents=_breadcrumbs(path), entries=[], error=str(exc)
        )
    return DirectoryListing(
        path=str(path),
        parents=_breadcrumbs(path),
        entries=[DirectoryEntry(name=name, path=str(path / name)) for name in names],
    )


def _is_dir_entry(entry: os.DirEntry[str]) -> bool:
    """Whether an entry is a directory this picker may descend into.

    Symlinks are followed: a project reached through a link is a real project, and
    a dangling link simply fails the check. Hidden entries are skipped because the
    picker is for choosing a project, not for browsing dotfiles.
    """
    if entry.name.startswith("."):
        return False
    try:
        return entry.is_dir(follow_symlinks=True)
    except OSError:
        return False


def create_directory(raw: Any, name: Any, fallback: Path) -> DirectoryListing:
    """Create one directory inside `raw`, then list `raw` again.

    `name` is one path component by contract: `..`, `.`, separators and empty
    strings are rejected rather than normalised, because normalising is how a
    "create a folder" button turns into a "create a folder anywhere" button.
    """
    path, error = resolve_directory(raw, fallback)
    if path is None or error is not None:
        return DirectoryListing(path=str(raw or fallback), parents=[], entries=[], error=error)
    if not isinstance(name, str):
        return DirectoryListing(path=str(path), parents=[], entries=[], error="name must be a string")
    if "\x00" in name:
        raise ValueError("Invalid folder name: null byte")
    cleaned = name.strip()
    if (
        cleaned in ("", ".", "..")
        or "/" in cleaned
        or "\\" in cleaned
        or os.sep in cleaned
        or (os.altsep and os.altsep in cleaned)
    ):
        return DirectoryListing(
            path=str(path),
            parents=[],
            entries=[],
            error="name must be a single folder name",
        )
    target = path / cleaned
    try:
        target.mkdir()
    except FileExistsError:
        return DirectoryListing(
            path=str(path), parents=[], entries=[], error=f"already exists: {cleaned}"
        )
    except OSError as exc:
        return DirectoryListing(path=str(path), parents=[], entries=[], error=str(exc))
    return list_directories(str(path), fallback)


_IGNORED_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", ".pytest_cache", ".ruff_cache"}
)


def list_workspace_files(raw_cwd: Any, fallback: Path, limit: int = 2000) -> dict[str, Any]:
    """List relative file paths inside *raw_cwd* for `@file` autocomplete.

    Uses `git ls-files --cached --others --exclude-standard` when *raw_cwd* is
    inside a git repository (respecting `.gitignore`), falling back to a bounded
    directory walk otherwise. Returns relative paths only, never file contents.
    """
    path, error = resolve_directory(raw_cwd, fallback)
    if path is None or error is not None:
        return {"cwd": str(raw_cwd or fallback), "files": [], "error": error}

    capped = max(1, min(int(limit), 5000))
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0:
            files = [line for line in proc.stdout.splitlines() if line.strip()][:capped]
            return {"cwd": str(path), "files": sorted(files)}
    except (OSError, subprocess.SubprocessError):
        pass

    collected: list[str] = []
    try:
        for root_dir, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
            rel_root = Path(root_dir).relative_to(path)
            for name in sorted(filenames):
                rel = name if rel_root == Path(".") else (rel_root / name).as_posix()
                collected.append(rel)
                if len(collected) >= capped:
                    return {"cwd": str(path), "files": collected}
    except OSError as exc:
        return {"cwd": str(path), "files": [], "error": str(exc)}
    return {"cwd": str(path), "files": collected}
