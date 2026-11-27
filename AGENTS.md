# Agent Guidelines & Development Workflow

## Operating Model & Identity
- **User (Daniel Wijaya)** directs vision, requirements, architecture, and approvals; does not write code or run Git/GitHub commands.
- **AI Agent** owns implementation, unit tests (`tests/`), Git/PRs, CI checks, and releases.
- **Repo**: `danielwijaya93/quartz` | **PyPI**: `quartz-agent` | **CLI**: `quartz` (`uv tool install --editable .`) | **Default branch**: `main` | **Git Author**: `Daniel Wijaya <danielwijaya93@gmail.com>`

## Engineering Standards (DE Bar)
- **Strict layering**: Preserve the acyclic package DAG enforced by `tests/core/test_import_boundaries.py`. Put shared logic in `core/` (`SessionController`, `core/commands/`) — never duplicate across `tui/`, `web/`, or `cli/`.
- **Small, focused modules**: Split files before ~500 lines (`<= 500` LOC / `<= 80` LOC per function, enforced by `tests/core/test_module_modularity.py`). Add HTTP/WS endpoints as `APIRouter` modules under `web/routes/` (never inline in `web/app.py`). Group >5 config params into dataclasses (`ModelConfig`, `RuntimeOptions`).
- **Async-first & SQLite WAL**: Prefer native `asyncio` (`astream_provider`, `exec_cell_async`, `agent_loop_async`, `run_turn_async`, `KernelClient`) over new `threading.Thread`s, locks, or `asyncio.to_thread` bridges. Operational stores and FTS5 live in `~/.quartz/quartz.db` via `src/quartz/storage/`.
- **Schema-first contracts**: Define web wire payloads in `src/quartz/web/schemas.py` and regenerate `webapp/src/protocol.generated.ts` via `uv run python scripts/export_web_schema.py` (verified by `test_protocol_parity.py`).
- **Real types, zero shortcuts**: Maintain 0 `pyright` (`standard`) and 0 `ruff` errors, and typed `TypedDict` boundaries (`tests/core/test_typed_boundaries.py`). Avoid `# type: ignore`, `# noqa`, `@ts-ignore`, or new `dict[str, Any]` boundaries unless narrowly justified.
- **Explicit errors**: Never silently swallow `except Exception: pass`. Narrow expected cleanup with `contextlib.suppress(...)` or log best-effort failures at `logger.debug(..., exc_info=True)`.
- **Fail closed on security & governance**: Keep web token auth (`~/.quartz/web-token`), strict `/api/fs/*` path checks, fail-closed OS sandbox (`~/.quartz` masked except `AGENTS.md`/`helpers.py`/`skills`), host-bridge approvals (`git`/`gh`/`quartz`), default-strict HMAC-signed snapshots (`~/.quartz/snapshot-key`), kernel resource quotas/ping watchdog (`kernel/limits.py`), and `/api/metrics` + `/api/audit` intact.
- **Design paths & Red-first tests**: For any new feature or non-trivial change, map **happy paths**, **unhappy paths**, and **edge/corner cases** first, ask the user for decisions on ambiguous behaviors (numbered list with recommended options), then write failing tests for those agreed paths before implementing (Red-Green-Refactor).
- **Test behavior & comment why**: Test happy paths plus edge/error/cancellation paths. Write comments explaining *why* a non-obvious invariant exists, not *what* the code does.
- **Fast navigation**: Check `docs/CODEBASE_MAP.md` and `docs/SYMBOL_INDEX.md` before broad searches so you can jump straight to the right file and line.

## PR Workflow (Features, Fixes, Refactors)
1. **Branch**: `git checkout main && git pull && git checkout -b <type>/<short-description>` (`feat/`, `fix/`, `perf/`, `refactor/`, `chore/`).
2. **Implement & Verify**: Write typed code + unit tests in `tests/`. Before committing, run `await verify("auto", gen_index=True)` (project helper in `.quartz/helpers.py` running `ruff`, `pyright`, `pytest`, and `webapp` verify in parallel), or run individually:
   - `uv run ruff check src tests`
   - `uv run pyright`
   - `uv run pytest`
   - `cd webapp && npm run verify` (whenever `webapp/` changed)
   - `uv run python scripts/gen_index.py` (refresh `docs/SYMBOL_INDEX.md` when symbols/files change)
3. **Version & Commit**: For `feat/`, `fix/`, `perf/`, `refactor/`, bump version via `uv run python scripts/bump_version.py <X.Y.Z>` (minor for `feat`, patch for `fix`/`perf`/`refactor`; skip for `chore/` or docs). Use Conventional Commits (`feat: ...`, `fix: ...`, `perf: ...`, `refactor: ...`).
4. **PR & CI**: `git push -u origin <branch>` -> `gh pr create --title "..." --body "..."` -> `gh pr checks --watch`.
5. **Merge & Post-merge**: `gh pr merge --squash --delete-branch && git checkout main && git pull`.
   - If `webapp/` changed: `cd webapp && npm run build && npm run android:apk`; attach `webapp/android/app/build/outputs/apk/debug/quartz-v*.apk` using `attach_file(...)`.
   - If `src/quartz/` changed: remind user to run `systemctl --user restart quartz-web`.
- **Docs-only fast track**: Direct commits + push to `main` are allowed for `AGENTS.md` / `README.md` / prompt templates.

## Parallel Worker & Worktree Workflow
1. Create worktree: `git worktree add -b <branch> .worktrees/<branch> main`.
2. Write self-contained brief (start with `YOU ARE A WORKER. YOU IMPLEMENT. YOU DO NOT ORCHESTRATE.`, forbid sub-workers/polling) and launch via `launch_worker(prompt_file, cwd, session_id, provider=..., model=..., effort=...)` or `bash()` with direct venv Python (no `uv` rebuild, use absolute prompt path, never `.resolve()` the venv python symlink):
   `cd /abs/path/.worktrees/<branch> && PYTHONPATH=/abs/path/.worktrees/<branch>/src /abs/path/.venv/bin/python -m quartz -p "$(cat /abs/path/.worktrees/.worker-prompt-<name>.md)" --cwd . --provider google --model gemini-4-hydrogen --effort medium`
3. Do not poll; wait for `[bash-done]`. Workers stop at lint/typecheck/tests/build and **never** verify UI. Clean up with `git worktree remove .worktrees/<branch>`.

## UI Verification (Orchestrator Only)
- Verify `webapp/` / `quartz web` changes against built `webapp/dist` served by real `quartz web` using **`invisible_playwright_mcp`** (`browser_open`, `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_read_text`, `browser_take_screenshot`), including empty/auth/error states.
- Never hand-roll headless Chrome/CDP `/tmp` harnesses. Prefer `browser_snapshot` accessible-name assertions. For phone review, serve on Tailscale and verify it is reachable there and refused on LAN/public internet.

## Security & Environment
- Never commit secrets; load via env vars or secret loaders.
- If `pyproject.toml` / entry points change: `uv tool install --editable . --force`.
- Run `uv run pytest` before and after major changes.
