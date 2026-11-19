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
