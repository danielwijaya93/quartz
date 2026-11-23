"""Minimal agent combining learn-claude-code's harness loop with Unharnessed's REPL."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path


def get_version() -> str:
    """Return the unified Quartz version from package metadata or pyproject.toml."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        match = re.search(
            r'^version\s*=\s*"([^"]+)"',
            pyproject.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    try:
        return importlib.metadata.version("quartz-agent")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = get_version()
