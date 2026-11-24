"""Mutable runtime state, persistence, and compaction for an agent session."""

from __future__ import annotations

import dataclasses
import logging
import threading
import warnings
from pathlib import Path
from typing import Any

from quartz.core.compact import (
    CompactionResult,
    compact_messages,
    estimate_context_tokens,
    estimate_text_tokens,
)
from quartz.core.images import ImageAttachment
from quartz.core.loop import TurnUsage
from quartz.core.session import Session
from quartz.kernel.base import CellResult, Kernel
from quartz.providers.base import LLMProvider

logger = logging.getLogger(__name__)

__all__ = ["AgentRuntime", "ModelConfig", "RuntimeOptions", "SessionState", "ViewConfig"]


@dataclasses.dataclass
class ModelConfig:
    """Provider, model, prompt, and context/compaction budget configuration."""

    provider_name: str
    model: str
    effort: str
    system_prompt: str
    max_context_tokens: int
    compact_reserve_tokens: int
    keep_recent_tokens: int


@dataclasses.dataclass
class RuntimeOptions:
    """Transport and execution-loop behavior flags for an agent runtime."""

    headless: bool = False
    progress_every: int = 0
    background_check_every: int = 0
    notifications: bool = True
    transport: str = "cli"


@dataclasses.dataclass
class ViewConfig:
    """Presentation and display preferences for a front-end view."""

    verbose: bool = False
    plain_output: bool = False
    show_thinking: bool = False
    show_details: bool = False


@dataclasses.dataclass
class SessionState:
    """Transcript history, usage, and snapshot persistence for an active session."""

    session: Session
    messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    usage: TurnUsage = dataclasses.field(default_factory=TurnUsage)
    cell_history: list[tuple[str, CellResult, float]] = dataclasses.field(default_factory=list)
    total_elapsed_s: float = 0.0
    turn_count: int = 0


