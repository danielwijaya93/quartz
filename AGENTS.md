# Agent Guidelines & Development Workflow

## Operating Model & Identity
- **User (Daniel Wijaya)** directs vision, requirements, architecture, and approvals; does not write code or run Git/GitHub commands.
- **AI Agent** owns implementation, unit tests (`tests/`), Git/PRs, CI checks, and releases.
- **Repo**: `danielwijaya93/quartz` | **PyPI**: `quartz-agent` | **CLI**: `quartz` (`uv tool install --editable .`) | **Default branch**: `main` | **Git Author**: `Daniel Wijaya <danielwijaya93@gmail.com>`

## PR Workflow (Features, Fixes, Refactors)
1. **Branch**: `git checkout main && git pull && git checkout -b <type>/<short-description>` (`feat/`, `fix/`, `perf/`, `refactor/`, `chore/`).
2. **Implement & Test**: Write typed Python + tests in `tests/`. Verify with `uv run pytest` (all tests must pass).
3. **Version & Commit**: For `feat/`, `fix/`, `perf/`, `refactor/`, bump version via `uv run python scripts/bump_version.py <X.Y.Z>` (minor for `feat`, patch for `fix`/`perf`/`refactor`; skip for `chore/` or docs). Use Conventional Commits (`feat: ...`, `fix: ...`, `perf: ...`).
4. **PR & CI**: `git push -u origin <branch>` -> `gh pr create --title "..." --body "..."` -> `gh pr checks --watch`.
5. **Merge & Post-merge**: `gh pr merge --squash --delete-branch && git checkout main && git pull`.
   - If `webapp/` changed: `cd webapp && npm run build && npm run android:apk`; offer to send `webapp/android/app/build/outputs/apk/debug/quartz-v*.apk` via `tg-send`.
   - If `src/quartz/` changed: remind user to run `systemctl --user restart quartz-web`.
- **Docs-only fast track**: Direct commits + push to `main` are allowed for `AGENTS.md` / `README.md` / prompt templates.

## Parallel Worker & Worktree Workflow
1. Create worktree: `git worktree add -b <branch> .worktrees/<branch> main`.
2. Write self-contained brief (start with `YOU ARE A WORKER. YOU IMPLEMENT. YOU DO NOT ORCHESTRATE.`, forbid sub-workers/polling) and launch via `launch_worker(..., provider=..., model=..., effort=...)` or `bash()`:
   `cd .worktrees/<branch> && env -u PYTHONPATH uv run python -m quartz -p "$(cat ../.worker-prompt-<name>.md)" --cwd .`
3. Do not poll; wait for `[bash-done]`. Workers stop at typecheck/tests/build and **never** verify UI. Clean up with `git worktree remove .worktrees/<branch>`.

## UI Verification (Orchestrator Only)
- Verify `webapp/` / `quartz web` changes against built `webapp/dist` served by real `quartz web` using **`invisible_playwright_mcp`** (`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_read_text`, `browser_take_screenshot`), including empty/error states.
- Never hand-roll headless Chrome/CDP `/tmp` harnesses. Prefer `browser_snapshot` accessible-name assertions. For phone review, serve on Tailscale and verify it is reachable there and refused on LAN/public internet.

## Security & Environment
- Never commit secrets; load via env vars or secret loaders.
- If `pyproject.toml` / entry points change: `uv tool install --editable . --force`.
- Run `uv run pytest` before and after major changes.
