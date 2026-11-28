# Agent Guidelines & Development Workflow

## Operating Model & Identity
- **User (Daniel Wijaya)** directs vision, requirements, architecture, and approvals; does not write code or run Git/GitHub commands.
- **AI Agent** owns implementation, unit tests (`tests/`), Git/PRs, CI checks, and releases.
- **Repo**: `danielwijaya93/quartz` | **PyPI**: `quartz-agent` | **CLI**: `quartz` (`uv tool install --editable .`) | **Default branch**: `main` | **Git Author**: `Daniel Wijaya <danielwijaya93@gmail.com>`

## Engineering Standards
- **Smallest complete change**: Reuse existing helpers, layers (`core/`), and conventions (`asyncio`, SQLite WAL, `schemas.py` -> `export_web_schema.py`); skip unrequested abstractions, wrappers, or boilerplate. Deletion beats addition.
- **Finish the whole scope**: Check `docs/CODEBASE_MAP.md` and callers before editing; fix root causes once in shared code and keep `0` `ruff`/`pyright` errors.
- **Test non-trivial logic**: Map happy, unhappy, and edge paths first; ask on ambiguous behavior, write failing tests before implementing, and comment only *why* in one line.
- **Never cut**: Trust-boundary validation, explicit error handling (no silent `except Exception: pass`), security/sandbox invariants, or anything the user asked for.

## PR Workflow (Features, Fixes, Refactors)
1. **Branch**: `git checkout main && git pull && git checkout -b <type>/<short-description>` (`feat/`, `fix/`, `perf/`, `refactor/`, `chore/`).
2. **Implement & Verify**: Write typed code + unit tests in `tests/`. Before committing, run `await verify("auto", gen_index=True)` (project helper in `.quartz/helpers.py` running `ruff`, `pyright`, `pytest`, and `webapp` verify in parallel), or run individually:
   - `uv run ruff check src tests`
   - `uv run pyright`
   - `uv run pytest`
   - `cd webapp && npm run verify` (whenever `webapp/` changed)
   - `uv run python scripts/gen_index.py` (refresh `docs/SYMBOL_INDEX.md` when symbols/files change)
3. **Visual Verification (Mandatory for features & user-facing fixes)**: For any `webapp/` change or user-facing web/API flow (skip pure docs or internal refactors), unit tests and CI are not sufficient proof. Before merging, build `webapp/dist`, run real `quartz web`, exercise the actual feature in `invisible_playwright_mcp`, take screenshots (`browser_take_screenshot`), inspect them visually with `view_image(...)`, and attach screenshot proof with `attach_file(...)`.
4. **Version & Commit**: For `feat/`, `fix/`, `perf/`, `refactor/`, bump version via `uv run python scripts/bump_version.py <X.Y.Z>` (minor for `feat`, patch for `fix`/`perf`/`refactor`; skip for `chore/` or docs). Use Conventional Commits (`feat: ...`, `fix: ...`, `perf: ...`, `refactor: ...`).
5. **PR & CI**: `git push -u origin <branch>` -> `gh pr create --title "..." --body "..."` -> `gh pr checks --watch`.
6. **Merge & Post-merge**: Merge once CI and visual verification are clean: `gh pr merge --squash --delete-branch && git checkout main && git pull`.
   - If `webapp/` changed: `cd webapp && npm run build && npm run android:apk`; attach `webapp/android/app/build/outputs/apk/debug/quartz-v*.apk` using `attach_file(...)`.
   - If `src/quartz/` changed: remind user to run `systemctl --user restart quartz-web`.
- **Docs-only fast track**: Direct commits + push to `main` are allowed for `AGENTS.md` / `README.md` / prompt templates.

## Parallel Worker & Worktree Workflow
1. Create worktree: `git worktree add -b <branch> .worktrees/<branch> main`.
2. Write self-contained brief (start with `YOU ARE A WORKER. YOU IMPLEMENT. YOU DO NOT ORCHESTRATE.`, forbid sub-workers/polling) and launch via `launch_worker(prompt_file, cwd, session_id, provider=..., model=..., effort=...)` or `bash()` with direct venv Python (no `uv` rebuild, use absolute prompt path, never `.resolve()` the venv python symlink):
   `cd /abs/path/.worktrees/<branch> && PYTHONPATH=/abs/path/.worktrees/<branch>/src /abs/path/.venv/bin/python -m quartz -p "$(cat /abs/path/.worktrees/.worker-prompt-<name>.md)" --cwd . --provider google --model gemini-4-hydrogen --effort medium`
3. Do not poll; wait for `[bash-done]`. Workers stop at lint/typecheck/tests/build and **never** verify UI. Clean up with `git worktree remove .worktrees/<branch>`.

## Visual Verification (Orchestrator Only)
- Required for all features and user-facing fixes (`webapp/` or user-facing web/API flows). Passing tests, CI, or DOM text snapshots alone is never enough to declare a feature working or fixed.
- Build `webapp/dist` and run real `quartz web`, then drive the real user flow via **`invisible_playwright_mcp`** (`browser_open`, `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_take_screenshot`), including empty/auth/error states.
- Use `browser_snapshot` for element targeting, but **always verify visually**: capture screenshots with `browser_take_screenshot`, inspect them directly with `view_image(...)` (checking layout, clipping, overlap, contrast, and actual rendered state), and attach the screenshots via `attach_file(...)` as proof.
- Never hand-roll headless Chrome/CDP `/tmp` harnesses. For phone review, serve on Tailscale and verify it is reachable there and refused on LAN/public internet.

## Security & Environment
- Never commit secrets; load via env vars or secret loaders.
- If `pyproject.toml` / entry points change: `uv tool install --editable . --force`.
- Run `uv run pytest` before and after major changes.
