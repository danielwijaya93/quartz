# Codebase Map

Quick orientation guide for `quartz` so you can jump straight to the right file without broad searches.
For exact class/function line numbers, run `uv run python scripts/gen_index.py` and read `docs/SYMBOL_INDEX.md`.

## Top-Level Layout

| Path | Purpose |
| :--- | :--- |
| `src/quartz/core/` | Shared agent loop, `SessionController`, turns, compaction, events, slash commands |
| `src/quartz/web/` | FastAPI server, WebSocket hub, session manager, permissions, push notifications |
| `src/quartz/tui/` | Terminal UI (`prompt_toolkit` dock, rendering, modals, replay) |
| `src/quartz/cli/` | CLI entrypoint (`app.py`) and subcommands (`subcommands.py`) |
| `src/quartz/providers/` | LLM provider adapters (`google.py`, `openai.py`), registry, HTTP pool |
| `src/quartz/kernel/` | Stateless one-shot Python cell runner, OS sandbox (`bwrap`/`sandbox-exec`), host-managed bash jobs |
| `src/quartz/mcp/` | MCP client and connection pool |
| `src/quartz/storage/` | Embedded SQLite WAL database engine and schema migrations |
| `src/quartz/auth/` | OAuth and token storage |
| `webapp/src/` | React + TypeScript web/mobile UI (`components/`, `useQuartz.ts`, `protocol.ts`) |
| `tests/` | Pytest suite mirroring `src/quartz/` (`core/`, `web/`, `tui/`, `kernel/`, etc.) |
| `scripts/` | Dev utilities (`gen_index.py`, `export_web_schema.py`, `bump_version.py`, `web_smoke.py`) |

## Where Common Changes Go

| Task | Primary Files |
| :--- | :--- |
| Agent loop, turns, or prompts | `src/quartz/core/loop.py`, `turn.py`, `prompt.py`, `controller.py` |
| Slash commands (shared TUI/Web) | `src/quartz/core/commands/` (`registry.py`, `*_cmds.py`) |
| HTTP / WebSocket endpoints | `src/quartz/web/routes/` (`auth.py`, `files.py`, `push.py`, `sessions.py`, `settings.py`, `ws.py`) |
| Web wire payloads | `src/quartz/web/schemas.py` -> run `uv run python scripts/export_web_schema.py` |
| Web UI components | `webapp/src/components/` (`Composer.tsx`, `Transcript.tsx`, `OpenUISurface.tsx`, etc.) |
| TUI rendering or keybindings | `src/quartz/tui/dock.py`, `dock_keys.py`, `render.py`, `turn_view.py` |
| Model / provider config | `src/quartz/providers/registry.py`, `config.py`, `src/quartz/settings.py` |
| Sandbox / cell runner | `src/quartz/kernel/worker.py`, `client.py`, `sandbox.py`, `host_bash.py`, `helpers.py` |

## Architecture Rules

- **Import DAG**: `core/` must not import `tui/`, `web/`, or `cli/`. Enforced by `tests/core/test_import_boundaries.py`.
- **Web schemas**: Edit `src/quartz/web/schemas.py` first, then regenerate `webapp/src/protocol.generated.ts`.
- **Focused modules**: Keep files under ~500 lines and functions under ~80 lines.

## Fast Verification Commands

- **Parallel all-in-one (REPL)**: `await verify("auto", gen_index=True)` (defined in `.quartz/helpers.py`)
- **Python lint & types**: `uv run ruff check src tests scripts` and `uv run pyright`
- **Scoped tests**: `uv run pytest tests/<area>/test_<name>.py -q --tb=short`
- **Full Python tests**: `uv run pytest -q --tb=short`
- **Webapp verify**: `cd webapp && npm run verify`
