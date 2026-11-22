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

> **After pulling a dependency change, reinstall.** `uv tool install --editable .`
> fixes the tool environment at install time, and that environment is separate from
> the repo venv — so a commit that changes `pyproject.toml` or `uv.lock` can leave
> `uv run` and every test green while the installed `quartz` command is stale
> (`quartz web` failed this way with `ModuleNotFoundError: No module named 'fastapi'`).
> Re-run `uv tool install --editable . --force` after such a pull.

CI enforces this: the `install` job installs the tool the same way and imports the
web dependencies through the tool's interpreter.

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
| (updates) | `5` | `progress_every` via `/settings progress <n>` (stored in `~/.quartz/config.json`; `0` disables, `-p` is off). |
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

### Staying updated

Quartz does not leave you watching a spinner in silence. Every `progress_every`
`ipython` calls (default `5`) the harness injects one line:

```
[progress] Update the user now on the progress.
```

That is the whole notice. It names no call count, because only the harness counts
and a number in the prompt is stale the moment a checkpoint is refused. If the
model already narrated on its own, the notice is skipped and the count restarts,
so you never read the same update twice.

The trigger is the harness's, so it cannot be forgotten, and the notice is
enforced rather than merely suggested. A model that answers a checkpoint with
another tool call and no text is refused: those cells do not run, and the call
returns `Update the user first.` as its result. Writing the update releases the
gate and the same turn continues, so a compliant model loses nothing and a silent
one cannot keep working. Thinking does not count — you cannot see it. After three
refusals the gate lets the work continue anyway, so a model that refuses to speak
can slow a turn down but never wedge it.

- Turn it off, or change the cadence, with `/settings progress <n>`
  (`/settings progress 0` disables it). The change is saved for future sessions
  and takes effect on the **next turn**, so one session can discuss at `5` and
  then work quietly at `20`. `/settings` answers immediately even mid-turn; a
  turn already running keeps the cadence it started with.
- `-p` runs stay quiet by default, where stdout is the answer and nothing else;
  pass `--progress-every N` to opt a long headless run in.

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
| `/settings` | Show or change user preferences: `reserve`, `keep_recent`, `progress` (stored in `~/.quartz/config.json`). |
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
| `--progress-every N` | Ask the model to update you every N `ipython` calls (`0` disables). Default: on interactively, off for `-p`. |

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
quartz web                     # Browser client (see "Quartz web" below)
```

MCP configuration lives in `~/.quartz/mcp.json` by default; provider logins
live in `~/.quartz/auth.json`.

## Quartz web

A browser transport for the same core, for when a terminal cannot show what the
answer means. The terminal renders seven turn events; `quartz web` renders the same
seven in a page — streamed markdown, the reasoning trace, collapsible tool cells
with inline images, compaction boundaries, and background-job notices.

It is **not** an ACP client. ACP exists so one client can drive many agents;
this drives exactly one. `quartz --acp` is untouched and remains the editor
transport. The wire format is specified in
[`docs/web-protocol.md`](docs/web-protocol.md).

```bash
cd webapp && npm ci && npm run build   # once, and after pulling webapp changes
quartz web                             # http://127.0.0.1:8765
quartz web --port 9000 --open          # custom port, open the browser
quartz web --cwd ~/some/project        # work in a different directory
```

The client is not bundled today. `quartz web` serves `webapp/dist` from a checkout,
a `_static` build shipped inside an installed package, or `QUARTZ_WEB_STATIC`
(`resolve_static_dir` in `src/quartz/web/app.py`); with none of those present it
answers `/` with a 503 "client not built" page rather than pretending, so run
`cd webapp && npm run build` before the first `quartz web`.

| Flag | Meaning |
| --- | --- |
| `--host` | Bind address (default `127.0.0.1`). A non-loopback host warns on startup. |
| `--port` | Bind port (default `8765`). |
| `--cwd` | Working directory for the agent. |
| `--open` | Open the default browser once the server is listening. |
| `--dev` | Serve the API only; run `npm run dev` in `webapp/` next to it. |

Sessions started from the browser are ordinary Quartz sessions: they live in the
same JSONL log with the same kernel snapshots, so `quartz -c` resumes one and the
web client can load a terminal session.

The page is **folder-first**, so nothing above needs a terminal. A collapsible
sidebar lists workspaces (a `cwd`) and the sessions in each one; `+` on a folder
starts a session there, `+ New` opens a tap-through folder picker (breadcrumbs, a
filter over the current listing, "New folder"), and clicking a session loads its
transcript. Sessions can be renamed and archived in place.

The sidebar lists sessions **started in the browser**, plus any session you
explicitly import. Importing is never automatic: the "Not in sidebar" dialog lists
everything else, terminal sessions included, with one button per row. Archiving
hides a row and nothing more — the transcript stays in `~/.quartz/sessions/*.jsonl`
untouched, and Restore brings it back. That state is the browser's own, in
`~/.quartz/web/sidebar.json`; the terminal never reads it. `origin` in a session's
own log (the first line) records which transport created it, so deleting the
sidebar file loses labels and imports, not history.

**There is no authentication.** The server is a local, single-user tool. Keep
`--host` on loopback unless you know what is in front of it.

Commands run from the browser palette use the same implementation as the terminal
(`core/commands.py`), so they cannot drift. Commands that need a terminal modal
today — `/login`, `/model`, `/session` and friends — report that plainly instead of
failing; run them in the terminal, or use the terminal for that one step.

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
             `-p` headless mode, flags), `subcommands.py` parses the `mcp` / `auth` /
             `web` arguments and nothing else, `acp.py` is the ACP JSON-RPC 2.0 stdio
             server.
  web/       app.py  session.py  bridge.py  sidebar.py  files.py
             The browser transport. `app.py` is the FastAPI app (routes, the one
             WebSocket, static SPA serving), `session.py` owns the runtime and the turn
             on its own thread, `bridge.py` is the `TurnObserver` that turns all seven
             turn events into wire frames, `sidebar.py` is the sidebar's own state
             (`~/.quartz/web/sidebar.json`) and `files.py` the directory listing behind
             the folder picker.
webapp/      Vite + React client for `quartz web` (static build, served by the server).
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
