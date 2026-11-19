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

### 2. Documentation & Agent Instruction Updates (Fast Track)
- Direct commits to `main` are permitted for documentation, prompt templates, or instruction files (e.g. `AGENTS.md`, `README.md`).
- Ensure changes are pushed immediately: `git push origin main`.

### 3. Parallel Worker & Worktree Workflow (Accelerated Workflow)
When the user requests working on a feature, bug fix, or investigation in parallel without blocking the main interactive session:
1. **Isolated Worktree**: Create a temporary git worktree under `.worktrees/<branch-name>`:
   ```bash
   git worktree add -b <branch-name> .worktrees/<branch-name> main
   ```
2. **Session Forking**: Spawn a background worker using `bash(...)` with session forking so the worker inherits the warm context and kernel snapshot:
   ```python
   from agent.session import Session
   sid = Session.latest_id()
   handle = bash(f"uv run quartz -p '<clear, actionable instructions>' -r {sid} --fork --cwd .worktrees/<branch-name>")
   ```
3. **Background Execution**:
   - The cell returns immediately with `handle.pid`, leaving the main session free to continue interacting with the user.
   - The worker runs headlessly in `.worktrees/<branch-name>`, implements the task, executes tests (`uv run pytest`), commits, and opens a PR.
4. **Harness Wake-up & Cleanup**:
   - When the worker finishes, the harness automatically emits `[bash-done pid:... exit:...]`, waking Quartz up to review the outcome.
   - Once complete or merged, clean up the worktree:
     ```bash
     git worktree remove .worktrees/<branch-name>
     ```

---

## Security & Environment Rules
- **No Hardcoded Secrets**: Never commit API keys, client secrets, tokens, or credentials to git. Load them via environment variables or secret loaders (e.g. `QUARTZ_GOOGLE_CLIENT_ID`, `OPENAI_API_KEY`, etc.).
- **Editable Install Sync**: Whenever dependencies, package metadata (`pyproject.toml`), or entry points change, re-run:
  ```bash
  uv tool install --editable . --force
  ```
- **Test Suite**: Never break existing tests. Run `uv run pytest` before and after major modifications.
