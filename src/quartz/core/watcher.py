"""Shared TurnWatcherService subscribing to the in-process EventBus."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from quartz.core.event_bus import EventBus, SessionEvent
from quartz.core.watcher_eval import evaluate_turn_with_model, truncate_recent_messages
from quartz.core.watcher_types import (
    DEFAULT_GOAL_PROMPT,
    DEFAULT_LOOP_PROMPT,
    WatcherDecision,
    WatcherGlobalConfig,
    WatcherMode,
    WatcherSessionState,
    WatcherStatePayload,
    render_watcher_prompt,
)

logger = logging.getLogger(__name__)

__all__ = ["SessionHooks", "TurnWatcherService", "WatcherEvaluatorFn"]

WatcherEvaluatorFn = Callable[
    [WatcherGlobalConfig, WatcherMode, Sequence[Mapping[str, str]], str],
    Awaitable[WatcherDecision],
]


@dataclasses.dataclass
class SessionHooks:
    """Callbacks registered per session so the watcher stays transport-agnostic."""

    get_messages: Callable[[], Sequence[Mapping[str, Any]]]
    inject_prompt: Callable[[str], bool]
    has_background_jobs: Callable[[], bool] = lambda: False
    has_pending_permission: Callable[[], bool] = lambda: False
    is_turn_running: Callable[[], bool] = lambda: False
    on_state_change: Callable[[WatcherStatePayload], None] | None = None


async def _default_evaluator(
    cfg: WatcherGlobalConfig,
    mode: WatcherMode,
    recent: Sequence[Mapping[str, str]],
    goal: str,
) -> WatcherDecision:
    return await evaluate_turn_with_model(cfg, mode, recent, goal=goal)


def _load_default_global_config() -> WatcherGlobalConfig:
    from quartz.settings import load_settings

    s = load_settings()
    return WatcherGlobalConfig(
        provider=s.watcher_provider,
        model=s.watcher_model,
        antistall_default=s.watcher_antistall_default,
        delay_seconds=float(s.watcher_delay_seconds),
        default_max_turns=s.watcher_default_max_turns,
        antistall_max_nudges=s.watcher_antistall_max_nudges,
        default_prompt=s.watcher_default_prompt,
    )


class TurnWatcherService:
    """Monitors session lifecycle events and injects follow-up prompts safely."""

    def __init__(
        self,
        bus: EventBus,
        *,
        get_config: Callable[[], WatcherGlobalConfig] | None = None,
        evaluator: WatcherEvaluatorFn | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._bus = bus
        self._get_config = get_config or _load_default_global_config
        self._evaluator = evaluator or _default_evaluator
        self._loop = loop
        self._lock = threading.RLock()
        self._hooks: dict[str, SessionHooks] = {}
        self._states: dict[str, WatcherSessionState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._unsub = bus.subscribe(self._on_event)

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if loop is None:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
        if loop is not None and not loop.is_closed():
            with self._lock:
                self._loop = loop

    def _resolve_loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running.is_running() and not running.is_closed():
            self._loop = running
            return running
        with self._lock:
            loop = self._loop
        if loop is not None and loop.is_running() and not loop.is_closed():
            return loop
        return None

    def register_session(
        self,
        session_id: str,
        *,
        get_messages: Callable[[], Sequence[Mapping[str, Any]]],
        inject_prompt: Callable[[str], bool],
        has_background_jobs: Callable[[], bool] | None = None,
        has_pending_permission: Callable[[], bool] | None = None,
        is_turn_running: Callable[[], bool] | None = None,
        on_state_change: Callable[[WatcherStatePayload], None] | None = None,
    ) -> None:
        cfg = self._get_config()
        hooks = SessionHooks(
            get_messages=get_messages,
            inject_prompt=inject_prompt,
            has_background_jobs=has_background_jobs or (lambda: False),
            has_pending_permission=has_pending_permission or (lambda: False),
            is_turn_running=is_turn_running or (lambda: False),
            on_state_change=on_state_change,
        )
        with self._lock:
            self._hooks[session_id] = hooks
            if session_id not in self._states:
                default_mode: WatcherMode = "auto" if cfg.antistall_default else "off"
                self._states[session_id] = WatcherSessionState(mode=default_mode)
        self._notify(session_id)

    def unregister_session(self, session_id: str) -> None:
        self._cancel_task(session_id)
        with self._lock:
            self._hooks.pop(session_id, None)
            self._states.pop(session_id, None)

    def get_state(self, session_id: str) -> WatcherSessionState:
        with self._lock:
            state = self._states.get(session_id)
            if state is None:
                cfg = self._get_config()
                state = WatcherSessionState(mode="auto" if cfg.antistall_default else "off")
                self._states[session_id] = state
            return state

    def state_payload(self, session_id: str) -> WatcherStatePayload:
        cfg = self._get_config()
        with self._lock:
            state = self.get_state(session_id)
            effective_delay = round(state.delay_seconds if state.delay_seconds is not None else cfg.delay_seconds)
            effective_max = (
                state.max_turns if state.max_turns is not None else cfg.default_max_turns
            )
            status = state.status
            if (not cfg.configured and state.mode != "loop") or state.mode == "off":
                status = "inactive"
            elif state.paused:
                status = "paused"
            return {
                "configured": cfg.configured,
                "provider": cfg.provider,
                "model": cfg.model,
                "mode": state.mode,
                "status": status,
                "goal": state.goal,
                "promptTemplate": state.prompt_template or self._effective_template(state, cfg),
                "delaySeconds": max(0, effective_delay),
                "maxTurns": max(1, effective_max),
                "turnCount": state.turn_count,
                "nudgesUsed": state.nudges_used,
                "maxNudges": cfg.antistall_max_nudges,
                "paused": state.paused,
                "countdownRemaining": state.countdown_remaining,
                "lastReason": state.last_reason,
            }

    def is_active_for_session(self, session_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(session_id)
            return task is not None and not task.done()

    def set_goal(
        self,
        session_id: str,
        goal: str,
        *,
        max_turns: int | None = None,
        delay_seconds: float | None = None,
        prompt_template: str | None = None,
    ) -> WatcherStatePayload:
        self._cancel_task(session_id)
        with self._lock:
            state = self.get_state(session_id)
            state.mode = "goal"
            state.goal = goal.strip()
            state.turn_count = 0
            state.paused = False
            state.status = "idle"
            state.last_reason = ""
            if max_turns is not None:
                state.max_turns = max(1, int(max_turns))
            if delay_seconds is not None:
                state.delay_seconds = max(0.0, float(delay_seconds))
            if prompt_template is not None:
                state.prompt_template = prompt_template.strip()
        self._notify(session_id)
        return self.state_payload(session_id)

    def configure_session(
        self,
        session_id: str,
        *,
        mode: WatcherMode | None = None,
        goal: str | None = None,
        prompt_template: str | None = None,
        delay_seconds: float | None = None,
        max_turns: int | None = None,
    ) -> WatcherStatePayload:
        self._cancel_task(session_id)
        with self._lock:
            state = self.get_state(session_id)
            if mode is not None:
                state.mode = mode
                state.paused = False
                state.status = "inactive" if mode == "off" else "idle"
                state.turn_count = 0
                state.nudges_used = 0
            if goal is not None:
                state.goal = goal.strip()
            if prompt_template is not None:
                state.prompt_template = prompt_template.strip()
            if delay_seconds is not None:
                state.delay_seconds = max(0.0, float(delay_seconds))
            if max_turns is not None:
                state.max_turns = max(1, int(max_turns))
            state.last_reason = ""
        self._notify(session_id)
        return self.state_payload(session_id)

    def pause(self, session_id: str, *, reason: str = "paused") -> WatcherStatePayload:
        self._cancel_task(session_id)
        with self._lock:
            state = self.get_state(session_id)
            state.paused = True
            state.status = "paused"
            state.countdown_remaining = 0
            state.last_reason = reason
        self._notify(session_id)
        return self.state_payload(session_id)

    def resume(self, session_id: str) -> WatcherStatePayload:
        with self._lock:
            state = self.get_state(session_id)
            state.paused = False
            if state.status in ("paused", "completed"):
                state.status = "idle"
            state.last_reason = ""
        self._notify(session_id)
        return self.state_payload(session_id)

    def cancel_countdown(self, session_id: str) -> bool:
        cancelled = self._cancel_task(session_id)
        with self._lock:
            state = self._states.get(session_id)
            if state is not None and state.status in ("countdown", "evaluating"):
                state.status = "idle"
                state.countdown_remaining = 0
        if cancelled:
            self._notify(session_id)
        return cancelled

    def clear_goal(self, session_id: str) -> WatcherStatePayload:
        cfg = self._get_config()
        default_mode: WatcherMode = "auto" if cfg.antistall_default else "off"
        return self.configure_session(
            session_id,
            mode=default_mode,
            goal="",
            prompt_template="",
        )

    async def continue_now(self, session_id: str) -> bool:
        """Skip remaining countdown and immediately evaluate/inject for ``session_id``."""
        self._cancel_task(session_id)
        return await self._evaluate_and_inject(session_id)

    def _effective_template(self, state: WatcherSessionState, cfg: WatcherGlobalConfig) -> str:
        if state.prompt_template.strip():
            return state.prompt_template
        if state.mode == "goal":
            return DEFAULT_GOAL_PROMPT
        if state.mode == "loop":
            return DEFAULT_LOOP_PROMPT
        return cfg.default_prompt

    def _notify(self, session_id: str) -> None:
        with self._lock:
            hooks = self._hooks.get(session_id)
        if hooks is None or hooks.on_state_change is None:
            return
        payload = self.state_payload(session_id)
        try:
            hooks.on_state_change(payload)
        except Exception:
            logger.debug("Watcher state callback failed for %s", session_id, exc_info=True)

    def _cancel_task(self, session_id: str) -> bool:
        with self._lock:
            task = self._tasks.pop(session_id, None)
        if task is None or task.done():
            return False
        loop = task.get_loop()
        if not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        return True

    def _on_event(self, event: SessionEvent) -> None:
        if self._closed:
            return
        sid = event.session_id
        if event.kind == "session_closed":
            self.unregister_session(sid)
            return
        if event.kind == "user_message":
            self._cancel_task(sid)
            with self._lock:
                state = self._states.get(sid)
                if state is not None:
                    state.nudges_used = 0
                    state.countdown_remaining = 0
                    if state.status == "countdown":
                        state.status = "idle"
                    if state.paused and state.mode == "auto":
                        state.paused = False
                        state.status = "idle"
            self._notify(sid)
            return
        if event.kind == "turn_start":
            self.cancel_countdown(sid)
            return
        if event.kind in ("turn_cancelled", "turn_error"):
            reason = "cancelled by user" if event.kind == "turn_cancelled" else "turn error"
            self.pause(sid, reason=reason)
            return
        if event.kind == "turn_end":
            self._schedule_check(sid)

    def _can_schedule(self, session_id: str, cfg: WatcherGlobalConfig) -> bool:
        with self._lock:
            hooks = self._hooks.get(session_id)
            state = self._states.get(session_id)
        if hooks is None or state is None:
            return False
        if state.mode == "off" or state.paused:
            return False
        if state.mode != "loop" and not cfg.configured:
            return False
        if hooks.is_turn_running() or hooks.has_background_jobs() or hooks.has_pending_permission():
            return False
        if state.mode == "auto" and state.nudges_used >= cfg.antistall_max_nudges:
            return False
        max_turns = state.max_turns if state.max_turns is not None else cfg.default_max_turns
        return not (state.mode in ("goal", "loop") and state.turn_count >= max_turns)

    def _schedule_check(self, session_id: str) -> None:
        cfg = self._get_config()
        if not self._can_schedule(session_id, cfg):
            return
        loop = self._resolve_loop()
        if loop is None:
            return
        self._cancel_task(session_id)

        def _spawn() -> None:
            if self._closed:
                return
            task = loop.create_task(
                self._countdown_and_run(session_id),
                name=f"quartz-watcher-{session_id}",
            )
            with self._lock:
                self._tasks[session_id] = task

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _spawn()
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_spawn)

    async def _countdown_and_run(self, session_id: str) -> None:
        try:
            cfg = self._get_config()
            with self._lock:
                state = self._states.get(session_id)
                if state is None:
                    return
                delay = (
                    state.delay_seconds if state.delay_seconds is not None else cfg.delay_seconds
                )
                state.status = "countdown"
                state.countdown_remaining = max(0, math.ceil(delay))
            self._notify(session_id)

            remaining = max(0.0, float(delay))
            while remaining > 0:
                step = min(1.0, remaining)
                await asyncio.sleep(step)
                remaining -= step
                with self._lock:
                    state = self._states.get(session_id)
                    if state is None:
                        return
                    state.countdown_remaining = max(0, math.ceil(remaining))
                if remaining > 0:
                    self._notify(session_id)

            await self._evaluate_and_inject(session_id)
        except asyncio.CancelledError:
            return
        finally:
            with self._lock:
                current = asyncio.current_task()
                if self._tasks.get(session_id) is current:
                    self._tasks.pop(session_id, None)

    async def _evaluate_and_inject(self, session_id: str) -> bool:
        cfg = self._get_config()
        if not self._can_schedule(session_id, cfg):
            with self._lock:
                state = self._states.get(session_id)
                if state is not None and not state.paused:
                    state.status = "idle"
                    state.countdown_remaining = 0
            self._notify(session_id)
            return False

        with self._lock:
            hooks = self._hooks.get(session_id)
            state = self._states.get(session_id)
            if hooks is None or state is None:
                return False
            mode = state.mode
            goal = state.goal
            state.status = "evaluating"
            state.countdown_remaining = 0
        self._notify(session_id)

        if mode == "loop":
            decision = WatcherDecision(action="continue", reason="loop budget active")
        else:
            recent = truncate_recent_messages(hooks.get_messages(), limit=3)
            decision = await self._evaluator(cfg, mode, recent, goal)

        if decision.action != "continue":
            with self._lock:
                state = self._states.get(session_id)
                if state is not None:
                    state.last_reason = decision.reason
                    state.status = "completed" if state.mode == "goal" else "idle"
            self._notify(session_id)
            return False

        # Re-verify guards right before injecting in case state changed while evaluating.
        if not self._can_schedule(session_id, cfg):
            return False

        with self._lock:
            state = self._states.get(session_id)
            if state is None:
                return False
            max_turns = state.max_turns if state.max_turns is not None else cfg.default_max_turns
            next_turn = state.turn_count + 1
            template = self._effective_template(state, cfg)
            prompt = render_watcher_prompt(
                template,
                turn=next_turn,
                max_turns=max_turns,
                goal=state.goal,
            )
            state.last_reason = decision.reason
            state.status = "idle"

        injected = False
        try:
            injected = bool(hooks.inject_prompt(prompt))
        except Exception:
            logger.debug("Watcher failed to inject prompt for %s", session_id, exc_info=True)
            return False

        if injected:
            with self._lock:
                state = self._states.get(session_id)
                if state is not None:
                    if state.mode == "auto":
                        state.nudges_used += 1
                    else:
                        state.turn_count += 1
        self._notify(session_id)
        return injected

    async def aclose(self) -> None:
        with self._lock:
            self._closed = True
            tasks = list(self._tasks.values())
            self._tasks.clear()
        self._unsub()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
