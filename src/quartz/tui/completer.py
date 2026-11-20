"""Slash-command and @file autocomplete for the interactive CLI."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from quartz.tui.commands import COMMAND_REGISTRY

_IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}


def _iter_workspace_files(cwd: Path, limit: int = 500) -> list[str]:
    results: list[str] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORE_DIRS and not d.startswith("."))
        rel_root = Path(root).relative_to(cwd)
        for fname in sorted(files):
            rel = str(rel_root / fname) if str(rel_root) != "." else fname
            results.append(rel)
            if len(results) >= limit:
                return results
    return results


class AgentCompleter(Completer):
    """Autocompletes `/commands` at line start and `@path` file mentions anywhere."""

    def __init__(self, cwd: Path, commands: dict[str, str] | None = None, *, cache_ttl_s: float = 5.0) -> None:
        self.cwd = cwd.resolve()
        self.commands = commands
        self._cache_ttl_s = cache_ttl_s
        self._files_cache: list[str] | None = None
        self._files_cache_ts: float = 0.0

    def _get_workspace_files(self) -> list[str]:
        now = time.monotonic()
        if self._files_cache is None or (now - self._files_cache_ts) >= self._cache_ttl_s:
            self._files_cache = _iter_workspace_files(self.cwd)
            self._files_cache_ts = now
        return self._files_cache

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor

        # Slash commands at start of input (before first space)
        active_commands = self.commands if self.commands is not None else COMMAND_REGISTRY.to_completion_dict()
        if text.startswith("/") and " " not in text:
            prefix = text.lower()
            for cmd, desc in active_commands.items():
                if cmd.startswith(prefix):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)
            return

        # @file mentions on the current whitespace-delimited token
        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            needle = word[1:].lower()
            for rel_path in self._get_workspace_files():
                if needle in rel_path.lower():
                    yield Completion(
                        f"@{rel_path}",
                        start_position=-len(word),
                        display=rel_path,
                    )
