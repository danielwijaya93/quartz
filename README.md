# Quartz

A lightweight, token-efficient coding agent built around sandboxed Python cells.

## Why Quartz

- **Token-efficient by design:** Gives the model a Python 3.14 REPL (`ipython`) with built-in helpers (`read`, `edit`, `rg`, `outline`, `env_info`, `bash`), chaining multi-step work into fewer round-trips instead of burning tokens on dozens of shell calls.
- **Fast and light on resources:** ~162 ms CLI startup, ~43 MB per headless run, and a shared ~120 MB `asyncio` web server across concurrent sessions—defying Python's heavy-runtime reputation.
- **Toggleable Generative UI (OpenUI):** Switch between clean Markdown and rich interactive UI (charts, tables, forms, KPIs) per session in the web/mobile interface.
- **Built-in non-blocking subagents:** Delegate parallel tasks to background `quartz -p` workers without blocking the main session.
- **Built-in agent automation:** Schedule recurring or event-driven agents via cron jobs and webhooks directly in `quartz web`.
- **Autonomous & anti-stall:** Goal mode, background job tracking, and enforced progress check-ins keep long-running coding sessions moving without silent stalls.
- **Toggleable OS sandbox:** Runs inside `bwrap` (Linux) / Seatbelt (macOS) by default with per-session `/tmp` and shared cache, or disable with `--sandbox-off` on dedicated machines.
- **Self-improving workflow:** Instant session search via SQLite WAL + FTS5 and user-extensible `~/.quartz/helpers.py` / `.quartz/helpers.py` functions.
- **Modern agent standard:** Progressive `SKILL.md` and MCP loading, global + repo `AGENTS.md` discovery, and multi-provider login-only auth (`~/.quartz/auth.json`) for Google Antigravity, OpenAI, OpenRouter, DeepSeek, and custom endpoints.

## Installation

Requires **Python >= 3.14**, [`uv`](https://docs.astral.sh/uv/), and `bubblewrap` (`bwrap`) on Linux for sandboxing.

```bash
git clone https://github.com/danielwijaya93/quartz.git
cd quartz
uv tool install --editable .
```

> After pulling dependency changes in `pyproject.toml` or `uv.lock`, refresh with `uv tool install --editable . --force`.

## First-Time Usage

1. **Log in to a provider** (credentials are stored in `~/.quartz/auth.json`, never read from `.env`):

   ```bash
   quartz auth login
   ```

2. **Start coding:**

   ```bash
   # Interactive terminal UI
   quartz

   # Web UI (prints the local URL + auth token)
   quartz web

   # Headless single-turn prompt (or disable sandbox with --sandbox-off)
   quartz -p "Summarize this repository."
   ```

## Essential Controls

| Input | Action |
| --- | --- |
| `Enter` | Submit prompt or steer a running turn |
| `Esc` | Abort the running turn |
| `Ctrl+T` / `Ctrl+O` | Toggle thinking trace / full tool details |
| `/login` | Authenticate or switch provider credentials |
| `/model` / `/settings` | Change model, effort, or session settings |
| `/new` / `/resume` | Start a new session or resume an existing one |
