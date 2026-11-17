"""Slash-command and @file autocomplete for the interactive CLI."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

SLASH_COMMANDS: dict[str, str] = {
    "/vars": "List live Python variables in the kernel",
    "/session": "Show active session ID and file paths",
    "/usage": "Show context window usage and cache hit rate",
    "/compact": "Summarize older messages while keeping the kernel alive",
    "/prompt": "Show the assembled system prompt",
    "/reset": "Clear history, start a new session, and restart the kernel",
    "/help": "Show available slash commands",
    "/exit": "Exit the agent",
}

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

    def __init__(self, cwd: Path, commands: dict[str, str] | None = None) -> None:
        self.cwd = cwd.resolve()
        self.commands = commands or SLASH_COMMANDS

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor

        # Slash commands at start of input (before first space)
        if text.startswith("/") and " " not in text:
            prefix = text.lower()
            for cmd, desc in self.commands.items():
                if cmd.startswith(prefix):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)
            return

        # @file mentions on the current whitespace-delimited token
        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            needle = word[1:].lower()
            for rel_path in _iter_workspace_files(self.cwd):
                if needle in rel_path.lower():
                    yield Completion(
                        f"@{rel_path}",
                        start_position=-len(word),
                        display=rel_path,
                    )
