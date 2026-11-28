"""Minimal agent combining learn-claude-code's harness loop with Unharnessed's REPL."""

from __future__ import annotations

import re
from pathlib import Path

_VERSION: str | None = None


def get_version() -> str:
    """Return the unified Quartz version from package metadata or pyproject.toml."""
    global _VERSION
    if _VERSION is not None:
        return _VERSION
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        match = re.search(
            r'^version\s*=\s*"([^"]+)"',
            pyproject.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            ver = match.group(1)
            _VERSION = ver
            return ver
    import importlib.metadata

    try:
        _VERSION = importlib.metadata.version("quartz-agent")
    except importlib.metadata.PackageNotFoundError:
        _VERSION = "0.0.0"
    return _VERSION


def __getattr__(name: str) -> str:
    if name == "__version__":
        return get_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
