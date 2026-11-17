"""Minimal Unharnessed-style system prompt with AGENTS.md/CLAUDE.md and SKILL.md discovery."""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path

from agent.mcp_client import load_mcp_servers

STATIC_SYSTEM_PROMPT = """# Minimal Agent

You have one tool, `ipython`: a persistent CPython REPL where top-level `await` works. Every other capability is a Python function in that REPL. Messages that start with `[<kind>]` come from the harness, not from the user.

## Functions in the REPL

- `bash(command: str, timeout: float | None = 600) -> BashHandle`: start a shell command in the background and return at once; each call is a new process, a command still running after `timeout` seconds is killed (`None`: no limit), and a `[bash-done]` notification arrives when a command ends after its cell
  - `BashHandle`: `.pid`, `.running`, `.command`, `.tail(n=50)`, `.output()`, `.poll() -> BashResult | None`, `.kill(sig=SIGTERM, grace=5.0)`, and `await handle -> BashResult`
  - `BashResult`: `.exit_code`, `.output`, `.duration`

## Rules

- Keep in mind that each tool call is a round trip: steps that do not need a result you have not seen yet can go in one call.
- Run programs with `bash()`, not `subprocess` or `os.system`. Write logic, loops and parsing in Python; use `bash()` only to call programs.
- `os.chdir()` and `os.environ` changes persist and apply to later `bash()` calls.
- Store what you read or compute in named variables instead of reading it again.
- Python variables survive compaction.
- Run an external project through its own environment (`uv run`, `.venv/bin/python`, its documented commands); do not install its dependencies into the kernel.
- Do not hold the turn with `sleep` or long blocking awaits: end the turn and wait for the notification.
- When you are done, stop calling tools and give the final answer, with any assumption you made."""


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


def discover_context_files(cwd: Path) -> list[tuple[Path, str]]:
    """Find AGENTS.md and CLAUDE.md files from repo/root down to cwd."""
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for directory in _ancestor_dirs(cwd):
        for filename in ("AGENTS.md", "CLAUDE.md"):
            candidate = directory / filename
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
    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip("\"'")
    if not name:
        name = skill_md.parent.name
    return (name, description)


def discover_skills(cwd: Path) -> list[tuple[str, str, Path]]:
    """Discover SKILL.md files from standard skill directories."""
    search_roots: list[Path] = [Path.home() / ".unharnessed" / "skills"]
    for directory in _ancestor_dirs(cwd):
        search_roots.append(directory / ".agents" / "skills")
        search_roots.append(directory / ".unharnessed" / "skills")

    skills_by_name: dict[str, tuple[str, str, Path]] = {}
    for root in search_roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            skill_md = entry / "SKILL.md"
            if entry.is_dir() and skill_md.is_file():
                parsed = _parse_skill_frontmatter(skill_md)
                if parsed:
                    name, desc = parsed
                    skills_by_name[name] = (name, desc, skill_md)
    return list(skills_by_name.values())


def build_system_prompt(cwd: Path | None = None) -> str:
    """Assemble the static prompt plus dynamic environment, skills, and context files."""
    work_dir = (cwd or Path.cwd()).resolve()
    sections = [STATIC_SYSTEM_PROMPT]

    env_block = (
        "## Environment\n\n"
        f"- Working directory: `{work_dir}`\n"
        f"- OS: `{platform.system()} {platform.release()}`\n"
        f"- Python: `{sys.version.split()[0]}`"
    )
    sections.append(env_block)

    mcp_servers = load_mcp_servers(work_dir)
    if mcp_servers:
        server_list = ", ".join(f"`{name}`" for name in sorted(mcp_servers))
        sections.append(
            "## MCP Servers\n\n"
            f"Configured servers: {server_list}\n"
            "Call them from Python via the pre-imported `mcp` object:\n"
            "- `await mcp.search_tools(query)` / `await mcp.list_tools(server=None)`\n"
            "- `await mcp.call_tool(server, tool, arguments={...})`"
        )

    skills = discover_skills(work_dir)
    if skills:
        lines = ["## Skills\n\nRead a skill's `SKILL.md` in Python when a task matches it:"]
        for name, desc, path in skills:
            summary = f": {desc}" if desc else ""
            lines.append(f"- `{name}` (`{path}`){summary}")
        sections.append("\n".join(lines))

    context_files = discover_context_files(work_dir)
    if context_files:
        ctx_parts = ["## Project Instructions"]
        for path, text in context_files:
            ctx_parts.append(f"### `{path}`\n\n{text}")
        sections.append("\n\n".join(ctx_parts))

    return "\n\n".join(sections)
