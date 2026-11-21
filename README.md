# Quartz

Minimal coding agent with a persistent Python REPL.

Early development — version 0.1.0 (see `pyproject.toml`).

## What is Quartz

- Persistent Python kernel REPL: the agent executes `ipython` cells in a long-lived
  subprocess and keeps variables, imports, and working directory across turns.
- Multi-provider by default: auto-detects Google Antigravity (OAuth), OpenAI,
  OpenRouter, DeepSeek, or a custom OpenAI-compatible endpoint.
- Sessions that survive restarts: append-only JSONL log plus a `dill` kernel
  snapshot, with resume, fork, and in-session session switching.
- Context compaction that keeps the kernel alive, plus background `bash()` job
  visibility from the interactive prompt and the `/jobs` command.
- MCP tool support (stdio and streamable HTTP) and an ACP (Agent Client Protocol)
  JSON-RPC stdio server mode for external clients.
- Login-only credentials: API keys are stored via `/login` in `~/.quartz/auth.json`,
  never read from `.env`.

## Installation

Requires Python >= 3.14 (see `pyproject.toml`). Install from source with `uv`:

```bash
git clone https://github.com/danielwijaya93/quartz.git
cd quartz
uv tool install --editable .
```

This installs the `quartz` command globally (entry point `quartz.cli.app:main`) while
keeping the checkout editable. The `quartz-agent` distribution is not yet
published to PyPI, so there is no `pip install quartz-agent` yet — `uv tool
install --editable .` from a checkout is the supported path.

## Quickstart

```bash
cp .env.example .env   # optional: non-secret config only
quartz
```

Headless, single prompt (exits after the turn):

```bash
quartz -p "Summarize this repo in one paragraph."
```

With an attached image:

```bash
quartz -p "What is in this diagram?" --image diagram.png
```

## Configuration

Credentials are **login-only**: run `/login <provider>` (or
`quartz auth login <provider>`) to store them in `~/.quartz/auth.json`.
API keys are never read from environment variables or `.env` files —
legacy `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `DEEPSEEK_API_KEY` /
`OPENROUTER_API_KEY` entries are silently ignored.

Quartz loads non-secret environment in this order, without overriding
values already set in the real environment
(see `src/quartz/cli/app.py::_load_env_files`):

1. `~/.quartz/.env`
2. `./.env` (the project `.env`)

Start from [`.env.example`](.env.example) and copy it to `.env`. The
configuration surface below is verified against `.env.example` and the code
that reads each variable.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODEL` | provider default | Default model; `--model` overrides it. |
| `PROVIDER` | `auto` | Provider backend; `--provider` overrides it. |
| `EFFORT` | provider default | Reasoning effort; `--effort` overrides it. |
| `QUARTZ_SYNC_OUTPUT` | auto-detect | `1`/`0` forces CSI 2026 synchronized output on/off (flicker-free dock repaints). |
| `QUARTZ_IMAGE_PROTOCOL` | auto-detect | `kitty`, `iterm2` or `none`: inline preview for `/image` and `/paste` (Kitty needs PNG; disabled under tmux). |
| (compaction) | `16384` / `32000` | `reserve_tokens` / `keep_recent_tokens` via `/settings` (stored in `~/.quartz/config.json`). |
| `QUARTZ_CLOUDCODE_ENDPOINT` | `antigravity` | Google Cloud Code host alias (`antigravity`, `gemini-cli`) or an `http(s)` URL. |
| `QUARTZ_HTTP_TIMEOUT` | `300` | Provider read timeout, in seconds. |
| `QUARTZ_GOOGLE_CLIENT_ID` | bundled | Optional override for the Google OAuth application. |
| `QUARTZ_GOOGLE_CLIENT_SECRET` | bundled | Optional override for the Google OAuth application. |

`MAX_CONTEXT_TOKENS` appears in older `.env` files but is **not** a
configuration knob: the context window is detected per model. If you set it,
remove it so the per-model value is used.

## Usage

### Interactive loop

```bash
quartz
```

Press `Enter` to submit a prompt. While a turn is running, type a follow-up and
press `Enter` to steer the turn; press `Esc` to abort it. `Ctrl+T` toggles the
thinking trace and `Ctrl+O` toggles full tool-call details.

