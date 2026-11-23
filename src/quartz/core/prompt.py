"""Minimal Unharnessed-style system prompt with AGENTS.md/CLAUDE.md and SKILL.md discovery."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from quartz.kernel.helpers import discover_skills, format_helpers_for_prompt, load_helpers
from quartz.mcp.client import load_mcp_servers

PROMPT_HEADER = """# Quartz

You have one tool, `ipython`: a persistent CPython REPL with top-level `await`. Variables, `os.chdir()`, and `os.environ` persist across cells and compaction. Messages starting with `[<kind>]` come from the harness."""

PROMPT_RULES = """## Rules
- Batch and chain operations generously into one cell: read multiple related files, inspect state, make edits, and verify results together rather than executing piecemeal single-call turns.
- Prioritize available REPL helpers and primitives over verbose standard-library boilerplate or multi-turn external commands.
- Use `bash()` for long-running or background processes; do not block a cell with `sleep` or minutes-long awaits (let long jobs finish in the background).
- Use raw strings (`r"..."`) for regexes or any string with backslashes (or `fixed=True` in `rg()` for literal searches) to avoid Python `SyntaxWarning`.
- Follow `## Project Instructions` strictly."""


def _ancestor_dirs(cwd: Path) -> list[Path]:
    """Return directories from filesystem/git root down to cwd."""
    resolved = cwd.resolve()
    chain = [resolved, *resolved.parents]
    # Stop at git root if inside a git repo, otherwise keep up to root
    trimmed: list[Path] = []
    for p in chain:
        trimmed.append(p)
        if (p / ".git").exists():
            break
    trimmed.reverse()
    return trimmed


def discover_context_files(
    cwd: Path, global_agents_path: Path | None = None
) -> list[tuple[Path, str]]:
    """Find global ~/.quartz/AGENTS.md plus AGENTS.md/CLAUDE.md from repo/root down to cwd."""
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    env_global = os.getenv("QUARTZ_AGENTS_FILE")
    g_path = (
        global_agents_path
        or (Path(env_global).expanduser() if env_global else Path.home() / ".quartz" / "AGENTS.md")
    ).resolve()
    if g_path.is_file():
        seen.add(g_path)
        try:
            text = g_path.read_text(encoding="utf-8").strip()
            if text:
                found.append((g_path, text))
        except OSError:
            pass

    for directory in _ancestor_dirs(cwd):
        for filename in ("AGENTS.md", "CLAUDE.md"):
            candidate = (directory / filename).resolve()
            if candidate.is_file() and candidate not in seen:
                seen.add(candidate)
                try:
                    text = candidate.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if text:
                    found.append((candidate, text))
    return found


# discover_skills is re-exported from quartz.kernel.helpers for backwards compatibility.


def build_system_prompt(
    cwd: Path | None = None,
    *,
    skills_dir: Path | None = None,
    global_agents_path: Path | None = None,
    global_helpers_path: Path | None = None,
    project_helpers_path: Path | None = None,
    headless: bool = False,
    trusted: bool | None = None,
) -> str:
    """Assemble the static prompt plus dynamic environment, skills, and context files."""
    work_dir = (cwd or Path.cwd()).resolve()
    loaded_helpers = load_helpers(
        cwd=work_dir,
        global_helpers_path=global_helpers_path,
        project_helpers_path=project_helpers_path,
        trusted=trusted,
    )
    helpers_block = format_helpers_for_prompt(loaded_helpers)
    prompt_base = f"{PROMPT_HEADER}\n\n{helpers_block}\n\n{PROMPT_RULES}"
    sections = [prompt_base]

    env_block = (
        "## Environment\n\n"
        f"- Working directory: `{work_dir}`\n"
        f"- OS: `{platform.system()} {platform.release()}`\n"
        f"- Python: `{sys.version.split()[0]}`"
    )
    sections.append(env_block)

    mcp_servers = load_mcp_servers(work_dir, trusted=trusted)
    if mcp_servers:
        sections.append(
            "## MCP Servers\n\n"
            f"{len(mcp_servers)} MCP server(s) configured. Use `mcp.list_servers()` or "
            "`await mcp.search_tools(query)` in Python to discover servers and tools, and "
            "`await mcp.<server>.<tool>(...)` or `await mcp.call_tool(server, tool, arguments={...})` to call them."
        )

    skills = discover_skills(skills_dir=skills_dir)
    if skills:
        sections.append(
            "## Skills\n\n"
            f"{len(skills)} skill(s) available. Use `find_skills(query)` in Python to search by keyword, "
            "then `read(path)` on the matching `SKILL.md` when a task matches."
        )

    context_files = discover_context_files(work_dir, global_agents_path=global_agents_path)
    if context_files:
        ctx_parts = ["## Project Instructions"]
        for path, text in context_files:
            ctx_parts.append(f"### `{path}`\n\n{text}")
        sections.append("\n\n".join(ctx_parts))

    if headless:
        sections.append(
            "## Headless Mode\n\n"
            "This is a single-turn non-interactive `-p` run. Execute the requested task directly "
            "without pausing to ask for plan approval."
        )

    return "\n\n".join(sections)
