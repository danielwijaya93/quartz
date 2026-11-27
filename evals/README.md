# Quartz A/B Eval Harness

Measure whether a REPL helper, prompt change, or compaction tweak **actually improves** real coding tasks before shipping it.

## How It Works
1. `evals/tasks.json` pins 5 real past tasks from this repo (`base_commit` + ground-truth `test_commit` + `test_paths`).
2. `evals/ab_runner.py` checks out `base_commit` into an isolated temporary git worktree per trial and runs headless `quartz -p` against **Control (A)** (`--control main`) and **Treatment (B)** (`--treatment <branch-or-path>`) with an isolated `SESSION_DIR`.
3. After each trial finishes, the grader overlays only `test_paths` from `test_commit` and runs `pytest`, then compares pass rate, cells (turns), wall time, uncached/total tokens, cache hit %, and helper adoption.

## Commands

```bash
# 1. Validate all task SHAs and test paths (instant, zero LLM spend)
.venv/bin/python -m evals.ab_runner --validate-tasks

# 2. Quick 1-task smoke A/B between main and a candidate branch
.venv/bin/python -m evals.ab_runner \
  --control main \
  --treatment feat/my-hypothesis \
  --task-ids closest-path-hints \
  --repeats 2 \
  --out /tmp/ab-smoke.json

# 3. Full 5-task A/B run with parallel arms (--workers 2)
.venv/bin/python -m evals.ab_runner \
  --control main \
  --treatment feat/my-hypothesis \
  --repeats 2 \
  --workers 2 \
  --out /tmp/ab-full.json
```