| Key | Action |
| --- | --- |
| `Enter` | Submit prompt, or steer a running turn |
| `Ctrl+J` | Insert a newline |
| `Esc` | Abort the running turn (or cancel a completion) |
| `Ctrl+C` | Clear the draft, abort the turn, or exit |
| `Ctrl+D` | Exit when the input is empty |
| `Ctrl+T` | Toggle the thinking trace |
| `Ctrl+O` | Toggle full tool-call details |

### Slash commands

| Command | Description |
| --- | --- |
| `/login [prov]` | Log in to a provider (OAuth or API key). |
| `/logout [prov]` | Log out and remove stored provider credentials. |
| `/model` | Choose provider, model, and reasoning effort. |
| `/continue [id]` | Pick and resume a previous session in this directory. |
| `/image [path]` | Attach an image file (or `/image clear`). |
| `/paste` | Attach an image from the system clipboard. |
| `/vars` | List live Python variables in the kernel. |
| `/helpers` | List available REPL helper functions (built-in and custom). |
| `/session` | Show the active session ID and file paths. |
| `/usage` | Show context window usage and cache hit rate. |
| `/expand [n or a..b]` | Expand a collapsed `ipython` cell (default: latest). Alias: `/last`. |
| `/compact [focus]` | Summarize older messages while keeping the kernel alive. |
| `/prompt` | Show the assembled system prompt. |
| `/reset` | Clear history, start a new session, and restart the kernel. |
| `/jobs` | List running background `bash()` jobs with elapsed time and output tail. |
| `/help` | Show available slash commands. |
| `/exit` | Exit the agent. Alias: `/quit`. |

### CLI flags

| Flag | Description |
| --- | --- |
| `-p, --prompt PROMPT` | Run a single prompt in headless mode and exit. |
| `--image PATH` | Attach an image file to the `-p` prompt (repeatable). |
| `--cwd PATH` | Working directory for the agent. |
| `-c, --continue` | Resume the most recent session. |
| `-r, --resume ID` | Resume a specific session by ID. |
| `--fork` | Fork the resumed session into a new session ID (requires `-r` or `-c`). |
| `--background-wait SECONDS` | Max seconds to wait for background `bash()` in `-p` mode (default `600`, `0` disables). |
| `--provider PROVIDER` | LLM provider backend (default: `auto`). |
| `--model MODEL` | Override `MODEL` from the environment. |
| `--effort EFFORT` | Reasoning effort level (e.g. `low`, `medium`, `high`, `max`, `xhigh`). |
| `--acp` | Run as an ACP JSON-RPC 2.0 server over stdio. |
| `--verbose` | Show full `ipython` code and output panels instead of compact summaries. |
| `--show-thinking` | Show the reasoning trace (same as `Ctrl+T`). |
| `--show-details` | Show full tool-call details (same as `Ctrl+O`). |

### Subcommands

```bash
quartz auth login [PROVIDER]   # Log in with an OAuth or API-key provider
quartz auth status             # Show current authentication status
quartz auth logout [PROVIDER]  # Remove stored credentials
quartz mcp list                # List configured MCP servers
quartz mcp add NAME [CMD ...]  # Add or update an MCP server
quartz mcp remove NAME         # Remove an MCP server
quartz mcp enable NAME         # Enable one or more MCP servers
quartz mcp disable NAME        # Disable one or more MCP servers
quartz acp                     # ACP server over stdio (same as --acp)
```

MCP configuration lives in `~/.quartz/mcp.json` by default; provider logins
live in `~/.quartz/auth.json`.

## Providers and models

Source of truth: `PROVIDERS` in `src/quartz/providers/registry.py`. Run
`/model` interactively to discover the models and reasoning-effort levels the
provider exposes.

| Provider | Auth | Example models |
| --- | --- | --- |
| `openrouter` | API key (`/login openrouter`) | `anthropic/claude-sonnet-4`, `openai/gpt-4.1`, `google/gemini-2.5-flash` |
| `openai` | API key (`/login openai`) | `gpt-4.1`, `gpt-4.1-mini`, `o4-mini`, `o3` |
| `deepseek` | API key (`/login deepseek`) | `deepseek-chat`, `deepseek-reasoner`, `deepseek-flash` |
| `google` | OAuth (Google Antigravity) | `gemini-3.8-flash-medium`, `gemini-2.5-pro`, `gemini-2.5-flash` |
| `custom` | API key (OpenAI-compatible) | any model on your endpoint |

