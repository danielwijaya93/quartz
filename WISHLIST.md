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
