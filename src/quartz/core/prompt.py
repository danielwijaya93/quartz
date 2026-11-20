"""Minimal Unharnessed-style system prompt with AGENTS.md/CLAUDE.md and SKILL.md discovery."""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path

from quartz.kernel.helpers import format_helpers_for_prompt, load_helpers
from quartz.mcp.client import load_mcp_servers, load_mcp_tool_cache

PROMPT_HEADER = """# Quartz

You have one tool, `ipython`: a persistent CPython REPL with top-level `await`. Variables, `os.chdir()`, and `os.environ` persist across cells and compaction. Messages starting with `[<kind>]` come from the harness."""

PROMPT_RULES = """## Rules
- Batch and chain operations generously into one cell: read multiple related files, inspect state, make edits, and verify results together rather than executing piecemeal single-call turns.
- Prioritize available REPL helpers and primitives over verbose standard-library boilerplate or multi-turn external commands.
- Use `bash()` for long-running or background processes; do not block a cell with `sleep` or minutes-long awaits (let long jobs finish in the background).
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


def build_system_prompt(
    cwd: Path | None = None,
    *,
    skills_dir: Path | None = None,
    global_agents_path: Path | None = None,
    global_helpers_path: Path | None = None,
    project_helpers_path: Path | None = None,
    headless: bool = False,
) -> str:
    """Assemble the static prompt plus dynamic environment, skills, and context files."""
    work_dir = (cwd or Path.cwd()).resolve()
    loaded_helpers = load_helpers(
        cwd=work_dir,
        global_helpers_path=global_helpers_path,
        project_helpers_path=project_helpers_path,
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

    mcp_servers = load_mcp_servers(work_dir)
    if mcp_servers:
        tool_cache = load_mcp_tool_cache()
        mcp_lines = [
            "## MCP Servers\n\n"
            "Call them from Python via the pre-imported `mcp` object "
            "(`await mcp.<server>.<tool>(...)`, `await mcp.call_tool(server, tool, arguments={...})`, or `await mcp.list_tools(server)`):"
        ]
        for name in sorted(mcp_servers):
            cached = tool_cache.get(name)
            if cached:
                tool_strs = [
                    f"`{t['name']}({', '.join(t.get('args', [])[:5])})`"
                    for t in cached[:12]
                ]
                suffix = f" (+{len(cached) - 12} more)" if len(cached) > 12 else ""
                mcp_lines.append(f"- `{name}`: {', '.join(tool_strs)}{suffix}")
            else:
                mcp_lines.append(f"- `{name}`")
        sections.append("\n".join(mcp_lines))

    skills = discover_skills(skills_dir=skills_dir)
    if skills:
        lines = ["## Skills\n\nRead a skill's `SKILL.md` in Python when a task matches it:"]
        for name, desc, path in skills:
            summary = f": {desc}" if desc else ""
            lines.append(f"- `{name}` (`{path}`){summary}")
        sections.append("\n".join(lines))

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