`custom` takes a base URL, so OpenAI-compatible local or third-party endpoints
(like the ones in `.env.example`) plug in without code changes.

## Project layout

```
src/quartz/
  cli/       app.py  subcommands.py  acp.py
             Composition root: `app.py` is the `quartz` entry point (interactive REPL,
             `-p` headless mode, flags), `subcommands.py` parses the `mcp` / `auth`
             arguments and nothing else, `acp.py` is the ACP JSON-RPC 2.0 stdio server.
  core/      loop.py  turn.py  events.py  runtime.py  session.py  compact.py
             prompt.py  images.py
             Front-end-free agent internals: the single-tool `ipython` loop (`loop.py`);
             the turn lifecycle and the `TurnObserver` port it reports into (`turn.py`,
             `events.py`); runtime state and JSONL sessions with dill kernel snapshots
             (`runtime.py`, `session.py`); compaction that keeps the kernel alive
             (`compact.py`); prompt assembly (`prompt.py`) and image helpers (`images.py`).
  kernel/    worker.py  client.py  actions.py  helpers.py  pycheck.py
             The execution side: the subprocess CPython REPL worker (top-level await,
             async `bash()`) in `worker.py` and the host-side JSONL client that owns it
             in `client.py`; the static cell classifier (`actions.py`), the pluggable REPL
             helpers (`helpers.py`), `symtable` undefined-name checks (`pycheck.py`).
  providers/ base.py  registry.py  selection.py  openai.py  google.py  config.py
             Model access: the provider protocol, stream events and shared SSE framing
             (`base.py`); the provider/model/credential/effort registry holding `PROVIDERS`
             (`registry.py`); "auto"/explicit provider resolution and `build_provider`
             (`selection.py`); the OpenAI-compatible and Google Antigravity backends
             (`openai.py`, `google.py`); endpoint and HTTP-timeout policy (`config.py`).
  tui/       dock.py  render.py  commands.py  turn_view.py  provider_setup.py
             modals.py  banner.py  replay.py  completer.py
             The interactive front end: the scrollback-native bottom dock (`dock.py`);
             terminal rendering, Markdown streaming and cell summaries (`render.py`);
             the `TurnView` that renders loop events (`turn_view.py`); the slash-command
             registry (`commands.py`); the Rich `/login` flows (`provider_setup.py`);
             shared inline modals (`modals.py`); the top banner (`banner.py`); resumed
             session replay (`replay.py`); completion (`completer.py`).
  mcp/       client.py
             In-kernel MCP client manager for stdio and streamable-HTTP servers.
  auth/      google.py  store.py  oauth.py
             Google AI Pro/Ultra OAuth2 PKCE and Cloud Code Assist token management
             (`google.py`), the atomic `~/.quartz/auth.json` store (`store.py`), and the
             OAuth client identity (`oauth.py`).
  settings.py  errors.py  __main__.py
             Cross-cutting: user preferences in `~/.quartz/config.json`, typed provider
             errors, and the `python -m quartz` entry point.
```

`cli/` is the composition root and may import `tui/` and `core/`; `tui/` and
`core/` must not import `cli/`. `core/` holds the agent loop, the turn lifecycle
and session state, and imports nothing from the front ends.
`tests/core/test_import_boundaries.py` enforces this.

## Development

```bash
uv run pytest
```

When dependencies, package metadata (`pyproject.toml`), or entry points change,
re-sync the editable install:

```bash
uv tool install --editable . --force
```

Workflow (see [`AGENTS.md`](AGENTS.md)): branch as
`<type>/<short-description>` (`feat/`, `fix/`, `perf/`, `refactor/`, `chore/`),
add tests in `tests/`, verify with `uv run pytest`, commit with Conventional
Commits, push, open a PR, and watch `gh pr checks --watch` until green. Never
commit secrets — load them via the environment or `~/.quartz/` files.

## Roadmap

See [`WISHLIST.md`](WISHLIST.md) for the architectural wishlist and reference
implementations (unified provider layer, OS-level sandboxing, credential
mediation). It is a planning document, not a changelog.

## License

No license yet — all rights reserved.
