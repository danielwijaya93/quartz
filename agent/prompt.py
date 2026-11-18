"""Minimal Unharnessed-style system prompt with AGENTS.md/CLAUDE.md and SKILL.md discovery."""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path

from agent.mcp_client import load_mcp_servers, load_mcp_tool_cache

STATIC_SYSTEM_PROMPT = """# Quartz

You have `ipython` (a persistent CPython REPL where top-level `await` works) plus direct `write` and `edit` tools for file creation/edits. Prefer `write`/`edit` over wrapping large file contents in Python strings. Messages that start with `[<kind>]` come from the harness, not from the user.

## Functions in the REPL

- `bash(command: str, timeout: float | None = 600) -> BashHandle`: start a shell command in the background and return at once; each call is a new process, a command still running after `timeout` seconds is killed (`None`: no limit), and a `[bash-done]` notification arrives when a command ends after its cell
  - `BashHandle`: `.pid`, `.running`, `.command`, `.tail(n=50)`, `.output()`, `.poll() -> BashResult | None`, `.kill(sig=SIGTERM, grace=5.0)`, and `await handle -> BashResult`
  - `BashResult`: `.exit_code`, `.output`, `.duration`
- `ls(path=".") -> list[str]`: sorted directory entries (trailing `/` for dirs)
- `find(pattern="*", path=".", limit=200) -> list[str]`: glob search skipping `.git`/`.venv`/`node_modules`
- `rg(pattern, path=".", glob=None, max_hits=100) -> str`: regex search returning `file:line:text`
- `read(path, start=1, end=200) -> str`: numbered 1-based line slice
- `edit(path, old, new) -> str` / `write(path, content) -> str`: exact-match edit / write file
- `gather(*coros)`: `asyncio.gather` for running async calls (`mcp.call_tool`, `await bash(...)`) concurrently

## Step economy

Every cell re-sends the whole conversation, so the number of cells — not the lines of Python — is what a run costs. **Make each cell do more work**: one cell can loop, branch, run many shell commands and call many MCP tools, and a cell doing ten things costs the same as a cell doing one. Before writing a cell, ask what else you will need when it returns, and get that too.

The same work, done badly and done well:

- Three cells of `ls`, then `ls`, then `cat` → one cell that walks the tree and prints every relevant file.
- A cell per library symbol, probing one at a time → one cell dumping exports, the `.d.ts` and `package.json` together.
- One cell to `find /`, another to poll it, another to wait → one bounded search from `$PWD`, awaited in that same cell.
- A cell per search query → one cell with the queries in a list, run and printed together.
- Independent local + MCP work → run `rg`/`read` and `await gather(mcp.call_tool(...))` together in Step 1; skip extra `web_fetch` calls if search snippets already answer the question.

## Rules

- Several `ipython` calls in one assistant message run in parallel; put dependent steps in one cell, and use `await gather(...)` when running multiple async calls together.
- Run programs with `bash()`, not `subprocess` or `os.system`. Write logic, loops and parsing in Python; use `bash()` only to call programs.
- `os.chdir()` and `os.environ` changes persist and apply to later `bash()` calls.
- Store what you read or compute in named variables instead of reading it again.
- Python variables survive compaction.
- Run an external project through its own environment (`uv run`, `.venv/bin/python`, its documented commands); do not install its dependencies into the kernel.
- Do not hold the turn with `sleep` or long blocking awaits: end the turn and wait for the notification. A command that needs minutes belongs in the background, not in a cell you wait on.
- Aim for the deliverable within a dozen cells. If the next two cells would still be exploration, stop and answer with the assumptions you made.
- When you are done, stop calling tools and give a concise, direct final answer (cite file paths/lines without repeating large code blocks), noting any assumptions you made."""


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
    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip("\"'")
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
    headless: bool = False,
) -> str:
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
        tool_cache = load_mcp_tool_cache()
        mcp_lines = [
            "## MCP Servers\n\n"
            "Call them from Python via the pre-imported `mcp` object "
            "(`await mcp.call_tool(server, tool, arguments={...})`, or `await mcp.list_tools(server)`):"
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
