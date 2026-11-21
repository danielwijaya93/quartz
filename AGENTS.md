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
2. **Fresh worker session**: Write the task to a brief file and spawn a background worker with `quartz -p`. With no `-r`/`-c`, this starts a brand new session:
   ```python
   handle = bash(
       "cd .worktrees/<branch-name> && "
       "env -u PYTHONPATH uv run quartz -p \"$(cat ../.worker-prompt-<name>.md)\" --cwd ."
   )
   ```
   **Write a brief file; do not rely on inherited context.** A self-contained brief is faster and more
   reliable than inherited history:
   - A worker that inherits an orchestrator's conversation tends to adopt its *role* — it starts
     delegating, reviewing and polling instead of implementing. Observed repeatedly: workers that were given
     a self-contained brief finished in 4-6 minutes; workers forked from a long coordination session stalled,
     spawned nested workers, or delivered nothing.
   - The parent's context is already partly spent. A worker that starts at 25%+ has less room to work.
   - The brief must stand alone: the goal, the files to read, the constraints, the verification steps, and
     the git/PR instructions. If the task truly needs this conversation's findings, put those findings *in
     the brief*.
   - Start the brief with an explicit role line (e.g. "YOU ARE A WORKER. YOU IMPLEMENT. YOU DO NOT
     ORCHESTRATE.") and tell it not to spawn sub-workers or poll other processes.
3. **Background Execution**:
   - The cell returns immediately with `handle.pid`, leaving the main session free to continue interacting with the user.
   - The worker runs headlessly in `.worktrees/<branch-name>`, implements the task, executes tests (`uv run pytest`), commits, and opens a PR.
   - Do not poll a running worker. The harness wakes you with `[bash-done pid:... exit:...]`; report progress to the user only when they ask or when the worker reports.
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
