# Quartz

Minimal coding agent with a stateless, sandboxed Python execution tool.

## Why Quartz

- **Sandboxed Python cells:** Each `ipython` call runs in a fresh `bwrap` Python 3.14 worker (`~/.quartz` masked, 0 MB idle RAM) with a per-session `/tmp` scratch directory and shared `~/.quartz/sandbox-cache`.
- **Fast & lightweight:** ~162 ms CLI startup, ~120 MB shared `asyncio` web server across concurrent sessions, and SQLite WAL + FTS5 session search.
- **Token-efficient on complex tasks:** Native Python stdlib plus built-in REPL helpers (`read`, `edit`, `rg`, `outline`, `env_info`, `bash`) let the model chain multi-step work in fewer round-trips.
- **Multi-provider, login-only auth:** Supports Google Antigravity (OAuth), OpenAI, OpenRouter, DeepSeek, and custom OpenAI-compatible endpoints via `~/.quartz/auth.json`.
- **TUI, Web & Headless:** Terminal UI (`quartz`), browser/mobile UI (`quartz web`) with optional Rich UI (OpenUI), single-prompt `-p` mode, and MCP tool support.

## Installation

Requires **Python >= 3.14**, [`uv`](https://docs.astral.sh/uv/), and `bubblewrap` (`bwrap`) on Linux for sandboxing.

```bash
git clone https://github.com/danielwijaya93/quartz.git
cd quartz
uv tool install --editable .
```

> After pulling dependency changes in `pyproject.toml` or `uv.lock`, refresh the global tool with:
> `uv tool install --editable . --force`

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

   # Headless single-turn prompt
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