class AgentRuntime:
    """Orchestrates execution context, session persistence, and view configuration."""

    cwd: Path
    kernel: Kernel
    client: LLMProvider
    model_config: ModelConfig
    options: RuntimeOptions
    session_state: SessionState
    view_config: ViewConfig
    pending_images: list[ImageAttachment]

    def __init__(
        self,
        cwd: Path,
        kernel: Kernel,
        client: LLMProvider,
        provider_name: str,
        model: str,
        effort: str,
        system_prompt: str,
        active_session: Session,
        messages: list[dict[str, Any]],
        usage_stats: TurnUsage,
        max_context_tokens: int,
        compact_reserve_tokens: int,
        keep_recent_tokens: int,
        verbose: bool = False,
        plain_output: bool = False,
        headless: bool = False,
        show_thinking: bool = False,
        show_details: bool = False,
        progress_every: int = 0,
        background_check_every: int = 0,
        notifications: bool = True,
        transport: str = "cli",
        session_state: SessionState | None = None,
        view_config: ViewConfig | None = None,
        cell_history: list[tuple[str, CellResult, float]] | None = None,
        pending_images: list[ImageAttachment] | None = None,
        total_elapsed_s: float = 0.0,
        turn_count: int = 0,
    ) -> None:
        model_config = ModelConfig(
            provider_name=provider_name,
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            max_context_tokens=max_context_tokens,
            compact_reserve_tokens=compact_reserve_tokens,
            keep_recent_tokens=keep_recent_tokens,
        )
        options = RuntimeOptions(
            headless=headless,
            progress_every=progress_every,
            background_check_every=background_check_every,
            notifications=notifications,
            transport=transport,
        )
        resolved_view = (
            view_config
            if view_config is not None
            else ViewConfig(
                verbose=verbose,
                plain_output=plain_output,
                show_thinking=show_thinking,
                show_details=show_details,
            )
        )
        resolved_session = (
            session_state
            if session_state is not None
            else SessionState(
                session=active_session,
                messages=messages,
                usage=usage_stats,
                cell_history=cell_history if cell_history is not None else [],
                total_elapsed_s=total_elapsed_s,
                turn_count=turn_count,
            )
        )
        self._init_from_parts(
            cwd=cwd,
            kernel=kernel,
            client=client,
            model_config=model_config,
            session_state=resolved_session,
            view_config=resolved_view,
            options=options,
            pending_images=pending_images if pending_images is not None else [],
        )

    def _init_from_parts(
        self,
        *,
        cwd: Path,
        kernel: Kernel,
        client: LLMProvider,
        model_config: ModelConfig,
        session_state: SessionState,
        view_config: ViewConfig,
        options: RuntimeOptions,
        pending_images: list[ImageAttachment],
    ) -> None:
        self.cwd = cwd
        self.kernel = kernel
        self.client = client
        self.model_config = model_config
        self.session_state = session_state
        self.view_config = view_config
        self.options = options
        self.pending_images = pending_images
        self._snapshot_thread: threading.Thread | None = None
        self._snapshot_lock = threading.Lock()

    @classmethod
    def from_configs(
        cls,
        cwd: Path,
        kernel: Kernel,
        client: LLMProvider,
        model_config: ModelConfig,
        session_state: SessionState,
        view_config: ViewConfig | None = None,
        options: RuntimeOptions | None = None,
        pending_images: list[ImageAttachment] | None = None,
    ) -> AgentRuntime:
        """Construct an `AgentRuntime` from structured config dataclasses."""
        runtime = cls.__new__(cls)
        runtime._init_from_parts(
            cwd=cwd,
            kernel=kernel,
            client=client,
            model_config=model_config,
            session_state=session_state,
            view_config=view_config if view_config is not None else ViewConfig(),
            options=options if options is not None else RuntimeOptions(),
            pending_images=pending_images if pending_images is not None else [],
        )
        return runtime

    @property
    def provider_name(self) -> str:
        return self.model_config.provider_name

    @provider_name.setter
    def provider_name(self, value: str) -> None:
        self.model_config.provider_name = value

    @property
    def model(self) -> str:
        return self.model_config.model

    @model.setter
    def model(self, value: str) -> None:
        self.model_config.model = value

    @property
    def effort(self) -> str:
        return self.model_config.effort

    @effort.setter
    def effort(self, value: str) -> None:
        self.model_config.effort = value

    @property
    def system_prompt(self) -> str:
        return self.model_config.system_prompt

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self.model_config.system_prompt = value

    @property
    def max_context_tokens(self) -> int:
        return self.model_config.max_context_tokens

    @max_context_tokens.setter
    def max_context_tokens(self, value: int) -> None:
        self.model_config.max_context_tokens = value

    @property
    def compact_reserve_tokens(self) -> int:
        return self.model_config.compact_reserve_tokens

    @compact_reserve_tokens.setter
    def compact_reserve_tokens(self, value: int) -> None:
        self.model_config.compact_reserve_tokens = value

    @property
    def keep_recent_tokens(self) -> int:
        return self.model_config.keep_recent_tokens

    @keep_recent_tokens.setter
    def keep_recent_tokens(self, value: int) -> None:
        self.model_config.keep_recent_tokens = value

    @property
    def headless(self) -> bool:
        return self.options.headless

    @headless.setter
    def headless(self, value: bool) -> None:
        self.options.headless = value

    @property
    def progress_every(self) -> int:
        return self.options.progress_every

    @progress_every.setter
    def progress_every(self, value: int) -> None:
        self.options.progress_every = value

    @property
    def background_check_every(self) -> int:
        return self.options.background_check_every

    @background_check_every.setter
    def background_check_every(self, value: int) -> None:
        self.options.background_check_every = value

    @property
    def notifications(self) -> bool:
        return self.options.notifications

    @notifications.setter
    def notifications(self, value: bool) -> None:
        self.options.notifications = value

    @property
    def transport(self) -> str:
        return self.options.transport

    @transport.setter
    def transport(self, value: str) -> None:
        self.options.transport = value

    @property
    def active_session(self) -> Session:
        return self.session_state.session

    @active_session.setter
    def active_session(self, s: Session) -> None:
        self.session_state.session = s

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.session_state.messages

    @messages.setter
    def messages(self, m: list[dict[str, Any]]) -> None:
        self.session_state.messages = m

    @property
    def usage_stats(self) -> TurnUsage:
        return self.session_state.usage

    @usage_stats.setter
    def usage_stats(self, u: TurnUsage) -> None:
        self.session_state.usage = u

    @property
    def cell_history(self) -> list[tuple[str, CellResult, float]]:
        return self.session_state.cell_history

    @cell_history.setter
    def cell_history(self, ch: list[tuple[str, CellResult, float]]) -> None:
        self.session_state.cell_history = ch

    @property
    def total_elapsed_s(self) -> float:
        return self.session_state.total_elapsed_s

    @total_elapsed_s.setter
    def total_elapsed_s(self, t: float) -> None:
        self.session_state.total_elapsed_s = t

    @property
    def turn_count(self) -> int:
        return self.session_state.turn_count

    @turn_count.setter
    def turn_count(self, c: int) -> None:
        self.session_state.turn_count = c

    @property
    def verbose(self) -> bool:
        return self.view_config.verbose

    @verbose.setter
    def verbose(self, v: bool) -> None:
        self.view_config.verbose = v

    @property
    def plain_output(self) -> bool:
        return self.view_config.plain_output

    @plain_output.setter
    def plain_output(self, p: bool) -> None:
        self.view_config.plain_output = p

    @property
    def show_thinking(self) -> bool:
        return self.view_config.show_thinking

    @show_thinking.setter
    def show_thinking(self, st: bool) -> None:
        self.view_config.show_thinking = st

    @property
    def show_details(self) -> bool:
        return self.view_config.show_details

    @show_details.setter
    def show_details(self, sd: bool) -> None:
        self.view_config.show_details = sd

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
                        manifest = session.save_kernel(kernel)
                    except Exception as exc:
                        warnings.warn(
                            f"Background kernel snapshot failed: {exc}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                        return
                    skipped = manifest.get("skipped") or []
                    if skipped:
                        logger.debug(
                            "Kernel snapshot skipped %d value%s: %s%s",
                            len(skipped),
                            "s" if len(skipped) != 1 else "",
                            "; ".join(skipped[:5]),
                            " ..." if len(skipped) > 5 else "",
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
