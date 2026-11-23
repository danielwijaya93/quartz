# Agent Guidelines & Development Workflow

## Operating Model
- **User Role**: The project owner (Daniel Wijaya) directs product vision, feature requirements, architectural choices, and approval decisions. The user **does not** write code, run Git commands, or manage GitHub directly.
- **AI Agent Responsibility**: The AI coding agent is fully responsible for the end-to-end engineering lifecycle:
  - Technical design and code implementation
  - Writing and maintaining automated unit tests
  - Git version control, branching, Pull Requests, and GitHub management
  - Monitoring automated CI checks and resolving failures
  - Releasing and packaging

---

## Project & Repository Identity
- **Repository**: `danielwijaya93/quartz` (GitHub)
- **PyPI Distribution Name**: `quartz-agent`
- **CLI Executable Name**: `quartz` (installed globally via `uv tool install --editable .`)
- **Default Branch**: `main`
- **Git Author**: Daniel Wijaya <danielwijaya93@gmail.com>

---

## Development Lifecycle & PR Workflow

### 1. Features, Bug Fixes, and Refactors (Strict PR Flow)
All non-trivial changes **must** follow the branch and Pull Request flow:

1. **Branching**:
   - Ensure `main` is up to date: `git checkout main && git pull`
   - Create a dedicated branch: `git checkout -b <type>/<short-description>`
     - Types: `feat/`, `fix/`, `perf/`, `refactor/`, `chore/`
     - Example: `git checkout -b feat/streaming-indicator`

2. **Implementation & Testing**:
   - Write clean, modular, typed Python code.
   - Add comprehensive tests in `tests/`.
   - Verify locally with `uv run pytest` before committing. All tests must pass.

3. **Committing**:
   - Use Conventional Commits (`feat: ...`, `fix: ...`, `perf: ...`, `test: ...`).
   - Keep commits focused and atomic.

4. **Pull Request & CI**:
   - Push branch: `git push -u origin <branch-name>`
   - Open PR via GitHub CLI:
     ```bash
     gh pr create --title "<type>: <title>" --body "<description of changes and test verification>"
     ```
   - Monitor CI status until green:
     ```bash
     gh pr checks --watch
     ```

5. **Merging & Cleanup**:
   - Once all CI checks pass, merge using squash:
     ```bash
     gh pr merge --squash --delete-branch
     ```
   - Switch back to `main` and pull:
     ```bash
     git checkout main && git pull
     ```
   - **Post-merge sync**:
     - If `webapp/` changed: run `cd webapp && npm run build` (for `quartz web`) and `npm run android:apk` (for Android). Offer to send `webapp/android/app/build/outputs/apk/debug/app-debug.apk` to the user via the `tg-send` skill.
     - If `src/quartz/` changed: remind the user to run `systemctl --user restart quartz-web`.

### 2. Documentation & Agent Instruction Updates (Fast Track)
- Direct commits to `main` are permitted for documentation, prompt templates, or instruction files (e.g. `AGENTS.md`, `README.md`).
- Ensure changes are pushed immediately: `git push origin main`.

### 3. Parallel Worker & Worktree Workflow (Accelerated Workflow)
When the user requests working on a feature, bug fix, or investigation in parallel without blocking the main interactive session:
1. **Isolated Worktree**: Create a temporary git worktree under `.worktrees/<branch-name>`:
   ```bash
   git worktree add -b <branch-name> .worktrees/<branch-name> main
   ```
2. **Fresh worker session**: Write the task to a brief file and spawn a background worker with `quartz -p`.
   With no `-r`/`-c`, this starts a brand new session:
   ```python
   handle = bash(
       "cd .worktrees/<branch-name> && "
       "env -u PYTHONPATH uv run quartz -p \"$(cat ../.worker-prompt-<name>.md)\" --cwd ."
   )
   ```
   The brief must be self-contained: goal, files to read, constraints, verification steps, and the
   git/PR instructions. Start it with an explicit role line (e.g. "YOU ARE A WORKER. YOU IMPLEMENT. YOU DO
   NOT ORCHESTRATE.") and tell it not to spawn sub-workers or poll other processes.
3. **Background Execution**:
   - The cell returns immediately with `handle.pid`, leaving the main session free to continue interacting with the user.
   - The worker runs headlessly in `.worktrees/<branch-name>`, implements the task, executes tests (`uv run pytest`), commits, and opens a PR.
4. **Harness Wake-up & Cleanup**:
   - Do not poll a running worker. When it finishes the harness emits `[bash-done pid:... exit:...]`, waking Quartz up to review the outcome.
   - Once complete or merged, clean up the worktree:
     ```bash
     git worktree remove .worktrees/<branch-name>
     ```
5. **The worker never verifies the UI.** Its job ends at typecheck, tests, and a production build.
   Browser verification belongs to the orchestrator session (see *UI Verification* below): workers
   that try it burn their whole run on headless-browser harnesses that hang, and a worker cannot
   see what it is looking at anyway.

### 4. UI Verification (orchestrator only)
Any change with a user-visible surface (`webapp/`, `quartz web`) is verified in a real browser by the
agent that owns the session, **not** by a background worker, and **always with the
`invisible_playwright_mcp` tools**:

- `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_read_text`,
  `browser_take_screenshot`.
- Do **not** hand-roll verification: no `google-chrome --headless --remote-debugging-port` plus a
  CDP script, no ad-hoc `/tmp` harness with a bundled `ws`, no `mock/server.mjs` on a long timeout.
  Those approaches repeatedly hung for minutes, left 300 MB of profile dirs in `/tmp`, and produced
  less evidence than a snapshot.
- Prefer `browser_snapshot` for assertions: it exposes accessible names, so a check states what a
  control is *called* instead of probing pixels or evaluating JavaScript.
- Verify against the built artifact the user will actually load (`webapp/dist`), served by a real
  `quartz web`, and check every claimed fix — including the empty and error states, not just the
  happy path.
- When the review target must be reached from the user's phone, serve on the Tailscale address and
  confirm first that it is reachable there **and** refused on the LAN and the public internet.

---

## Security & Environment Rules
- **No Hardcoded Secrets**: Never commit API keys, client secrets, tokens, or credentials to git. Load them via environment variables or secret loaders (e.g. `QUARTZ_GOOGLE_CLIENT_ID`, `OPENAI_API_KEY`, etc.).
- **Editable Install Sync**: Whenever dependencies, package metadata (`pyproject.toml`), or entry points change, re-run:
  ```bash
  uv tool install --editable . --force
  ```
- **Test Suite**: Never break existing tests. Run `uv run pytest` before and after major modifications.
