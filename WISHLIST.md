# Quartz Roadmap & Wishlist

This document tracks architectural ideas, future enhancements, and reference implementations for Quartz.

---

## 1. Unified LLM Provider Layer
- **Status**: Wishlist / Planned
- **Reference**: [Pi AI Provider Architecture (`earendil-works/pi/packages/ai`)](https://github.com/earendil-works/pi/tree/main/packages/ai)
- **Goal**: Replace the current duck-typed client emulation with a standardized, modular provider layer.
- **Key Concepts**:
  - Unified event stream (`ModelChunk` / stream events) regardless of backend (OpenAI, Anthropic native API, Google Cloud Code Assist, Ollama, Bedrock).
  - Normalized error hierarchy (`ContextLengthExceededError`, `RateLimitError`, `AuthenticationError`) so context recovery and auto-compaction trigger reliably across all providers.
  - Decoupled tool calling and message normalization.
  - Multi-provider configuration without vendor SDK dependency bloat.

---

## 2. `quartz --sandbox` — OS-level sandbox flag
- **Status**: Wishlist / Planned
- **Reference**: [Claude Code sandbox environments](https://code.claude.com/docs/en/sandbox-environments), [Axl sandbox backends](https://github.com/Observal/Axl/blob/main/docs/architecture/sandbox-backends.md), [pi containerization](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/containerization.md)
- **Goal**: Wrap the whole Quartz process tree (`quartz` CLI → `kernel_worker` → `bash()` children → MCP processes) in an OS-level boundary with a single flag and zero Dockerfile. First target: Linux Bubblewrap + Landlock + seccomp; macOS falls back to Seatbelt; unsupported platforms warn and run unsandboxed.
- **Key Concepts**:
  - `quartz --sandbox` (auto-detect `bwrap` availability, fall back with a warning) and `QUARTZ_SANDBOX` config-file toggle; one line to enable.
  - Workspace-only write access (`--bind $PWD $PWD`), host home / `~/.ssh` / credential files masked, capabilities dropped, `--unshare-net` optional, `prlimit` rlimits (CPU time, nproc, address space).
  - No refactor of `kernel_worker` — wrap the outer `Popen` in `KernelClient.start()`; everything below inherits the boundary.
  - Explicit opt-in backends later: OCI container (Docker/Podman rootless) for reproducible toolchains, microVM (Gondolin/Firecracker-style) for kernel-level separation.
  - Developer-friendliness is the point: pi's three-pattern setup (Gondolin extension, Dockerfile, OpenShell gateway) costs a step-function in setup effort; a flag + config keeps the sandbox at "install `bwrap`, set one option".

### Tier breakdown — what each tier actually protects

Three tiers exist; coverage differs decisively. Quartz ships full-permission today (no approval prompts, no deny lists), so the sandbox is the *only* protection layer, and the tier choice determines what stops.

| | Tier 1: sandboxed Bash only | Tier 2: sandbox runtime (whole process) ✅ | Tier 3: container / VM |
|---|---|---|---|
| What it isolates | `bash()` shell commands + children | **Entire Quartz**: CLI, `kernel_worker`, `bash()`, MCP servers | Whole process *and* its dependencies, in a new kernel |
| Quartz damage via `open()` / `os.remove` / `shutil.rmtree` in the REPL | ❌ Not blocked — the kernel itself runs unsandboxed | ✅ Blocked — same boundary as `rm` | ✅ Blocked |
| Damage via MCP servers / hooks | ❌ Not blocked | ✅ Blocked | ✅ Blocked |
| Filesystem | shell commands limited; Read/Edit are outside | Workspace-only; `~/.ssh`, `~/.aws`, browser profiles, `/proc/*/environ` unreachable | Same as Tier 2; additionally protects against kernel exploits |
| Network / secret exfiltration | ✅ (for shell commands) | ✅ with `--unshare-net`; with credential mediation (§3) secrets never enter | ✅ |
| Process damage (`kill`, fork bomb, disk fill) | ✅ (for shell commands) | ✅ PID namespace + `prlimit` rlimits | ✅ + hard resource cgroups |
| Inside-workspace damage (deleted tree, weakened tests, backdoors) | ❌ | ❌ | ❌ — needs review layers above the sandbox |

**Target: Tier 2.** Tier 1 is insufficient for Quartz because Quartz's damage runs through the persistent Python REPL (`open`, `subprocess`, `rmtree` in `kernel_worker`), not only through `bash()`. Tier 3 remains an opt-in backend for kernel-level separation.

### Sensible defaults (`QUARTZ_SANDBOX` / `--sandbox`)

Defaults must work for a non-expert with **zero** config. The goal: a user enables the sandbox and Quartz works; tightening/loosening is optional, never required.

```json
// ~/.quartz/config.json — sensible defaults, overridable per-project
{
  "sandbox": {
    "enabled": true,
    "backend": "auto",
    "allowWrite": ["$CWD", "$TMPDIR"],
    "denyWrite": [".git/hooks", ".git/config", ".mcp.json", ".claude/commands", ".claude/agents"],
    "network": "allowlist",
    "allowHosts": ["api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com", "api.deepseek.com", "openrouter.ai", "pypi.org", "files.pythonhosted.org"],
    "rlimits": { "cpuSeconds": 0, "nproc": 512, "addressSpaceMB": 0, "fileSizeMB": 1024 },
    "mask": ["~/.ssh", "~/.aws", "~/.config/gcloud", "~/.kube", "~/.pki", "~/.config/gh/hosts.yml"]
  }
}
```

- **`"backend": "auto"`** — probe `bwrap` on Linux / Seatbelt on macOS; no backend found → warn once and run unsandboxed. No config edits ever needed for a *safe-enough* start.
- **`allowWrite: [$CWD, $TMPDIR]`** — workspace + temp only. Everything the agent needs to work, nothing it needs to break.
- **`denyWrite`** always wins over `allowWrite` (`denyWrite` takes precedence), matching Claude Code's runtime: `.git/hooks` and `.git/config` are denied even inside the workspace, so the agent can't persist via git hooks or rewrite remotes.
- **`network: "allowlist"`** — default-allow LLM provider endpoints + PyPI (for `pip install`), default-deny everything else. The user never configures this to start; they add hosts only if a task needs them. Falls back to `"none"` if no mediating egress hook is configured.
- **Credentials (`QUARTZ_SECRET_*`)** documented as: set the placeholder, Quartz substitutes at egress for allowlisted hosts only, and raw values never enter the boundary.
- **Mask list** — built-in, unconditional: host home, SSH keys, cloud credentials, browser profiles, and `/proc` are masked/restricted. Not configurable away without an explicit `--sandbox-off` escape (which logs a warning).
- **Overrides are additive/narrowing only**: a project's `.quartz/config.json` may *tighten* (fewer hosts, more masks) but never widen beyond the user's `~/.quartz/config.json`. This prevents a malicious repo from silently granting itself broader access via repo-level config.

---

## 3. Credential mediation inside the sandbox
- **Status**: Wishlist / Planned
- **Reference**: [Gondolin secret non-exposure design](https://earendil-works.github.io/gondolin/security/), [OpenShell credential injection](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/containerization.md)
- **Goal**: Real secret values never enter the sandbox — the guest/agent receives only placeholders, and substitutes happen at egress for allowlisted destinations only. This is the part of sandboxing that is genuinely hard to DIY and the reason a boundary beats "the agent must not read `~/.ssh`".
- **Key Concepts**:
  - Env vars like `QUARTZ_SECRET_ANTHROPIC_API_KEY` carry placeholders; a host-side proxy/egress hook scans outbound headers and replaces them with real values only when the destination hostname matches the secret's allowlist. Blocking when placeholder is found for a non-allowlisted host.
  - URL-query substitution only as explicit opt-in (reflection risk); `Authorization: Basic` / `Proxy-Authorization` base64 decode/re-encode handled specially.
  - Secrets stay outside the boundary: auth files (`~/.quartz/auth.json`, `~/.claude.json`, SSH keys) never mounted into the workspace bind.
  - Pairs with `quartz --sandbox` (item 2): the boundary isolates *execution*, mediation controls *credentials*. Either alone is weaker than both.
  - Non-goal: defending against a malicious host or local user; DoS from the guest is out of scope.

---

## 4. Herdr integration — report agent state so herdr recognizes quartz as an agent
- **Status**: Wishlist / Deferred
- **Reference**: herdr (herdr.dev, `herdr integration install pi` writes ~/.pi/agent/extensions/herdr-agent-state.ts — streams JSON over HERDR_SOCKET_PATH: pane.report_agent {pane_id, source, agent, state}, pane.report_agent_session, pane.release_agent; states working|blocked|idle; gated on HERDR_ENV=1 && HERDR_SOCKET_PATH && HERDR_PANE_ID, no-op outside herdr). Agent-detection manifest: https://herdr.dev/agent-detection/index.toml (quartz is NOT in it — 0 matches — so quartz renders as a plain terminal today).
- **Goal**: Quartz shows in herdr's agent panel with correct working/blocked/idle state when run inside a herdr pane, like pi/agy/codex do.
- **Key Concepts / Approach** (from the pi reference):
  - Quartz-side reporter (agent/herdr.py, stdlib socket client, no deps): connect to $HERDR_SOCKET_PATH on startup when HERDR_ENV/HERDR_SOCKET_PATH/HERDR_PANE_ID present; send `pane.report_agent` on the same lifecycle points pi uses: agent_start -> working, agent_end -> debounced idle, blocked for confirmation/edit prompts and retryable provider errors, `pane.report_agent_session` with the quartz session id, `pane.release_agent` on exit. Outside herdr: zero overhead, zero behavior change.
  - Branch/diff badges come from herdr's own git scan of cwd — no quartz work needed; correct state reporting alone lights up the panel.
  - Upstream: propose quartz to herdr (quartz.toml manifest + `herdr integration install quartz`) OR ship the reporter inside quartz with a manual install step until herdr adds quartz to its index. Either way quartz owns the reporter, not a herdr-internal plugin.
  - Non-goal: replacing herdr's screen-scraping manifest rules — the socket reporter is the preferred path (same as pi).
- **Non-goal**: implementing today — this entry is deferred until the user asks.

---

## 5. `kernel/actions.py` reaches into `tui/` — invert it
- **Status**: Wishlist / Deferred
- **Where it hurts**: `src/quartz/kernel/actions.py` is 648 lines of *presentation vocabulary* — cell label prose (`describe_cell_actions`, `describe_group_actions`, `describe_cell_intent`, `describe_job_label`, `format_action_labels`, plus 12 private `_describe_*` / `_intent_*` / `_shorten_*` helpers) living in the execution package. Lines 174-648 are that vocabulary; only lines 38-173 (`_attr_chain`, `_classify_bash_command`, `classify_cell_actions`) classify anything.
- **The cycle workaround**: `src/quartz/kernel/actions.py:222` does `from quartz.tui.banner import _short_path  # local import: no cycle`, followed by a hand-rolled `Path.home()` fallback at `:224-235` inside `_shorten_path` (`:219-239`) that exists only because importing a TUI module from the execution package is wrong. The fallback re-implements `tui/banner.py::_short_path`'s three branches (exact match, prefix match, passthrough) and wraps the whole thing in bare `except Exception`.
- **Consumers are all views**: the public label/intent functions are imported at `src/quartz/tui/render.py:27`, `src/quartz/tui/dock.py:411`, and `src/quartz/tui/commands.py:662`. Nothing in `kernel/` or `core/` consumes them.
- **Consequence**: `kernel/` cannot be exercised without importing `tui`, and the fallback duplicates `_short_path` badly for no reason.
- **Proposed shape**: move the label/intent vocabulary to `tui/` (e.g. `tui/labels.py`), keep in `kernel/actions.py` only what is genuinely about classifying a cell (`classify_cell_actions` and the AST classifiers), and delete the `_shorten_path` fallback. Measured, that relocates ~475 of the file's 648 lines (`:174-648`); the three call sites change import path only.
- **Why deferred**: identified while landing #44-#46 and deliberately kept out of them, since those PRs were behaviour-neutral moves. Do this as its own PR with the description-text goldens in `tests/tui/test_ux.py` (plus `tests/kernel/test_jobs.py` and `tests/kernel/test_perf_helpers.py`) as the safety net.
- **Not in this PR**: this entry is a planning note only — do not implement the move here.
