"""Mutable runtime state, persistence, and compaction for a CLI session."""

from __future__ import annotations

import dataclasses
import threading
import warnings
from pathlib import Path
from typing import Any

from agent.compact import (
    CompactionResult,
    compact_messages,
    estimate_context_tokens,
    estimate_text_tokens,
)
from agent.images import ImageAttachment
from agent.kernel_client import CellResult, KernelClient
from agent.loop import TurnUsage
from agent.provider import LLMProvider
from agent.session import Session


@dataclasses.dataclass
class AgentRuntime:
    """Mutable session and UI state for one CLI invocation."""

    cwd: Path
    kernel: KernelClient
    client: LLMProvider
    provider_name: str
    model: str
    effort: str
    system_prompt: str
    active_session: Session
    messages: list[dict[str, Any]]
    usage_stats: TurnUsage
    max_context_tokens: int
    compact_reserve_tokens: int
    keep_recent_tokens: int
    verbose: bool
    plain_output: bool
    headless: bool
    cell_history: list[tuple[str, CellResult, float]] = dataclasses.field(default_factory=list)
    pending_images: list[ImageAttachment] = dataclasses.field(default_factory=list)
    total_elapsed_s: float = 0.0
    turn_count: int = 0
    _snapshot_thread: threading.Thread | None = dataclasses.field(default=None, repr=False)
    _snapshot_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def wait_snapshot(self) -> None:
        """Wait for any in-flight background kernel snapshot to complete."""
        with self._snapshot_lock:
            t = self._snapshot_thread
            if t is not None and t.is_alive():
                t.join(timeout=30.0)
            self._snapshot_thread = None

    def persist(self, *, compacted: bool = False) -> None:
        """Persist messages, UI metadata, usage, and (in interactive mode) kernel snapshot."""
        self.active_session.save_messages(self.messages, compacted=compacted)
        if hasattr(self.active_session, "save_ui_meta"):
            self.active_session.save_ui_meta()
        if self.usage_stats.prompt_tokens > 0 or self.usage_stats.total_prompt_tokens > 0:
            self.active_session.save_usage(self.usage_stats.to_dict())
        if not self.headless and getattr(self.kernel, "dirty", True):
            with self._snapshot_lock:
                prev = self._snapshot_thread
                if prev is not None and prev.is_alive():
                    prev.join(timeout=30.0)
                session = self.active_session
                kernel = self.kernel

                def _save_kernel_bg() -> None:
                    try:
                        session.save_kernel(kernel)
                    except Exception as exc:
                        warnings.warn(
                            f"Background kernel snapshot failed: {exc}",
                            RuntimeWarning,
                            stacklevel=2,
                        )

                t = threading.Thread(target=_save_kernel_bg, daemon=True)
                self._snapshot_thread = t
                t.start()

    def compact(self, focus: str | None = None) -> CompactionResult:
        """Compact older messages while keeping the kernel alive."""
        result = compact_messages(
            self.client,
            self.model,
            self.messages,
            self.kernel,
            keep_recent_tokens=self.keep_recent_tokens,
            focus=focus,
        )
        self.messages = result.messages
        if result.changed:
            self.usage_stats.prompt_tokens = (
                estimate_context_tokens(result.messages) + estimate_text_tokens(self.system_prompt)
            )
            self.persist(compacted=True)
        return result
